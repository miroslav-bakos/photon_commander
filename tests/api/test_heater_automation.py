# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from ssc.api.heater_automation import HeaterAutomationService
from ssc.api.tasks import TaskRegistry
from ssc.api.weather_service import WeatherService
from ssc.settings.models import HeaterAutomationSettings
from ssc.settings.store import SettingsStore
from ssc.state.models import LocationSource, LocationState
from ssc.weather.base import ForecastPoint, ForecastSeries


class _Settings:
    def __init__(self) -> None:
        self.heater = HeaterAutomationSettings(enabled=True, on_margin_c=5, off_margin_c=7)
        self.location = LocationState(
            latitude_deg=48.15,
            longitude_deg=17.11,
            source=LocationSource.MANUAL,
            acquired_at=datetime.now(tz=UTC),
        )

    def active_location(self) -> LocationState:
        return self.location


class _Weather:
    def __init__(self, series: ForecastSeries) -> None:
        self.series = series

    async def forecast(self, latitude_deg: float, longitude_deg: float) -> ForecastSeries:
        assert (latitude_deg, longitude_deg) == (48.15, 17.11)
        return self.series


class _Device:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.calls: list[bool] = []

    async def heater_get_state(self) -> tuple[bool, None]:
        return self.enabled, None

    async def heater_set_state(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.calls.append(enabled)


def _series(*, margin_c: float, stale: bool = False) -> ForecastSeries:
    now = datetime.now(tz=UTC)
    point = ForecastPoint(
        t=now,
        cloud_total=20,
        cloud_low=10,
        cloud_mid=5,
        cloud_high=5,
        precip_prob=0,
        humidity=90,
        dew_point_c=7,
        temp_c=7 + margin_c,
        wind_ms=1,
        visibility_m=10_000,
        seeing_arcsec=None,
        transparency=None,
        source="metno",
    )
    return ForecastSeries(points=(point,), fetched_at=now, stale_since=now if stale else None)


def _service(series: ForecastSeries, device: _Device) -> HeaterAutomationService:
    return HeaterAutomationService(
        settings=cast(SettingsStore, _Settings()),
        weather=cast(WeatherService, _Weather(series)),
        device=device,
        tasks=TaskRegistry(),
    )


@pytest.mark.asyncio
async def test_low_dew_point_margin_turns_heater_on() -> None:
    device = _Device(enabled=False)

    result = await _service(_series(margin_c=2), device).evaluate()

    assert result.dew_margin_c == 2
    assert result.action == "turned_on"
    assert device.calls == [True]


@pytest.mark.asyncio
async def test_high_dew_point_margin_turns_heater_off() -> None:
    device = _Device(enabled=True)

    result = await _service(_series(margin_c=9), device).evaluate()

    assert result.dew_margin_c == 9
    assert result.action == "turned_off"
    assert device.calls == [False]


@pytest.mark.asyncio
async def test_stale_weather_never_changes_heater() -> None:
    device = _Device(enabled=False)

    result = await _service(_series(margin_c=1, stale=True), device).evaluate()

    assert result.action is None
    assert result.note == "weather forecast is stale; heater state left unchanged"
    assert device.calls == []
