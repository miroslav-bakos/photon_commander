"""Worldwide BYO-key provider adapters and their shared safety behaviour."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from ssc.settings.secrets import BackendStatus
from ssc.weather.base import ProviderUnavailableError
from ssc.weather.cache import ForecastCache
from ssc.weather.providers.visualcrossing import VisualCrossingConfig, VisualCrossingProvider
from ssc.weather.providers.weatherapi import WeatherApiConfig, WeatherApiProvider

AVAILABLE = BackendStatus(available=True, backend="fake.Keyring")
NOW = datetime(2026, 9, 12, 20, tzinfo=UTC)


class FakeSecrets:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)


WEATHERAPI_PAYLOAD = {
    "forecast": {
        "forecastday": [
            {
                "hour": [
                    {
                        "time_epoch": 1789243200,
                        "temp_c": 12.5,
                        "dewpoint_c": 9.1,
                        "humidity": 80,
                        "chance_of_rain": 15,
                        "cloud": 42,
                        "wind_kph": 18,
                        "wind_degree": 270,
                        "vis_km": 9.5,
                    }
                ]
            }
        ]
    }
}
VISUAL_CROSSING_PAYLOAD = {
    "days": [
        {
            "hours": [
                {
                    "datetimeEpoch": 1789243200,
                    "temp": 12.5,
                    "dew": 9.1,
                    "humidity": 80,
                    "precipprob": 15,
                    "cloudcover": 42,
                    "windspeed": 18,
                    "winddir": 270,
                    "visibility": 9.5,
                }
            ]
        }
    ]
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_class", "config", "secret_name", "url", "payload", "source"),
    [
        (
            WeatherApiProvider,
            WeatherApiConfig(),
            "weatherapi_api_key",
            "https://api.weatherapi.com/v1/forecast.json",
            WEATHERAPI_PAYLOAD,
            "weatherapi",
        ),
        (
            VisualCrossingProvider,
            VisualCrossingConfig(),
            "visualcrossing_api_key",
            "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/51.4800,0.0000",
            VISUAL_CROSSING_PAYLOAD,
            "visualcrossing",
        ),
    ],
)
async def test_normalises_units_keeps_key_server_side_and_caches(
    provider_class: type,
    config: object,
    secret_name: str,
    url: str,
    payload: dict,
    source: str,
) -> None:
    cache = ForecastCache()
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            route = router.get(url).mock(return_value=httpx.Response(200, json=payload))
            provider = provider_class(
                config,
                cache=cache,
                secrets=FakeSecrets({secret_name: "top-secret"}),
                backend_status=AVAILABLE,
                http=http,
                now=lambda: NOW,
            )
            first = await provider.fetch(51.48, 0.0)
            second = await provider.fetch(51.48, 0.0)

    assert route.call_count == 1
    assert first == second
    point = first.points[0]
    assert point.source == source
    assert point.temp_c == 12.5
    assert point.dew_point_c == 9.1
    assert point.cloud_total == 42
    assert point.wind_ms == pytest.approx(5.0)
    assert point.visibility_m == 9500
    assert "top-secret" not in repr(cache)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "label"),
    [
        (
            WeatherApiProvider(
                WeatherApiConfig(),
                cache=ForecastCache(),
                secrets=FakeSecrets(),
                backend_status=AVAILABLE,
            ),
            "WeatherAPI.com",
        ),
        (
            VisualCrossingProvider(
                VisualCrossingConfig(),
                cache=ForecastCache(),
                secrets=FakeSecrets(),
                backend_status=AVAILABLE,
            ),
            "Visual Crossing",
        ),
    ],
)
async def test_missing_key_is_explicit_and_makes_no_request(provider: object, label: str) -> None:
    availability = await provider.availability()
    assert availability.available is False
    assert f"no {label} API key" in (availability.reason or "")
    with pytest.raises(ProviderUnavailableError, match=f"no {label} API key"):
        await provider.fetch(51.48, 0.0)
    await provider.aclose()
