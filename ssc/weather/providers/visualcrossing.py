# SPDX-License-Identifier: GPL-3.0-only
"""Visual Crossing global Timeline forecast adapter (user-supplied key)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote

from ssc.weather.base import ForecastPoint, ForecastSeries
from ssc.weather.providers.keyed import KeyedForecastProvider
from ssc.weather.providers.weatherapi import _number, _scaled

PROVIDER_NAME: Final = "visualcrossing"
DEFAULT_SECRET_NAME: Final = "visualcrossing_api_key"
DEFAULT_BASE_URL: Final = (
    "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
)


@dataclass(frozen=True, slots=True)
class VisualCrossingConfig:
    secret_name: str = DEFAULT_SECRET_NAME
    base_url: str = DEFAULT_BASE_URL
    request_timeout_s: float = 10.0
    cache_ttl_s: float = 1800.0
    failure_threshold: int = 3
    reset_timeout_s: float = 60.0


class VisualCrossingProvider(KeyedForecastProvider):
    name = PROVIDER_NAME
    display_name = "Visual Crossing"

    def request_url(self, latitude_deg: float, longitude_deg: float) -> str:
        location = quote(f"{latitude_deg:.4f},{longitude_deg:.4f}", safe=",")
        return f"{self._config.base_url}/{location}"

    def request_params(
        self, latitude_deg: float, longitude_deg: float, api_key: str
    ) -> dict[str, str]:
        del latitude_deg, longitude_deg
        return {
            "key": api_key,
            "unitGroup": "metric",
            "include": "hours",
            "contentType": "json",
            "elements": "datetimeEpoch,temp,dew,humidity,precipprob,cloudcover,windspeed,winddir,visibility",
        }

    def normalise(self, payload: dict[str, Any], *, fetched_at: datetime) -> ForecastSeries:
        points: list[ForecastPoint] = []
        for day in payload["days"]:
            for hour in day["hours"]:
                points.append(
                    ForecastPoint(
                        t=datetime.fromtimestamp(hour["datetimeEpoch"], tz=UTC),
                        cloud_total=_number(hour, "cloudcover"),
                        cloud_low=None,
                        cloud_mid=None,
                        cloud_high=None,
                        precip_prob=_number(hour, "precipprob"),
                        humidity=_number(hour, "humidity"),
                        dew_point_c=_number(hour, "dew"),
                        temp_c=_number(hour, "temp"),
                        wind_ms=_scaled(hour, "windspeed", 1 / 3.6),
                        wind_direction_deg=_number(hour, "winddir"),
                        visibility_m=_scaled(hour, "visibility", 1000),
                        seeing_arcsec=None,
                        transparency=None,
                        source=self.name,
                    )
                )
        return ForecastSeries(points=tuple(points), fetched_at=fetched_at)


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_SECRET_NAME",
    "VisualCrossingConfig",
    "VisualCrossingProvider",
]
