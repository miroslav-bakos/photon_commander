# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from __future__ import annotations

from datetime import UTC, datetime

from ssc.api.weather_service import WeatherService
from ssc.weather.base import ForecastPoint, ForecastSeries, ProviderAvailability


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def availability(self) -> ProviderAvailability:
        return ProviderAvailability(available=True)

    async def fetch(self, latitude_deg: float, longitude_deg: float) -> ForecastSeries:
        del latitude_deg, longitude_deg
        self.calls += 1
        now = datetime(2026, 9, 12, tzinfo=UTC)
        return ForecastSeries(
            points=(
                ForecastPoint(
                    t=now,
                    cloud_total=10,
                    cloud_low=None,
                    cloud_mid=None,
                    cloud_high=None,
                    precip_prob=0,
                    humidity=50,
                    dew_point_c=5,
                    temp_c=10,
                    wind_ms=1,
                    visibility_m=10_000,
                    seeing_arcsec=None,
                    transparency=None,
                    source=self.name,
                ),
            ),
            fetched_at=now,
        )


async def test_forecast_uses_only_the_selected_provider() -> None:
    metno = _Provider("metno")
    openweathermap = _Provider("openweathermap")
    service = WeatherService(
        providers=[metno, openweathermap],
        selected_provider="openweathermap",
    )

    result = await service.forecast(48.1, 17.1)

    assert result.points[0].source == "openweathermap"
    assert metno.calls == 0
    assert openweathermap.calls == 1

    service.set_provider("metno")
    result = await service.forecast(48.1, 17.1)
    assert result.points[0].source == "metno"
    assert metno.calls == 1
    assert openweathermap.calls == 1
