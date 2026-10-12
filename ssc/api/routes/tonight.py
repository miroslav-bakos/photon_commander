# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``GET /api/tonight`` (DESIGN.md §6 / this task's brief): the header
widget's one round trip — tonight's astronomical dark window, the Moon across
it, the forecast rows that fall inside it, and SSC's own capture-conditions
synthesis, all from a single poll.

A compact composite rather than three separate calls because the widget polls
it, wants one round trip, and the whole thing can be cached (and rate-limited,
should that ever matter) as a unit. Thin: parse, delegate to
``ssc.api.tonight_service.build_tonight``, serialise. The actual composition —
picking tonight's dark window, weighing the Moon by how long it is up, scoring
the forecast — lives in ``ssc/api/tonight_service.py`` and ``ssc/api/conditions.py``,
both unit-tested without a running app.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from fastapi import APIRouter, HTTPException, status

from ssc.api.astro_service import AstroUnavailableError
from ssc.api.deps import ServicesDep, SessionDep, require_location
from ssc.api.plans_service import PlanTimezoneUnknownError, resolve_timezone
from ssc.api.schemas import (
    CaptureConditionsModel,
    EventTimeModel,
    MoonTonightModel,
    TonightCloudModel,
    TonightResponse,
    TonightWeatherModel,
    TonightWeatherRowModel,
)
from ssc.api.tonight_service import TonightResult, build_tonight
from ssc.astro.events import EventTime, Site
from ssc.astro.units import Deg, Metres
from ssc.state.models import LocationState
from ssc.weather.base import ForecastPoint

router = APIRouter(prefix="/api", tags=["tonight"])


def _event_model(event: EventTime) -> EventTimeModel:
    return EventTimeModel(
        event=str(event.event),
        when=event.when,
        outcome=event.outcome,
        threshold_deg=float(event.threshold_deg),
    )


def _row_model(point: ForecastPoint) -> TonightWeatherRowModel:
    return TonightWeatherRowModel(
        t=point.t,
        cloud=TonightCloudModel(
            total=point.cloud_total,
            low=point.cloud_low,
            mid=point.cloud_mid,
            high=point.cloud_high,
        ),
        precip_prob=point.precip_prob,
        humidity=point.humidity,
        dew_point_c=point.dew_point_c,
        temp_c=point.temp_c,
        wind_ms=point.wind_ms,
        wind_direction_deg=point.wind_direction_deg,
        source=point.source,
    )


def _response(
    location: LocationState, generated_at: datetime, result: TonightResult
) -> TonightResponse:
    weather_model: TonightWeatherModel | None = None
    if result.weather is not None:
        window_start = result.dark_window.start
        window_end = result.dark_window.end
        rows = [
            point
            for point in result.weather.points
            if (window_start is None or point.t >= window_start)
            and (window_end is None or point.t <= window_end)
        ]
        weather_model = TonightWeatherModel(
            fetched_at=result.weather.fetched_at,
            stale_since=result.weather.stale_since,
            rows=[_row_model(point) for point in rows],
        )

    conditions_model: CaptureConditionsModel | None = None
    if result.conditions is not None:
        conditions_model = CaptureConditionsModel(
            category=result.conditions.category,
            score=result.conditions.score,
            reasons=list(result.conditions.reasons),
            factors=[
                {
                    "name": factor.name,
                    "measurement": factor.measurement,
                    "points_deducted": factor.points_deducted,
                }
                for factor in result.conditions.factors
            ],
        )

    return TonightResponse(
        location=location,
        generated_at=generated_at,
        dark_window_start=result.dark_window.start,
        dark_window_end=result.dark_window.end,
        dark_window_note=result.dark_window.note,
        moon=MoonTonightModel(
            illuminated_fraction=result.moon.illuminated_fraction,
            phase_angle_deg=float(result.moon.phase_angle_deg),
            waxing=result.moon.waxing,
            moonrise=_event_model(result.moonrise),
            moonset=_event_model(result.moonset),
        ),
        weather=weather_model,
        weather_unavailable_reason=result.weather_unavailable_reason,
        conditions=conditions_model,
        conditions_unavailable_reason=result.conditions_unavailable_reason,
    )


@router.get("/tonight", response_model=TonightResponse)
async def tonight(
    services: ServicesDep, session: SessionDep, night_date: date | None = None
) -> TonightResponse:
    del session
    location = require_location(services)
    site = Site(
        latitude_deg=Deg(location.latitude_deg),
        longitude_deg=Deg(location.longitude_deg),
        elevation_m=Metres(location.elevation_m or 0.0),
    )
    now = datetime.now(tz=UTC)
    anchor = now
    if night_date is not None:
        try:
            timezone = resolve_timezone(location)
        except PlanTimezoneUnknownError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        anchor = datetime.combine(night_date, time(hour=12), tzinfo=timezone).astimezone(UTC)
    try:
        result = await build_tonight(
            astro=services.astro, weather=services.weather, site=site, now=anchor
        )
    except AstroUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return _response(location, now, result)


__all__ = ["router"]
