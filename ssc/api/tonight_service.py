# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Orchestration for ``GET /api/tonight`` (the header widget): the
astronomical dark window remaining tonight, the Moon across it, the merged
forecast rows that fall inside it, and SSC's own capture-conditions synthesis
(``ssc.api.conditions``) built from both.

Kept out of ``ssc/api/routes/tonight.py`` for the same reason
``ssc.api.astro_service`` sits below ``ssc/api/routes/astro.py``: the route
parses, delegates, serialises; the actual composition lives here, where it
can be unit tested (``tests/api/test_tonight_service.py``) without a running
app or a network call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from ssc.api.astro_service import AstroService
from ssc.api.conditions import CaptureConditions, MoonContribution, assess_capture_conditions
from ssc.api.weather_service import WeatherService, WeatherUnavailableError
from ssc.astro.events import EventTime, LunarEvent, MoonPhase, Outcome, Site, SolarEvent
from ssc.weather.base import ForecastSeries

#: How far ahead to search for tonight's astronomical dusk/dawn boundary.
#: Generous enough to comfortably find both at any latitude short of the
#: polar circles, where "tonight" stops having its usual meaning — see
#: :func:`pick_dark_window` for what happens there instead of a guess.
DARK_WINDOW_SEARCH_HOURS: Final = 30.0

#: How far *back* the solar solve starts. `pick_dark_window`'s "it is already
#: dark right now" branch keys off the most recent dusk at or before `now` — so
#: if the solve begins at `now`, that dusk is never in the list, the branch can
#: never fire, and after nightfall the widget reports *tomorrow* evening's
#: window instead of the one the user is standing in. Twelve hours is enough to
#: catch the preceding dusk at any non-polar latitude.
DARK_WINDOW_LOOKBACK_HOURS: Final = 12.0

#: How many instants to sample the Moon's altitude across the dark window.
#: Coarse enough to be cheap, fine enough that a rise/set inside the window
#: is not missed by aliasing: the Moon's altitude changes at well under
#: 0.5 deg/min even near the horizon, so this comfortably resolves a typical
#: 6-10h dark window.
MOON_ALTITUDE_SAMPLES: Final = 30

#: Used only when the dark window's end could not be determined (see
#: :func:`pick_dark_window`) but forecast rows still need *some* bound to
#: slice against, so the widget shows something rather than nothing.
_FALLBACK_WINDOW_HOURS: Final = 8.0


@dataclass(frozen=True, slots=True)
class DarkWindow:
    """The astronomical-dark portion of "tonight", from ``now`` forward.

    ``start``/``end`` are ``None`` together when the solved solar events show
    no astronomical darkness at all in the searched span — surfaced as
    ``note``, never guessed at (CLAUDE.md invariant 7). ``end`` alone can be
    ``None`` when darkness has already begun but the matching dawn was not
    found inside the search span.
    """

    start: datetime | None
    end: datetime | None
    note: str | None = None


def pick_dark_window(
    solar_events: Sequence[EventTime], *, now: datetime, search_end: datetime
) -> DarkWindow:
    """Pick tonight's dark window from a solved list of solar events.

    A live header widget cares about the darkness *remaining*, not what
    already elapsed: if it is already dark right now (the most recent
    dusk/dawn boundary before ``now`` was a dusk), the window starts at
    ``now`` rather than at today's already-past dusk. Otherwise it starts at
    the next astronomical dusk. It always ends at the next astronomical dawn
    after that start.
    """
    dusks = sorted(
        e.when
        for e in solar_events
        if e.event == SolarEvent.ASTRONOMICAL_DUSK and e.when is not None
    )
    dawns = sorted(
        e.when
        for e in solar_events
        if e.event == SolarEvent.ASTRONOMICAL_DAWN and e.when is not None
    )

    if not dusks and not dawns:
        dusk_outcome = next(
            (e.outcome for e in solar_events if e.event == SolarEvent.ASTRONOMICAL_DUSK),
            None,
        )
        if dusk_outcome is Outcome.ALWAYS_BELOW:
            # The Sun never rose above the astronomical threshold in the
            # searched span at all: dark the whole time (polar night).
            return DarkWindow(start=now, end=search_end)
        return DarkWindow(
            start=None,
            end=None,
            note=(
                f"no astronomical darkness in the next {DARK_WINDOW_SEARCH_HOURS:.0f} "
                "hours (continuous daylight or twilight)"
            ),
        )

    last_dusk = max((d for d in dusks if d <= now), default=None)
    last_dawn = max((d for d in dawns if d <= now), default=None)
    already_dark = last_dusk is not None and (last_dawn is None or last_dusk > last_dawn)

    if already_dark:
        start = now
    else:
        next_dusk = min((d for d in dusks if d > now), default=None)
        if next_dusk is None:
            return DarkWindow(
                start=None, end=None, note="no further astronomical dusk in the searched span"
            )
        start = next_dusk

    end = min((d for d in dawns if d > start), default=None)
    if end is None:
        return DarkWindow(
            start=start,
            end=None,
            note="darkness begins but the matching dawn falls outside the searched span",
        )
    return DarkWindow(start=start, end=end)


