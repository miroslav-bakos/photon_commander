"""The M5 crash-injection gate, extended to a target that moves (DESIGN.md 3.6).

``test_crash_resume.py`` proves the general property: crash after every journal
record, resume, and reach an identical position with identical side effects. A
body target puts that property under a load nothing else in the schedule does,
because the *right* coordinates are a function of when the effect is issued —
so "identical" has to be defined before it can be asserted. It means:

1. every device call the resumed run makes is the call the uninterrupted run
   made, in the same order, **with the same coordinates**; and
2. the final journal, timestamps aside, is the same document.

Which implies the executor's rule, and this file is where it is checked:
an effect already journalled as *issued* is reissued verbatim from the journal —
a resume never re-resolves a slew that may already have reached the device — while
an effect not yet issued is resolved fresh at the moment it is issued, so the
*next* re-centring segment corrects whatever the crash cost.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from ssc.astro.bodies import BodyId
from ssc.plan.models import BodyTargetSpec
from ssc.scheduler.executor import (
    CompletionMode,
    EffectCall,
    EffectName,
    ItemStatus,
    JournalRecord,
    RecordKind,
    RunStatus,
    reduce,
    run,
)
from tests.scheduler.fakes import (
    FakeCapturePort,
    FakeClock,
    FakeControls,
    FakeDeviceCommands,
    FakeDeviceEvents,
    FakeJournal,
    LinearBodyResolver,
    effect_calls,
)
from tests.scheduler.schedules import BASE_UTC, build_body_plan, build_body_schedule

RUN_ID = "crash-resume-body"

#: 0.1 h of RA per hour — absurdly fast for a real body, and deliberately so:
#: over the fixture's 30-minute session the target moves 45 arcmin of RA, which
#: is far larger than any tolerance, so "replayed from the journal" and
#: "resolved again" can never be confused for one another by rounding.
RA_RATE_H_PER_HOUR = 0.1


def _resolver() -> LinearBodyResolver:
    return LinearBodyResolver(
        epoch=BASE_UTC, ra_j2000_h=5.0, dec_j2000_deg=20.0, rate_h_per_hour=RA_RATE_H_PER_HOUR
    )


#: Where a fresh process's clock starts: five minutes before the first item.
START_AT = BASE_UTC - timedelta(minutes=5)


async def _drive(
    journal: FakeJournal, device: FakeDeviceCommands, *, start_at: datetime = START_AT
) -> None:
    """One process's whole life. ``start_at`` is when *this* process began.

    A resumed process is handed the instant the crashed one last durably wrote,
    because that is what a supervisor restart looks like: the clock does not
    rewind, and a body's position is a function of the clock. Resuming with a
    fresh process's start time would be modelling a time machine, not a crash.
    """
    plan = build_body_plan()
    schedule = build_body_schedule(plan)
    clock = FakeClock(start_at)
    capture = FakeCapturePort(device, clock=clock, cycle_s=15 * 60)
    await run(
        run_id=RUN_ID,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=capture,
        resolver=_resolver(),
        poll_interval_s=3600.0,
    )


def _build_reference() -> tuple[list[JournalRecord], list[tuple]]:
    journal = FakeJournal(RUN_ID)
    device = FakeDeviceCommands()
    asyncio.run(_drive(journal, device))
    return journal.records, device.calls


REFERENCE_JOURNAL, REFERENCE_CALLS = _build_reference()


def _strip_timestamps(records: list[JournalRecord]) -> list[JournalRecord]:
    return [replace(record, at=BASE_UTC) for record in records]


def _resolved_calls_in_prefix(prefix: list[JournalRecord]) -> list[tuple]:
    calls: list[tuple] = []
    for record in prefix:
        if record.effect is not None and record.effect_outcome is not None:
            calls.extend(effect_calls(record.effect))
    return calls


class TestReferenceRunSanity:
    def test_it_completed(self) -> None:
        assert REFERENCE_JOURNAL[-1].kind is RecordKind.RUN_ENDED
        assert REFERENCE_JOURNAL[-1].run_status is RunStatus.COMPLETED

    def test_it_set_the_lunar_rate_before_pointing(self) -> None:
        names = [name for name, _ in REFERENCE_CALLS]
        assert names.index(EffectName.SET_TRACKING_RATE) < names.index(EffectName.SLEW_TO)
        rate = next(a for n, a in REFERENCE_CALLS if n is EffectName.SET_TRACKING_RATE)
        assert rate == {"rate": "lunar"}

    def test_it_re_centred_once_per_segment(self) -> None:
        slews = [args for name, args in REFERENCE_CALLS if name is EffectName.SLEW_TO]
        assert len(slews) == 2
        # The second segment resolved the body again, half an hour of fixture
        # motion later — this is the re-centring the whole feature is for.
        assert slews[1]["ra_j2000_h"] != slews[0]["ra_j2000_h"]

    def test_every_slew_matches_the_resolver_at_its_own_instant(self) -> None:
        resolver = _resolver()
        issued = [
            record
            for record in REFERENCE_JOURNAL
            if record.effect is not None
            and record.effect.name is EffectName.SLEW_TO
            and record.effect_outcome is None
        ]
        assert len(issued) == 2
        for record in issued:
            assert record.effect is not None
            assert record.effect.args["ra_j2000_h"] == pytest.approx(resolver.ra_at(record.at))

    def test_device_calls_match_journal_effects(self) -> None:
        assert _resolved_calls_in_prefix(REFERENCE_JOURNAL) == REFERENCE_CALLS


def _assert_durable_effects_and_verification(records: list[JournalRecord]) -> None:
    issued: dict[tuple[str | None, int | None, int], EffectCall] = {}
    for index, record in enumerate(records):
        if record.effect is None:
            continue
        key = (record.item_id, record.panel_index, record.attempt)
        if record.effect_outcome is None:
            # A restart may replace an unresolved issue with verification at
            # the same attempt; resolution must match the latest durable issue.
            issued[key] = record.effect
        else:
            assert key in issued
            assert record.effect == issued[key]
        if record.effect.args.get("resume_field_verification"):
            assert record.effect.name is EffectName.CENTER_TARGET
            previous = reduce(records[:index]).items[record.item_id]
            assert record.step == previous.step
            assert record.status is None or record.status is previous.status
            assert record.held is None or record.held == previous.held


@pytest.mark.parametrize("k", range(0, len(REFERENCE_JOURNAL) + 1))
async def test_crash_at_every_transition_resumes_identically(k: int) -> None:
    prefix = REFERENCE_JOURNAL[:k]
    journal = FakeJournal(RUN_ID, seed=prefix)
    device = FakeDeviceCommands()
    device.seed(_resolved_calls_in_prefix(prefix))

    await _drive(journal, device, start_at=prefix[-1].at if prefix else START_AT)

    _assert_durable_effects_and_verification(journal.records)

    # Restart adds only same-target, non-advancing optical verification.
    verification = [record for record in journal.records if record.effect is not None
                    and record.effect.args.get("resume_field_verification")]
    for record in verification:
        assert record.effect is not None
        assert record.effect.name is EffectName.CENTER_TARGET
        args = {key: value for key, value in record.effect.args.items()
                if key not in {"resume_field_verification", "resume_autofocus_during_capture"}}
        owned = [candidate for candidate in REFERENCE_JOURNAL
                 if candidate.item_id == record.item_id
                 and candidate.panel_index == record.panel_index
                 and candidate.effect is not None
                 and candidate.effect.name is EffectName.CENTER_TARGET]
        assert owned
        assert any(args == candidate.effect.args for candidate in owned)
    ordinary = [record for record in journal.records if record not in verification]
    assert [replace(record, attempt=None) for record in _strip_timestamps(ordinary)
            if record.effect is None or record.effect_outcome is not None] == [
        replace(record, attempt=None) for record in _strip_timestamps(REFERENCE_JOURNAL)
        if record.effect is None or record.effect_outcome is not None]
    assert device.calls == _resolved_calls_in_prefix(journal.records)
    # Full adapter calls already match every durable effect in order above.
    # The ordinary resolved effects retain the exact reference call sequence.
    assert _resolved_calls_in_prefix(ordinary) == REFERENCE_CALLS



async def test_a_slew_already_journalled_is_replayed_never_re_resolved() -> None:
    """The durability rule, isolated.

    A journal that shows a slew *issued* and never resolved is the crash window:
    the device may already have that command. The resumed run reissues the exact
    coordinates the journal holds — even though the resolver, asked again, would
    now answer somewhere very different — because reissuing an identical command
    is idempotent and re-resolving one is not.
    """
    stale_ra, stale_dec = 1.234, -7.5
    seed = [
        JournalRecord(at=BASE_UTC, run_id=RUN_ID, kind=RecordKind.RUN_STARTED, reason="started"),
        JournalRecord(
            at=BASE_UTC,
            run_id=RUN_ID,
            kind=RecordKind.ITEM_TRANSITION,
            item_id="startup",
            step=2,
            attempt=1,
            status=ItemStatus.DONE,
            reason="startup completed",
        ),
        JournalRecord(
            at=BASE_UTC + timedelta(minutes=5),
            run_id=RUN_ID,
            kind=RecordKind.ITEM_TRANSITION,
            item_id="goto-moon",
            panel_index=0,
            step=3,
            attempt=1,
            status=ItemStatus.RUNNING,
            effect=EffectCall(
                name=EffectName.SLEW_TO,
                args={"ra_j2000_h": stale_ra, "dec_j2000_deg": stale_dec},
                completion=CompletionMode.DEVICE_EVENT,
                event_kind="slew_complete",
                deadline_s=120.0,
            ),
            effect_outcome=None,
            reason="issuing slew_to (attempt 1)",
        ),
    ]
    journal = FakeJournal(RUN_ID, seed=seed)
    device = FakeDeviceCommands()
    await _drive(journal, device, start_at=seed[-1].at)

    slews = [args for name, args in device.calls if name is EffectName.SLEW_TO]
    assert slews[0] == {"ra_j2000_h": stale_ra, "dec_j2000_deg": stale_dec}
    # ...and the *next* segment is not stuck with it: it resolves from scratch,
    # which is what bounds a resumed run's pointing error to one re-centring
    # interval rather than to however long the process was down.
    assert slews[1]["ra_j2000_h"] != stale_ra
    assert slews[1]["ra_j2000_h"] == pytest.approx(
        _resolver().ra_at(BASE_UTC + timedelta(minutes=20))
    )


def test_the_fixture_really_targets_a_body() -> None:
    goto = next(item for item in build_body_plan().items if item.id == "goto-moon")
    assert isinstance(goto.target, BodyTargetSpec)  # type: ignore[union-attr]
    assert goto.target.body is BodyId.MOON  # type: ignore[union-attr]
    assert goto.ra_j2000_h is None  # type: ignore[union-attr]


def test_durable_effect_check_rejects_wrong_issued_coordinates() -> None:
    records = list(REFERENCE_JOURNAL)
    index = next(i for i, record in enumerate(records)
                 if record.effect is not None and record.effect.name is EffectName.SLEW_TO
                 and record.effect_outcome is None)
    effect = records[index].effect
    assert effect is not None
    records[index] = replace(records[index], effect=replace(
        effect, args={**effect.args, "ra_j2000_h": -99.0}))
    with pytest.raises(AssertionError):
        _assert_durable_effects_and_verification(records)


def test_verification_check_rejects_advancing_the_owned_step() -> None:
    prefix = REFERENCE_JOURNAL[:26]
    previous = reduce(prefix).items["goto-moon"]
    owned = next(record.effect for record in REFERENCE_JOURNAL
                 if record.item_id == "goto-moon" and record.effect is not None
                 and record.effect.name is EffectName.CENTER_TARGET)
    assert owned is not None
    verification = replace(prefix[-1], effect=replace(
        owned, args={**owned.args, "resume_field_verification": True}),
        effect_outcome=None, step=previous.step + 1, attempt=previous.attempt + 1,
        status=None, held=None)
    with pytest.raises(AssertionError):
        _assert_durable_effects_and_verification([*prefix, verification])
