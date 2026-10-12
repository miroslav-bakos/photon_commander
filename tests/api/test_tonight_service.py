"""Tests for ``ssc.api.tonight_service``: the dark-window picker (pure,
synthetic ``EventTime`` lists — no ephemeris needed) and ``build_tonight``'s
composition (real ``AstroService``, a fake ``WeatherService``).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from ssc.api.astro_service import build_astro_service
from ssc.api.tonight_service import DarkWindow, build_tonight, pick_dark_window
from ssc.api.weather_service import WeatherUnavailableError
from ssc.astro.events import (
    EphemerisMissingError,
    EventTime,
    Outcome,
    Site,
    SolarEvent,
    load_ephemeris,
)
from ssc.astro.units import Deg, Metres
from ssc.weather.base import ForecastPoint, ForecastSeries


def _ephemeris_available() -> bool:
    try:
        with load_ephemeris():
            return True
    except EphemerisMissingError:
        return False


requires_ephemeris = pytest.mark.skipif(
    not _ephemeris_available() and os.environ.get("SSC_REQUIRE_EPHEMERIS") != "1",
    reason="ephemeris kernel not present; see ssc.astro.events.load_ephemeris",
)

BUCHAREST = Site(latitude_deg=Deg(44.4268), longitude_deg=Deg(26.1025), elevation_m=Metres(85.0))
_NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
_SEARCH_END = _NOW + timedelta(hours=30)


def _occurs(event: str, when: datetime) -> EventTime:
    return EventTime(event=event, when=when, outcome=Outcome.OCCURS, threshold_deg=Deg(-18.0))


def _absent(event: str, outcome: Outcome) -> EventTime:
    return EventTime(event=event, when=None, outcome=outcome, threshold_deg=Deg(-18.0))


# --- pick_dark_window: pure, synthetic events -------------------------------- #


def test_not_yet_dark_starts_at_the_next_dusk() -> None:
    dusk = _NOW + timedelta(hours=5)
    dawn = _NOW + timedelta(hours=13)
    events = [
        _occurs(SolarEvent.ASTRONOMICAL_DUSK, dusk),
        _occurs(SolarEvent.ASTRONOMICAL_DAWN, dawn),
    ]
    window = pick_dark_window(events, now=_NOW, search_end=_SEARCH_END)
    assert window.start == dusk
    assert window.end == dawn
    assert window.note is None


def test_already_dark_starts_at_now_not_at_the_elapsed_dusk() -> None:
    """A live widget cares about darkness remaining, not what already elapsed."""
    dusk = _NOW - timedelta(hours=2)  # already happened
    dawn = _NOW + timedelta(hours=6)
    events = [
        _occurs(SolarEvent.ASTRONOMICAL_DUSK, dusk),
        _occurs(SolarEvent.ASTRONOMICAL_DAWN, dawn),
    ]
    window = pick_dark_window(events, now=_NOW, search_end=_SEARCH_END)
    assert window.start == _NOW
    assert window.end == dawn


def test_no_astronomical_darkness_in_the_span_is_surfaced_not_guessed() -> None:
    events = [
        _absent(SolarEvent.ASTRONOMICAL_DUSK, Outcome.ALWAYS_ABOVE),
        _absent(SolarEvent.ASTRONOMICAL_DAWN, Outcome.ALWAYS_ABOVE),
    ]
    window = pick_dark_window(events, now=_NOW, search_end=_SEARCH_END)
    assert window.start is None
    assert window.end is None
    assert window.note is not None


def test_polar_night_is_dark_for_the_whole_searched_span() -> None:
    events = [
        _absent(SolarEvent.ASTRONOMICAL_DUSK, Outcome.ALWAYS_BELOW),
        _absent(SolarEvent.ASTRONOMICAL_DAWN, Outcome.ALWAYS_BELOW),
    ]
    window = pick_dark_window(events, now=_NOW, search_end=_SEARCH_END)
    assert window.start == _NOW
    assert window.end == _SEARCH_END
    assert window.note is None


def test_dawn_outside_the_search_span_is_surfaced() -> None:
    dusk = _NOW + timedelta(hours=5)
    events = [_occurs(SolarEvent.ASTRONOMICAL_DUSK, dusk)]
    window = pick_dark_window(events, now=_NOW, search_end=_SEARCH_END)
    assert window.start == dusk
    assert window.end is None
    assert window.note is not None


def test_dark_window_is_a_frozen_dataclass() -> None:
    window = DarkWindow(start=_NOW, end=_NOW + timedelta(hours=1))
    with pytest.raises(AttributeError):
        window.start = _NOW  # type: ignore[misc]


# --- build_tonight: real AstroService, fake WeatherService -------------------- #


class _FakeWeatherService:
    def __init__(self, series: ForecastSeries | None = None, error: str | None = None) -> None:
        self._series = series
        self._error = error

    async def forecast(self, latitude_deg: float, longitude_deg: float) -> ForecastSeries:
        del latitude_deg, longitude_deg
        if self._error is not None:
            raise WeatherUnavailableError(self._error)
        assert self._series is not None
        return self._series


def _row(t: datetime, **overrides: object) -> ForecastPoint:
    defaults: dict[str, object] = {
        "t": t,
        "cloud_total": 10.0,
        "cloud_low": 0.0,
        "cloud_mid": 0.0,
        "cloud_high": 10.0,
        "precip_prob": 0.0,
        "humidity": 50.0,
        "dew_point_c": 5.0,
        "temp_c": 12.0,
        "wind_ms": 2.0,
        "visibility_m": None,
        "seeing_arcsec": None,
        "transparency": None,
        "source": "metno",
    }
    defaults.update(overrides)
    return ForecastPoint(**defaults)  # type: ignore[arg-type]


@requires_ephemeris
@pytest.mark.asyncio
async def test_build_tonight_with_weather_produces_conditions() -> None:
    astro = await build_astro_service()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    # Rows spanning a wide window so at least some fall inside whatever dark
    # window tonight resolves to, wherever "now" happens to fall.
    rows = tuple(_row(now + timedelta(hours=h)) for h in range(0, 30))
    weather = _FakeWeatherService(series=ForecastSeries(points=rows, fetched_at=now))
    try:
        result = await build_tonight(astro=astro, weather=weather, site=BUCHAREST, now=now)
    finally:
        astro.close()

    assert result.weather_unavailable_reason is None
    assert result.weather is not None
    # moonrise/moonset always resolve to a real EventTime (occurs or an
    # explained absence), never left unset.
    assert result.moonrise.event == "moonrise"
    assert result.moonset.event == "moonset"
    # Bucharest always has a dark window, and conditions follow from it. The
    # earlier form of this assertion branched on whether the window resolved
    # and demanded an unavailable-reason otherwise — a false dichotomy that
    # failed whenever `now` fell after dusk (a partial window still yields
    # perfectly good conditions), and hid the lookback bug below for a day.
    assert result.dark_window.start is not None
    assert result.conditions is not None
    assert result.conditions_unavailable_reason is None
    assert result.conditions.reasons


@requires_ephemeris
@pytest.mark.asyncio
async def test_after_dusk_reports_tonight_not_tomorrow_evening() -> None:
    """The bug this exists for: solar events were solved starting at ``now``,
    so a dusk that had already happened was never in the list. That made
    ``pick_dark_window``'s "already dark" branch unreachable, and after
    nightfall the widget reported *tomorrow* evening's window — at 22:20 local
    the user was told tonight's darkness had not started yet.

    Deterministic on purpose. The composition test above uses the wall clock
    and therefore only reproduced this for part of the day.
    """
    astro = await build_astro_service()
    # 22:20 local in Bucharest (UTC+3 in August) — comfortably after
    # astronomical dusk, and the exact situation someone is in when observing.
    now = datetime(2026, 8, 28, 19, 20, tzinfo=UTC)
    rows = tuple(_row(now + timedelta(hours=h)) for h in range(0, 30))
    weather = _FakeWeatherService(series=ForecastSeries(points=rows, fetched_at=now))
    try:
        result = await build_tonight(astro=astro, weather=weather, site=BUCHAREST, now=now)
    finally:
        astro.close()

    assert result.dark_window.start is not None
    # Already dark, so the window starts now — not at a dusk hours away.
    assert result.dark_window.start == now
    assert result.dark_window.end is not None
    # And it ends at tonight's dawn, within hours, not a day and a half later.
    assert result.dark_window.end - now < timedelta(hours=12)
    assert result.dark_window.note is None


@requires_ephemeris
@pytest.mark.asyncio
async def test_build_tonight_degrades_gracefully_when_weather_is_unavailable() -> None:
    astro = await build_astro_service()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    weather = _FakeWeatherService(error="no weather provider is configured")
    try:
        result = await build_tonight(astro=astro, weather=weather, site=BUCHAREST, now=now)
    finally:
        astro.close()

    assert result.weather is None
    assert result.weather_unavailable_reason == "no weather provider is configured"
    assert result.conditions is None
    assert result.conditions_unavailable_reason == "no weather provider is configured"
    # The Moon and dark window are astro-only and must still be answered.
    assert result.moon.illuminated_fraction is not None
    assert result.moonrise.event == "moonrise"
