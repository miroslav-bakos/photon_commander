# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Met.no Locationforecast 2.0 — the default, keyless provider.

Verified live against ``api.met.no/weatherapi/locationforecast/2.0/complete``
on 2026-08-28 (see `tests/weather/fixtures/metno_complete_51.48_0.0.json`,
captured with a real identifying User-Agent). The ``compact`` variant of this
API omits the low/mid/high cloud split and dew point entirely; ``complete``
is used specifically because DESIGN.md names split cloud cover as the field
that matters most for astronomy.

Terms actually observed, not assumed (`THIRD_PARTY_NOTICES.md`):

* **User-Agent is mandatory and must identify the app plus a real contact.**
  A generic UA is a ToS violation, not a style nitpick — this module refuses
  to construct a client without one (:class:`MetNoConfigError`).
* **20 requests/second** cap (not a bottleneck in practice — this app polls
  far slower — but documented for completeness).
* **No ETag.** The live response carries `Last-Modified` and `Expires`, not
  an `ETag`; conditional requests here use `If-Modified-Since`, confirmed
  live to return `304 Not Modified` with an empty body.
* **`Expires` drives the cache TTL.** Observed ~30 minutes ahead of `Date` on
  a live response; :data:`FALLBACK_TTL_S` is used only if a response is ever
  missing the header, which has not been observed but must not crash if it
  happens.
* **429 / Retry-After** must be honoured: this provider force-opens its
  circuit breaker for the advertised duration rather than retrying sooner.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final

import httpx

from ssc.weather.base import (
    Clock,
    ForecastPoint,
    ForecastSeries,
    NowUTC,
    ProviderAvailability,
    ProviderConfigError,
    ProviderUnavailableError,
)
from ssc.weather.cache import CacheEntry, ForecastCache, make_cache_key, now_utc, stale_copy
from ssc.weather.circuit import CircuitBreaker

PROVIDER_NAME: Final = "metno"

DEFAULT_BASE_URL: Final = "https://api.met.no/weatherapi/locationforecast/2.0/complete"

#: DESIGN.md / COMPLIANCE.md: 20 req/s. Not enforced client-side as a token
#: bucket — this app's polling cadence never approaches it — but recorded as
#: the documented obligation.
RATE_LIMIT_PER_S: Final = 20

DEFAULT_REQUEST_TIMEOUT_S: Final = 10.0

#: Used only when a response omits `Expires` (not observed live, but the code
#: must not assume the header is always present).
FALLBACK_TTL_S: Final = 1800.0

DEFAULT_FAILURE_THRESHOLD: Final = 3
DEFAULT_RESET_TIMEOUT_S: Final = 60.0


class MetNoConfigError(ProviderConfigError):
    """Raised when no real contact has been supplied for the User-Agent."""


@dataclass(frozen=True, slots=True)
class MetNoConfig:
    """``contact`` is the deploying operator's own identifying detail (an
    email address or a URL), read from settings — never a literal string
    baked into this module. See module docstring: sending a generic
    User-Agent is a ToS violation, so :func:`build_user_agent` refuses an
    empty one rather than falling back to something generic."""

    contact: str
    app_name: str = "ssc-core"
    base_url: str = DEFAULT_BASE_URL
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    reset_timeout_s: float = DEFAULT_RESET_TIMEOUT_S
    fallback_ttl_s: float = FALLBACK_TTL_S


def build_user_agent(*, app_name: str, contact: str) -> str:
    contact = contact.strip()
    if not contact:
        raise MetNoConfigError(
            "Met.no requires an identifying User-Agent with a real contact "
            "(an email address or a URL) — refusing to send a generic one, "
            "which is a ToS violation, not a missing nicety. Set a contact "
            "for the Met.no provider in settings before enabling it."
        )
    return f"{app_name} ({contact})"


def _parse_http_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _get(details: dict[str, Any], field_name: str) -> float | None:
    value = details.get(field_name)
    return float(value) if value is not None else None


