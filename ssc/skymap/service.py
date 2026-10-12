# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Fetch-through tile serving, offline-pack prefetch jobs, and the ``skymap``
section of the authoritative snapshot.

Policy this module enforces:

* A tile is served from the cache, else fetched from the originating archive
  (bounded timeout, at most :data:`UPSTREAM_CONCURRENCY` in flight), stored,
  served. Offline or an upstream failure is a :class:`TileUnavailableError` with
  the reason — the router turns it into a 404 with a JSON body. Never a blank
  or placeholder tile (CLAUDE.md invariant 7).
* A prefetch job's row is written before its first tile is requested and
  updated as it runs (CLAUDE.md invariant 6). A restart marks anything still
  running ``interrupted``; nothing resumes on its own.
* Every change is one whole-section ``skymap.changed`` event through
  ``ssc.state`` (invariant 4), throttled to :data:`PUBLISH_INTERVAL_S` while
  a job is streaming progress so two clients see the same numbers without
  the WS ring being flooded.
* The identifying ``User-Agent`` is built the way ``ssc.weather`` builds
  Met.no's, from the same operator contact; the caller passes a factory so
  a contact changed in Settings takes effect on the next request.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import logging
import shutil
import time
import uuid
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

import httpx

from ssc.skymap import fits as fits_reader
from ssc.skymap import hips, mercator
from ssc.skymap import png as png_stretch
from ssc.skymap.cache import PROPERTIES_ORDER, TileCache, now_utc
from ssc.skymap.surveys import (
    BUNDLED_PACK_MISSING,
    REGISTRY,
    TILE_MEDIA_TYPES,
    Survey,
    SurveyFrame,
    TileScheme,
    find_survey,
    tile_count,
    upstream_tile_url,
)
from ssc.state.events import SkymapChanged
from ssc.state.models import (
    BulkCutoutJobState,
    CutoutState,
    PrefetchJobState,
    PrefetchJobStatus,
    SkymapState,
    SurveyCacheState,
)
from ssc.state.store import StateStore

_log = logging.getLogger(__name__)

UPSTREAM_CONCURRENCY: Final = 6
UPSTREAM_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
#: A pack bigger than this is refused up front, with what *would* fit named
#: in the message (see ``_largest_order_within_limit``).
#:
#: For a square-grid pyramid (TOAST, Mercator — a ``2^order x 2^order`` grid
#: of 256px tiles), the cumulative tile count through order ``Z`` is
#: ``(4**(Z+1) - 1) / 3``, and the resulting resolution is a uniform
#: ``1_296_000 / (256 * 2**Z)`` arcsec/px at the equator (worked out and
#: cross-checked against the Seestar S30 Pro's ~1.29° field, 2026-09-08):
#:
#:   order   tiles(cumulative)   arcsec/px   S30 field width, px
#:     7           21,845           39.6            117
#:     8           87,381           19.8            235
#:     9          349,525            9.9            470
#:    10        1,398,101            4.9            939
#:
#: Order 10 is already essentially the sensor's own native resolution
#: (3.99"/px) — no framing purpose is served by a deeper pack, and it is
#: 1.4M requests against a public research archive for it. Order 9 is a
#: comfortable framing preview (a 470px-wide field) at 1.4x the old limit,
#: so the ceiling sits just above it: enough for a genuine full-sky offline
#: pack at framing resolution (the ask this constant exists to satisfy),
#: never enough to reach native resolution.
PREFETCH_MAX_TILES: Final = 349_525
PUBLISH_INTERVAL_S: Final = 0.5
#: A prefetch job retries one tile this many times before giving up on it —
#: legacysurvey.org is a shared research archive, not a CDN, and a busy
#: moment there (a timeout, a 502, a dropped connection) is routine at the
#: scale of a several-thousand-tile job, not a reason to fail the whole run
#: and make the user start over from tile zero (2026-09-08 bug: one such
#: blip anywhere in a batch used to abort the entire job outright). Mirrors
#: ``ssc.skymap.cutouts``' own ``_RETRY_ATTEMPTS``/``_RETRY_BASE_DELAY_S``.
#: Never applied to a live (non-pinned) tile request: a map pan wants a fast
#: failure, not a multi-second stall.
PREFETCH_RETRY_ATTEMPTS: Final = 4
PREFETCH_RETRY_BASE_DELAY_S: Final = 1.0
#: HTTP statuses worth retrying (rate-limited or a transient server error);
#: anything else (403, …) looks permanent and is surfaced immediately.
_TRANSIENT_UPSTREAM_STATUS: Final = frozenset({429, 500, 502, 503, 504})
#: How many jobs, newest first, the snapshot lists.
JOB_HISTORY: Final = 20
MIN_TILE_DISK_FREE_BYTES: Final = 10 * 1024**3
MIN_TILE_DISK_FREE_FRACTION: Final = 0.05

#: The Seestar S30 Pro's field width, quoted directly from the live dev
#: fixture's own optics (architecture policy "Dev hardware": IMX662, 150mm, ~3.99"/px
#: native) so a tile-pack refusal can say what an order actually buys for
#: *framing* rather than leaving the user to guess a number and retry. Not
#: imported from ``ssc.astro.framing.field_geometry`` — that is per-profile
#: and per-optic (a real telescope's actual field varies with the optic
#: attached), while this is a fixed, approximate reference point for one
#: message string, and ``ssc.astro`` is out of this change's scope.
_S30_FIELD_ARCSEC: Final = 1.29 * 3600
#: A tile pyramid pixel's true angular size varies across a Mercator/TOAST
#: tile (finer away from the equator; a HiPS/HEALPix pixel is a different
#: shape again) — this is the coarsest (equatorial) case for a standard
#: ``2^order x 2^order`` grid of 256px tiles, i.e. a worst-case bound, not
#: an exact per-pixel value. Only meaningful for :data:`TileScheme.TOAST`/
#: :data:`TileScheme.MERCATOR`; HiPS has no simple equivalent (see
#: ``_order_resolution_arcsec``).
_EQUATOR_ARCSEC_PER_TILE_ROW: Final = 1_296_000.0  # 360 deg in arcsec
_TILE_PIXELS: Final = 256

