# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The weather forecast route (DESIGN.md §6): the raw, per-provider evidence
behind the header widget and (eventually) the Settings tab's provider status.

Thin: parse, delegate to ``ssc.api.weather_service.WeatherService``, serialise
the merged :class:`~ssc.weather.base.ForecastSeries`. No judgement about
whether tonight is any good happens here — that synthesis is
``ssc.api.conditions``, exposed by ``GET /api/tonight``
(``ssc/api/routes/tonight.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ssc.api.deps import ServicesDep, SessionDep, require_location
from ssc.api.schemas import ForecastPointModel, WeatherForecastResponse
from ssc.api.weather_service import WeatherUnavailableError
from ssc.weather.base import ForecastPoint

router = APIRouter(prefix="/api/weather", tags=["weather"])


def _point_model(point: ForecastPoint) -> ForecastPointModel:
    return ForecastPointModel(
        t=point.t,
        cloud_total=point.cloud_total,
        cloud_low=point.cloud_low,
        cloud_mid=point.cloud_mid,
        cloud_high=point.cloud_high,
        precip_prob=point.precip_prob,
        humidity=point.humidity,
        dew_point_c=point.dew_point_c,
        temp_c=point.temp_c,
        wind_ms=point.wind_ms,
        wind_direction_deg=point.wind_direction_deg,
        visibility_m=point.visibility_m,
        source=point.source,
    )


@router.get("/forecast", response_model=WeatherForecastResponse)
async def forecast(services: ServicesDep, session: SessionDep) -> WeatherForecastResponse:
    """The merged forecast for the active location: Met.no primary, any
    user-keyed provider (OpenWeatherMap) a ranked fallback, per-row ``source``
    provenance preserved (DESIGN.md §6)."""
    del session
    location = require_location(services)
    try:
        series = await services.weather.forecast(location.latitude_deg, location.longitude_deg)
    except WeatherUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return WeatherForecastResponse(
        location=location,
        fetched_at=series.fetched_at,
        stale_since=series.stale_since,
        points=[_point_model(point) for point in series.points],
    )


__all__ = ["router"]
