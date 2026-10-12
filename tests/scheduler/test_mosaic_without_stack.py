"""Optional live integration must not control persisted mosaic panel progression."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

import ssc.capture  # noqa: F401
from ssc.astro.events import Site
from ssc.astro.units import Deg, Metres
from ssc.plan.models import (
    AbsoluteTimeSpec,
    CatalogTargetSpec,
    DurationEndSpec,
    MosaicGridSpec,
    MosaicItem,
    Plan,
)
from ssc.scheduler.compiler import compile_plan
from ssc.scheduler.executor import EffectName, ItemStatus, RunStatus, run
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


@pytest.mark.parametrize("live_stack", [False, True])
async def test_saved_recompiled_mosaic_acquires_every_panel(
    tmp_path, ephemeris, profile, live_stack
):
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    start = datetime(2026, 10, 7, 21, tzinfo=UTC)
    plan = Plan(
        id="mosaic",
        name="IC 1805",
        created_at=now,
        updated_at=now,
        items=(
            MosaicItem(
                id="mosaic",
                start=AbsoluteTimeSpec(local_datetime=start.replace(tzinfo=None)),
                end=DurationEndSpec(minutes=5),
                target=CatalogTargetSpec(name="IC 1805", ra_j2000_h=2.55, dec_j2000_deg=61.45),
                mosaic=MosaicGridSpec(ra_num=2, dec_num=1),
                live_stack=live_stack,
                dither=None,
            ),
        ),
    )

    def compile_saved(saved):
        return compile_plan(
            saved,
            site=Site(
                latitude_deg=Deg(51.4934), longitude_deg=Deg(-0.0098), elevation_m=Metres(45)
            ),
            tz=ZoneInfo("UTC"),
            night_date=date(2026, 10, 7),
            ephemeris=ephemeris,
            now=now,
            profile=profile,
        )

    path = tmp_path / "mosaic.sqlite3"
    db = await open_database(path)
    await migrate(db)
    repo = RunRepository(db)
    await repo.create(
        run_id=plan.id, plan=plan, plan_revision=64, schedule=compile_saved(plan), started_at=start
    )
    await db.aclose()
    db = await open_database(path)
    try:
        repo = RunRepository(db)
        saved = await repo.get(plan.id)
        schedule = compile_saved(saved.plan)
        device = FakeDeviceCommands()
        clock = FakeClock(start)
        state = await run(
            run_id=plan.id,
            plan=saved.plan,
            schedule=schedule,
            journal=repo.journal,
            device=device,
            events=FakeDeviceEvents(),
            clock=clock,
            controls=FakeControls(),
            capture=FakeCapturePort(device, clock=clock, cycle_s=60),
        )
        slews = [args for name, args in device.calls if name is EffectName.SLEW_TO]
        assert len(slews) == len(schedule.items[0].panels) == 2
        records = await repo.journal.read_all(plan.id)
        science_panels = {
            r.panel_index
            for r in records
            if r.effect is not None
            and r.effect.name is EffectName.EXPOSE_FRAME
            and r.effect_outcome is not None
        }
        assert science_panels == {0, 1}
        assert state.run_status is RunStatus.COMPLETED
        assert state.items["mosaic"].status is ItemStatus.DONE
    finally:
        await db.aclose()
