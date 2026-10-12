# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Opt-in weather-driven dew-heater control with fail-safe unknown handling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from ssc.api.tasks import TaskRegistry
from ssc.api.weather_service import WeatherService, WeatherUnavailableError
from ssc.settings.store import SettingsStore

_log = logging.getLogger(__name__)
DEFAULT_EVALUATION_INTERVAL_S: Final = 300.0


class HeaterDevice(Protocol):
    async def heater_get_state(self) -> tuple[bool | None, str | None]: ...
    async def heater_set_state(self, *, enabled: bool) -> None: ...


@dataclass(frozen=True, slots=True)
class HeaterAutomationEvaluation:
    evaluated_at: datetime | None = None
    dew_margin_c: float | None = None
    action: str | None = None
    note: str | None = None


class HeaterAutomationService:
    def __init__(
        self,
        *,
        settings: SettingsStore,
        weather: WeatherService,
        device: HeaterDevice,
        tasks: TaskRegistry,
        interval_s: float = DEFAULT_EVALUATION_INTERVAL_S,
    ) -> None:
        self._settings = settings
        self._weather = weather
        self._device = device
        self._tasks = tasks
        self._interval_s = interval_s
        self._wake = asyncio.Event()
        self._evaluation = HeaterAutomationEvaluation()

    @property
    def evaluation(self) -> HeaterAutomationEvaluation:
        return self._evaluation

    def start(self) -> None:
        self._tasks.spawn(self._run(), name="dew-heater-automation")

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            if self._settings.heater.enabled:
                try:
                    await self.evaluate()
                except Exception as exc:
                    _log.warning("automatic dew-heater evaluation failed: %s", exc)
                    self._record(
                        datetime.now(tz=UTC),
                        note=f"{type(exc).__name__}: {exc}; heater state left unchanged",
                    )
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval_s)

    async def evaluate(self) -> HeaterAutomationEvaluation:
        now = datetime.now(tz=UTC)
        location = self._settings.active_location()
        if location is None:
            return self._record(now, note="no observing location is available")
        try:
            forecast = await self._weather.forecast(location.latitude_deg, location.longitude_deg)
        except WeatherUnavailableError as exc:
            return self._record(now, note=str(exc))
        if forecast.stale_since is not None:
            return self._record(now, note="weather forecast is stale; heater state left unchanged")
        if not forecast.points:
            return self._record(now, note="weather forecast has no points; heater state left unchanged")
        point = min(forecast.points, key=lambda row: abs((row.t - now).total_seconds()))
        if point.temp_c is None or point.dew_point_c is None:
            return self._record(
                now,
                note="current temperature or dew point is unknown; heater state left unchanged",
            )

        margin = point.temp_c - point.dew_point_c
        enabled, state_note = await self._device.heater_get_state()
        config = self._settings.heater
        if margin <= config.on_margin_c and enabled is not True:
            await self._device.heater_set_state(enabled=True)
            _log.info("automatic dew heater on at %.1f C dew-point margin", margin)
            return self._record(now, margin=margin, action="turned_on")
        if margin >= config.off_margin_c and enabled is True:
            await self._device.heater_set_state(enabled=False)
            _log.info("automatic dew heater off at %.1f C dew-point margin", margin)
            return self._record(now, margin=margin, action="turned_off")
        note = state_note if enabled is None else "heater already matches the automatic policy"
        return self._record(now, margin=margin, note=note)

    def _record(
        self,
        when: datetime,
        *,
        margin: float | None = None,
        action: str | None = None,
        note: str | None = None,
    ) -> HeaterAutomationEvaluation:
        self._evaluation = HeaterAutomationEvaluation(
            evaluated_at=when,
            dew_margin_c=margin,
            action=action,
            note=note,
        )
        return self._evaluation


__all__ = ["HeaterAutomationEvaluation", "HeaterAutomationService"]
