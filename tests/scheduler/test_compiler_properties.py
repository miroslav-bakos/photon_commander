"""Hypothesis properties for the compiler (M5 gate, CLAUDE.md):

* compiled items stay in the plan's order and each has ``start_utc <= end_utc``
* a ``relative`` chain never precedes the item it chains from
* RA wraps correctly at the 0h/24h boundary in a target spec (mosaic panels)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from ssc.astro.events import Ephemeris, Site
from ssc.astro.framing import Profile
from ssc.astro.units import Deg, Metres
from ssc.plan import (
    AbsoluteTimeSpec,
    MosaicGridSpec,
    MosaicItem,
    Plan,
    RelativeTimeSpec,
    StartupItem,
)
from ssc.scheduler import compile_plan

_SITE = Site(latitude_deg=Deg(51.4934), longitude_deg=Deg(-0.0098), elevation_m=Metres(45.0))
_TZ = ZoneInfo("Europe/London")
_NIGHT_DATE = date(2026, 8, 28)
_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
_BASE_LOCAL = datetime(2026, 8, 28, 22, 0)  # noqa: DTZ001 — deliberately naive, see AbsoluteTimeSpec


@st.composite
def _nonnegative_chain_plan(draw: st.DrawFn) -> Plan:
    """A first item at a fixed local time, followed by 1-5 more chained with
    non-negative ``relative`` offsets and non-negative durations — by
    construction every start should land at or after its predecessor's end."""
    count = draw(st.integers(min_value=1, max_value=5))
    durations = draw(
        st.lists(
            st.floats(min_value=0.0, max_value=120.0, allow_nan=False),
            min_size=count,
            max_size=count,
        )
    )
    offsets = draw(
        st.lists(
            st.floats(min_value=0.0, max_value=90.0, allow_nan=False),
            min_size=count,
            max_size=count,
        )
    )
    items = [
        StartupItem(
            id="item-0",
            start=AbsoluteTimeSpec(local_datetime=_BASE_LOCAL),
            duration_min=durations[0],
        )
    ]
    for i in range(1, count):
        items.append(
            StartupItem(
                id=f"item-{i}",
                start=RelativeTimeSpec(offset_min=offsets[i]),
                duration_min=durations[i],
            )
        )
    return Plan(id="p", name="hyp", items=tuple(items), created_at=_NOW, updated_at=_NOW)


@given(plan=_nonnegative_chain_plan())
@settings(deadline=None, max_examples=30)
def test_chained_items_stay_ordered_and_never_precede_their_predecessor(
    plan: Plan, ephemeris: Ephemeris, profile: Profile
) -> None:
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    assert [item.item_id for item in schedule.items] == [item.id for item in plan.items]

    previous_end = None
    for item in schedule.items:
        assert item.start_utc is not None
        assert item.end_utc is not None
        assert item.start_utc <= item.end_utc
        if previous_end is not None:
            assert item.start_utc >= previous_end
        previous_end = item.end_utc


# --- RA wrap at the 0h/24h boundary -----------------------------------------

_NEAR_ZERO_OR_WRAP = st.one_of(
    st.floats(min_value=0.0, max_value=0.3, allow_nan=False),
    st.floats(min_value=23.7, max_value=23.999, allow_nan=False),
)


@given(ra_center_h=_NEAR_ZERO_OR_WRAP, ra_num=st.integers(min_value=1, max_value=4))
@settings(deadline=None, max_examples=25)
def test_mosaic_panels_wrap_correctly_at_the_ra_boundary(
    ra_center_h: float, ra_num: int, ephemeris: Ephemeris, profile: Profile
) -> None:
    plan = Plan(
        id="p",
        name="wrap",
        items=(
            MosaicItem(
                id="m",
                start=AbsoluteTimeSpec(local_datetime=_BASE_LOCAL),
                duration_min=5,
                ra_j2000_h=ra_center_h,
                dec_j2000_deg=10.0,
                mosaic=MosaicGridSpec(ra_num=ra_num, dec_num=1, overlap_pct=10),
            ),
        ),
        created_at=_NOW,
        updated_at=_NOW,
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    item = schedule.items[0]
    assert len(item.panels) == ra_num
    for panel in item.panels:
        assert 0.0 <= panel.ra_j2000_deg < 360.0
        assert -90.0 <= panel.dec_j2000_deg <= 90.0
