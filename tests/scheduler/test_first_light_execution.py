"""Reusable first-light block semantics at the executor boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ssc.plan.models import (
    AbsoluteTimeSpec,
    CaptureCalibrationItem,
    CatalogTargetSpec,
    DitherSpec,
    DurationEndSpec,
    FixedCountSpec,
    FrameType,
    FramingSpec,
    GotoAndStackItem,
    MosaicGridSpec,
    MosaicItem,
    MountMode,
    Plan,
    RelativeTimeSpec,
    ShutdownItem,
    TakeExposuresItem,
    WaitItem,
)
from ssc.scheduler.compiler import CompiledPanel
from ssc.scheduler.executor import (
    ControlCommand,
    ControlKind,
    EffectName,
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
from tests.scheduler.schedules import BASE_UTC, build_schedule


def _plan(*items: object) -> Plan:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Plan(
        id="first-light-test",
        name="First light",
        created_at=now,
        updated_at=now,
        items=items,  # type: ignore[arg-type]
    )


class ResumeAfterAutomaticPause:
    def __init__(self, journal: FakeJournal) -> None:
        self.journal = journal
        self.sent = False

    async def poll(self) -> ControlCommand | None:
        paused = any(
            record.kind is RecordKind.CONTROL
            and record.control is ControlKind.PAUSE
            and record.item_id == "adjust"
            for record in self.journal.records
        )
        if paused and not self.sent:
            self.sent = True
            return ControlCommand(kind=ControlKind.RESUME, reason="alignment accepted")
        return None


async def test_operator_checkpoint_is_durable_and_resume_advances_past_it() -> None:
    plan = _plan(
        WaitItem(
            id="adjust",
            start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
            requires_confirmation=True,
            instructions="Physically refine polar alignment, then accept.",
        ),
        ShutdownItem(id="shutdown", start=RelativeTimeSpec(offset_min=0)),
    )
    schedule = build_schedule(plan)
    journal = FakeJournal("checkpoint")
    device = FakeDeviceCommands()

    state = await run(
        run_id=journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=FakeClock(BASE_UTC),
        controls=ResumeAfterAutomaticPause(journal),
    )

    pause = next(
        record
        for record in journal.records
        if record.kind is RecordKind.CONTROL and record.control is ControlKind.PAUSE
    )
    assert pause.item_id == "adjust"
    # Step 0 is the recomputed start-time sleep; the durable checkpoint
    # advances past phase 1 so a resumed process continues at phase 2.
    assert pause.step == 2
    assert pause.reason == "Physically refine polar alignment, then accept."
    assert state.items["adjust"].status is ItemStatus.DONE
    assert state.run_status is RunStatus.COMPLETED

    # A remount/restart from the durable pause starts at step 1; it cannot
    # silently ask for (or skip back into) the physical adjustment again.
    resumed_journal = FakeJournal("checkpoint", seed=journal.records[: journal.records.index(pause) + 1])
    resumed_device = FakeDeviceCommands()
    resumed = await run(
        run_id=resumed_journal.run_id,
        schedule=schedule,
        plan=plan,
        journal=resumed_journal,
        device=resumed_device,
        events=FakeDeviceEvents(),
        clock=FakeClock(BASE_UTC),
        controls=FakeControls(queued=[ControlCommand(kind=ControlKind.RESUME)]),
    )
    assert resumed.items["adjust"].status is ItemStatus.DONE
    assert sum(
        record.control is ControlKind.PAUSE for record in resumed_journal.records
        if record.kind is RecordKind.CONTROL
    ) == 1


async def test_target_imaging_owns_pointing_capture_and_live_stack_lifecycle() -> None:
    item = TakeExposuresItem(
        id="m31",
        start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
        target=CatalogTargetSpec(name="M31", ra_j2000_h=0.7123056, dec_j2000_deg=41.269167),
        framing=FramingSpec(mode=MountMode.EQ, lens="tele"),
        count=FixedCountSpec(frames=2),
        exposure_sec=30,
        gain=80,
        filter_name="LP",
        frame_type=FrameType.LIGHT,
        live_stack=True,
    )
    plan = _plan(item)
    device = FakeDeviceCommands()
    capture = FakeCapturePort(device)
    state = await run(
        run_id="m31",
        schedule=build_schedule(plan),
        plan=plan,
        journal=FakeJournal("m31"),
        device=device,
        events=FakeDeviceEvents(),
        clock=FakeClock(BASE_UTC),
        controls=FakeControls(),
        capture=capture,
    )

    names = [name for name, _ in device.calls]
    assert names[:5] == [
        EffectName.UNPARK,
        EffectName.SET_TRACKING_RATE,
        EffectName.SLEW_TO,
        EffectName.SET_FILTER,
        EffectName.SET_GAIN,
    ]
    frames = [args for name, args in device.calls if name is EffectName.EXPOSE_FRAME]
    assert len(frames) == 2
    assert all(frame["object_name"] == "M31" and frame["live_stack"] is True for frame in frames)
    assert names[-1] is EffectName.STOP_LIVE_STACK
    assert state.items["m31"].status is ItemStatus.DONE


async def test_new_run_skips_expired_panel_and_starts_in_current_window() -> None:
    first = GotoAndStackItem(
        id="panel-1",
        start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
        duration_min=10,
        target_name="IC 1805 1",
        ra_j2000_h=2.5,
        dec_j2000_deg=61.0,
    )
    second = GotoAndStackItem(
        id="panel-2",
        start=RelativeTimeSpec(offset_min=0),
        duration_min=10,
        target_name="IC 1805 2",
        ra_j2000_h=2.7,
        dec_j2000_deg=61.0,
    )
    plan = _plan(first, second)
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC + timedelta(minutes=12))

    state = await run(
        run_id="start-from-now",
        schedule=build_schedule(plan),
        plan=plan,
        journal=FakeJournal("start-from-now"),
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=FakeCapturePort(device, clock=clock, cycle_s=60),
    )

    slews = [args for name, args in device.calls if name is EffectName.SLEW_TO]
    assert state.items["panel-1"].status is ItemStatus.SKIPPED
    assert "current time" in state.items["panel-1"].reason
    assert slews
    assert all(args["ra_j2000_h"] == second.ra_j2000_h for args in slews)


async def test_panel_autofocus_runs_after_slew_and_before_stack() -> None:
    item = GotoAndStackItem(
        id="panel",
        start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
        end=DurationEndSpec(minutes=10),
        target=CatalogTargetSpec(
            name="IC 1396 1", ra_j2000_h=21.61, dec_j2000_deg=57.48
        ),
        autofocus_before=True,
        exposure_sec=23,
        gain=117,
        filter_name="LP",
    )
    plan = _plan(item)
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC)
    capture = FakeCapturePort(device, clock=clock, cycle_s=600)
    await run(
        run_id="panel-autofocus",
        schedule=build_schedule(plan),
        plan=plan,
        journal=FakeJournal("panel-autofocus"),
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=capture,
    )

    names = [name for name, _ in device.calls]
    assert names == [
        EffectName.SET_TRACKING_RATE,
        EffectName.STOP_LIVE_STACK,
        EffectName.SLEW_TO,
        EffectName.SET_FILTER,
        EffectName.CENTER_TARGET,
        EffectName.AUTOFOCUS,
        EffectName.SET_GAIN,
        EffectName.EXPOSE_FRAME,
        EffectName.STOP_LIVE_STACK,
    ]
    assert (EffectName.SET_FILTER, {"position": "LP"}) in device.calls
    assert (EffectName.SET_GAIN, {"gain": 117}) in device.calls
    assert (
        EffectName.EXPOSE_FRAME,
        {
            "block_id": "panel",
            "frame_index": 0,
            "exposure_sec": 23.0,
            "gain": 117,
            "frame_type": "light",
            "filter_name": "LP",
            "object_name": "IC 1396 1",
            "live_stack": True,
            "dither_every_n_frames": 6,
        },
    ) in device.calls


async def test_scheduled_panel_dithers_between_frames_at_its_own_cadence() -> None:
    item = GotoAndStackItem(
        id="panel",
        start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
        end=DurationEndSpec(minutes=10),
        target=CatalogTargetSpec(name="IC 1805 1", ra_j2000_h=2.55, dec_j2000_deg=61.45),
        exposure_sec=30,
        gain=200,
        filter_name="LP",
        dither=DitherSpec(min_pixels=15, max_pixels=200, every_n_frames=2),
    )
    plan = _plan(item)
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC)
    await run(
        run_id="panel-dither",
        schedule=build_schedule(plan),
        plan=plan,
        journal=FakeJournal("panel-dither"),
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=FakeCapturePort(device, clock=clock, cycle_s=60),
    )

    dithers = [args for name, args in device.calls if name is EffectName.DITHER]
    assert len(dithers) == 4
    assert all(
        15 <= float(args["pixels"]) <= 200
        and args["max_pixels"] == 200
        and args["object_name"] == "IC 1805 1"
        for args in dithers
    )


async def test_combined_mosaic_applies_capture_settings_to_every_panel() -> None:
    item = MosaicItem(
        id="mosaic",
        start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
        end=DurationEndSpec(minutes=10),
        target=CatalogTargetSpec(
            name="IC 1805", ra_j2000_h=2.55, dec_j2000_deg=61.45
        ),
        mosaic=MosaicGridSpec(ra_num=2, dec_num=1, overlap_pct=20),
        exposure_sec=17,
        gain=91,
        filter_name="LP",
    )
    plan = _plan(item)
    schedule = build_schedule(plan)
    compiled = schedule.items[0]
    assert compiled.start_utc is not None
    panels = tuple(
        CompiledPanel(
            row=0,
            col=index,
            ra_j2000_deg=38.0 + index,
            dec_j2000_deg=61.45,
            start_utc=compiled.start_utc + timedelta(minutes=5 * index),
            end_utc=compiled.start_utc + timedelta(minutes=5 * (index + 1)),
        )
        for index in range(2)
    )
    schedule = schedule.model_copy(
        update={"items": (compiled.model_copy(update={"panels": panels}),)}
    )
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC)
    capture = FakeCapturePort(device, clock=clock, cycle_s=300)
    await run(
        run_id="mosaic-camera-settings",
        schedule=schedule,
        plan=plan,
        journal=FakeJournal("mosaic-camera-settings"),
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=capture,
    )

    names = [name for name, _ in device.calls]
    assert names == [EffectName.SET_TRACKING_RATE] + 2 * [
        EffectName.STOP_LIVE_STACK,
        EffectName.SLEW_TO,
        EffectName.SET_FILTER,
        EffectName.CENTER_TARGET,
        EffectName.SET_GAIN,
        EffectName.EXPOSE_FRAME,
        EffectName.STOP_LIVE_STACK,
    ]
    assert device.calls.count((EffectName.SET_FILTER, {"position": "LP"})) == 2
    assert device.calls.count((EffectName.SET_GAIN, {"gain": 91})) == 2
    frames = [args for name, args in device.calls if name is EffectName.EXPOSE_FRAME]
    assert [frame["frame_index"] for frame in frames] == [0, 1]
    assert all(
        frame["exposure_sec"] == 17.0
        and frame["gain"] == 91
        and frame["filter_name"] == "LP"
        and frame["live_stack"] is True
        for frame in frames
    )


async def test_scheduled_darks_verify_safe_state_before_shared_calibration() -> None:
    item = CaptureCalibrationItem(
        id="darks",
        start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
        frame_type="dark",
        count=50,
        exposure_sec=30,
        gain=80,
        filter_name="Dark",
        optic_id="tele",
        generate_master=True,
        safe_state=True,
    )
    plan = _plan(item)
    device = FakeDeviceCommands()
    state = await run(
        run_id="darks",
        schedule=build_schedule(plan),
        plan=plan,
        journal=FakeJournal("darks"),
        device=device,
        events=FakeDeviceEvents(),
        clock=FakeClock(BASE_UTC),
        controls=FakeControls(),
    )

    assert [name for name, _ in device.calls] == [
        EffectName.SET_FILTER,
        EffectName.SET_TRACKING,
        EffectName.FIND_HOME,
        EffectName.PARK,
        EffectName.CAPTURE_CALIBRATION,
    ]
    calibration = device.calls[-1][1]
    assert calibration == {
        "frame_type": "dark", "count": 50, "exposure_sec": 30.0,
        "gain": 80, "filter_name": "Dark", "optic_id": "tele",
        "flat_source": None, "generate_master": True,
        "run_id": "darks", "block_id": "darks", "catalogue_resumable": True,
    }
    assert state.items["darks"].status is ItemStatus.DONE
