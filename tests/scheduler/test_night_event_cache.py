"""Night astronomy is reused without reusing plan-dependent results."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from ssc.astro.events import Ephemeris, Site
from ssc.astro.units import Deg, Metres
from ssc.scheduler import compiler


def test_night_events_reuse_and_separate_all_astronomical_inputs(monkeypatch):
    solar = Mock(return_value=())
    lunar = Mock(return_value=())
    monkeypatch.setattr(compiler, "solar_events", solar)
    monkeypatch.setattr(compiler, "lunar_events", lunar)
    compiler._night_events.cache_clear()
    ephemeris = Ephemeris(Path("kernel"), object(), object())
    site = Site(Deg(48), Deg(17), Metres(200))
    start = datetime(2026, 10, 3, 10, tzinfo=UTC)
    end = start + timedelta(days=1)
    try:
        first = compiler._night_events(ephemeris, site, start, end)
        assert compiler._night_events(ephemeris, site, start, end) is first
        assert solar.call_count == lunar.call_count == 1
        for different_site in (
            Site(Deg(49), Deg(17), Metres(200)),
            Site(Deg(48), Deg(18), Metres(200)),
            Site(Deg(48), Deg(17), Metres(300)),
        ):
            compiler._night_events(ephemeris, different_site, start, end)
        compiler._night_events(ephemeris, site, start + timedelta(days=1), end + timedelta(days=1))
        compiler._night_events(Ephemeris(Path("kernel"), object(), object()), site, start, end)
        assert solar.call_count == lunar.call_count == 6
        # Browsing many nights must not grow the cache without a bound.
        for day in range(20):
            offset = timedelta(days=day)
            compiler._night_events(ephemeris, site, start + offset, end + offset)
        assert compiler._night_events.cache_info().currsize == 16
    finally:
        compiler._night_events.cache_clear()
