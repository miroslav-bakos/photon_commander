# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Provider adapters. Exactly two, per `THIRD_PARTY_NOTICES.md`'s weather section:
Met.no (keyless default) plus user-keyed global providers.

Do not add a third. Open-Meteo, 7Timer! ASTRO, Meteoblue and Astrospheric are
excluded on commercial-licensing grounds — an adapter that exists is an
adapter someone enables.
"""

from __future__ import annotations

from ssc.weather.providers.metno import MetNoConfig, MetNoConfigError, MetNoProvider
from ssc.weather.providers.openweathermap import (
    OpenWeatherMapConfig,
    OpenWeatherMapProvider,
)
from ssc.weather.providers.visualcrossing import VisualCrossingConfig, VisualCrossingProvider
from ssc.weather.providers.weatherapi import WeatherApiConfig, WeatherApiProvider

__all__ = [
    "MetNoConfig",
    "MetNoConfigError",
    "MetNoProvider",
    "OpenWeatherMapConfig",
    "OpenWeatherMapProvider",
    "VisualCrossingConfig",
    "VisualCrossingProvider",
    "WeatherApiConfig",
    "WeatherApiProvider",
]
