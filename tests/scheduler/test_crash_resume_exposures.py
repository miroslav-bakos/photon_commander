"""The M5 crash-injection gate, applied to the exposure block (DESIGN.md 4.3.0).

Same method as ``test_crash_resume.py`` — run a schedule once uninterrupted,
then for **every** journal record index ``k`` resume from a journal truncated
there and assert the final journal and the final device call log are identical —
but over a sixty-frame ``take_exposures`` block, where "identical" has a sharper
meaning: sixty frames, each exactly once, whatever instant the process died at.

The block's progress is never read from the journal. It is the number of frames
the catalogue holds (``CapturePort.frames_captured``), which is what makes
"interrupted after 37, resumes at 38" a fact about the frames on disk rather
than about a counter someone remembered to increment.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from ssc.scheduler.executor import (
    EffectName,
    EffectOutcome,
    ItemStatus,
    JournalRecord,
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
    effect_calls,
)
from tests.scheduler.schedules import (
    BASE_UTC,
    DITHER_EVERY_N_FRAMES,
    EXPOSURE_FRAMES,
    build_exposure_plan,
    build_exposure_schedule,
)

RUN_ID = "crash-resume-exposures"


async def _drive(journal: FakeJournal, device: FakeDeviceCommands) -> FakeCapturePort:
    plan = build_exposure_plan()
    schedule = build_exposure_schedule(plan)
    clock = FakeClock(BASE_UTC - timedelta(minutes=5))
    capture = FakeCapturePort(device)
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
        gate_inputs=None,
        poll_interval_s=3600.0,
    )
    return capture


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


def _frame_calls(calls: list[tuple]) -> list[tuple]:
    return [
        call
        for call in calls
        if call[0] is EffectName.EXPOSE_FRAME and call[1].get("block_id") == "lights"
    ]


class TestReferenceRun:
    def test_it_completed(self) -> None:
        assert REFERENCE_JOURNAL[-1].kind.value == "run_ended"
        assert REFERENCE_JOURNAL[-1].run_status is RunStatus.COMPLETED

    def test_it_took_exactly_the_frames_asked_for(self) -> None:
        frames = _frame_calls(REFERENCE_CALLS)
        assert len(frames) == EXPOSURE_FRAMES
        assert [args["frame_index"] for _, args in frames] == list(range(EXPOSURE_FRAMES))

    def test_the_block_sets_filter_and_gain_and_carries_exposure_on_every_frame(self) -> None:
        names = [name for name, _ in REFERENCE_CALLS]
        first_frame = next(
            index
            for index, (name, args) in enumerate(REFERENCE_CALLS)
            if name is EffectName.EXPOSE_FRAME and args.get("block_id") == "lights"
        )
        before = names[:first_frame]
        assert EffectName.SET_FILTER in before
        assert EffectName.SET_GAIN in before
        assert all(args["exposure_sec"] == 10.0 for _, args in _frame_calls(REFERENCE_CALLS))

    def test_it_dithered_on_the_block_s_own_cadence(self) -> None:
        dithers = [call for call in REFERENCE_CALLS if call[0] is EffectName.DITHER]
        assert len(dithers) == EXPOSURE_FRAMES // DITHER_EVERY_N_FRAMES - 1

    def test_the_block_finished_with_its_frame_count_as_the_reason(self) -> None:
        done = [
            record
            for record in REFERENCE_JOURNAL
            if record.item_id == "lights" and record.status is ItemStatus.DONE
        ]
        assert len(done) == 1
        assert done[0].reason == f"{EXPOSURE_FRAMES} of {EXPOSURE_FRAMES} frames captured"

    def test_every_frame_is_journalled_issued_then_resolved(self) -> None:
        issued = [
            record
            for record in REFERENCE_JOURNAL
            if record.effect is not None
            and record.effect.name is EffectName.EXPOSE_FRAME
            and record.effect.args.get("block_id") == "lights"
            and record.effect_outcome is None
        ]
        resolved = [
            record
            for record in REFERENCE_JOURNAL
            if record.effect is not None
            and record.effect.name is EffectName.EXPOSE_FRAME
            and record.effect.args.get("block_id") == "lights"
            and record.effect_outcome is EffectOutcome.COMPLETED
        ]
        assert len(issued) == len(resolved) == EXPOSURE_FRAMES

    def test_the_device_calls_match_the_journal(self) -> None:
        assert _resolved_calls_in_prefix(REFERENCE_JOURNAL) == REFERENCE_CALLS


@pytest.mark.parametrize("k", range(0, len(REFERENCE_JOURNAL) + 1))
async def test_crash_at_every_transition_resumes_identically(k: int) -> None:
    prefix = REFERENCE_JOURNAL[:k]
    journal = FakeJournal(RUN_ID, seed=prefix)
    device = FakeDeviceCommands()
    device.seed(_resolved_calls_in_prefix(prefix))

    await _drive(journal, device)

    pending = reduce(prefix).items.get("lights")
    recovered = pending is not None and pending.pending_effect is not None and pending.pending_effect.name is EffectName.DITHER
    actual = _strip_timestamps(journal.records)
    expected = _strip_timestamps(REFERENCE_JOURNAL)
    if recovered:
        # The unresolved intent is consumed after stability proof, rather than
        # blindly replaying a possibly completed physical offset.
        completion = next(i for i, record in enumerate(actual) if
            record.effect is not None and record.effect.name is EffectName.DITHER
            and "reconciled without another offset" in record.reason)
        actual[completion] = replace(actual[completion], reason=expected[completion].reason)
        expected_calls = list(REFERENCE_CALLS)
        physical_index = len(_resolved_calls_in_prefix(prefix))
        assert expected_calls[physical_index][0] is EffectName.DITHER
        del expected_calls[physical_index]
    else:
        expected_calls = REFERENCE_CALLS
    assert actual == expected
    assert device.calls == expected_calls
    assert len(_frame_calls(device.calls)) == EXPOSURE_FRAMES


async def test_a_block_interrupted_after_37_frames_resumes_at_38() -> None:
    """The brief's own case, driven through the harness rather than by reading a
    counter: truncate the journal at the moment frame 37 was resolved, hand the
    resumed run a catalogue holding exactly those 37 frames, and watch which
    frame it shoots next."""
    frame_37_resolved = next(
        index
        for index, record in enumerate(REFERENCE_JOURNAL)
        if record.effect is not None
        and record.effect.name is EffectName.EXPOSE_FRAME
        and record.effect_outcome is EffectOutcome.COMPLETED
        and record.effect.args["frame_index"] == 36  # 0-based: the 37th frame
    )
    prefix = REFERENCE_JOURNAL[: frame_37_resolved + 1]
    already_shot = _resolved_calls_in_prefix(prefix)
    assert len(_frame_calls(already_shot)) == 37

    journal = FakeJournal(RUN_ID, seed=prefix)
    device = FakeDeviceCommands()
    device.seed(already_shot)
    fresh_call_floor = len(device.calls)

    await _drive(journal, device)

    resumed_frames = _frame_calls(device.calls[fresh_call_floor:])
    assert resumed_frames[0][1]["frame_index"] == 37  # 0-based: the 38th frame
    assert len(_frame_calls(device.calls)) == EXPOSURE_FRAMES
    assert device.calls == REFERENCE_CALLS


async def test_a_frame_that_landed_but_was_never_journalled_is_not_re_exposed() -> None:
    """The genuinely ambiguous crash: between "issued" and "resolved", where the
    frame itself did reach the catalogue.

    The generic rule for an unresolved effect is to reissue it — a duplicate
    slew is a slew. A duplicate *frame* is a file, so here the catalogue settles
    it: 38 rows exist for a block whose journal only ever issued frame 37, so
    frame 37 is confirmed, not shot again.
    """
    frame_37_issued = next(
        index
        for index, record in enumerate(REFERENCE_JOURNAL)
        if record.effect is not None
        and record.effect.name is EffectName.EXPOSE_FRAME
        and record.effect_outcome is None
        and record.effect.args["frame_index"] == 37
    )
    prefix = REFERENCE_JOURNAL[: frame_37_issued + 1]
    journal = FakeJournal(RUN_ID, seed=prefix)
    device = FakeDeviceCommands()
    device.seed(_resolved_calls_in_prefix(prefix))
    # The frame the journal never resolved *did* land: the catalogue holds it.
    device.seed([(EffectName.EXPOSE_FRAME, dict(REFERENCE_JOURNAL[frame_37_issued].effect.args))])  # type: ignore[union-attr]
    fresh_call_floor = len(device.calls)

    plan = build_exposure_plan()
    schedule = build_exposure_schedule(plan)
    capture = FakeCapturePort(device)
    await run(
        run_id=RUN_ID,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=FakeClock(BASE_UTC - timedelta(minutes=5)),
        controls=FakeControls(),
        capture=capture,
        gate_inputs=None,
        poll_interval_s=3600.0,
    )

    frames = _frame_calls(device.calls)
    assert len(frames) == EXPOSURE_FRAMES
    assert [args["frame_index"] for _, args in frames] == list(range(EXPOSURE_FRAMES))
    # Frame 37 was confirmed from the catalogue, so the resumed process's own
    # first frame is 38.
    assert _frame_calls(device.calls[fresh_call_floor:])[0][1]["frame_index"] == 38
    confirmed = [
        record
        for record in journal.records
        if record.effect is not None
        and record.effect.name is EffectName.EXPOSE_FRAME
        and record.effect_outcome is EffectOutcome.COMPLETED
        and "already in the catalogue" in record.reason
    ]
    assert len(confirmed) == 1
    assert reduce(journal.records).items["lights"].status is ItemStatus.DONE
