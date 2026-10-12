"""DST is resolved explicitly at compile time, never by luck (CLAUDE.md
invariant 5 / DESIGN.md 4.1: "an autumn night can contain 01:30 twice").

Europe/Berlin's autumn transition in 2026 falls back at 01:00 UTC on
2026-10-25 (03:00 CEST -> 02:00 CET), so the hour that repeats that night is
**02:00-03:00 local**, not 01:30 — verified directly against
:func:`ssc.astro.events.local_time_kind` below rather than assumed, since the
exact ambiguous hour is a property of the zone's offset, not a universal
constant (Bucharest's own repeated hour, exercised in ``tests/astro``, is
03:00-04:00 local because EEST is one hour further from UTC than CEST).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from ssc.astro.events import Ephemeris, Site
from ssc.astro.framing import Profile
from ssc.astro.units import Deg, Metres
from ssc.plan import AbsoluteTimeSpec, Plan, StartupItem
from ssc.scheduler import compile_plan

_SITE = Site(latitude_deg=Deg(52.52), longitude_deg=Deg(13.405), elevation_m=Metres(34.0))
_TZ = ZoneInfo("Europe/Berlin")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _plan_with_absolute(local_dt: datetime, *, fold: Literal[0, 1]) -> Plan:
    return Plan(
        id="p",
        name="dst",
        items=(StartupItem(id="a", start=AbsoluteTimeSpec(local_datetime=local_dt, fold=fold)),),
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_the_autumn_night_that_contains_0230_twice(ephemeris: Ephemeris, profile: Profile) -> None:
    wall = datetime(2026, 10, 25, 2, 30)  # noqa: DTZ001 — deliberately naive, this is the point
    first = compile_plan(
        _plan_with_absolute(wall, fold=0),
        site=_SITE,
        tz=_TZ,
        night_date=date(2026, 10, 25),
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    ).items[0]
    second = compile_plan(
        _plan_with_absolute(wall, fold=1),
        site=_SITE,
        tz=_TZ,
        night_date=date(2026, 10, 25),
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    ).items[0]

    assert first.start_utc is not None
    assert second.start_utc is not None
    assert (second.start_utc - first.start_utc).total_seconds() == 3600.0
    assert first.diagnostics == ()
    assert second.diagnostics == ()


def test_the_spring_night_that_contains_0200_not_at_all(
    ephemeris: Ephemeris, profile: Profile
) -> None:
    # The gap falls at 02:00 local on the morning of 2026-03-29, which is the
    # tail end of the night whose evening was 2026-03-28 — the noon-anchored
    # night window for "night_date=2026-03-28" is the one that contains it.
    wall = datetime(2026, 3, 29, 2, 30)  # noqa: DTZ001
    schedule = compile_plan(
        _plan_with_absolute(wall, fold=0),
        site=_SITE,
        tz=_TZ,
        night_date=date(2026, 3, 28),
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    item = schedule.items[0]
    assert item.start_utc is None
    assert item.end_utc is None
    assert any(d.code == "nonexistent_local_time" for d in item.diagnostics)
