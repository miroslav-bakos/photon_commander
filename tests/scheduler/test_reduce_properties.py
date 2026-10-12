"""Hypothesis: :func:`ssc.scheduler.executor.reduce` is deterministic and total over
any prefix of a valid journal.

"Valid journal" here means one that :func:`ssc.scheduler.executor.run` actually
produced — the strategy below drives a real (small, fast, in-memory) run over a
randomly generated plan, with some items configured to fail, so the journals under
test are genuine ones rather than hand-waved shapes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ssc.plan.models import (
    AbsoluteTimeSpec,
    FilterItem,
    ParkItem,
    Plan,
    PlanItem,
    RelativeTimeSpec,
    SetExposureItem,
    SetGainItem,
    ShutdownItem,
)
from ssc.scheduler.compiler import CompiledItem, CompiledSchedule, NightWindow
from ssc.scheduler.executor import DeviceCommandError, EffectName, JournalRecord, reduce, run
from tests.scheduler.fakes import (
    FakeClock,
    FakeControls,
    FakeDeviceCommands,
    FakeDeviceEvents,
    FakeJournal,
)

_BASE = datetime(2026, 3, 1, 21, 0, tzinfo=UTC)
_NOW = datetime(2026, 3, 1, tzinfo=UTC)

_ITEM_FACTORIES = {
    EffectName.SET_EXPOSURE: lambda item_id, start: SetExposureItem(
        id=item_id, start=start, exposure_sec=10.0
    ),
    EffectName.SET_GAIN: lambda item_id, start: SetGainItem(id=item_id, start=start, gain=80),
    EffectName.SET_FILTER: lambda item_id, start: FilterItem(
        id=item_id, start=start, position="LP"
    ),
    EffectName.PARK: lambda item_id, start: ParkItem(id=item_id, start=start),
    EffectName.SHUTDOWN: lambda item_id, start: ShutdownItem(id=item_id, start=start),
}


@st.composite
def _plan_and_failures(draw: st.DrawFn) -> tuple[Plan, dict[EffectName, Exception]]:
    kinds = draw(st.lists(st.sampled_from(list(_ITEM_FACTORIES)), min_size=1, max_size=6))
    items: list[PlanItem] = []
    for i, kind in enumerate(kinds):
        item_id = f"item{i}"
        start = (
            AbsoluteTimeSpec(local_datetime=_BASE.replace(tzinfo=None))
            if i == 0
            else RelativeTimeSpec(offset_min=0.0)
        )
        items.append(_ITEM_FACTORIES[kind](item_id, start))
    plan = Plan(
        id="prop-plan",
        name="reduce property fixture",
        created_at=_NOW,
        updated_at=_NOW,
        items=tuple(items),
    )

    failing_kinds = draw(
        st.sets(st.sampled_from(list(_ITEM_FACTORIES)), max_size=len(_ITEM_FACTORIES))
    )
    raise_on = {kind: DeviceCommandError("simulated failure") for kind in failing_kinds}
    return plan, raise_on


def _schedule_for(plan: Plan) -> CompiledSchedule:
    night = NightWindow(
        date="2026-03-01",
        tz="UTC",
        window_start_utc=_BASE - timedelta(hours=8),
        window_end_utc=_BASE + timedelta(hours=8),
        dark_start_utc=None,
        dark_end_utc=None,
        solar_events=(),
        lunar_events=(),
    )
    items = tuple(
        CompiledItem(
            item_id=item.id,
            kind=item.kind,
            enabled=True,
            start_utc=_BASE,
            end_utc=_BASE,
            resolution="property fixture",
            end_resolution="property fixture",
        )
        for item in plan.items
    )
    return CompiledSchedule(
        plan_id=plan.id, plan_revision="prop-revision", compiled_at=_BASE, night=night, items=items
    )


def _drive_to_journal(plan: Plan, raise_on: dict[EffectName, Exception]) -> list[JournalRecord]:
    async def _go() -> list[JournalRecord]:
        journal = FakeJournal("prop-run")
        await run(
            run_id=journal.run_id,
            schedule=_schedule_for(plan),
            plan=plan,
            journal=journal,
            device=FakeDeviceCommands(raise_on=raise_on),
            events=FakeDeviceEvents(),
            clock=FakeClock(_BASE - timedelta(minutes=1)),
            controls=FakeControls(),
            poll_interval_s=3600.0,
        )
        return journal.records

    return asyncio.run(_go())


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None, max_examples=40
)
@given(_plan_and_failures())
def test_reduce_is_deterministic_and_total_over_every_prefix(plan_and_failures: tuple) -> None:
    plan, raise_on = plan_and_failures
    journal = _drive_to_journal(plan, raise_on)
    assert journal  # a run always at least writes RUN_STARTED and RUN_ENDED

    for k in range(len(journal) + 1):
        prefix = journal[:k]
        first = reduce(prefix)
        second = reduce(prefix)
        assert first == second  # deterministic: same input, same output, every time

    # Totality: folding the whole journal, and every prefix of it, never raises —
    # already implied by the loop above completing, asserted explicitly for clarity.
    reduce(journal)
