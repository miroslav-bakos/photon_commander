# SPDX-License-Identifier: GPL-3.0-only
"""Shared failure, secret, and cache discipline for keyed weather APIs."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import httpx

from ssc.settings.secrets import BackendStatus, SecretStore, probe_backend
from ssc.weather.base import (
    Clock,
    ForecastSeries,
    NowUTC,
    ProviderAvailability,
    ProviderUnavailableError,
)
from ssc.weather.cache import CacheEntry, ForecastCache, make_cache_key, now_utc, stale_copy
from ssc.weather.circuit import CircuitBreaker


class KeyedForecastProvider:
    """Base for server-side BYO-key providers.

    Subclasses supply only endpoint parameters and response normalisation. API
    keys never enter cache keys, response objects, logs, or exception text.
    """

    name: str
    display_name: str

    def __init__(
        self,
        config: Any,
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
        self._backend_status = backend_status

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def availability(self) -> ProviderAvailability:
        status = self._backend_status if self._backend_status is not None else await probe_backend()
        if not status.available:
            return ProviderAvailability(
                available=False, reason=f"secrets backend unavailable: {status.reason}"
            )
        if await self._secrets.get(self._config.secret_name) is None:
            return ProviderAvailability(
                available=False, reason=f"no {self.display_name} API key is configured"
            )
        return ProviderAvailability(available=True)

    def request_url(self, latitude_deg: float, longitude_deg: float) -> str:
        del latitude_deg, longitude_deg
        return str(self._config.base_url)

    def request_params(
        self, latitude_deg: float, longitude_deg: float, api_key: str
    ) -> dict[str, str]:
        raise NotImplementedError

    def normalise(self, payload: dict[str, Any], *, fetched_at: Any) -> ForecastSeries:
        raise NotImplementedError

    async def fetch(self, latitude_deg: float, longitude_deg: float) -> ForecastSeries:
        cache_key = make_cache_key(self.name, latitude_deg, longitude_deg)
        cached = self._cache.get(cache_key)
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
            raise ProviderUnavailableError(f"{self.name}: circuit open and no cached series")

        api_key = await self._secrets.get(self._config.secret_name)
        if api_key is None:
            if cached is not None:
                return stale_copy(cached)
            raise ProviderUnavailableError(f"{self.name}: API key was removed during fetch")
        try:
            response = await self._http.get(
                self.request_url(latitude_deg, longitude_deg),
                params=self.request_params(latitude_deg, longitude_deg, api_key),
            )
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            if cached is not None:
                return stale_copy(cached)
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
            raise ProviderUnavailableError(f"{self.name}: server returned {response.status_code}")

        self._breaker.record_success()
        series = self.normalise(response.json(), fetched_at=now)
        self._cache.put(
            cache_key,
            CacheEntry(
                series=series,
                fetched_at=now,
                expires_at=now + timedelta(seconds=self._config.cache_ttl_s),
            ),
        )
        return series