def _first_event(events: Sequence[EventTime], kind: LunarEvent) -> EventTime:
    for event in events:
        if event.event == kind:
            return event
    # lunar_events() always returns at least one entry per kind (an explained
    # absence when it does not occur) — reaching here is a wiring bug.
    raise RuntimeError(f"lunar_events returned no {kind!r} entry at all")


def _moon_contribution(
    astro: AstroService, site: Site, dark_window: DarkWindow, illuminated_fraction: float
) -> MoonContribution:
    if dark_window.start is None or dark_window.end is None:
        # No known dark window to sample: the Moon cannot be scored against
        # it, so it contributes nothing — "unknown span" and "never up" both
        # mean the same thing for this input (never a guessed altitude).
        return MoonContribution(
            illuminated_fraction=illuminated_fraction,
            up_fraction=0.0,
            mean_altitude_above_horizon_deg=None,
        )
    series = astro.moon_altitude_series(
        site, dark_window.start, dark_window.end, MOON_ALTITUDE_SAMPLES
    )
    altitudes = [float(alt) for _, alt in series]
    up = [a for a in altitudes if a > 0.0]
    up_fraction = len(up) / len(altitudes) if altitudes else 0.0
    mean_altitude = sum(up) / len(up) if up else None
    return MoonContribution(
        illuminated_fraction=illuminated_fraction,
        up_fraction=up_fraction,
        mean_altitude_above_horizon_deg=mean_altitude,
    )


@dataclass(frozen=True, slots=True)
class TonightResult:
    """The un-serialised answer to ``GET /api/tonight``;
    ``ssc/api/routes/tonight.py`` turns this into ``TonightResponse``."""

    dark_window: DarkWindow
    moon: MoonPhase
    moonrise: EventTime
    moonset: EventTime
    weather: ForecastSeries | None
    #: Set whenever ``weather`` is ``None`` — never a silent gap.
    weather_unavailable_reason: str | None
    conditions: CaptureConditions | None
    #: Set whenever ``conditions`` is ``None`` — either weather was
    #: unavailable, or no forecast row fell inside tonight's window.
    conditions_unavailable_reason: str | None


def _solve_tonight(
    astro: AstroService, site: Site, now: datetime
) -> tuple[DarkWindow, MoonPhase, EventTime, EventTime, MoonContribution]:
    search_end = now + timedelta(hours=DARK_WINDOW_SEARCH_HOURS)
    # Solar events start BEFORE `now` so an already-begun night is visible to
    # `pick_dark_window`; lunar events still start at `now`, because a moonrise
    # that already happened is not the next one a user is waiting for.
    solar = astro.solar_events(site, now - timedelta(hours=DARK_WINDOW_LOOKBACK_HOURS), search_end)
    lunar = astro.lunar_events(site, now, search_end)
    dark_window = pick_dark_window(solar, now=now, search_end=search_end)

    phase = astro.moon_phase(site, dark_window.start or now)
    moonrise = _first_event(lunar, LunarEvent.MOONRISE)
    moonset = _first_event(lunar, LunarEvent.MOONSET)
    moon_contribution = _moon_contribution(astro, site, dark_window, phase.illuminated_fraction)

    return dark_window, phase, moonrise, moonset, moon_contribution


async def build_tonight(
    *, astro: AstroService, weather: WeatherService, site: Site, now: datetime
) -> TonightResult:
    dark_window, phase, moonrise, moonset, moon_contribution = await astro.compute(
        _solve_tonight, astro, site, now
    )

    weather_series: ForecastSeries | None = None
    weather_reason: str | None = None
    try:
        weather_series = await weather.forecast(float(site.latitude_deg), float(site.longitude_deg))
    except WeatherUnavailableError as exc:
        weather_reason = str(exc)

    conditions: CaptureConditions | None = None
    conditions_reason: str | None = None
    if weather_series is None:
        conditions_reason = weather_reason or "no weather forecast is available"
    else:
        window_start = dark_window.start or now
        window_end = dark_window.end or (window_start + timedelta(hours=_FALLBACK_WINDOW_HOURS))
        rows = [
            point
            for point in weather_series.points
            if window_start <= point.t <= window_end and point.cloud_total is not None
        ]
        if not rows:
            conditions_reason = "no forecast rows with cloud cover fall inside tonight's window"
        else:
            conditions = assess_capture_conditions(rows, moon_contribution)

    return TonightResult(
        dark_window=dark_window,
        moon=phase,
        moonrise=moonrise,
        moonset=moonset,
        weather=weather_series,
        weather_unavailable_reason=weather_reason,
        conditions=conditions,
        conditions_unavailable_reason=conditions_reason,
    )


__all__ = [
    "DARK_WINDOW_LOOKBACK_HOURS",
    "DARK_WINDOW_SEARCH_HOURS",
    "MOON_ALTITUDE_SAMPLES",
    "DarkWindow",
    "TonightResult",
    "build_tonight",
    "pick_dark_window",
]
