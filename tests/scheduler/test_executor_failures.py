"""Deadline miss -> failed with reason; NotImplemented from the device -> failed with
the reason text, run continues past it either way (CLAUDE.md invariant 7: no silent
degradation, no stub)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ssc.scheduler.executor import (
    ControlCommand,
    ControlKind,
    DeviceCommandError,
    DeviceCommandNotImplementedError,
    DeviceFocusConditionsError,
    EffectName,
    EffectOutcome,
    ItemStatus,
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
)
from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule


class RecoveringDevice(FakeDeviceCommands):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def _record(self, name: EffectName, args: dict) -> None:
        await super()._record(name, args)
        if name is EffectName.SLEW_TO and self.failures:
            self.failures -= 1
            raise DeviceCommandError("connection lost")


async def test_transient_slew_error_recovers_without_repeating_startup() -> None:
    plan = build_plan()
    journal = FakeJournal("recover-slew")
    device = RecoveringDevice(2)
    clock = FakeClock(BASE_UTC)
    state = await run(
        run_id=journal.run_id,
        schedule=build_schedule(plan),
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=FakeCapturePort(device, clock=clock, cycle_s=60),
    )
    assert state.items["goto"].status is ItemStatus.DONE
    assert state.run_status is RunStatus.COMPLETED
    assert sum(name is EffectName.SLEW_TO for name, _ in device.calls) == 3
    assert sum(name is EffectName.STARTUP for name, _ in device.calls) == 1
    recovery = [r for r in journal.records if r.retry_at is not None]
    assert [r.recovery_attempts for r in recovery] == [1, 2]
    assert [(r.retry_at - r.at).total_seconds() for r in recovery] == [5, 15]


async def test_persistent_error_retries_through_window_then_runs_shutdown() -> None:
    plan = build_plan()
    journal = FakeJournal("recover-until-deadline")
    device = RecoveringDevice(1000)
    clock = FakeClock(BASE_UTC)
    state = await run(
        run_id=journal.run_id,
        schedule=build_schedule(plan),
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=FakeCapturePort(device, clock=clock, cycle_s=60),
    )
    assert state.items["goto"].status is ItemStatus.DONE
    assert state.items["shutdown"].status is ItemStatus.DONE
    recovery = [r for r in journal.records if r.retry_at is not None]
    assert len(recovery) > 3
    end = build_schedule(plan).items[1].end_utc
    assert all(r.retry_at <= end for r in recovery)


async def test_recovery_backoff_and_attempts_survive_journal_restart() -> None:
    class CrashJournal(FakeJournal):
        async def append(self, record) -> None:
            await super().append(record)
            if record.retry_at is not None:
                raise RuntimeError("power cut after durable recovery")

    plan = build_plan()
    journal = CrashJournal("recover-crash")
    device = RecoveringDevice(1)
    clock = FakeClock(BASE_UTC)
    with pytest.raises(RuntimeError, match="power cut"):
        await run(
            run_id=journal.run_id,
            schedule=build_schedule(plan),
            plan=plan,
            journal=journal,
            device=device,
            events=FakeDeviceEvents(),
            clock=clock,
            controls=FakeControls(),
            capture=FakeCapturePort(device, clock=clock),
        )
    records = journal.records
    recovered = reduce(records).items["goto"]
    assert recovered.recovery_attempts == 1
    assert recovered.retry_at == clock.now() + timedelta(seconds=5)
    restarted = FakeJournal(journal.run_id, records)
    resumed_device = RecoveringDevice(1)
    state = await run(
        run_id=journal.run_id,
        schedule=build_schedule(plan),
        plan=plan,
        journal=restarted,
        device=resumed_device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=FakeCapturePort(resumed_device, clock=clock, cycle_s=60),
    )
    assert state.items["goto"].status is ItemStatus.DONE
    assert [r.recovery_attempts for r in restarted.records if r.retry_at] == [1, 2]


@pytest.mark.parametrize("recoverable, expected_attempts", [(True, 1), (False, 1)])
async def test_nonwindowed_error_stays_visible_until_night_boundary(
    recoverable: bool, expected_attempts: int
) -> None:
    plan = build_plan()
    journal = FakeJournal("recover-filter")
    device = FakeDeviceCommands(
        raise_on={EffectName.SET_FILTER: DeviceCommandError("offline", recoverable=recoverable)}
    )
    clock = FakeClock(BASE_UTC)
    state = await run(
        run_id=journal.run_id,
        schedule=build_schedule(plan),
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=FakeCapturePort(device, clock=clock, cycle_s=60),
    )
    failures = [
        r
        for r in journal.records
        if r.item_id == "filter" and r.effect_outcome is EffectOutcome.FAILED
    ]
    assert len(failures) == expected_attempts
    assert failures[-1].retry_at is not None
    assert state.items["filter"].status is ItemStatus.DONE
    assert state.items["shutdown"].status is ItemStatus.DONE


async def test_operator_stop_interrupts_recovery_before_next_attempt() -> None:
    class StopDuringRecovery(FakeControls):
        async def poll(self):
            if not any(r.control is ControlKind.STOP for r in journal.records) and any(
                r.retry_at for r in journal.records
            ):
                return ControlCommand(kind=ControlKind.STOP, reason="operator stopped recovery")
            return None

    plan = build_plan()
    journal = FakeJournal("recover-stop")
    device = RecoveringDevice(100)
    clock = FakeClock(BASE_UTC)
    state = await run(
        run_id=journal.run_id,
        schedule=build_schedule(plan),
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=StopDuringRecovery(),
        capture=FakeCapturePort(device, clock=clock),
    )
    assert state.run_status is RunStatus.STOPPED
    assert sum(name is EffectName.SLEW_TO for name, _ in device.calls) == 1
    assert (EffectName.PARK, {}) in device.calls


async def test_saved_frame_with_error_is_not_exposed_twice_during_recovery() -> None:
    class SavedButErroredCapture(FakeCapturePort):
        async def expose_frame(self, **kwargs):
            await super().expose_frame(**kwargs)
            if kwargs["block_id"] == "goto" and kwargs["frame_index"] == 0:
                raise DeviceCommandError("connection lost after save")

    plan = build_plan()
    journal = FakeJournal("recover-saved-frame")
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC)
    state = await run(
        run_id=journal.run_id,
        schedule=build_schedule(plan),
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=SavedButErroredCapture(device, clock=clock, cycle_s=60),
    )
    assert state.items["goto"].status is ItemStatus.DONE
    frames = [
        args["frame_index"]
        for name, args in device.calls
        if name is EffectName.EXPOSE_FRAME and args["block_id"] == "goto"
    ]
    assert len(frames) == len(set(frames))


async def test_restart_after_window_does_not_retry_stale_slew() -> None:
    class CrashJournal(FakeJournal):
        async def append(self, record):
            await super().append(record)
            if record.retry_at:
                raise RuntimeError("power cut")

    plan = build_plan()
    schedule = build_schedule(plan)
    journal = CrashJournal("recover-expired")
    device = RecoveringDevice(100)
    clock = FakeClock(BASE_UTC)
    with pytest.raises(RuntimeError, match="power cut"):
        await run(
            run_id=journal.run_id,
            schedule=schedule,
            plan=plan,
            journal=journal,
            device=device,
            events=FakeDeviceEvents(),
            clock=clock,
            controls=FakeControls(),
            capture=FakeCapturePort(device, clock=clock),
        )
    clock.advance(3600)
    restarted = FakeJournal(journal.run_id, journal.records)
    state = await run(
        run_id=journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=restarted,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=FakeCapturePort(device, clock=clock),
    )
    assert state.items["goto"].status is ItemStatus.DONE
    assert sum(name is EffectName.SLEW_TO for name, _ in device.calls) == 1
    assert state.items["shutdown"].status is ItemStatus.DONE


@pytest.mark.parametrize("focus_failures", [2, 1000])
async def test_poor_frame_focus_recovers_or_waits_until_dawn_without_failing_night(
    focus_failures: int,
) -> None:
    class FocusCapture(FakeCapturePort):
        focused = False
        focus_attempts = 0

        async def focus_required(self) -> bool:
            frames = [
                args
                for name, args in device.calls
                if name is EffectName.EXPOSE_FRAME and args["block_id"] == "goto"
            ]
            return len(frames) >= 3 and not self.focused

        async def autofocus(self) -> None:
            self.focus_attempts += 1
            if self.focus_attempts <= focus_failures:
                raise DeviceFocusConditionsError("too few stars under clouds")
            self.focused = True

    plan = build_plan()
    journal = FakeJournal("focus-cloud-recovery")
    plan = plan.model_copy(
        update={
            "items": tuple(
                item.model_copy(update={"end": item.end.model_copy(update={"minutes": 40.0})})
                if item.id == "goto"
                else item
                for item in plan.items
            )
        }
    )
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC)
    capture = FocusCapture(device, clock=clock, cycle_s=60)
    state = await run(
        run_id=journal.run_id,
        schedule=build_schedule(plan),
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=capture,
    )
    assert state.run_status is RunStatus.COMPLETED
    assert state.items["goto"].status is ItemStatus.DONE
    assert state.items["shutdown"].status is ItemStatus.DONE
    assert capture.focus_attempts >= 3
    assert (EffectName.STOP_LIVE_STACK, {}) in device.calls
    frames = [
        args["frame_index"]
        for name, args in device.calls
        if name is EffectName.EXPOSE_FRAME and args["block_id"] == "goto"
    ]
    assert len(frames) == len(set(frames))
    if focus_failures == 2:
        assert len(frames) > 3
    else:
        assert len(frames) == 3
    deferred = [record for record in journal.records if record.focus_conditions]
    assert deferred
    assert all(record.status is not ItemStatus.FAILED for record in deferred)
    assert (deferred[0].retry_at - deferred[0].at).total_seconds() == 600
    assert (deferred[1].retry_at - deferred[1].at).total_seconds() == 600


async def test_standalone_focus_conditions_skip_expired_focus_but_run_shutdown() -> None:
    from ssc.plan.models import AutofocusItem

    class CloudyDevice(FakeDeviceCommands):
        async def autofocus(self) -> None:
            raise DeviceFocusConditionsError("no measurable stars")

    original = build_plan()
    plan = original.model_copy(
        update={
            "items": (
                AutofocusItem(id="focus", start=original.items[0].start),
                original.items[-1],
            )
        }
    )
    schedule = build_schedule(plan)
    schedule = schedule.model_copy(
        update={
            "night": schedule.night.model_copy(
                update={
                    "dark_end_utc": BASE_UTC + timedelta(minutes=25),
                }
            )
        }
    )
    journal = FakeJournal("focus-cloud-deadline")
    state = await run(
        run_id=journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=CloudyDevice(),
        events=FakeDeviceEvents(),
        clock=FakeClock(BASE_UTC),
        controls=FakeControls(),
    )
    assert state.items["focus"].status is ItemStatus.DONE
    assert state.items["shutdown"].status is ItemStatus.DONE
    assert state.run_status is RunStatus.COMPLETED


async def test_device_event_timeout_recovers_until_window_with_distinct_reason() -> None:
    plan = build_plan()
    schedule = build_schedule(plan)
    journal = FakeJournal("failures-timeout")
    device = FakeDeviceCommands()
    events = FakeDeviceEvents(timeout_on=frozenset({"slew_complete"}))
    state = await run(
        run_id=journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=events,
        clock=FakeClock(BASE_UTC - timedelta(minutes=5)),
        controls=FakeControls(),
        poll_interval_s=3600.0,
    )

    assert state.items["goto"].status is ItemStatus.DONE
    assert not any(name is EffectName.EXPOSE_FRAME for name, _ in device.calls)
    assert state.items["filter"].status is ItemStatus.DONE
    assert state.items["shutdown"].status is ItemStatus.DONE
    assert state.run_status is RunStatus.COMPLETED
    failures = [r for r in journal.records if r.item_id == "goto"
                and r.effect_outcome is EffectOutcome.FAILED]
    assert len(failures) >= 1
    assert all("timed out" in r.reason and "slew_complete" in r.reason for r in failures)


async def test_not_implemented_waits_through_window_and_continues() -> None:
    plan = build_plan()
    schedule = build_schedule(plan)
    journal = FakeJournal("failures-not-implemented")
    device = FakeDeviceCommands(
        raise_on={
            EffectName.SET_FILTER: DeviceCommandNotImplementedError("FilterWheel not present")
        }
    )
    clock = FakeClock(BASE_UTC - timedelta(minutes=5))
    state = await run(
        run_id=journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=FakeCapturePort(device, clock=clock, cycle_s=600),
        poll_interval_s=3600.0,
    )

    assert state.items["filter"].status is ItemStatus.DONE
    failures = [r for r in journal.records if r.item_id == "filter"
                and r.effect_outcome is EffectOutcome.FAILED]
    assert failures and all("FilterWheel not present" in r.reason for r in failures)
    assert state.items["goto"].status is ItemStatus.DONE
    assert state.items["shutdown"].status is ItemStatus.DONE
    assert state.run_status is RunStatus.COMPLETED


async def test_failed_shutdown_makes_the_run_failed_after_other_items_settle() -> None:
    plan = build_plan()
    schedule = build_schedule(plan)
    journal = FakeJournal("failed-shutdown")
    device = FakeDeviceCommands(
        raise_on={EffectName.PARK: DeviceCommandError("park verification failed")}
    )
    state = await run(
        run_id=journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=FakeClock(BASE_UTC - timedelta(minutes=5)),
        controls=FakeControls(),
        poll_interval_s=3600.0,
    )

    assert state.items["shutdown"].status is ItemStatus.FAILED
    assert state.run_status is RunStatus.FAILED
