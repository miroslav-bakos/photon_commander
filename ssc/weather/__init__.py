# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Weather forecast normalisation, provenance and offline-first caching.

See `docs/DESIGN.md` §6 and `THIRD_PARTY_NOTICES.md`'s weather section for the
shape and the (deliberately short) provider list this package ships.
"""

from __future__ import annotations

from ssc.weather.base import (
    ForecastPoint,
    ForecastSeries,
    Provider,
    ProviderAvailability,
    ProviderConfigError,
    ProviderError,
    ProviderUnavailableError,
)
from ssc.weather.cache import CacheEntry, CacheKey, ForecastCache, make_cache_key
from ssc.weather.merge import merge_series

__all__ = [
    "CacheEntry",
    "CacheKey",
    "ForecastCache",
    "ForecastPoint",
    "ForecastSeries",
    "Provider",
    "ProviderAvailability",
    "ProviderConfigError",
    "ProviderError",
    "ProviderUnavailableError",
    "make_cache_key",
    "merge_series",
]
