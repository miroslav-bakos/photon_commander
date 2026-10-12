# SPDX-License-Identifier: GPL-3.0-only
"""WeatherAPI.com global forecast adapter (user-supplied API key)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from ssc.weather.base import ForecastPoint, ForecastSeries
from ssc.weather.providers.keyed import KeyedForecastProvider

PROVIDER_NAME: Final = "weatherapi"
DEFAULT_SECRET_NAME: Final = "weatherapi_api_key"
DEFAULT_BASE_URL: Final = "https://api.weatherapi.com/v1/forecast.json"


@dataclass(frozen=True, slots=True)
class WeatherApiConfig:
    secret_name: str = DEFAULT_SECRET_NAME
    base_url: str = DEFAULT_BASE_URL
    request_timeout_s: float = 10.0
    cache_ttl_s: float = 1800.0
    failure_threshold: int = 3
    reset_timeout_s: float = 60.0


class WeatherApiProvider(KeyedForecastProvider):
    name = PROVIDER_NAME
    display_name = "WeatherAPI.com"

    def request_params(
        self, latitude_deg: float, longitude_deg: float, api_key: str
    ) -> dict[str, str]:
        return {
            "key": api_key,
            "q": f"{latitude_deg:.4f},{longitude_deg:.4f}",
            "days": "3",
            "aqi": "no",
            "alerts": "no",
        }

    def normalise(self, payload: dict[str, Any], *, fetched_at: datetime) -> ForecastSeries:
        points: list[ForecastPoint] = []
        for day in payload["forecast"]["forecastday"]:
            for hour in day["hour"]:
                points.append(
                    ForecastPoint(
                        t=datetime.fromtimestamp(hour["time_epoch"], tz=UTC),
                        cloud_total=_number(hour, "cloud"),
                        cloud_low=None,
                        cloud_mid=None,
                        cloud_high=None,
                        precip_prob=_number(hour, "chance_of_rain"),
                        humidity=_number(hour, "humidity"),
                        dew_point_c=_number(hour, "dewpoint_c"),
                        temp_c=_number(hour, "temp_c"),
                        wind_ms=_scaled(hour, "wind_kph", 1 / 3.6),
                        wind_direction_deg=_number(hour, "wind_degree"),
                        visibility_m=_scaled(hour, "vis_km", 1000),
                        seeing_arcsec=None,
                        transparency=None,
                        source=self.name,
                    )
                )
        return ForecastSeries(points=tuple(points), fetched_at=fetched_at)


def _number(row: dict[str, Any], name: str) -> float | None:
    value = row.get(name)
    return None if value is None else float(value)


def _scaled(row: dict[str, Any], name: str, scale: float) -> float | None:
    value = _number(row, name)
    return None if value is None else value * scale


__all__ = ["DEFAULT_BASE_URL", "DEFAULT_SECRET_NAME", "WeatherApiConfig", "WeatherApiProvider"]
