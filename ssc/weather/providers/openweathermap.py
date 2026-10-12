# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""OpenWeatherMap — the only keyed provider, disabled until the user supplies
their own key (DESIGN.md §6 / `THIRD_PARTY_NOTICES.md`).

Uses the free-tier ``/data/2.5/forecast`` (5 day / 3 hour) endpoint —
verified against OpenWeatherMap's own published schema for that endpoint (a
live call with a real key was not available in this environment; see the
report accompanying this change for what that means for the checked-in
fixture). It has no low/mid/high cloud split (that is Open-Meteo's shape,
and Open-Meteo is excluded on licensing grounds — see `THIRD_PARTY_NOTICES.md`),
no dew point, and no seeing/transparency; those fields are `None` on every
point this provider produces.

**Availability is gated on the secrets-backend capability, reused rather
than re-probed.** `ssc/settings/secrets.py`'s `BackendStatus`/`probe_backend`
is the single source of truth for "does this host even have a usable
keyring" (`docs/DEPLOYMENT.md`); this module asks for that status rather
than inventing its own keyring-availability detection, and reports the
*specific* reason (backend unavailable vs. no key entered) rather than
failing obscurely at first fetch.

**The key never appears in a response, a log line, or a cache key** — it is
used once, server-side, to build a request, and the cache key is
``(provider, rounded lat/lon)`` exactly like every other provider (see
`ssc/weather/cache.py`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx

from ssc.settings.secrets import BackendStatus, SecretStore, probe_backend
from ssc.weather.base import (
    Clock,
    ForecastPoint,
    ForecastSeries,
    NowUTC,
    ProviderAvailability,
    ProviderUnavailableError,
)
from ssc.weather.cache import CacheEntry, ForecastCache, make_cache_key, now_utc, stale_copy
from ssc.weather.circuit import CircuitBreaker

PROVIDER_NAME: Final = "openweathermap"

DEFAULT_BASE_URL: Final = "https://api.openweathermap.org/data/2.5/forecast"

#: The registry name in `ssc/settings/secrets.py`'s `SecretRegistry`.
DEFAULT_SECRET_NAME: Final = "openweathermap_api_key"

#: OpenWeatherMap's documented free-tier cap.
RATE_LIMIT_PER_MINUTE: Final = 60

DEFAULT_REQUEST_TIMEOUT_S: Final = 10.0

#: This endpoint does not document `ETag`/`If-Modified-Since` support, so
#: caching here is TTL-only. The underlying model updates roughly every 3
#: hours; 30 minutes keeps this app well clear of the rate limit while still
#: noticing a new run well within a night's observing window.
DEFAULT_CACHE_TTL_S: Final = 1800.0

DEFAULT_FAILURE_THRESHOLD: Final = 3
DEFAULT_RESET_TIMEOUT_S: Final = 60.0


@dataclass(frozen=True, slots=True)
class OpenWeatherMapConfig:
    secret_name: str = DEFAULT_SECRET_NAME
    base_url: str = DEFAULT_BASE_URL
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    cache_ttl_s: float = DEFAULT_CACHE_TTL_S
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    reset_timeout_s: float = DEFAULT_RESET_TIMEOUT_S


def _normalise(payload: dict[str, Any], *, fetched_at: datetime) -> ForecastSeries:
    points = []
    for item in payload["list"]:
        t = datetime.fromtimestamp(item["dt"], tz=UTC)
        main = item["main"]
        clouds = item.get("clouds", {})
        wind = item.get("wind", {})
        points.append(
            ForecastPoint(
                t=t,
                cloud_total=float(clouds["all"]) if "all" in clouds else None,
                # OpenWeatherMap does not split cloud cover by altitude.
                cloud_low=None,
                cloud_mid=None,
                cloud_high=None,
                precip_prob=float(item["pop"]) * 100.0 if "pop" in item else None,
                humidity=float(main["humidity"]) if "humidity" in main else None,
                # Not reported by this endpoint (only OWM's One Call product has it).
                dew_point_c=None,
                temp_c=float(main["temp"]) if "temp" in main else None,
                wind_ms=float(wind["speed"]) if "speed" in wind else None,
                wind_direction_deg=float(wind["deg"]) if "deg" in wind else None,
                visibility_m=float(item["visibility"]) if "visibility" in item else None,
                seeing_arcsec=None,
                transparency=None,
                source=PROVIDER_NAME,
            )
        )
    return ForecastSeries(points=tuple(points), fetched_at=fetched_at)


class OpenWeatherMapProvider:
    """Keyed; `availability()` is `False` until a key is configured and the
    secrets backend can actually serve one."""

    name = PROVIDER_NAME

    def __init__(
        self,
        config: OpenWeatherMapConfig,
        *,
        cache: ForecastCache,
        secrets: SecretStore,
        http: httpx.AsyncClient | None = None,
        clock: Clock = time.monotonic,
        now: NowUTC = now_utc,
        backend_status: BackendStatus | None = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._secrets = secrets
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=config.request_timeout_s)
        self._breaker = CircuitBreaker(
            failure_threshold=config.failure_threshold,
            reset_timeout_s=config.reset_timeout_s,
            clock=clock,
        )
        self._now = now
        # A caller that already probed the backend at startup (the normal
        # case — probing involves a bounded keyring round trip and should
        # not happen on every availability check) can inject that status
        # directly. When not given, this provider probes it itself.
        self._backend_status = backend_status

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def availability(self) -> ProviderAvailability:
        status = self._backend_status if self._backend_status is not None else await probe_backend()
        if not status.available:
            return ProviderAvailability(
                available=False,
                reason=f"secrets backend unavailable: {status.reason}",
            )
        key = await self._secrets.get(self._config.secret_name)
        if key is None:
            return ProviderAvailability(
                available=False,
                reason="no OpenWeatherMap API key is configured",
            )
        return ProviderAvailability(available=True)

    async def fetch(self, latitude_deg: float, longitude_deg: float) -> ForecastSeries:
        key = make_cache_key(self.name, latitude_deg, longitude_deg)
        cached = self._cache.get(key)
        now = self._now()

        if cached is not None and cached.expires_at is not None and now < cached.expires_at:
            return cached.series

        availability = await self.availability()
        if not availability.available:
            if cached is not None:
                return stale_copy(cached)
            raise ProviderUnavailableError(f"{self.name}: {availability.reason}")

        if not self._breaker.allow_request():
            if cached is not None:
                return stale_copy(cached)
            raise ProviderUnavailableError(
                f"{self.name}: circuit open and no cached series for this location"
            )

        api_key = await self._secrets.get(self._config.secret_name)
        if api_key is None:
            # Availability just confirmed a key exists; a concurrent
            # deletion between those two awaits is the only way to land
            # here. Treat it the same as "not configured" — never send a
            # request with no key.
            if cached is not None:
                return stale_copy(cached)
            raise ProviderUnavailableError(f"{self.name}: API key was removed during fetch")

        try:
            response = await self._http.get(
                self._config.base_url,
                params={
                    "lat": f"{latitude_deg:.4f}",
                    "lon": f"{longitude_deg:.4f}",
                    "appid": api_key,
                    "units": "metric",
                },
            )
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            if cached is not None:
                return stale_copy(cached)
            # The key must never reach a log line or an exception message.
            raise ProviderUnavailableError(f"{self.name}: request failed") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            self._breaker.force_open(
                for_s=float(retry_after) if retry_after else self._config.reset_timeout_s
            )
            if cached is not None:
                return stale_copy(cached)
            raise ProviderUnavailableError(f"{self.name}: rate limited (429)")

        if response.status_code >= 400:
            self._breaker.record_failure()
            if cached is not None:
                return stale_copy(cached)
            # Deliberately no response body in the message: OpenWeatherMap
            # echoes query parameters (including appid) into some error
            # payloads, and the key must never surface in an exception.
            raise ProviderUnavailableError(f"{self.name}: server returned {response.status_code}")

        self._breaker.record_success()
        series = _normalise(response.json(), fetched_at=now)
        entry = CacheEntry(
            series=series,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self._config.cache_ttl_s),
        )
        self._cache.put(key, entry)
        return series


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CACHE_TTL_S",
    "DEFAULT_SECRET_NAME",
    "RATE_LIMIT_PER_MINUTE",
    "OpenWeatherMapConfig",
    "OpenWeatherMapProvider",
]
