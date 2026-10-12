"""Operational failure waits through the panel window, preserving saved work."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

import ssc.capture  # noqa: F401
from ssc.astro.events import Site
from ssc.astro.units import Deg, Metres
from ssc.plan.models import (
    AbsoluteTimeSpec,
    DurationEndSpec,
    GotoAndStackItem,
    MosaicGridSpec,
    MosaicItem,
    Plan,
    RelativeTimeSpec,
)
from ssc.scheduler.compiler import compile_plan
from ssc.scheduler.executor import (
    ControlCommand,
    ControlKind,
    DeviceCommandError,
    DeviceCommandNotImplementedError,
    EffectName,
    EffectOutcome,
    ItemStatus,
    RunStatus,
    reduce,
    run,
)
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from ssc.store.runs import RunRepository
from tests.scheduler.fakes import (
    FakeCapturePort,
    FakeClock,
    FakeControls,
    FakeDeviceCommands,
    FakeDeviceEvents,
)


class PowerCut(BaseException):
    pass


@pytest.mark.parametrize(
    "failure,stop,mosaic",
    [
        ("center", False, False),
        ("capture", False, False),
        ("unsupported", False, False),
        ("capture", True, False),
        ("capture", False, True),
    ],
)
async def test_panel_recovery_reopens_and_waits_until_window_or_stop(
    tmp_path, ephemeris, profile, failure, stop, mosaic
):
    start = datetime(2026, 10, 7, 21, tzinfo=UTC)
    plan = Plan(
        id="window-recovery",
        name="Night recovery",
        created_at=start,
        updated_at=start,
        items=(
            GotoAndStackItem(
                id="first",
                start=AbsoluteTimeSpec(local_datetime=start.replace(tzinfo=None)),
                end=DurationEndSpec(minutes=1.5),
                target_name="M45",
                ra_j2000_h=3.79,
                dec_j2000_deg=24.2,
                live_stack=False,
                dither=None,
            ),
            GotoAndStackItem(
                id="next",
                start=RelativeTimeSpec(offset_min=0),
                end=DurationEndSpec(minutes=1),
                target_name="IC1805",
                ra_j2000_h=2.55,
                dec_j2000_deg=61.45,
                live_stack=False,
                dither=None,
            ),
        ),
    )

    if mosaic:
        first = plan.items[0]
        plan = plan.model_copy(
            update={
                "items": (
                    MosaicItem(
                        id=first.id,
                        start=first.start,
                        end=first.end,
                        target_name=first.target_name,
                        ra_j2000_h=first.ra_j2000_h,
                        dec_j2000_deg=first.dec_j2000_deg,
                        live_stack=False,
                        dither=None,
                        mosaic=MosaicGridSpec(ra_num=2, dec_num=1),
                    ),
                )
            }
        )

    def compile_saved(value):
        return compile_plan(
            value,
            site=Site(
                latitude_deg=Deg(51.4934), longitude_deg=Deg(-0.0098), elevation_m=Metres(45)
            ),
            tz=ZoneInfo("UTC"),
            night_date=date(2026, 10, 7),
            ephemeris=ephemeris,
            now=start,
            profile=profile,
        )

    clock = FakeClock(start)
    controls = FakeControls()
    device = FakeDeviceCommands()
    world = {"failures": 0, "saved": 0, "second_panel": False}
    waits = []
    original_sleep = clock.sleep_until

    async def bounded_sleep(until_utc):
        delay = (until_utc - clock.now()).total_seconds()
        if world["failures"] and delay > 0:
            waits.append(delay)
        await original_sleep(until_utc)

    clock.sleep_until = bounded_sleep

    def fail():
        world["failures"] += 1
        if stop and world["failures"] == 3:
            controls.push(ControlCommand(kind=ControlKind.STOP, reason="owner Stop"))
        if failure == "unsupported":
            raise DeviceCommandNotImplementedError(
                "optical solver unavailable; wait for correction"
            )
        raise DeviceCommandError("transient acquisition/centering outage")

    class FaultCapture(FakeCapturePort):
        async def center_target(self, **kwargs):
            if mosaic:
                world["second_panel"] = (
                    kwargs["ra_j2000_h"] != schedule.items[0].panels[0].ra_j2000_deg / 15
                )
            if kwargs["ra_j2000_h"] == 3.79 and failure in {"center", "unsupported"}:
                fail()
            await super().center_target(**kwargs)

        async def expose_frame(self, **kwargs):
            if kwargs["block_id"] == "first" and not world["second_panel"]:
                if world["saved"]:
                    fail()
                world["saved"] += 1
            await super().expose_frame(**kwargs)

    class CutAfterRecovery:
        def __init__(self, journal):
            self.journal = journal

        async def read_all(self, run_id):
            return await self.journal.read_all(run_id)

        async def append(self, record):
            await self.journal.append(record)
            if record.retry_at is not None:
                raise PowerCut()

    path = tmp_path / "night.sqlite3"
    db = await open_database(path)
    try:
        await migrate(db)
        repo = RunRepository(db)
        schedule = compile_saved(plan)
        deadline = schedule.items[0].panels[0].end_utc if mosaic else schedule.items[0].end_utc
        await repo.create(
            run_id=plan.id, plan=plan, plan_revision=64, schedule=schedule, started_at=start
        )
        with pytest.raises(PowerCut):
            await run(
                run_id=plan.id,
                plan=plan,
                schedule=schedule,
                journal=CutAfterRecovery(repo.journal),
                device=device,
                events=FakeDeviceEvents(),
                clock=clock,
                controls=controls,
                capture=FaultCapture(device, clock=clock, cycle_s=10),
            )
        prefix = await repo.journal.read_all(plan.id)
        assert reduce(prefix).items["first"].status is ItemStatus.RUNNING
        assert "outage" in prefix[-1].reason or "unavailable" in prefix[-1].reason
    finally:
        await db.aclose()
    db = await open_database(path)
    try:
        repo = RunRepository(db)
        saved = await repo.get(plan.id)
        state = await run(
            run_id=plan.id,
            plan=saved.plan,
            schedule=compile_saved(saved.plan),
            journal=repo.journal,
            device=device,
            events=FakeDeviceEvents(),
            clock=clock,
            controls=controls,
            capture=FaultCapture(device, clock=clock, cycle_s=10),
        )
        records = await repo.journal.read_all(plan.id)
        assert not any(r.status is ItemStatus.FAILED for r in records)
        retries = [r for r in records if r.item_id == "first" and r.retry_at is not None]
        assert retries and all(r.retry_at <= deadline for r in retries)
        if not stop:
            assert retries[-1].retry_at == deadline

        assert world["failures"] >= 3
        first_frames = [
            args
            for name, args in device.calls
            if name is EffectName.EXPOSE_FRAME and args["block_id"] == "first"
        ]
        if mosaic:
            assert (
                sum(
                    r.effect is not None
                    and r.effect.name is EffectName.EXPOSE_FRAME
                    and r.effect_outcome is EffectOutcome.COMPLETED
                    and r.panel_index == 0
                    for r in records
                )
                == 1
            )
            assert world["second_panel"]
        else:
            assert len(first_frames) == (1 if failure == "capture" else 0)
        assert waits and all(delay >= 5 for delay in waits)
        assert len(waits) < 20, "recovery busy-polls instead of bounded backoff"
        if stop:
            assert state.run_status is RunStatus.STOPPED
            assert "next" not in state.items
            assert EffectName.PARK in [name for name, _ in device.calls]
        else:
            assert state.items["first"].status in {ItemStatus.SKIPPED, ItemStatus.DONE}
            if mosaic:
                assert any(
                    r.panel_index == 1
                    and r.effect is not None
                    and r.effect.name is EffectName.EXPOSE_FRAME
                    for r in records
                )
            else:
                assert state.items["next"].status is ItemStatus.DONE
            assert state.run_status is RunStatus.COMPLETED
            assert clock.now() >= deadline
    finally:
        await db.aclose()
