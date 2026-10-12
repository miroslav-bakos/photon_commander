# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The one normalisation shape every provider adapter targets (DESIGN.md §6).

**`None` means unknown; it never means zero.** A provider that does not report
a field leaves it `None` — never a substituted, interpolated or coerced value.
`seeing_arcsec` and `transparency` are kept on :class:`ForecastPoint` because the
shape should not lie about what a provider *could* supply, but no shipped
provider fills them (see `THIRD_PARTY_NOTICES.md`'s weather section): both came
from 7Timer!/Meteoblue, which are excluded on commercial-licensing grounds.
Getting them back is a product decision, not something this module should
paper over with an approximation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    """One timestamp's forecast, from exactly one provider.

    Field-level merging across providers is deliberately not offered anywhere
    in this package: a row's values all came from the same source, and that
    source is recorded in ``source`` so the UI can show it. Splicing a cloud
    figure from one provider onto a humidity figure from another would erase
    that provenance and manufacture a reading nobody actually measured.
    """

    t: datetime
    cloud_total: float | None  # %
    cloud_low: float | None  # %
    cloud_mid: float | None  # %
    cloud_high: float | None  # %
    precip_prob: float | None  # %
    humidity: float | None  # %
    dew_point_c: float | None
    temp_c: float | None
    wind_ms: float | None
    visibility_m: float | None
    seeing_arcsec: float | None  # always None from a shipped provider; see module docstring
    transparency: float | None  # always None from a shipped provider; see module docstring
    source: str  # provenance, shown in the UI
    wind_direction_deg: float | None = None  # meteorological direction, clockwise from north


@dataclass(frozen=True, slots=True)
class ForecastSeries:
    """A run of :class:`ForecastPoint`, plus whether it is fresh.

    ``fetched_at`` is when this data actually left a provider (or, for a
    merged series, the earliest such time among its inputs). ``stale_since``
    is ``None`` for a fresh fetch; when a provider fetch failed and the last
    good cached series was served instead (the offline-is-normal case —
    CLAUDE.md invariant 7 / this package's domain rules), it carries the
    timestamp of *that* last good fetch, so the UI can render a staleness
    badge with an honest "as of" time rather than silently going quiet.
    """

    points: tuple[ForecastPoint, ...]
    fetched_at: datetime
    stale_since: datetime | None = None

    def as_stale(self, *, since: datetime) -> ForecastSeries:
        """The same points, marked stale as of ``since``. Used when a cache
        hit is served in place of a failed live fetch — the points themselves
        are unchanged; only their freshness claim changes."""
        return replace(self, stale_since=since)


@dataclass(frozen=True, slots=True)
class ProviderAvailability:
    """Whether a provider can be asked for a forecast right now.

    Distinct from a fetch failure: this is a static "should this provider
    even be tried" check (no key configured, secrets backend unavailable),
    not a transient network condition. A caller should not retry a fetch
    against a provider that reports ``available=False`` — it will not
    magically start working.
    """

    available: bool
    reason: str | None = None  # required when available is False


class ProviderError(Exception):
    """Base for every error this package raises. Never swallowed silently."""


class ProviderUnavailableError(ProviderError):
    """A fetch could not be served, fresh or cached, and there is nothing to
    fall back to for this provider. The caller (a ranked-fallback merge, or
    `scheduler-engine`) decides what happens next — this package never
    fabricates a series to avoid raising."""


class ProviderConfigError(ProviderError):
    """The provider is misconfigured in a way that must not be worked around
    silently — e.g. Met.no without a real `User-Agent` contact. Raised at
    construction time, before any request is attempted."""


@runtime_checkable
class Provider(Protocol):
    """What every provider adapter implements."""

    #: Short, stable identifier. Used as `ForecastPoint.source` and as the
    #: provider component of a cache key — never a display string.
    name: str

    async def availability(self) -> ProviderAvailability:
        """Whether this provider should be tried at all right now."""
        ...

    async def fetch(self, latitude_deg: float, longitude_deg: float) -> ForecastSeries:
        """The best series this provider can currently offer for a location.

        Implementations serve a fresh fetch when one succeeds, the last good
        cached series (marked stale) when a fetch fails but a cache entry
        exists, and raise :class:`ProviderUnavailableError` only when neither is
        possible. A fetch must never block indefinitely or raise for a
        merely-offline night when a cache entry is available.
        """
        ...


#: A monotonic clock, injected everywhere this package needs one instead of
#: calling `time.monotonic` directly, so tests can control it exactly.
Clock = Callable[[], float]

#: An async clock-like source of wall time, injected instead of
#: `datetime.now(UTC)` for the same reason.
NowUTC = Callable[[], datetime]

__all__ = [
    "Clock",
    "ForecastPoint",
    "ForecastSeries",
    "NowUTC",
    "Provider",
    "ProviderAvailability",
    "ProviderConfigError",
    "ProviderError",
    "ProviderUnavailableError",
]