#: A *nominal* per-format tile size, used only in a refusal message — that
#: code path is synchronous (no request has started yet, so nothing can be
#: sampled) and needs some figure to name rather than none. Deliberately
#: labelled "about" wherever it is shown, and superseded the moment a real
#: figure exists: ``estimate()`` always prefers ``TileCache.mean_tile_bytes``
#: (measured from whatever is actually cached) or a fresh sample
#: (``_sample_one_tile``) over this table. Rounded from a live measurement
#: of this instance's own cache, 2026-09-08 (``sqlite3 … "SELECT survey,
#: AVG(bytes) … GROUP BY survey"``): Legacy Surveys DR11 (jpg) averaged
#: ~29 KB/tile over 341 real cached tiles; Pan-STARRS (png) ~1.2 KB/tile
#: over 505, but that sample was mostly low-order (mostly-blank) tiles, so
#: the png figure below is rounded well up rather than quoted as-is.
_NOMINAL_TILE_BYTES: Final[dict[str, int]] = {
    "jpg": 25_000,
    "jpeg": 25_000,
    "png": 5_000,
    "webp": 15_000,
    "fits": 200_000,
}


def _order_resolution_arcsec(scheme: TileScheme, order: int) -> float | None:
    """Equatorial (coarsest-case) arcsec/px of a full-sky pack through
    ``order``, for the schemes where that is a single well-defined number —
    ``None`` for HiPS, whose HEALPix pixels are not a uniform square grid
    (see the ``PREFETCH_MAX_TILES`` docstring for the table this computes)."""
    if scheme is TileScheme.HIPS:
        return None
    return _EQUATOR_ARCSEC_PER_TILE_ROW / (_TILE_PIXELS * (1 << order))


def _format_bytes(n: int) -> str:
    """Mirrors ``web/src/tabs/SettingsTab/OfflineMapsPanel.tsx``'s own
    ``formatBytes`` closely enough that a number quoted server-side (in a
    refusal message) and the same number rendered client-side read the
    same way."""
    value = float(n)
    if value < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"  # pragma: no cover - unreachable, satisfies mypy


Reason = Literal[
    "offline",
    "timeout",
    "upstream",
    "missing",
    "unknown_survey",
    "survey_unavailable",
    "format",
    "out_of_range",
    "not_hips",
    "cache_full",
]

PrefetchMode = Literal["sky", "cone"]
Spawn = Callable[[Coroutine[Any, Any, Any], str], asyncio.Task[Any]]


def build_user_agent(contact: str | None) -> str:
    """``SSC/<version> (+<contact>)``; without the contact when none is set.

    STScI/IRSA do not mandate a contact the way Met.no does, so an unset one
    degrades the UA rather than refusing the request — but the operator's
    contact, when configured, is sent so the archive can reach us.
    """
    try:
        version = importlib.metadata.version("ssc")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"
    cleaned = (contact or "").strip()
    return f"SSC/{version} (+{cleaned})" if cleaned else f"SSC/{version}"


def _upstream_fetch_ext(survey: Survey) -> str:
    """The extension actually requested from the archive.

    Equal to ``survey.tile_format`` for every survey except one with
    ``fits_stretch`` set, where the client is served ``.png`` but the
    archive only ever has ``.fits`` — see ``Survey.fits_stretch``.
    """
    return "fits" if survey.fits_stretch else survey.tile_format


def _stretch_fits_tile(survey: Survey, data: bytes) -> bytes:
    """Raw calibrated FITS bytes -> the display PNG ``fits_stretch`` promises.

    A single plane (NSNS's H-alpha/[OIII]/[SII] layers are all ``NAXIS=2``; the
    first plane is used if a multi-plane tile ever appears) through
    ``asinh_stretch_reference`` against the survey's own fixed black/white
    point (``fits_stretch_black``/``fits_stretch_white``) — **not**
    ``asinh_stretch``, which derives that point from each tile's own
    percentiles and, for a tile pyramid, makes adjacent tiles disagree about
    what a given intensity should look like (found live 2026-09-09 in
    ``nsns-halpha`` — see ``Survey.fits_stretch_black``'s docstring). Every
    tile of a survey is deterministic and mutually consistent: two requests
    for the same tile draw the same pixels, and a bright tile and its fainter
    neighbour draw continuously rather than as a hard-edged mosaic.
    """
    try:
        image = fits_reader.read_fits_image(data)
    except fits_reader.FitsError as exc:
        raise TileUnavailableError(
            "format", f"{survey.id}: upstream tile is not readable FITS: {exc}"
        ) from exc
    plane = image.data if image.naxis == 2 else image.data[0]
    assert survey.fits_stretch_black is not None and survey.fits_stretch_white is not None, (
        f"survey {survey.id}: fits_stretch without fits_stretch_black/white "
        "should have been refused by the registry's own validator"
    )
    pixels = png_stretch.asinh_stretch_reference(
        plane, survey.fits_stretch_black, survey.fits_stretch_white
    )
    return png_stretch.encode_grayscale_png(pixels)


class TileUnavailableError(Exception):
    """The tile cannot be served, and why. The reason is data, not prose."""

    def __init__(self, reason: Reason, detail: str, *, upstream_status: int | None = None) -> None:
        super().__init__(detail)
        self.reason: Reason = reason
        self.detail = detail
        self.upstream_status = upstream_status


@dataclass(frozen=True, slots=True)
class TileResult:
    data: bytes
    media_type: str
    source: Literal["cache", "upstream", "bundled"]


@dataclass(frozen=True, slots=True)
class PrefetchRequest:
    survey: str
    mode: PrefetchMode
    max_order: int
    ra_deg: float | None = None
    dec_deg: float | None = None
    radius_deg: float | None = None


