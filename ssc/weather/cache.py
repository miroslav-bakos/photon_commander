# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The provider cache: keyed with intent, and the thing that makes offline
the normal case instead of a failure.

**Cache key.** ``(provider, rounded lat/lon)`` — never wall-clock time.
Rounding to :data:`DEFAULT_PRECISION` decimal degrees (~1.1 km at the
equator) means a site that jitters by a few metres between fixes still hits
the same cache entry, and a request is never issued just because the caller's
GPS fix moved in the noise. The model run itself is not known until a
response arrives, so it cannot be part of the *key* computed before the
request; it is recorded on the :class:`CacheEntry` once known, both for
provenance and so a future contributor does not "fix" this by keying on
wall-clock time instead.

**Freshness.** A cache hit whose ``expires_at`` (from the provider's own
``Expires`` header, or a provider-specific fallback TTL when one is absent)
is still in the future is served with **no network request at all** — this
is what keeps polling from outrunning a provider's model-update cadence.
``last_modified``/``etag`` are carried so a provider that has to make a
request anyway can send a conditional one and treat ``304`` as a TTL
refresh rather than a fresh parse.

**Persistence.** Backed by one JSON file on disk when a path is given, so the
last-known series survives a process restart — a dark site with no signal is
the normal case (CLAUDE.md invariant 7 / DESIGN.md §6), not a corner case
worth losing on restart. In-memory only (no path) is fine for tests.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ssc.weather.base import ForecastPoint, ForecastSeries

_log = logging.getLogger(__name__)

#: Decimal degrees of rounding for the cache key. ~1.1 km of latitude.
DEFAULT_PRECISION: Final = 2


@dataclass(frozen=True, slots=True)
class CacheKey:
    provider: str
    lat_rounded: float
    lon_rounded: float

    def as_str(self) -> str:
        # Stable, human-readable — used as the on-disk JSON object key.
        return f"{self.provider}:{self.lat_rounded:.{DEFAULT_PRECISION}f}:{self.lon_rounded:.{DEFAULT_PRECISION}f}"


def make_cache_key(
    provider: str,
    latitude_deg: float,
    longitude_deg: float,
    *,
    precision: int = DEFAULT_PRECISION,
) -> CacheKey:
    return CacheKey(provider, round(latitude_deg, precision), round(longitude_deg, precision))


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One provider's last successful fetch for one location."""

    series: ForecastSeries
    fetched_at: datetime
    #: When this entry can be served without a network request. ``None``
    #: means "always revalidate" (no ``Expires``/equivalent was given).
    expires_at: datetime | None
    etag: str | None = None
    last_modified: str | None = None
    #: The provider's own model-run identifier, when it has one (e.g.
    #: Met.no's `properties.meta.updated_at`). Recorded for provenance and
    #: debugging; not part of the cache key — see module docstring.
    model_run: str | None = None


def _point_to_json(p: ForecastPoint) -> dict[str, Any]:
    d = asdict(p)
    d["t"] = p.t.isoformat()
    return d


def _point_from_json(d: Mapping[str, Any]) -> ForecastPoint:
    kwargs = dict(d)
    kwargs["t"] = datetime.fromisoformat(kwargs["t"])
    return ForecastPoint(**kwargs)


def _entry_to_json(entry: CacheEntry) -> dict[str, Any]:
    return {
        "points": [_point_to_json(p) for p in entry.series.points],
        "fetched_at": entry.fetched_at.isoformat(),
        "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        "etag": entry.etag,
        "last_modified": entry.last_modified,
        "model_run": entry.model_run,
    }


def _entry_from_json(d: Mapping[str, Any]) -> CacheEntry:
    fetched_at = datetime.fromisoformat(d["fetched_at"])
    series = ForecastSeries(
        points=tuple(_point_from_json(p) for p in d["points"]),
        fetched_at=fetched_at,
    )
    expires_at = datetime.fromisoformat(d["expires_at"]) if d.get("expires_at") else None
    return CacheEntry(
        series=series,
        fetched_at=fetched_at,
        expires_at=expires_at,
        etag=d.get("etag"),
        last_modified=d.get("last_modified"),
        model_run=d.get("model_run"),
    )


class ForecastCache:
    """In-memory, optionally disk-persisted. One instance per process is the
    intended use — providers share it by cache-key namespace (their own
    ``name``), not by having separate cache instances."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        self._entries: dict[str, CacheEntry] = {}
        if path is not None and path.exists():
            self._load()

    def get(self, key: CacheKey) -> CacheEntry | None:
        return self._entries.get(key.as_str())

    def put(self, key: CacheKey, entry: CacheEntry) -> None:
        self._entries[key.as_str()] = entry
        if self._path is not None:
            self._save()

    def _load(self) -> None:
        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt or unreadable cache file is not a reason to fail
            # startup — it just means this provider starts with no cached
            # series, same as a first run.
            _log.warning("could not load weather cache at %s: %s", self._path, exc)
            return
        for key_str, entry_json in raw.items():
            try:
                self._entries[key_str] = _entry_from_json(entry_json)
            except (KeyError, ValueError) as exc:
                _log.warning("dropping unreadable weather cache entry %r: %s", key_str, exc)

    def _save(self) -> None:
        assert self._path is not None
        payload = {k: _entry_to_json(v) for k, v in self._entries.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)


def stale_copy(entry: CacheEntry) -> ForecastSeries:
    """The cached series, marked stale as of its own last-fetch time — served
    when a live fetch fails but a cache entry still exists."""
    return replace(entry.series, stale_since=entry.fetched_at)


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "DEFAULT_PRECISION",
    "CacheEntry",
    "CacheKey",
    "ForecastCache",
    "make_cache_key",
    "now_utc",
    "stale_copy",
]