def _normalise(payload: dict[str, Any], *, fetched_at: datetime) -> ForecastSeries:
    points = []
    for entry in payload["properties"]["timeseries"]:
        t = datetime.fromisoformat(entry["time"])
        details = entry["data"]["instant"]["details"]
        points.append(
            ForecastPoint(
                t=t,
                cloud_total=_get(details, "cloud_area_fraction"),
                cloud_low=_get(details, "cloud_area_fraction_low"),
                cloud_mid=_get(details, "cloud_area_fraction_medium"),
                cloud_high=_get(details, "cloud_area_fraction_high"),
                # Met.no reports precipitation *amount*, not probability.
                precip_prob=None,
                humidity=_get(details, "relative_humidity"),
                dew_point_c=_get(details, "dew_point_temperature"),
                temp_c=_get(details, "air_temperature"),
                wind_ms=_get(details, "wind_speed"),
                wind_direction_deg=_get(details, "wind_from_direction"),
                # Not reported by locationforecast at any detail level.
                visibility_m=None,
                seeing_arcsec=None,
                transparency=None,
                source=PROVIDER_NAME,
            )
        )
    return ForecastSeries(points=tuple(points), fetched_at=fetched_at)


class MetNoProvider:
    """Keyless, always "available" — see :meth:`availability`."""

    name = PROVIDER_NAME

    def __init__(
        self,
        config: MetNoConfig,
        *,
        cache: ForecastCache,
        http: httpx.AsyncClient | None = None,
        clock: Clock = time.monotonic,
        now: NowUTC = now_utc,
    ) -> None:
        # Fails fast, at construction — never silently degrades to a
        # generic UA (module docstring / MetNoConfigError).
        build_user_agent(app_name=config.app_name, contact=config.contact)
        self._config = config
        self._cache = cache
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=config.request_timeout_s)
        self._breaker = CircuitBreaker(
            failure_threshold=config.failure_threshold,
            reset_timeout_s=config.reset_timeout_s,
            clock=clock,
        )
        self._now = now

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def availability(self) -> ProviderAvailability:
        # Keyless: nothing to check beyond configuration, which is already
        # validated at construction time.
        return ProviderAvailability(available=True)

    async def fetch(self, latitude_deg: float, longitude_deg: float) -> ForecastSeries:
        key = make_cache_key(self.name, latitude_deg, longitude_deg)
        cached = self._cache.get(key)
        now = self._now()

        if cached is not None and cached.expires_at is not None and now < cached.expires_at:
            # Still within the provider's own Expires window: no request at
            # all. This is what keeps polling from outrunning model updates.
            return cached.series

        if not self._breaker.allow_request():
            if cached is not None:
                return stale_copy(cached)
            raise ProviderUnavailableError(
                f"{self.name}: circuit open and no cached series for this location"
            )

        headers = {
            "User-Agent": build_user_agent(
                app_name=self._config.app_name, contact=self._config.contact
            )
        }
        if cached is not None and cached.last_modified is not None:
            headers["If-Modified-Since"] = cached.last_modified

        try:
            response = await self._http.get(
                self._config.base_url,
                params={"lat": f"{latitude_deg:.4f}", "lon": f"{longitude_deg:.4f}"},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            if cached is not None:
                return stale_copy(cached)
            raise ProviderUnavailableError(f"{self.name}: request failed: {exc}") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            self._breaker.force_open(
                for_s=float(retry_after) if retry_after else self._config.reset_timeout_s
            )
            if cached is not None:
                return stale_copy(cached)
            raise ProviderUnavailableError(f"{self.name}: rate limited (429)")

        if response.status_code == 304:
            self._breaker.record_success()
            assert cached is not None  # a 304 implies we sent If-Modified-Since
            refreshed = CacheEntry(
                series=cached.series,
                fetched_at=now,
                expires_at=_parse_http_date(response.headers.get("Expires")) or cached.expires_at,
                etag=cached.etag,
                last_modified=response.headers.get("Last-Modified", cached.last_modified),
                model_run=cached.model_run,
            )
            self._cache.put(key, refreshed)
            return refreshed.series

        if response.status_code >= 400:
            self._breaker.record_failure()
            if cached is not None:
                return stale_copy(cached)
            raise ProviderUnavailableError(f"{self.name}: server returned {response.status_code}")

        self._breaker.record_success()
        payload = response.json()
        series = _normalise(payload, fetched_at=now)
        expires_at = _parse_http_date(response.headers.get("Expires"))
        entry = CacheEntry(
            series=series,
            fetched_at=now,
            expires_at=expires_at,
            etag=None,
            last_modified=response.headers.get("Last-Modified"),
            model_run=payload.get("properties", {}).get("meta", {}).get("updated_at"),
        )
        self._cache.put(key, entry)
        return series


__all__ = [
    "DEFAULT_BASE_URL",
    "FALLBACK_TTL_S",
    "RATE_LIMIT_PER_S",
    "MetNoConfig",
    "MetNoConfigError",
    "MetNoProvider",
    "build_user_agent",
]
