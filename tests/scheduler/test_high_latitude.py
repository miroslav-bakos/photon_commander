"""Above roughly 60N in midsummer, astronomical night does not occur at all
(DESIGN.md 4.1). An ``event``-kind ``TimeSpec`` naming astronomical dusk/dawn
there must come back unschedulable with an explained absence — never a
guessed time (CLAUDE.md invariant 7).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from ssc.astro.events import Ephemeris, Site, SolarEvent
from ssc.astro.framing import Profile
from ssc.astro.units import Deg, Metres
from ssc.plan import EventTimeSpec, Plan, StartupItem
from ssc.scheduler import compile_plan

#: Tromso, Norway — well above the Arctic Circle.
_SITE = Site(latitude_deg=Deg(69.6496), longitude_deg=Deg(18.9560), elevation_m=Metres(10.0))
_TZ = ZoneInfo("Europe/Oslo")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_astronomical_dusk_does_not_occur_at_midsummer(
    ephemeris: Ephemeris, profile: Profile
) -> None:
    plan = Plan(
        id="p",
        name="polar",
        items=(StartupItem(id="a", start=EventTimeSpec(event=SolarEvent.ASTRONOMICAL_DUSK)),),
        created_at=_NOW,
        updated_at=_NOW,
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=date(2026, 6, 21),
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    item = schedule.items[0]
    assert item.start_utc is None
    assert item.end_utc is None
    diagnostic = next(d for d in item.diagnostics if d.code == "event_does_not_occur")
    assert "always_above" in diagnostic.message

    # The night window itself says the same thing, not just the item.
    assert schedule.night.dark_start_utc is None
    assert schedule.night.dark_end_utc is None
