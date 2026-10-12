"""Controls: pause/resume/skip/stop/retry each leave the journal in the expected
shape, and ``stop`` parks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from ssc.scheduler.executor import (
    ControlCommand,
    ControlKind,
    DeviceCommandNotImplementedError,
    EffectName,
    EffectOutcome,
    ItemStatus,
    RecordKind,
    RunStatus,
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


@dataclass
class ControlsAfterCalls:
    """Returns ``command`` the first time ``device`` has recorded at least
    ``threshold`` calls, so a control can be injected deterministically "mid-item"
    without depending on real timing or loop-iteration counts."""

    device: FakeDeviceCommands
    threshold: int
    command: ControlCommand
    fired: bool = field(default=False, init=False)

    async def poll(self) -> ControlCommand | None:
        if not self.fired and len(self.device.calls) >= self.threshold:
            self.fired = True
            return self.command
        return None


class FlakyCapturePort(FakeCapturePort):
    """A scheduled frame fails exactly once, then succeeds, for ``retry``."""

    def __init__(self, device: FakeDeviceCommands, *, clock: FakeClock) -> None:
        super().__init__(device, clock=clock, cycle_s=600)
        self._attempts = 0

    async def expose_frame(self, **kwargs: object) -> None:
        self._attempts += 1
        if self._attempts == 1:
            # A permanent startup error stays visible even though later
            # recoverable failures now retry automatically.
            raise DeviceCommandNotImplementedError("startup confirmation unavailable")
        await super().expose_frame(**kwargs)  # type: ignore[arg-type]


def _run_id(name: str) -> str:
    return f"controls-{name}"


async def test_pause_then_resume_lets_the_run_finish() -> None:
    plan = build_plan()
    schedule = build_schedule(plan)
    journal = FakeJournal(_run_id("pause-resume"))
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC - timedelta(minutes=5))
    controls = FakeControls(
        queued=[
            ControlCommand(kind=ControlKind.PAUSE, reason="operator paused"),
            ControlCommand(kind=ControlKind.RESUME, reason="operator resumed"),
        ]
    )
    state = await run(
        run_id=journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=controls,
        capture=FakeCapturePort(device, clock=clock, cycle_s=600),
        poll_interval_s=3600.0,
    )

    control_records = [r for r in journal.records if r.kind is RecordKind.CONTROL]
    assert [r.control for r in control_records] == [ControlKind.PAUSE, ControlKind.RESUME]
    assert [r.run_status for r in control_records] == [RunStatus.PAUSED, RunStatus.RUNNING]
    assert state.run_status is RunStatus.COMPLETED
    assert state.items["shutdown"].status is ItemStatus.DONE


async def test_skip_leaves_the_item_skipped_and_the_run_continues() -> None:
    plan = build_plan()
    schedule = build_schedule(plan)
    journal = FakeJournal(_run_id("skip"))
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC - timedelta(minutes=5))
    controls = FakeControls(
        queued=[ControlCommand(kind=ControlKind.SKIP, item_id="goto", reason="cloudy")]
    )
    state = await run(
        run_id=journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=controls,
        capture=FakeCapturePort(device, clock=clock, cycle_s=600),
        poll_interval_s=3600.0,
    )

    assert state.items["goto"].status is ItemStatus.SKIPPED
    assert state.items["shutdown"].status is ItemStatus.DONE
    assert all(name != EffectName.SLEW_TO for name, _ in device.calls)
    skip_records = [
        r for r in journal.records if r.kind is RecordKind.CONTROL and r.control is ControlKind.SKIP
    ]
    assert len(skip_records) == 1
    assert skip_records[0].item_id == "goto"
    assert skip_records[0].reason == "cloudy"


async def test_stop_aborts_the_active_item_and_parks() -> None:
    plan = build_plan()
    schedule = build_schedule(plan)
    journal = FakeJournal(_run_id("stop"))
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC - timedelta(minutes=5))
    # Fire once the goto tracking-rate and slew calls have both completed.
    # goto's tracking-rate and slew calls have both completed — i.e. while
    # "goto" is the active item.
    controls = ControlsAfterCalls(
        device=device,
        threshold=11,
        command=ControlCommand(kind=ControlKind.STOP, reason="operator stop"),
    )
    state = await run(
        run_id=journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=controls,
        capture=FakeCapturePort(device, clock=clock, cycle_s=600),
        poll_interval_s=3600.0,
    )

    assert state.run_status is RunStatus.STOPPED
    assert state.items["goto"].status is ItemStatus.ABORTED
    assert "filter" not in state.items  # never reached, so never even touched
    assert (EffectName.STOP_STACK, {}) in device.calls
    assert (EffectName.PARK, {}) in device.calls

    stop_records = [
        r for r in journal.records if r.kind is RecordKind.CONTROL and r.control is ControlKind.STOP
    ]
    assert len(stop_records) == 2  # issued, then resolved
    assert stop_records[0].effect_outcome is None
    assert stop_records[1].effect_outcome is not None
    assert journal.records[-1].kind is RecordKind.RUN_ENDED
    assert journal.records[-1].run_status is RunStatus.STOPPED


async def test_retry_reruns_an_item_without_erasing_an_unrelated_recovery() -> None:
    plan = build_plan()
    schedule = build_schedule(plan)
    journal = FakeJournal(_run_id("retry"))
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC - timedelta(minutes=5))
    capture = FlakyCapturePort(device, clock=clock)
    controls = ControlsAfterCalls(
        device=device,
        # The first frame attempt fails after the goto setup effects. The call
        # count therefore remains at the setup boundary until this retry fires.
        threshold=13,
        command=ControlCommand(kind=ControlKind.RETRY, item_id="goto", reason="try again"),
    )
    state = await run(
        run_id=journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=controls,
        capture=capture,
        poll_interval_s=3600.0,
    )

    assert state.run_status is RunStatus.COMPLETED
    assert state.items["startup"].status is ItemStatus.DONE
    assert state.items["goto"].status is ItemStatus.DONE
    assert state.ended_reason == "all items settled"
    assert any(
        record.item_id == "startup" and record.effect_outcome is EffectOutcome.FAILED
        for record in journal.records
    )
    frame_calls = [args for name, args in device.calls if name is EffectName.EXPOSE_FRAME]
    assert [args["block_id"] for args in frame_calls] == ["startup", "goto"]
    names = [name for name, _ in device.calls]
    slew_indexes = [index for index, name in enumerate(names) if name is EffectName.SLEW_TO]
    assert len(slew_indexes) == 2
    assert all(names[index - 1] is EffectName.STOP_LIVE_STACK for index in slew_indexes)
    retry_records = [
        r
        for r in journal.records
        if r.kind is RecordKind.CONTROL and r.control is ControlKind.RETRY
    ]
    assert len(retry_records) == 1
    assert retry_records[0].item_id == "goto"
