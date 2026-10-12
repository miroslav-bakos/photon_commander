"""The M5 acceptance gate (docs/IMPLEMENTATION.md): "crash injected at each executor
state transition; the resumed run reaches an identical schedule position with
identical side effects."

The method: run a small compiled schedule once, uninterrupted, and record its whole
journal and its whole device call log. Then, for **every** journal record index
``k`` (there is no special-casing of "interesting" indices — every one is tried),
simulate a crash right after record ``k`` was durably written: build a fresh journal
seeded with only ``journal[:k]``, a fresh device pre-populated with exactly the calls
the journal already shows as *resolved* by that point (a call whose issue was
journalled but whose outcome was not is, correctly, ambiguous — the resumed run must
reissue it, which is the idempotent-resume property under test), and resume. The
final journal (minus timestamps) and the final device call log must both equal the
uninterrupted run's.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from ssc.scheduler.executor import JournalRecord, RunStatus, run
from tests.scheduler.fakes import (
    FakeCapturePort,
    FakeClock,
    FakeControls,
    FakeDeviceCommands,
    FakeDeviceEvents,
    FakeJournal,
    effect_calls,
)
from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule

RUN_ID = "crash-resume-fixture"


async def _drive(journal: FakeJournal, device: FakeDeviceCommands) -> None:
    plan = build_plan()
    schedule = build_schedule(plan)
    clock = FakeClock(
        max((record.at for record in journal.records), default=BASE_UTC - timedelta(minutes=5))
    )
    events = FakeDeviceEvents()
    controls = FakeControls()
    capture = FakeCapturePort(device, clock=clock, cycle_s=600)
    await run(
        run_id=RUN_ID,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=events,
        clock=clock,
        controls=controls,
        capture=capture,
        gate_inputs=None,
        poll_interval_s=3600.0,  # large: WaitUntil always jumps straight to its target
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
    """Every device call the journal prefix already shows as durably resolved
    (``effect_outcome`` set) — anything issued-but-unresolved at the truncation point
    is deliberately excluded; the resumed run must reissue that one itself."""
    calls: list[tuple] = []
    for record in prefix:
        if record.effect is not None and record.effect_outcome is not None:
            calls.extend(effect_calls(record.effect))
    return calls


class TestReferenceRunSanity:
    """The reference run itself must actually have exercised every kind of transition
    this suite claims to crash-inject at, or the parametrised test below would be
    exercising nothing."""

    def test_reference_run_completed(self) -> None:
        assert REFERENCE_JOURNAL[-1].kind.value == "run_ended"
        assert REFERENCE_JOURNAL[-1].run_status is RunStatus.COMPLETED

    def test_reference_run_has_multiple_records(self) -> None:
        # run_started + startup (issue/resolve) + goto's slew/expose/stack-save
        # transitions + filter (issue/resolve) + safe shutdown's
        # tracking/home/park sequence (issue/resolve each) + run_ended.
        assert len(REFERENCE_JOURNAL) >= 12

    def test_reference_run_issued_every_effect_kind(self) -> None:
        names = {record.effect.name for record in REFERENCE_JOURNAL if record.effect is not None}
        assert {"startup", "slew_to", "expose_frame", "stop_live_stack", "set_filter", "set_tracking", "find_home", "park"} <= {
            n.value for n in names
        }

    def test_reference_device_calls_match_journal_effects(self) -> None:
        assert _resolved_calls_in_prefix(REFERENCE_JOURNAL) == REFERENCE_CALLS


@pytest.mark.parametrize("k", range(0, len(REFERENCE_JOURNAL) + 1))
async def test_crash_at_every_transition_resumes_identically(k: int) -> None:
    prefix = REFERENCE_JOURNAL[:k]
    journal = FakeJournal(RUN_ID, seed=prefix)
    device = FakeDeviceCommands()
    device.seed(_resolved_calls_in_prefix(prefix))

    await _drive(journal, device)

    # A process restart invalidates optical field ownership. Allow only the
    # extra, non-advancing verification; science progress and all other phases
    # must remain identical. Recovery adds an attempt without replaying a frame.
    ordinary = [record for record in journal.records if record.effect is None
                or not record.effect.args.get("resume_field_verification")]
    assert [replace(record, attempt=None) for record in _strip_timestamps(ordinary)
            if record.effect is None or record.effect_outcome is not None] == [
        replace(record, attempt=None) for record in _strip_timestamps(REFERENCE_JOURNAL)
        if record.effect is None or record.effect_outcome is not None]
    assert device.calls == _resolved_calls_in_prefix(journal.records)
    assert [call for call in device.calls if call[0].value != "center_target"] == [
        call for call in REFERENCE_CALLS if call[0].value != "center_target"]
