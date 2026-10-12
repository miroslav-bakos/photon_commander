from datetime import date, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from ssc.api.routes.runs import run_history
from ssc.astro.events import Site
from ssc.astro.units import Deg, Metres
from ssc.scheduler.compiler import compile_plan
from ssc.state.models import RunLifecycle
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from ssc.store.plans import PlanRepository
from ssc.store.runs import RunRepository
from tests.scheduler.conftest import ephemeris, profile  # noqa: F401
from tests.scheduler.schedules import BASE_UTC, build_plan


async def test_history_uses_executed_night_and_revision_after_reopen(tmp_path, ephemeris, profile):  # noqa: F811 - imported pytest fixtures
    path = tmp_path / 'history.sqlite'
    db = await open_database(path)
    await migrate(db)
    plan = build_plan()
    plans = PlanRepository(db)
    await plans.create(plan, night_date=date(2026, 1, 2))
    repo = RunRepository(db)
    site = Site(latitude_deg=Deg(48.15), longitude_deg=Deg(17.1), elevation_m=Metres(150))
    for run_id, night, offset in [('older', date(2026, 1, 1), 4), ('newer', date(2026, 1, 1), 5), ('other-night', date(2026, 1, 2), 28)]:
        schedule = compile_plan(plan, site=site, tz=ZoneInfo('Europe/Bratislava'),
            night_date=night, ephemeris=ephemeris, now=BASE_UTC, profile=profile)
        await repo.create(run_id=run_id, plan=plan, plan_revision=1,
            schedule=schedule, started_at=BASE_UTC + timedelta(hours=offset))
        await repo.set_status(run_id, RunLifecycle.COMPLETED, ended_at=BASE_UTC + timedelta(hours=offset + 1))
    await plans.update(plan.id, plan.model_copy(update={'name': 'Edited after execution'}), expected_revision=1)
    await plans.delete(plan.id)
    await db.aclose()
    reopened = await open_database(path)
    try:
        response = await run_history(date(2026, 1, 1), date(2026, 1, 1), SimpleNamespace(database=reopened), None)
        assert [item.run_id for item in response.runs] == ['newer', 'older']
        assert all(item.night_date == date(2026, 1, 1) for item in response.runs)
        assert all(item.started_at.date() == date(2026, 1, 2) for item in response.runs)
        assert all(item.plan_name == plan.name and item.plan_revision == 1 for item in response.runs)
        assert all(item.status == RunLifecycle.COMPLETED for item in response.runs)
        empty = await run_history(date(2026, 1, 3), date(2026, 1, 3), SimpleNamespace(database=reopened), None)
        assert empty.runs == []
    finally:
        await reopened.aclose()


@pytest.mark.parametrize('end', [date(2025, 12, 31), date(2026, 3, 5)])
async def test_history_rejects_reversed_or_oversized_range(end):
    with pytest.raises(HTTPException) as caught:
        await run_history(date(2026, 1, 1), end, SimpleNamespace(), None)
    assert caught.value.status_code == 422