@dataclass(frozen=True, slots=True)
class PrefetchEstimate:
    survey: str
    tile_count: int
    #: Measured — from what is already cached, or else one freshly sampled
    #: tile (``SkymapService._sample_one_tile``); ``None`` only when neither
    #: is possible (e.g. the archive is unreachable right now).
    mean_tile_bytes: int | None
    #: ``mean_tile_bytes * (tile_count - already_cached)`` — the *remaining*
    #: download, not the whole pack's size, so a re-run over a partly-cached
    #: pack does not overstate what is left to fetch.
    estimated_bytes: int | None
    already_cached: int
    #: Measured the same way as ``mean_tile_bytes``, from this survey's own
    #: observed round-trip time, divided across ``UPSTREAM_CONCURRENCY``
    #: parallel fetches; ``None`` under the same condition as the bytes
    #: figure. Never a guess (architecture policy invariant 7): both are absent
    #: together, or present together.
    estimated_seconds: float | None


class PrefetchRefusedError(ValueError):
    """The job cannot be started as asked. The message says what to change."""


class JobNotFoundError(LookupError):
    pass


@dataclass(slots=True)
class _Plan:
    total: int
    addresses: Callable[[], Iterator[tuple[int, int]]]


class SkymapService:
    def __init__(
        self,
        cache: TileCache,
        *,
        max_bytes: int,
        user_agent: Callable[[], str],
        bundled_root: Path,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = now_utc,
        cutouts_max_bytes: int = 0,
        cutouts_provider: Callable[[], Coroutine[Any, Any, tuple[list[CutoutState], int]]]
        | None = None,
        bulk_cutouts_provider: Callable[[], Coroutine[Any, Any, list[BulkCutoutJobState]]]
        | None = None,
    ) -> None:
        self._cache = cache
        self._db = cache.database
        self._max_bytes = max_bytes
        self._cache_bytes = 0
        self._cache_write_lock = asyncio.Lock()
        self._cache_full_warned = False
        self._user_agent = user_agent
        self._bundled_root = bundled_root
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, follow_redirects=True)
        self._clock = clock
        self._now = now
        self._semaphore = asyncio.Semaphore(UPSTREAM_CONCURRENCY)
        self._state: StateStore | None = None
        self._spawn: Spawn | None = None
        self._jobs: dict[str, asyncio.Task[Any]] = {}
        self._cancel_requested: set[str] = set()
        self._upstream_reachable: bool | None = None
        #: Runtime reasons a registry-enabled survey still cannot be served.
        self._unavailable: dict[str, str] = {}
        #: Per-survey mean tile round-trip time, seconds — feeds the
        #: prefetch estimate's time figure. In memory only; see
        #: ``_note_latency``.
        self._mean_latency_s: dict[str, float] = {}
        self._last_publish = float("-inf")
        self._deferred: asyncio.TimerHandle | None = None
        #: Filled in by ``ssc.skymap.cutouts.CutoutService.compose``, when a
        #: cutout service is wired — target-field cutouts share this section
        #: of the snapshot (one section, one publisher: this class).
        self._cutouts_max_bytes = cutouts_max_bytes
        self._cutouts_provider = cutouts_provider
        #: Filled in by ``ssc.skymap.bulk_prefetch.BulkCutoutService.compose``,
        #: when wired — bulk cutout jobs share this section too (still one
        #: section, one publisher).
        self._bulk_cutouts_provider = bulk_cutouts_provider

    # -- lifecycle ----------------------------------------------------------- #

    async def startup(self) -> SkymapState:
        """Mark jobs a previous process left running, and build the first
        ``skymap`` section for the initial snapshot."""
        self._cache_bytes = await self._cache.total_bytes()
        for survey in REGISTRY:
            if survey.local_dir is None:
                continue
            directory = self._bundled_root / survey.local_dir
            if await asyncio.to_thread(directory.is_dir):
                self._unavailable.pop(survey.id, None)
            else:
                self._unavailable[survey.id] = f"{BUNDLED_PACK_MISSING}: expected at {directory}"
                _log.warning("survey %s disabled: %s", survey.id, self._unavailable[survey.id])
        stamp = self._now().isoformat()
        await self._db.execute(
            "UPDATE skymap_prefetch_jobs SET status = ?, error = ?, updated_at = ?, "
            "finished_at = ? WHERE status IN ('queued', 'running')",
            (
                PrefetchJobStatus.INTERRUPTED,
                "ssc-core restarted while this download was running; start it again",
                stamp,
                stamp,
            ),
        )
        return await self._compose()

    def attach(self, state: StateStore, spawn: Spawn) -> None:
        self._state = state
        self._spawn = spawn

    async def aclose(self) -> None:
        if self._deferred is not None:
            self._deferred.cancel()
            self._deferred = None
        if self._owns_http:
            await self._http.aclose()

    # -- tiles --------------------------------------------------------------- #

    def unavailable_reason(self, survey: Survey) -> str | None:
        """Why the survey cannot be served right now, or ``None`` if it can."""
        if not survey.enabled:
            return survey.pending_clearance or f"survey {survey.id!r} is disabled"
        return self._unavailable.get(survey.id)

    def _survey(self, survey_id: str) -> Survey:
        survey = find_survey(survey_id)
        if survey is None:
            raise TileUnavailableError("unknown_survey", f"no survey {survey_id!r} in the registry")
        reason = self.unavailable_reason(survey)
        if reason is not None:
            raise TileUnavailableError("survey_unavailable", reason)
        return survey

    def _check_address(self, survey: Survey, order: int, npix: int, ext: str) -> None:
        if ext not in TILE_MEDIA_TYPES:
            raise TileUnavailableError("format", f"unsupported tile extension {ext!r}")
        if ext != survey.tile_format:
            raise TileUnavailableError(
                "format", f"{survey.id} serves .{survey.tile_format} tiles, not .{ext}"
            )
        if not 0 <= order <= survey.max_order:
            raise TileUnavailableError(
                "out_of_range", f"{survey.id} has orders 0..{survey.max_order}, not {order}"
            )
        if not 0 <= npix < tile_count(survey, order):
            raise TileUnavailableError(
                "out_of_range", f"npix {npix} is out of range at order {order}"
            )

    async def get_tile(self, survey_id: str, order: int, npix: int, ext: str) -> TileResult:
        survey = self._survey(survey_id)
        self._check_address(survey, order, npix, ext)
        if survey.local_dir is not None:
            path = self._bundled_root / survey.local_dir / hips.tile_path(order, npix, ext)
            return TileResult(
                await self._read_bundled(survey, path), TILE_MEDIA_TYPES[ext], "bundled"
            )
        cached = await self._cache.lookup(survey.id, order, npix)
        if cached is not None:
            if cached.status == "missing":
                raise TileUnavailableError("missing", "the archive has no tile here (known)")
            data = await self._cache.read(survey, cached)
            if data is not None:
                return TileResult(data, TILE_MEDIA_TYPES[cached.ext], "cache")
        data = await self._fetch(
            survey,
            order,
            npix,
            ext,
            upstream_tile_url(survey, order, npix, _upstream_fetch_ext(survey)),
            pinned=False,
        )
        return TileResult(data, TILE_MEDIA_TYPES[ext], "upstream")

    async def get_properties(self, survey_id: str) -> bytes:
        survey = self._survey(survey_id)
        if survey.scheme is not TileScheme.HIPS:
            raise TileUnavailableError(
                "not_hips", f"{survey.id} is a TOAST pyramid; no properties file"
            )
        if survey.local_dir is not None:
            return await self._read_bundled(
                survey, self._bundled_root / survey.local_dir / "properties"
            )
        cached = await self._cache.lookup(survey.id, PROPERTIES_ORDER, 0)
        if cached is not None and cached.status == "cached":
            data = await self._cache.read(survey, cached)
            if data is not None:
                return data
        return await self._fetch(
            survey,
            PROPERTIES_ORDER,
            0,
            "properties",
            f"{survey.upstream_base_url}/properties",
            pinned=True,
        )

    @staticmethod
    async def _read_bundled(survey: Survey, path: Path) -> bytes:
        """A bundled survey is read from disk and never fetched; a file the
        pack does not have is ``missing``, like an upstream 404."""
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise TileUnavailableError(
                "missing", f"the bundled {survey.id} pack has no {path.name}"
            ) from exc

    async def _fetch(
        self, survey: Survey, order: int, npix: int, ext: str, url: str, *, pinned: bool
    ) -> bytes:
        """One tile, with retry-and-backoff for a prefetch job (``pinned``).

        A live map request (``pinned=False``) makes exactly one attempt — it
        wants a fast, explainable failure, not a stall. A prefetch job makes
        up to :data:`PREFETCH_RETRY_ATTEMPTS`, doubling the delay each time,
        and only for an error that looks transient (a timeout, a dropped
        connection, or a 429/5xx); a tile the archive genuinely does not have
        (404) or an error that looks permanent (403, …) is never retried.
        """
        attempts = PREFETCH_RETRY_ATTEMPTS if pinned else 1
        delay = PREFETCH_RETRY_BASE_DELAY_S
        for attempt in range(attempts):
            try:
                return await self._fetch_once(survey, order, npix, ext, url, pinned=pinned)
            except TileUnavailableError as exc:
                last_attempt = attempt == attempts - 1
                retryable = exc.reason in ("offline", "timeout") or (
                    exc.reason == "upstream" and exc.upstream_status in _TRANSIENT_UPSTREAM_STATUS
                )
                if exc.reason == "missing" or not retryable or last_attempt:
                    raise
                _log.info(
                    "prefetch %s Norder%d/Npix%d: %s (attempt %d/%d), retrying in %.0fs",
                    survey.id,
                    order,
                    npix,
                    exc.reason,
                    attempt + 1,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable: the loop above always returns or raises")

    async def _fetch_once(
        self, survey: Survey, order: int, npix: int, ext: str, url: str, *, pinned: bool
    ) -> bytes:
        async with self._semaphore:
            started = self._clock()
            try:
                response = await self._http.get(url, headers={"User-Agent": self._user_agent()})
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                await self._note_reachable(False)
                raise TileUnavailableError(
                    "offline", f"cannot reach {survey.upstream_host}: {exc}"
                ) from exc
            except httpx.TimeoutException as exc:
                raise TileUnavailableError(
                    "timeout", f"{survey.upstream_host} did not answer in time: {exc}"
                ) from exc
            except httpx.HTTPError as exc:
                raise TileUnavailableError("upstream", f"{survey.upstream_host}: {exc}") from exc
        await self._note_reachable(True)
        if response.status_code == 404:
            if order != PROPERTIES_ORDER:
                await self._cache.store_missing(survey, order, npix, ext, pinned=pinned)
            raise TileUnavailableError(
                "missing", "the archive has no tile here", upstream_status=404
            )
        if response.status_code != 200:
            raise TileUnavailableError(
                "upstream",
                f"{survey.upstream_host} answered {response.status_code}",
                upstream_status=response.status_code,
            )
        data = response.content
        if survey.fits_stretch and order != PROPERTIES_ORDER:
            # The archive's own bytes are calibrated float32 FITS; ``ext``
            # here is always the client-facing ``png`` (see
            # ``_upstream_fetch_ext`` — the URL already asked upstream for
            # ``.fits``), so what is cached and served from here on is the
            # converted PNG, never the raw scientific data.
            data = _stretch_fits_tile(survey, data)
        async with self._cache_write_lock:
            previous = await self._cache.lookup(survey.id, order, npix)
            previous_bytes = previous.bytes if previous is not None else 0
            usage = await asyncio.to_thread(shutil.disk_usage, self._cache.root.parent)
            reserve = max(MIN_TILE_DISK_FREE_BYTES, int(usage.total * MIN_TILE_DISK_FREE_FRACTION))
            cache_full = (
                self._cache_bytes - previous_bytes + len(data) > self._max_bytes
                or usage.free - len(data) < reserve
            )
            if cache_full:
                if pinned:
                    raise TileUnavailableError(
                        "cache_full",
                        "tile cache is full or disk free-space reserve reached; "
                        "clear unwanted surveys or increase tiles_max_bytes",
                    )
                if not self._cache_full_warned:
                    _log.warning("tile cache full; serving new browsing tiles without caching them")
                    self._cache_full_warned = True
            else:
                await self._cache.store(survey, order, npix, ext, data, pinned=pinned)
                self._cache_bytes += len(data) - previous_bytes
        if order != PROPERTIES_ORDER:
            # Feeds the prefetch-cost estimate's time figure (``estimate()``);
            # a tiny one-off ``properties`` fetch is not a representative tile.
            self._note_latency(survey.id, self._clock() - started)
        if not pinned:
            await self._publish(force=False)
        return data

    def _note_latency(self, survey_id: str, seconds: float) -> None:
        """A light exponential moving average of one tile's round-trip time,
        kept in memory only (like ``mean_tile_bytes``, it is reseeded from
        the next real fetch after a restart — nothing here needs to survive
        one, it is a display estimate, not a state the executor acts on)."""
        prior = self._mean_latency_s.get(survey_id)
        self._mean_latency_s[survey_id] = seconds if prior is None else prior * 0.8 + seconds * 0.2

    async def _note_reachable(self, reachable: bool) -> None:
        if self._upstream_reachable == reachable:
            return
        self._upstream_reachable = reachable
        await self._publish(force=True)

    async def clear(self, survey_id: str) -> int:
        survey = find_survey(survey_id)
        if survey is None:
            raise TileUnavailableError("unknown_survey", f"no survey {survey_id!r} in the registry")
        async with self._cache_write_lock:
            freed = await self._cache.clear(survey)
            self._cache_bytes -= freed
            self._cache_full_warned = False
        await self._publish(force=True)
        return freed

    # -- prefetch ------------------------------------------------------------ #

    @staticmethod
    def _cone_tiles(
        survey: Survey, ra_deg: float, dec_deg: float, radius_deg: float, max_order: int
    ) -> dict[int, list[int]]:
        """Every tile through ``max_order``/``max_zoom`` that may intersect
        the cone, dispatched on the survey's own addressing scheme. Never
        called for :data:`TileScheme.TOAST` — ``_plan`` refuses that mode
        before reaching here (no cone addressing is defined for it)."""
        if survey.scheme is TileScheme.HIPS:
            lon, lat = (
                hips.equatorial_to_galactic(ra_deg, dec_deg)
                if survey.frame is SurveyFrame.GALACTIC
                else (ra_deg, dec_deg)
            )
            return hips.cone_tiles_by_order(lon, lat, radius_deg, max_order)
        return mercator.cone_tiles_by_zoom(ra_deg, dec_deg, radius_deg, max_order)

    def _plan(self, survey: Survey, request: PrefetchRequest) -> _Plan:
        if survey.local_dir is not None:
            raise PrefetchRefusedError(
                f"{survey.id} ships with the product; there is nothing to download"
            )
        if request.mode == "sky" and not survey.production_cleared:
            raise PrefetchRefusedError(
                f"{survey.id} is evaluation-only; whole-sky download is disabled"
            )
        if request.max_order > survey.max_order:
            raise PrefetchRefusedError(
                f"{survey.id} has orders 0..{survey.max_order}; max_order {request.max_order} "
                "does not exist upstream"
            )
        if request.mode == "sky":
            total = sum(tile_count(survey, order) for order in range(request.max_order + 1))

            def sky() -> Iterator[tuple[int, int]]:
                for order in range(request.max_order + 1):
                    yield from ((order, npix) for npix in range(tile_count(survey, order)))

            plan = _Plan(total=total, addresses=sky)
        else:
            if survey.scheme is TileScheme.TOAST:
                raise PrefetchRefusedError(
                    f"{survey.id} is a TOAST pyramid: cone prefetch is not defined for TOAST "
                    "surveys; use mode 'sky'"
                )
            if request.ra_deg is None or request.dec_deg is None or request.radius_deg is None:
                raise PrefetchRefusedError("mode 'cone' requires ra_deg, dec_deg and radius_deg")
            by_order = self._cone_tiles(
                survey, request.ra_deg, request.dec_deg, request.radius_deg, request.max_order
            )
            total = sum(len(tiles) for tiles in by_order.values())

            def cone() -> Iterator[tuple[int, int]]:
                for order, tiles in by_order.items():
                    yield from ((order, npix) for npix in tiles)

            plan = _Plan(total=total, addresses=cone)
        if plan.total > PREFETCH_MAX_TILES:
            raise PrefetchRefusedError(self._refusal_message(survey, request, plan.total))
        return plan

    def _largest_order_within_limit(
        self, survey: Survey, request: PrefetchRequest
    ) -> tuple[int, int] | None:
        """The deepest order below ``request.max_order`` whose plan still
        fits :data:`PREFETCH_MAX_TILES`, and its tile count — or ``None`` if
        even order 0 does not fit (never expected: order 0 is at most 12
        tiles, HiPS's worst case)."""
        for order in range(request.max_order - 1, -1, -1):
            if request.mode == "sky":
                total = sum(tile_count(survey, o) for o in range(order + 1))
            else:
                assert request.ra_deg is not None
                assert request.dec_deg is not None
                assert request.radius_deg is not None
                by_order = self._cone_tiles(
                    survey, request.ra_deg, request.dec_deg, request.radius_deg, order
                )
                total = sum(len(tiles) for tiles in by_order.values())
            if total <= PREFETCH_MAX_TILES:
                return order, total
        return None

    def _refusal_message(self, survey: Survey, request: PrefetchRequest, total: int) -> str:
        """Names what *would* work, so the caller never has to guess a
        number and retry (the live complaint this replaces: "Lower
        max_order or the cone radius" named no actual value)."""
        base = f"that pack is {total:,} tiles; the limit is {PREFETCH_MAX_TILES:,}."
        suggestion = self._largest_order_within_limit(survey, request)
        if suggestion is None:
            return f"{base} Even order 0 does not fit here; this survey cannot be prefetched as a pack."
        order, order_tiles = suggestion
        nominal = _NOMINAL_TILE_BYTES.get(survey.tile_format)
        size = "" if nominal is None else f", about {_format_bytes(nominal * order_tiles)}"
        resolution = _order_resolution_arcsec(survey.scheme, order)
        framing = ""
        if resolution is not None:
            field_px = round(_S30_FIELD_ARCSEC / resolution)
            framing = (
                f' at {resolution:.1f}"/px — a Seestar S30 Pro field (~1.29°) is about '
                f"{field_px}px wide at that resolution, plenty for framing"
            )
        narrow = "" if request.mode == "sky" else " (or shrink the cone radius)"
        return (
            f"{base} Order {order} fits: {order_tiles:,} tiles{size}{framing}. "
            f"Lower max_order to {order}{narrow} and it will go through."
        )

    async def estimate(
        self, request: PrefetchRequest, *, sample_if_unknown: bool = True
    ) -> PrefetchEstimate:
        """How big (and how long) the pack would be.

        ``sample_if_unknown`` is ``False`` only for ``start_prefetch``'s own
        internal budget check: that call must stay side-effect-free (no
        network fetch hidden inside "start the job the user asked for"), so
        it estimates from whatever is already measured and accepts ``None``
        exactly as this method always used to. The dedicated ``/estimate``
        route the UI calls *before* offering "Start download" is the one
        place a first-ever look at a survey is worth one real network round
        trip to answer honestly instead of with nothing.
        """
        survey = self._survey_for_prefetch(request.survey)
        plan = self._plan(survey, request)

        async def already_cached() -> int:
            counts = await self._cache.order_counts(survey.id)
            return min(
                sum(
                    min(counts.get(o, 0), tile_count(survey, o))
                    for o in range(request.max_order + 1)
                ),
                plan.total,
            )

        already = await already_cached()
        mean_bytes = await self._cache.mean_tile_bytes(survey.id)
        mean_latency = self._mean_latency_s.get(survey.id)
        if (
            sample_if_unknown
            and plan.total - already > 0
            and (mean_bytes is None or mean_latency is None)
        ):
            # Nothing of this survey's has ever been fetched by this process
            # (and nothing is cached from a previous one): sample one real
            # tile so the estimate is measured rather than absent. Never a
            # guess (invariant 7) — a sample that fails (offline, …) simply
            # leaves both figures ``None``, exactly as before this existed.
            # The sample itself may land in ``already`` (order 0 is always
            # inside ``request.max_order``), so both are re-read afterwards.
            await self._sample_one_tile(survey)
            already = await already_cached()
            mean_bytes = await self._cache.mean_tile_bytes(survey.id)
            mean_latency = self._mean_latency_s.get(survey.id)
        to_fetch = plan.total - already
        estimated_bytes = None if mean_bytes is None else mean_bytes * to_fetch
        estimated_seconds = (
            None if mean_latency is None else (to_fetch / UPSTREAM_CONCURRENCY) * mean_latency
        )
        return PrefetchEstimate(
            survey=survey.id,
            tile_count=plan.total,
            mean_tile_bytes=mean_bytes,
            estimated_bytes=estimated_bytes,
            already_cached=already,
            estimated_seconds=estimated_seconds,
        )

    async def _sample_one_tile(self, survey: Survey) -> None:
        """Fetch exactly one real tile (order 0, npix 0 — valid for every
        scheme) so an estimate has a measured size and round-trip time
        instead of none. Best-effort and silent: an unreachable archive
        leaves the estimate at ``None``, same as if this were never tried."""
        with contextlib.suppress(TileUnavailableError):
            await self._fetch(
                survey,
                0,
                0,
                survey.tile_format,
                upstream_tile_url(survey, 0, 0, _upstream_fetch_ext(survey)),
                pinned=False,
            )

    def _survey_for_prefetch(self, survey_id: str) -> Survey:
        try:
            return self._survey(survey_id)
        except TileUnavailableError as exc:
            raise PrefetchRefusedError(exc.detail) from exc

    async def start_prefetch(self, request: PrefetchRequest) -> PrefetchJobState:
        if self._state is None or self._spawn is None:
            raise RuntimeError("SkymapService.attach() was not called")
        survey = self._survey_for_prefetch(request.survey)
        plan = self._plan(survey, request)
        estimate = await self.estimate(request, sample_if_unknown=False)
        if estimate.estimated_bytes is not None and estimate.estimated_bytes > self._max_bytes:
            raise PrefetchRefusedError(
                f"estimated {estimate.estimated_bytes} bytes exceeds the tile cache budget of "
                f"{self._max_bytes} bytes (tiles_max_bytes); a pack is never evicted, so "
                "raise the budget or lower max_order"
            )
        if any(not task.done() for task in self._jobs.values()):
            raise PrefetchRefusedError("a download is already running; wait for it or cancel it")

        now = self._now()
        job = PrefetchJobState(
            id=uuid.uuid4().hex[:12],
            survey=survey.id,
            mode=request.mode,
            max_order=request.max_order,
            ra_deg=request.ra_deg,
            dec_deg=request.dec_deg,
            radius_deg=request.radius_deg,
            status=PrefetchJobStatus.QUEUED,
            total=plan.total,
            created_at=now,
            updated_at=now,
        )
        # Durable before acted on (invariant 6): the row exists before any tile is asked for.
        await self._db.execute(
            "INSERT INTO skymap_prefetch_jobs (id, survey, mode, max_order, ra_deg, dec_deg, "
            "radius_deg, status, done, total, missing, bytes, error, created_at, updated_at, "
            "finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 0, NULL, ?, ?, NULL)",
            (
                job.id,
                job.survey,
                job.mode,
                job.max_order,
                job.ra_deg,
                job.dec_deg,
                job.radius_deg,
                job.status,
                job.total,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        task = self._spawn(self._run(job, survey, plan), f"skymap-prefetch-{job.id}")
        task.add_done_callback(lambda finished: self._reap(job.id, finished))
        self._jobs[job.id] = task
        await self._publish(force=True)
        return job

    async def cancel_prefetch(self, job_id: str) -> None:
        task = self._jobs.get(job_id)
        if task is None or task.done():
            raise JobNotFoundError(f"no running download {job_id!r}")
        self._cancel_requested.add(job_id)
        task.cancel()

    def _reap(self, job_id: str, task: asyncio.Task[Any]) -> None:
        """Give the row a terminal status however the task ended, including
        the ways ``_run``'s own handlers cannot see — the same reconciliation
        ``BulkCutoutService`` does, for the same reason. ``Task.cancel()``
        before the coroutine's first step throws into a coroutine that never
        started, so not one line of ``_run`` runs, its ``try`` included; a
        cancel arriving while ``_run`` is still writing its ``RUNNING`` row
        used to escape the same way. The row then stayed ``running`` for the
        life of the process — ``startup()``'s reaper only runs at process
        start, and :meth:`cancel_prefetch` refuses a task that is already
        done, so the job could never be cancelled a second time either."""
        if self._spawn is None:  # pragma: no cover — attach() precedes start
            return
        if task.cancelled():
            requested = job_id in self._cancel_requested
            self._cancel_requested.discard(job_id)
            status = PrefetchJobStatus.CANCELLED if requested else PrefetchJobStatus.INTERRUPTED
            error = (
                "cancelled by the user"
                if requested
                else "ssc-core shut down while this download was running; start it again"
            )
        else:
            exc = task.exception()
            if exc is None:
                return
            status = PrefetchJobStatus.FAILED
            error = f"unexpected error while downloading: {exc}"
        self._spawn(self._settle(job_id, status, error), f"skymap-prefetch-settle-{job_id}")

    async def _settle(self, job_id: str, status: PrefetchJobStatus, error: str) -> None:
        """Write ``status`` only if the row is still non-terminal — whenever
        ``_run`` did reach its own handler it has already written a truer
        one, tile counts and all, and this must not overwrite it. Progress
        is left as last published: this knows the job ended, not how far it
        got."""
        row = await self._db.fetch_one(
            "SELECT status FROM skymap_prefetch_jobs WHERE id = ?", (job_id,)
        )
        if row is None or str(row["status"]) not in ("queued", "running"):
            return
        stamp = self._now().isoformat()
        await self._db.execute(
            "UPDATE skymap_prefetch_jobs SET status = ?, error = ?, updated_at = ?, "
            "finished_at = ? WHERE id = ?",
            (status.value, error, stamp, stamp, job_id),
        )
        _log.info("prefetch %s reaped as %s: %s", job_id, status.value, error)
        await self._publish(force=True)

    async def _run(self, job: PrefetchJobState, survey: Survey, plan: _Plan) -> None:
        done = missing = size = 0
        status = PrefetchJobStatus.RUNNING
        error: str | None = None

        async def one(order: int, npix: int) -> tuple[int, int]:
            """(bytes fetched, 1 if missing)."""
            if await self._cache.is_cached(survey.id, order, npix):
                return 0, 0
            try:
                data = await self._fetch(
                    survey,
                    order,
                    npix,
                    survey.tile_format,
                    upstream_tile_url(survey, order, npix, _upstream_fetch_ext(survey)),
                    pinned=True,
                )
            except TileUnavailableError as exc:
                if exc.reason == "missing":
                    return 0, 1
                raise
            return len(data), 0

        try:
            # Inside the try, not before it: a cancel delivered while this
            # first write/publish was in flight used to escape `_run`
            # entirely and leave the row at `running` for good.
            await self._write_progress(job.id, status, done, missing, size, None, finished=False)
            await self._publish(force=True)
            batch: list[tuple[int, int]] = []
            for address in plan.addresses():
                batch.append(address)
                if len(batch) < UPSTREAM_CONCURRENCY:
                    continue
                results = await asyncio.gather(*(one(o, n) for o, n in batch))
                batch.clear()
                done += len(results)
                size += sum(r[0] for r in results)
                missing += sum(r[1] for r in results)
                await self._progress_tick(job.id, done, missing, size)
            if batch:
                results = await asyncio.gather(*(one(o, n) for o, n in batch))
                done += len(results)
                size += sum(r[0] for r in results)
                missing += sum(r[1] for r in results)
            status = PrefetchJobStatus.COMPLETE
        except asyncio.CancelledError:
            if job.id in self._cancel_requested:
                self._cancel_requested.discard(job.id)
                status = PrefetchJobStatus.CANCELLED
                error = "cancelled by the user"
            else:
                status = PrefetchJobStatus.INTERRUPTED
                error = "ssc-core shut down while this download was running; start it again"
            # The row and the snapshot must both say so, even mid-cancellation.
            await asyncio.shield(self._finish(job.id, status, done, missing, size, error))
            raise
        except TileUnavailableError as exc:
            status = PrefetchJobStatus.FAILED
            error = f"{exc.reason}: {exc.detail}"
            _log.warning("prefetch %s failed after %d/%d tiles: %s", job.id, done, job.total, error)
        await self._finish(job.id, status, done, missing, size, error)

    async def _finish(
        self,
        job_id: str,
        status: PrefetchJobStatus,
        done: int,
        missing: int,
        size: int,
        error: str | None,
    ) -> None:
        await self._write_progress(job_id, status, done, missing, size, error, finished=True)
        await self._publish(force=True)

    async def _progress_tick(self, job_id: str, done: int, missing: int, size: int) -> None:
        if self._clock() - self._last_publish < PUBLISH_INTERVAL_S:
            return
        await self._write_progress(
            job_id, PrefetchJobStatus.RUNNING, done, missing, size, None, finished=False
        )
        await self._publish(force=True)

    async def _write_progress(
        self,
        job_id: str,
        status: PrefetchJobStatus,
        done: int,
        missing: int,
        size: int,
        error: str | None,
        *,
        finished: bool,
    ) -> None:
        stamp = self._now().isoformat()
        await self._db.execute(
            "UPDATE skymap_prefetch_jobs SET status = ?, done = ?, missing = ?, bytes = ?, "
            "error = ?, updated_at = ?, finished_at = ? WHERE id = ?",
            (status, done, missing, size, error, stamp, stamp if finished else None, job_id),
        )

    # -- the snapshot section ------------------------------------------------ #

    async def _jobs_from_db(self) -> list[PrefetchJobState]:
        rows = await self._db.fetch_all(
            "SELECT * FROM skymap_prefetch_jobs ORDER BY created_at DESC, id LIMIT ?",
            (JOB_HISTORY,),
        )
        jobs: list[PrefetchJobState] = []
        for row in rows:
            finished = row["finished_at"]
            jobs.append(
                PrefetchJobState(
                    id=str(row["id"]),
                    survey=str(row["survey"]),
                    mode=str(row["mode"]),
                    max_order=int(row["max_order"]),
                    ra_deg=None if row["ra_deg"] is None else float(row["ra_deg"]),
                    dec_deg=None if row["dec_deg"] is None else float(row["dec_deg"]),
                    radius_deg=None if row["radius_deg"] is None else float(row["radius_deg"]),
                    status=PrefetchJobStatus(str(row["status"])),
                    done=int(row["done"]),
                    total=int(row["total"]),
                    missing=int(row["missing"]),
                    bytes=int(row["bytes"]),
                    error=None if row["error"] is None else str(row["error"]),
                    created_at=datetime.fromisoformat(str(row["created_at"])),
                    updated_at=datetime.fromisoformat(str(row["updated_at"])),
                    finished_at=None if finished is None else datetime.fromisoformat(str(finished)),
                )
            )
        return jobs

    async def survey_state(self, survey: Survey) -> SurveyCacheState:
        stats = await self._cache.survey_stats(survey.id)
        reason = self.unavailable_reason(survey)
        return SurveyCacheState(
            id=survey.id,
            enabled=reason is None,
            reason=reason,
            bundled=survey.bundled,
            cached_tiles=stats.cached_tiles,
            cached_bytes=stats.cached_bytes,
            missing_tiles=stats.missing_tiles,
            pack_complete_order=await self._cache.pack_complete_order(survey),
        )

    async def _compose(self) -> SkymapState:
        cutouts, cutouts_bytes = (
            ([], 0) if self._cutouts_provider is None else await self._cutouts_provider()
        )
        bulk_cutout_jobs = (
            [] if self._bulk_cutouts_provider is None else await self._bulk_cutouts_provider()
        )
        return SkymapState(
            surveys=[await self.survey_state(survey) for survey in REGISTRY],
            jobs=await self._jobs_from_db(),
            cache_bytes=await self._cache.total_bytes(),
            cache_max_bytes=self._max_bytes,
            upstream_reachable=self._upstream_reachable,
            cutouts=cutouts,
            cutouts_bytes=cutouts_bytes,
            cutouts_max_bytes=self._cutouts_max_bytes,
            bulk_cutout_jobs=bulk_cutout_jobs,
        )

    async def _publish(self, *, force: bool) -> None:
        """Publish now, or — when throttled — make sure a publish happens once
        the interval is up. A throttled change is deferred, never dropped:
        the snapshot must converge on the truth even if nothing else changes."""
        if self._state is None or self._spawn is None:
            return
        elapsed = self._clock() - self._last_publish
        if not force and elapsed < PUBLISH_INTERVAL_S:
            if self._deferred is None:
                loop = asyncio.get_running_loop()
                self._deferred = loop.call_later(PUBLISH_INTERVAL_S - elapsed, self._publish_later)
            return
        if self._deferred is not None:
            self._deferred.cancel()
            self._deferred = None
        self._last_publish = self._clock()
        try:
            skymap = await self._compose()
        except Exception:
            # A composition failure must not end the publish loop — this
            # coroutine is spawned as a fire-and-forget background task
            # (``skymap-publish``) that ``ssc.api.tasks`` logs and drops on
            # any exception, with nothing left to restart it; before this
            # guard, one bad read here (2026-09-10, a concurrent-write race
            # in ``TileCache.order_counts`` — since fixed) killed the task
            # and froze the whole ``skymap`` state section for the rest of
            # the process's life, silently. Logging loudly and returning
            # keeps last published state in place (stale, not blank — architecture policy
            # invariant 7) and lets the *next* publish (the next tile fetch,
            # job tick, or the deferred catch-up timer) heal it forward.
            _log.exception(
                "skymap-publish: composing the sky-map state failed; "
                "keeping the last published snapshot and retrying next cycle"
            )
            return
        self._state.apply(SkymapChanged(skymap=skymap))

    def _publish_later(self) -> None:
        self._deferred = None
        if self._spawn is not None:
            self._spawn(self._publish(force=True), "skymap-publish")

    async def publish(self) -> None:
        """Recompute and publish the section now (after a cache clear, say)."""
        await self._publish(force=True)


__all__ = [
    "JOB_HISTORY",
    "PREFETCH_MAX_TILES",
    "PUBLISH_INTERVAL_S",
    "UPSTREAM_CONCURRENCY",
    "UPSTREAM_TIMEOUT",
    "JobNotFoundError",
    "PrefetchEstimate",
    "PrefetchMode",
    "PrefetchRefusedError",
    "PrefetchRequest",
    "Reason",
    "SkymapService",
    "TileResult",
    "TileUnavailableError",
    "build_user_agent",
]
