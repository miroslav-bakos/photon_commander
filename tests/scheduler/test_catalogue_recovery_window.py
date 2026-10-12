"""Unknown science catalogue progress blocks exposure without abandoning the panel."""

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
    Plan,
    RelativeTimeSpec,
)
from ssc.scheduler.compiler import compile_plan
from ssc.scheduler.executor import EffectName, FrameCountError, ItemStatus, RunStatus, run
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


@pytest.mark.parametrize("persistent", [False, True])
async def test_unknown_catalogue_reopen_recovers_or_saves_retained_at_deadline(
    tmp_path, ephemeris, profile, persistent
):
    start = datetime(2026, 10, 7, 21, tzinfo=UTC)
    first = GotoAndStackItem(
        id="first",
        start=AbsoluteTimeSpec(local_datetime=start.replace(tzinfo=None)),
        end=DurationEndSpec(minutes=2),
        target_name="M45",
        ra_j2000_h=3.79,
        dec_j2000_deg=24.2,
        live_stack=True,
        dither=None,
    )
    following = GotoAndStackItem(
        id="next",
        start=RelativeTimeSpec(offset_min=0),
        end=DurationEndSpec(minutes=1),
        target_name="IC1805",
        ra_j2000_h=2.55,
        dec_j2000_deg=61.45,
        live_stack=False,
        dither=None,
    )
    plan = Plan(
        id="catalogue-night",
        name="Catalogue recovery",
        created_at=start,
        updated_at=start,
        items=(first, following),
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

    device = FakeDeviceCommands()
    clock = FakeClock(start)
    saved = 0
    errors = 0
    retained_saves = []

    class Capture(FakeCapturePort):
        async def expose_frame(self, **kwargs):
            nonlocal saved
            await super().expose_frame(**kwargs)
            if kwargs["block_id"] == "first":
                saved += 1

        async def frames_captured(self, **kwargs):
            nonlocal errors
            if kwargs["item_id"] == "first" and saved and (persistent or errors < 2):
                errors += 1
                raise FrameCountError("catalogue read outage; acquisition count unknown")
            return await super().frames_captured(**kwargs)

        async def stop_live_stack(self, *, require_retained=False):
            if saved:
                retained_saves.append(require_retained)
            await super().stop_live_stack(require_retained=require_retained)

    class CutOnVisibleWait:
        def __init__(self, journal):
            self.journal = journal

        async def read_all(self, run_id):
            return await self.journal.read_all(run_id)

        async def append(self, record):
            await self.journal.append(record)
            if "catalogue read outage" in record.reason:
                raise PowerCut()

    path = tmp_path / "night.sqlite3"
    db = await open_database(path)
    try:
        await migrate(db)
        repo = RunRepository(db)
        schedule = compile_saved(plan)
        await repo.create(
            run_id=plan.id, plan=plan, plan_revision=64, schedule=schedule, started_at=start
        )
        with pytest.raises(PowerCut):
            await run(
                run_id=plan.id,
                plan=plan,
                schedule=schedule,
                journal=CutOnVisibleWait(repo.journal),
                device=device,
                events=FakeDeviceEvents(),
                clock=clock,
                controls=FakeControls(),
                capture=Capture(device, clock=clock, cycle_s=20),
            )
        prefix = await repo.journal.read_all(plan.id)
        assert prefix[-1].status is ItemStatus.RUNNING
        assert saved == 1
    finally:
        await db.aclose()
    db = await open_database(path)
    try:
        repo = RunRepository(db)
        value = await repo.get(plan.id)
        state = await run(
            run_id=plan.id,
            plan=value.plan,
            schedule=compile_saved(value.plan),
            journal=repo.journal,
            device=device,
            events=FakeDeviceEvents(),
            clock=clock,
            controls=FakeControls(),
            capture=Capture(device, clock=clock, cycle_s=20),
        )
        records = await repo.journal.read_all(plan.id)
        assert not any(record.status is ItemStatus.FAILED for record in records)
        frames = [
            args["frame_index"]
            for name, args in device.calls
            if name is EffectName.EXPOSE_FRAME and args["block_id"] == "first"
        ]
        assert len(frames) == len(set(frames)) == saved
        assert state.items["next"].status is ItemStatus.DONE
        assert state.run_status is RunStatus.COMPLETED
        if persistent:
            assert saved == 1
            assert True in retained_saves, (
                "known saved science requires retained integration proof at deadline"
            )
        else:
            assert saved > 1
        assert errors < 10, "catalogue outage must wait, not busy-poll"
    finally:
        await db.aclose()
