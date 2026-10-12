# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Target-field cutouts: deep imagery for one field, fetched once from the
originating archive and cached beside the target with its WCS so the map can
overlay it — offline, once fetched (M11 spec).

``THIRD_PARTY_NOTICES.md`` "Sky-survey layers" and its "Endpoint gate,
2026-09-07" subsection are the source of truth for every URL template,
format, size cap and failure mode below; this module should never read
differently from that document without the document changing first.

Policy mirrored from :mod:`ssc.skymap.service` (the tile prefetch jobs):

* A job's row is written *before* the first upstream request (architecture policy
  invariant 6); a restart marks anything still ``queued``/``running`` as
  ``interrupted``, and nothing resumes on its own.
* ``unavailable`` is the archive itself answering and confirming there is
  nothing here (off-footprint, or a lookup step unreachable with no fallback
  route) — never presented as ``failed`` (an unexpected error worth a retry).
* Every change is one whole-section ``skymap.changed`` event, via the same
  ``SkymapService.publish()`` the tile cache already uses — one section, one
  publisher, so a client never sees the tile half of the section and the
  cutout half disagree about the moment they were true.
* One in-flight request per upstream host, backed off on a transient failure,
  with a hard client timeout — the failure modes IRSA/SDSS/Legacy Surveys
  actually exhibit are silent hangs, not clean errors.
* A discovered or templated URL is fetched only if its host is the one the
  registry names for that source — a compromised or redirected response can
  never make ``ssc-core`` reach out to an arbitrary host.
* "Nothing resumes on its own" is about automatic resumption, not about
  waste: AllWISE's own multi-tile fetch (``_fetch_wise_incremental``) writes
  each Atlas tile to disk and its row the moment it lands, so a viewer's
  explicit retry after an interruption skips whatever is already there
  rather than re-fetching a mosaic that can be tens of tiles wide.
"""

from __future__ import annotations

import asyncio
import bz2
import contextlib
import gzip
import json
import logging
import math
import os
import shutil
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
import numpy as np

from ssc.astro.mosaic import sky_tile_centers_deg
from ssc.skymap.fits import FitsError, FitsImage, ImageWCS, extract_wcs, read_fits_image
from ssc.skymap.png import asinh_stretch, describe_stretch, encode_grayscale_png, encode_rgb_png
from ssc.skymap.surveys import (
    LEGACY_SURVEYS_CUTOUT,
    SDSS_CUTOUT,
    UNWISE_CUTOUT,
    UNWISE_LAYER,
    CutoutSource,
    find_cutout_source,
    legacy_layer,
)
from ssc.state.models import CutoutJobStatus, CutoutState
from ssc.store.database import Database
from ssc.store.plans import PlanRepository

_log = logging.getLogger(__name__)

CUTOUT_TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)
#: THIRD_PARTY_NOTICES.md endpoint gate: "90 s hard timeout" on SDSS's getjpeg quick-look.
SDSS_QUICKLOOK_TIMEOUT: Final = httpx.Timeout(90.0, connect=10.0)
#: How many cutouts, newest first, the snapshot lists.
CUTOUT_HISTORY: Final = 50
#: A saved plan item's target protects the cutout covering it from eviction
#: within this radius (M11 spec).
PLAN_PROTECTION_RADIUS_DEG: Final = 0.05
#: `_fetch_unwise` tiles any request wider than this into an n x n grid of
#: this size (reported live 2026-09-08) so each tile gets its own full
#: `UNWISE_CUTOUT.max_pixels` budget instead of sharing one budget across the
#: whole footprint -- the same 3 deg every other source already caps its own
#: request at (`web/src/skymap/SkyMap.tsx`'s `MAX_CUTOUT_SIZE_DEG`).
UNWISE_TILE_SIZE_DEG: Final = 3.0
#: `schedule_autofetch`'s debounce window -- see that method's docstring.
#: Chosen well above a keystroke's own interval (typing through a target
#: search fires far faster than this) and well below what would read as a
#: sluggish response once a user actually settles on one target.
AUTOFETCH_DEBOUNCE_S: Final = 0.5
#: The default autofetch source: a single bounded request to a commercially
#: cleared, genuinely all-sky CC BY source.
AUTOFETCH_SOURCE_ID: Final = UNWISE_CUTOUT.id.value
#: `schedule_autofetch`'s own request size -- deliberately its own constant,
#: not `UNWISE_TILE_SIZE_DEG` (that name is unWISE's own tiling unit and
#: means nothing to `hips2fits`, which takes `fov` directly). This is the
#: general "a field, not a survey" cutout ceiling every source but unWISE's
#: own wider default already self-limits to (`CutoutRequest`'s docstring in
#: `ssc/api/schemas.py`; `MAX_CUTOUT_SIZE_DEG` in
#: `web/src/skymap/SkyMap.tsx`) -- enough to frame a resolved target with
#: some surrounding context, not a wide mosaic. Numerically still 3 deg,
#: same as before; the fix is that it no longer means "unWISE's tile unit"
#: by accident.
AUTOFETCH_SIZE_DEG: Final = 3.0
_RETRY_ATTEMPTS: Final = 3
_RETRY_BASE_DELAY_S: Final = 1.0
#: A job in one of these statuses is not the final word — `request()` (a
#: viewer's Retry action re-POSTs the exact same request) restarts it in
#: place rather than handing back the same stale failure forever. Not
#: `UNAVAILABLE`: the archive itself already confirmed there is nothing
#: here, and asking again would only ask the same question.
_RETRYABLE_STATUSES: Final = frozenset(
    {CutoutJobStatus.FAILED, CutoutJobStatus.CANCELLED, CutoutJobStatus.INTERRUPTED}
)
#: `startup()`'s reaper only runs once, at process start — it cannot help a
#: fetch that dies in a *live* process (2026-09-09: the actual mechanism
#: behind a "stuck running forever" row turned out to be an unhandled
#: exception escaping `_run` entirely, now fixed, but the reaper alone was
#: never going to catch a failure mode like that). `request()` treats a row
#: in one of these statuses as dead too, defensively, whenever this process
#: has no live task backing it — not on an age threshold, which would
#: either be too short for a genuine multi-tile AllWISE fetch or too long
#: to actually help.
_IN_FLIGHT_STATUSES: Final = frozenset({CutoutJobStatus.QUEUED, CutoutJobStatus.RUNNING})
#: IRSA's SIA v2 ``BAND`` parameter is a spectral interval in metres, not a
#: bandpass name — a bare ``BAND=W1`` is rejected as an unknown parameter
#: value; a wavelength inside the band's range selects it (verified live
#: against IRSA's own ``energy_bandpassname`` column, THIRD_PARTY_NOTICES.md
#: "AllWISE cutouts" fix, 2026-09-07).
WISE_BAND_WAVELENGTH_M: Final[dict[str, float]] = {
    "W1": 3.4e-6,
    "W2": 4.6e-6,
    "W3": 12e-6,
    "W4": 22e-6,
}
#: How many Atlas tiles `_wise_access_urls` keeps, nearest to the requested
#: centre first, per band. A genuinely full 6 deg box needs up to ~21
#: intersecting tiles (measured live, 2026-09-09), which at ~45 MB/~100 s
#: each -- sequential, since `_get`'s per-host lock forbids concurrent IRSA
#: requests -- can serialise to tens of minutes per band; the owner asked
#: for the full 6 deg box anyway (2026-09-09, twice), so this is no longer a
#: coverage cap, only a defensive ceiling against a degenerate SIA response
#: (comfortably above the measured need). `_fetch_wise_incremental` is what
#: makes a fetch this size livable: every tile is stored, and the row's
#: progress touched, the moment it lands, so it is observable while it
#: runs and a retry after an interruption resumes rather than restarts.
WISE_MAX_ATLAS_TILES: Final = 25

Spawn = Callable[[Coroutine[Any, Any, Any], str], "asyncio.Task[Any]"]


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


class CutoutRefusedError(ValueError):
    """The request cannot be started as asked. The message says what to change."""


class CutoutNotFoundError(LookupError):
    pass


class _OffFootprintError(Exception):
    """The archive itself answered and there is nothing here."""


class _UpstreamError(Exception):
    """An unexpected upstream failure: offline, timeout, or a bad response.

    ``status_code``/``timed_out`` are set at the raise site so
    :func:`_humanize_upstream_error` can classify the failure without
    parsing ``str(self)`` back apart — that string (e.g. "host answered
    500") is kept verbatim as the diagnostic detail, never the primary
    message a viewer reads.
    """

    def __init__(
        self, message: str, *, status_code: int | None = None, timed_out: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.timed_out = timed_out


class _NotFoundError(Exception):
    """The upstream answered 404. Callers decide whether that means
    off-footprint or missing, per source."""


def _humanize_upstream_error(label: str, exc: _UpstreamError) -> str:
    """A sentence a viewer reads, not a bare "host answered 500" — the raw
    text (``str(exc)``) is kept separately as ``CutoutState.error_detail``
    for a title/tooltip. Classified from the fields ``_UpstreamError`` was
    raised with, never by parsing the message back apart."""
    if exc.timed_out:
        return f"{label} took too long to respond — try again later."
    if exc.status_code is not None and 500 <= exc.status_code < 600:
        return f"{label} could not produce this image (server error) — try again later."
    if exc.status_code is not None:
        return f"{label} refused this request (HTTP {exc.status_code}) — try again later."
    return f"{label} could not produce this image (connection problem) — try again later."


def _angular_separation_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle separation, degrees in, degrees out."""
    p1, p2 = math.radians(dec1), math.radians(dec2)
    dphi = math.radians(dec2 - dec1)
    dlambda = math.radians(ra2 - ra1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(a))))


def release_tag(source: CutoutSource, dec_deg: float) -> str:
    """The release tag :func:`cutout_cache_key` folds in. Public (not
    ``_release_tag``) because ``ssc.skymap.bulk_prefetch`` needs the exact
    same cache key ``CutoutService.request`` computes, to classify a pair as
    already-cached without duplicating the fetch path itself."""
    if source.id == "unwise":
        return UNWISE_LAYER
    if source.id == "wise":
        return "allwise"
    if source.id == "sdss":
        return "dr17"
    if source.id == "legacy-surveys":
        return legacy_layer(dec_deg)
    raise CutoutRefusedError(f"no release tag known for source {source.id!r}")  # pragma: no cover


def cutout_cache_key(
    source_id: str, release: str, ra_deg: float, dec_deg: float, size_deg: float
) -> str:
    """``source + release tag + geometry rounded to 0.01 deg`` — the row's
    natural key; a second request for the same key is a cache hit."""
    return f"{source_id}:{release}:{ra_deg:.2f}:{dec_deg:.2f}:{size_deg:.2f}"


@dataclass(frozen=True, slots=True)
class _Piece:
    band: str | None
    format: str  # "fits" | "jpg"
    data: bytes
    image: FitsImage | None
    wcs: ImageWCS | None
    #: The piece's pixel dimensions when `image` is `None` (a quicklook JPEG
    #: has no `FitsImage` to read them from, but the request that produced it
    #: already knows them) -- `_store_pieces` prefers `image.width/height`
    #: when an image is present, these otherwise. `None` for every FITS
    #: piece (redundant with `image`) and unused if a piece is neither.
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class CutoutPieceRecord:
    """One manifest row — plain numbers, never an opaque WCS object, because
    the browser (not this process) draws it."""

    idx: int
    band: str | None
    format: str
    path: str
    bytes: int
    width: int | None
    height: int | None
    wcs: ImageWCS | None
    stretch: str | None
    display_png_path: str | None


@dataclass(frozen=True, slots=True)
class ImportPiece:
    """One piece's already-fetched bytes and manifest metadata, handed to
    :meth:`CutoutService.import_cutout` by ``ssc.skymap.export_import``.

    Distinct from :class:`_Piece`: that one carries raw upstream bytes plus
    a parsed :class:`~ssc.skymap.fits.FitsImage` for :meth:`_store_pieces`
    to render a display PNG from; an imported piece's display PNG (if any)
    was already rendered by the process that exported it, so it is carried
    here as plain bytes and written as-is — never re-decoded from FITS."""

    band: str | None
    format: str
    data: bytes
    display_png: bytes | None
    stretch: str | None
    width: int | None
    height: int | None
    wcs: ImageWCS | None


@dataclass(frozen=True, slots=True)
class _SdssField:
    run: int
    rerun: int
    camcol: int
    field: int


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _votable_rows(xml_bytes: bytes, columns: tuple[str, ...]) -> list[dict[str, str]]:
    """A minimal, tolerant VOTable row reader: the named columns, as strings.

    Not a general VOTable parser — SIA v2 discovery responses are small (a
    handful of rows) and this reads only the columns this module ever needs
    (``access_url`` plus, for AllWISE, the ``dataproduct_subtype``/
    ``access_format`` pair that tells a calibrated intensity image apart
    from the weight/uncertainty FITS and auxiliary tables SIA lists for the
    same tile), tolerant of the namespace IRSA's service uses. A column
    absent from the response is simply missing from every row's dict rather
    than raising — callers key off ``.get()``.
    """

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    table = next((el for el in root.iter() if local(el.tag) == "TABLE"), None)
    if table is None:
        return []
    field_names = [el.get("name") for el in table.iter() if local(el.tag) == "FIELD"]
    indices = {column: field_names.index(column) for column in columns if column in field_names}
    if not indices:
        return []
    rows: list[dict[str, str]] = []
    for tr in (el for el in table.iter() if local(el.tag) == "TR"):
        cells = [child.text or "" for child in tr if local(child.tag) == "TD"]
        rows.append({column: cells[idx] for column, idx in indices.items() if idx < len(cells)})
    return rows


def _sql_search_rows(data: bytes) -> list[dict[str, Any]]:
    """SkyServer's ``SqlSearch?format=json`` shape:
    ``[{"TableName": ..., "Rows": [{...}, ...]}]``."""
    try:
        parsed = json.loads(data)
    except ValueError:
        return []
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        rows = parsed[0].get("Rows")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _maybe_gunzip(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def _render_display(image: FitsImage) -> bytes | None:
    """The server-rendered display PNG (asinh, percentile-clipped) so the
    browser never parses FITS. A single plane is grayscale; exactly three
    planes (Legacy Surveys' ``grz`` cube) render as a colour composite.
    Anything else falls back to its first plane rather than guessing a
    composite that was not asked for.
    """
    if image.data.ndim == 2:
        return encode_grayscale_png(asinh_stretch(image.data))
    if image.data.ndim == 3 and image.data.shape[0] == 3:
        planes = [asinh_stretch(image.data[i]) for i in range(3)]
        rgb = np.stack(planes, axis=-1)
        return encode_rgb_png(rgb)
    if image.data.ndim == 3:
        return encode_grayscale_png(asinh_stretch(image.data[0]))
    return None  # pragma: no cover - read_fits_image refuses anything else


class CutoutService:
    """Fetch-through target-field cutouts: WISE (IRSA), SDSS (SkyServer/SAS),
    Legacy Surveys — cached on disk under ``root/<source>/<cutout_id>/``,
    indexed in SQLite (``0005_cutouts.sql``)."""

    def __init__(
        self,
        database: Database,
        root: Path,
        *,
        max_bytes: int,
        user_agent: Callable[[], str],
        plans: PlanRepository,
        http: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] = now_utc,
        autofetch_debounce_s: float = AUTOFETCH_DEBOUNCE_S,
    ) -> None:
        self._db = database
        self._root = root
        self._max_bytes = max_bytes
        self._user_agent = user_agent
        self._plans = plans
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=CUTOUT_TIMEOUT, follow_redirects=False)
        self._now = now
        self._autofetch_debounce_s = autofetch_debounce_s
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[str, asyncio.Task[Any]] = {}
        self._autofetch_task: asyncio.Task[Any] | None = None
        self._on_change: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._spawn: Spawn | None = None

    @property
    def root(self) -> Path:
        """``data_dir/cutouts`` — read by ``ssc.skymap.export_import`` to
        build archive member paths without duplicating this class's own
        on-disk layout decisions."""
        return self._root

    def attach(self, on_change: Callable[[], Coroutine[Any, Any, None]], spawn: Spawn) -> None:
        self._on_change = on_change
        self._spawn = spawn

    async def startup(self) -> None:
        """A restart marks anything still ``queued``/``running`` as
        ``interrupted`` — CLAUDE.md invariant 6, mirroring
        ``SkymapService.startup``'s treatment of prefetch jobs."""
        stamp = self._now().isoformat()
        await self._db.execute(
            "UPDATE skymap_cutouts SET status = ?, error = ?, updated_at = ? "
            "WHERE status IN ('queued', 'running')",
            (
                CutoutJobStatus.INTERRUPTED,
                "ssc-core restarted while this cutout was fetching; request it again",
                stamp,
            ),
        )
        # Migration 0015 removes the database rows. SQL cannot erase the
        # corresponding legacy files, so an upgraded commercial install
        # removes that retired source directory here as well.
        _remove_tree(self._root / "dss2-color")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- the snapshot section -------------------------------------------------- #

    async def compose(self) -> tuple[list[CutoutState], int]:
        rows = await self._db.fetch_all(
            "SELECT * FROM skymap_cutouts ORDER BY created_at DESC, id LIMIT ?",
            (CUTOUT_HISTORY,),
        )
        cutouts = [_state_of(row) for row in rows]
        total = await self._total_bytes()
        return cutouts, total

    async def _total_bytes(self) -> int:
        row = await self._db.fetch_one(
            "SELECT COALESCE(SUM(bytes), 0) AS total FROM skymap_cutouts"
        )
        return int(row["total"]) if row is not None else 0

    async def _publish(self) -> None:
        if self._on_change is not None:
            await self._on_change()

    # -- requesting a cutout ---------------------------------------------------- #

    @staticmethod
    def _resolve_bands(source: CutoutSource, bands: tuple[str, ...] | None) -> tuple[str, ...]:
        if not source.bands:
            return ()
        requested = bands if bands else source.default_bands
        unknown = [band for band in requested if band not in source.bands]
        if unknown:
            raise CutoutRefusedError(
                f"{source.id} does not have band(s) {unknown}; it has {list(source.bands)}"
            )
        return tuple(requested)

    async def request(
        self,
        source_id: str,
        ra_deg: float,
        dec_deg: float,
        size_deg: float,
        bands: tuple[str, ...] | None,
    ) -> CutoutState:
        source = find_cutout_source(source_id)
        if source is None:
            raise CutoutRefusedError(f"no cutout source {source_id!r}")
        resolved_bands = self._resolve_bands(source, bands)
        release = release_tag(source, dec_deg)
        key = cutout_cache_key(source.id, release, ra_deg, dec_deg, size_deg)
        existing = await self.row_by_key(key)
        orphaned = (
            existing is not None
            and existing.status in _IN_FLIGHT_STATUSES
            and not self._job_is_alive(existing.id)
        )
        if existing is not None and existing.status not in _RETRYABLE_STATUSES and not orphaned:
            return existing
        if self._spawn is None:
            raise RuntimeError("CutoutService.attach() was not called")
        now = self._now()
        if existing is not None:
            # A retry (LayersPanel's Retry action re-POSTs the exact same
            # request): the same row, id and cache key, reset to queued —
            # not a second INSERT, which `skymap_cutouts_cache_key`'s unique
            # index would refuse anyway. "Durable before acted on"
            # (invariant 6) holds the same way: this UPDATE lands before the
            # upstream request `_run` makes.
            cutout_id = existing.id
            if source.id != "wise":
                # Every source but AllWISE fetches in one batch and stores
                # all its pieces at the end (`_fetch` + `_store_pieces`), so
                # a retry always re-fetches everything from scratch — it has
                # no way to skip a piece already on disk the way
                # `_fetch_wise_incremental` does. Anything left in
                # `skymap_cutout_pieces` from an earlier partial attempt (an
                # orphaned `running` row, or a batch job that died partway
                # through storing) would collide with that table's own
                # primary key (``cutout_id``, ``idx``) the moment this retry
                # tries to insert piece 0 again. AllWISE's own incremental
                # pieces are deliberately left alone here — resuming instead
                # of re-fetching them is the whole point.
                await self._db.execute(
                    "DELETE FROM skymap_cutout_pieces WHERE cutout_id = ?", (cutout_id,)
                )
            await self._db.execute(
                "UPDATE skymap_cutouts SET status = ?, bytes = 0, error = NULL, "
                "error_detail = NULL, bands = ?, updated_at = ?, fetched_at = NULL, "
                "last_used = ? WHERE id = ?",
                (
                    CutoutJobStatus.QUEUED,
                    json.dumps(list(resolved_bands)) if resolved_bands else None,
                    now.isoformat(),
                    now.isoformat(),
                    cutout_id,
                ),
            )
            self._jobs[cutout_id] = self._spawn(
                self._run(cutout_id, source, ra_deg, dec_deg, size_deg, resolved_bands),
                f"cutout-{cutout_id}",
            )
            await self._publish()
            return CutoutState(
                id=cutout_id,
                source=source.id,
                ra_deg=ra_deg,
                dec_deg=dec_deg,
                size_deg=size_deg,
                status=CutoutJobStatus.QUEUED,
                bytes=0,
                error=None,
                error_detail=None,
                created_at=existing.created_at,
                updated_at=now,
            )
        cutout_id = uuid.uuid4().hex[:12]
        state = CutoutState(
            id=cutout_id,
            source=source.id,
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            size_deg=size_deg,
            status=CutoutJobStatus.QUEUED,
            bytes=0,
            error=None,
            created_at=now,
            updated_at=now,
        )
        # Durable before acted on (invariant 6): the row exists before any
        # upstream request is made.
        await self._db.execute(
            "INSERT INTO skymap_cutouts (id, cache_key, source, ra_deg, dec_deg, size_deg, "
            "bands, status, bytes, error, created_at, updated_at, fetched_at, last_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, ?)",
            (
                cutout_id,
                key,
                source.id,
                ra_deg,
                dec_deg,
                size_deg,
                json.dumps(list(resolved_bands)) if resolved_bands else None,
                state.status,
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        self._jobs[cutout_id] = self._spawn(
            self._run(cutout_id, source, ra_deg, dec_deg, size_deg, resolved_bands),
            f"cutout-{cutout_id}",
        )
        await self._publish()
        return state

    def _job_is_alive(self, cutout_id: str) -> bool:
        """Whether *this process* still has a task backing ``cutout_id``.
        A ``queued``/``running`` row with no live task here can never
        finish on its own — nothing is ever going to call `_finish` for it
        again — so `request()` uses this to tell a genuinely in-flight
        fetch from an orphaned row apart, defensively, independent of
        `startup()`'s own reaper (see `_IN_FLIGHT_STATUSES`)."""
        task = self._jobs.get(cutout_id)
        return task is not None and not task.done()

    # -- autofetch on resolve ----------------------------------------------------- #
    #
    # "Allow autofetch setting for the resolved object + offline caching
    # them" (owner). Caching is already this class's own job -- a cutout
    # `request()` starts lands in the same on-disk cache, offline-readable
    # afterwards, as any explicit "Fetch" click -- so the only new behaviour
    # is *when* a request gets made without the user clicking anything.
    #
    # **The on/off switch itself is not this module's.** It was a
    # hardcoded frontend constant before (`AUTO_FETCH_TARGET_CUTOUT`,
    # `web/src/skymap/SkyMap.tsx`, defaulted off 2026-09-09 — "disable
    # automated 6 degrees cutoffs overlay for testing"); making it a real
    # per-install *setting* means a persisted, user-toggleable value, which
    # is `ssc.settings`' model (`SettingsModel`, `SettingsStore`) — owned by
    # `backend-core`, mid-edit in `ssc/api/`, `ssc/state/` and `ssc/main.py`
    # at the time this was written, so this module does not cross into it.
    # `schedule_autofetch` below is the seam: it assumes whoever calls it —
    # wherever target resolution happens, also not `ssc/skymap`'s — has
    # already read that setting and calls this only when it is on. Default
    # off is the setting's own concern, mirroring `ObjectInfoSettings.enabled`
    # (`ssc/settings/models.py`): off by default is a product decision, not
    # one this module can infer from a licence reading.
    #
    # **The source is now a parameter too** (2026-09-10, owner: "autofetch
    # source = i agree, let's do it" — a configurable source defaulting to
    # DSS2 colour). `ssc.settings` needs to grow a place to persist which
    # source alongside the existing bool — this module's recommendation is
    # a `cutout_autofetch_source: str` setting next to
    # `cutout_autofetch_enabled`, validated against
    # `find_cutout_source(...).autofetch_allowed` the same way this
    # method's own guard does, rather than a free-standing enum backend-core
    # would have to keep in sync with `CutoutSourceId` by hand. Whoever
    # calls `schedule_autofetch` passes that setting's value straight
    # through as `source_id`; leaving it unset falls back to
    # `AUTOFETCH_SOURCE_ID`.
    def schedule_autofetch(
        self, ra_deg: float, dec_deg: float, source_id: str = AUTOFETCH_SOURCE_ID
    ) -> None:
        """Debounced trigger for one autofetch, at ``AUTOFETCH_SIZE_DEG`` — a
        single untiled request, deliberately smaller than the old 6-degree
        unWISE default: autofetch is meant to be the cheap, ambient case,
        not the wide-context fetch a user asks for explicitly.

        ``source_id`` defaults to ``AUTOFETCH_SOURCE_ID`` but is a real parameter, not a fixed
        choice — the eligibility guard is what keeps this safe, not the
        absence of a parameter. Only a source with
        ``CutoutSource.autofetch_allowed`` set may be named here: one whose
        own cutout logic satisfies its terms with a single bounded
        per-target request, never one that would tile a footprint into a
        sweep (AllWISE), and never one that is bundled or default-only.
        Checked *before* the debounce, synchronously — a caller naming an
        ineligible or unknown source finds out immediately, not silently,
        three lines into a background task nobody is awaiting
        (CLAUDE.md invariant 7).

        **Debounced, not queued**: a caller invoking this once per resolved
        target — including once per keystroke of a target search that
        re-resolves on every character — must not start a fetch per
        keystroke. Each call cancels whatever previous call is still
        *waiting out its debounce window* and replaces it; only the last
        target in a fast sequence, once nothing newer has arrived for
        ``AUTOFETCH_DEBOUNCE_S``, actually reaches :meth:`request`. This is
        "last resolve wins", not a per-keystroke queue and not a per-host
        rate limiter — the one-in-flight-per-host lock in :meth:`_get`
        already handles etiquette for whatever request does go out.

        **Cancellation stops there — it never reaches a fetch that has
        begun** (2026-09-10 incident: it used to, and a cancellation
        landing between :meth:`request`'s durable row write and the line
        that spawns the task to fill it left a `queued` row nothing would
        ever finish). The debounce sleep is genuinely cheap to redo per
        keystroke; a fetch that already reached :meth:`request` is not
        cheap to discard and is nearly free to let finish instead — it
        populates the cache for a target the user may well return to, and
        letting it run removes this failure class entirely rather than
        needing a terminal state written on every possible cancellation
        point inside :meth:`request`. :meth:`_run_autofetch` shields the
        call once the debounce has elapsed for exactly this reason.

        Idempotent beyond that: :meth:`request` itself is a cache-key
        lookup first, so firing this again for a target already fetched
        (or already in flight) costs nothing beyond the lookup.
        """
        if self._spawn is None:
            raise RuntimeError("CutoutService.attach() was not called")
        source = find_cutout_source(source_id)
        if source is None:
            raise CutoutRefusedError(f"no cutout source {source_id!r}")
        if not source.autofetch_allowed:
            raise CutoutRefusedError(
                f"{source.id.value} cannot be autofetched — it is not a single "
                "bounded per-target request, or is bundled/default-only"
            )
        if self._autofetch_task is not None and not self._autofetch_task.done():
            self._autofetch_task.cancel()
        self._autofetch_task = self._spawn(
            self._run_autofetch(ra_deg, dec_deg, source.id.value), "cutout-autofetch"
        )

    async def _run_autofetch(self, ra_deg: float, dec_deg: float, source_id: str) -> None:
        try:
            await asyncio.sleep(self._autofetch_debounce_s)
        except asyncio.CancelledError:
            # Superseded by a newer `schedule_autofetch` call, or the
            # service shut down — either way, not this target's turn, and
            # nothing has been touched yet.
            return
        # Past the debounce, this fetch has committed: `request()` writes a
        # durable row and only then spawns the task that will ever move it
        # to a terminal state (invariant 6). A newer `schedule_autofetch`
        # call cancelling *this* wrapper task partway through that sequence
        # used to abandon a `queued`/`running` row nothing would ever
        # finish — `asyncio.shield` keeps `request()` running to completion
        # regardless of what happens to this wrapper, the same discipline
        # `bulk_prefetch.py` and `service.py` already give their own
        # cancelled jobs. `schedule_autofetch`'s own `.cancel()` can now
        # only ever land on the `sleep` above.
        await asyncio.shield(self.request(source_id, ra_deg, dec_deg, AUTOFETCH_SIZE_DEG, None))

    async def row_by_key(self, cache_key: str) -> CutoutState | None:
        """The cutout already keyed by ``cache_key`` (any status), or
        ``None``. Public — ``ssc.skymap.bulk_prefetch`` uses this to
        classify a pair as already-resolved before deciding whether it has
        anything to wait for, without duplicating :meth:`request`."""
        row = await self._db.fetch_one(
            "SELECT * FROM skymap_cutouts WHERE cache_key = ?", (cache_key,)
        )
        return None if row is None else _state_of(row)

    async def wait_for(self, cutout_id: str) -> CutoutState:
        """Block until the job :meth:`request` started for ``cutout_id``
        reaches a terminal status; returns immediately if it already has (a
        cache hit, or a job from before this process's lifetime — always
        already terminal, since :meth:`startup` marks anything still
        running as interrupted). Used by the bulk prefetch queue so it can
        dispatch one pair at a time without re-implementing ``_run``."""
        task = self._jobs.get(cutout_id)
        if task is not None:
            await asyncio.wait([task])
        return await self.get_state(cutout_id)

    async def _run(
        self,
        cutout_id: str,
        source: CutoutSource,
        ra_deg: float,
        dec_deg: float,
        size_deg: float,
        bands: tuple[str, ...],
    ) -> None:
        await self._set_status(cutout_id, CutoutJobStatus.RUNNING)
        await self._publish()
        try:
            if source.id == "wise":
                # AllWISE alone is tiled and persisted incrementally
                # (`_fetch_wise_incremental`): a 6 deg box can need up to
                # ~21 Atlas tiles per band at ~45 MB/~100 s each (measured
                # live, 2026-09-09) over IRSA's one-request-per-host lock,
                # so every tile is written to disk and its row committed
                # the moment it lands rather than batched at the end —
                # invariant 6, and the only way an interruption keeps
                # finished tiles instead of discarding the whole mosaic.
                total = await self._fetch_wise_incremental(
                    cutout_id, ra_deg, dec_deg, size_deg, bands
                )
            else:
                pieces = await self._fetch(source, ra_deg, dec_deg, size_deg, bands)
                total = await self._store_pieces(cutout_id, source.id, pieces)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(
                    cutout_id,
                    CutoutJobStatus.INTERRUPTED,
                    await self._stored_bytes(cutout_id),
                    "ssc-core shut down while this cutout was fetching; request it again",
                )
            )
            raise
        except _OffFootprintError as exc:
            await self._finish(
                cutout_id,
                CutoutJobStatus.UNAVAILABLE,
                await self._stored_bytes(cutout_id),
                str(exc),
            )
            return
        except _UpstreamError as exc:
            _log.warning("cutout %s (%s) failed: %s", cutout_id, source.id, exc)
            await self._finish(
                cutout_id,
                CutoutJobStatus.FAILED,
                await self._stored_bytes(cutout_id),
                _humanize_upstream_error(source.label, exc),
                error_detail=str(exc),
            )
            return
        except Exception as exc:  # CLAUDE.md invariant 7: never leak a task silently
            # Anything else -- a disk write failure, a bug in stretch/WCS
            # handling, a DB constraint violation while storing a piece --
            # used to propagate straight out of this spawned task: nothing
            # in `_run` caught it, so `TaskRegistry` logged it and moved on,
            # but no one ever touched this row again. That is the exact
            # shape a "stuck running forever" cutout takes -- `updated_at`
            # frozen at the `_set_status(RUNNING)` write above, invisible to
            # `startup()`'s reaper because the reaper only runs at process
            # start, not while a live task is silently dying (2026-09-09
            # incident: three cutout rows near M31 never progressed past
            # that write). Whatever pieces did land stay on disk and in the
            # manifest; only the row's own status/error move.
            _log.exception("cutout %s (%s) failed unexpectedly", cutout_id, source.id)
            await self._finish(
                cutout_id,
                CutoutJobStatus.FAILED,
                await self._stored_bytes(cutout_id),
                f"{source.label}: unexpected error while fetching — try again.",
                error_detail=str(exc),
            )
            return
        await self._finish(cutout_id, CutoutJobStatus.COMPLETE, total, None)
        await self._enforce_budget()

    async def _set_status(self, cutout_id: str, status: CutoutJobStatus) -> None:
        stamp = self._now().isoformat()
        await self._db.execute(
            "UPDATE skymap_cutouts SET status = ?, updated_at = ? WHERE id = ?",
            (status, stamp, cutout_id),
        )

    async def _stored_bytes(self, cutout_id: str) -> int:
        """Bytes already written to ``skymap_cutout_pieces`` for this row --
        0 for a source that only ever stores its pieces in one batch at the
        end (nothing lands until `_fetch` returns, so a mid-fetch failure
        there always finds none), the real partial total for AllWISE's
        incremental fetch, whose tiles are already on disk and in the
        manifest by the time an interruption or an unexpected failure is
        handled. Used instead of a bare ``0`` in `_run`'s failure paths so
        an interrupted/failed row never claims to have lost work it still
        has."""
        row = await self._db.fetch_one(
            "SELECT COALESCE(SUM(bytes), 0) AS total FROM skymap_cutout_pieces WHERE cutout_id = ?",
            (cutout_id,),
        )
        return int(row["total"]) if row is not None else 0

    async def _finish(
        self,
        cutout_id: str,
        status: CutoutJobStatus,
        size: int,
        error: str | None,
        *,
        error_detail: str | None = None,
    ) -> None:
        stamp = self._now().isoformat()
        await self._db.execute(
            "UPDATE skymap_cutouts SET status = ?, bytes = ?, error = ?, error_detail = ?, "
            "updated_at = ?, fetched_at = ? WHERE id = ?",
            (
                status,
                size,
                error,
                error_detail,
                stamp,
                stamp if status == CutoutJobStatus.COMPLETE else None,
                cutout_id,
            ),
        )
        await self._publish()

    # -- manifest / pieces / delete / list --------------------------------------- #

    async def get_state(self, cutout_id: str) -> CutoutState:
        row = await self._db.fetch_one("SELECT * FROM skymap_cutouts WHERE id = ?", (cutout_id,))
        if row is None:
            raise CutoutNotFoundError(f"no cutout {cutout_id!r}")
        return _state_of(row)

    async def get_manifest(self, cutout_id: str) -> tuple[CutoutState, list[CutoutPieceRecord]]:
        state = await self.get_state(cutout_id)
        await self._touch(cutout_id)
        rows = await self._db.fetch_all(
            "SELECT * FROM skymap_cutout_pieces WHERE cutout_id = ? ORDER BY idx", (cutout_id,)
        )
        return state, [_piece_of(row) for row in rows]

    async def get_piece_bytes(self, cutout_id: str, idx: int, ext: str) -> tuple[bytes, str]:
        row = await self._db.fetch_one(
            "SELECT * FROM skymap_cutout_pieces WHERE cutout_id = ? AND idx = ?", (cutout_id, idx)
        )
        if row is None:
            raise CutoutNotFoundError(f"cutout {cutout_id!r} has no piece {idx}")
        piece = _piece_of(row)
        if ext == "png":
            if piece.display_png_path is None:
                raise CutoutNotFoundError(f"piece {idx} of cutout {cutout_id!r} has no display PNG")
            path = self._root / piece.display_png_path
            media = "image/png"
        elif ext == piece.format:
            path = self._root / piece.path
            media = {"fits": "application/fits", "jpg": "image/jpeg"}[piece.format]
        else:
            raise CutoutNotFoundError(f"piece {idx} is .{piece.format}, not .{ext}")
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise CutoutNotFoundError(
                f"piece {idx} of cutout {cutout_id!r} is not on disk"
            ) from exc
        await self._touch(cutout_id)
        return data, media

    async def _touch(self, cutout_id: str) -> None:
        await self._db.execute(
            "UPDATE skymap_cutouts SET last_used = ? WHERE id = ?",
            (self._now().isoformat(), cutout_id),
        )

    async def delete(self, cutout_id: str) -> None:
        """The supported recovery path for a cutout in any state, including
        a poisoned ``running`` row that will never finish on its own
        (2026-09-09: the only other way to clear one was restarting
        ssc-core, and restarting the owner's own instance is never an
        agent's or an operator's call to make). If a fetch is still live for
        this id, it is cancelled and awaited *before* either DELETE: `_run`'s
        own ``CancelledError`` handler would otherwise try to write a piece
        row for a ``cutout_id`` these two statements are about to remove --
        ``skymap_cutout_pieces.cutout_id`` is a foreign key to
        ``skymap_cutouts.id`` with ``foreign_keys=ON``, so that write would
        raise, not silently do nothing."""
        row = await self._db.fetch_one(
            "SELECT source FROM skymap_cutouts WHERE id = ?", (cutout_id,)
        )
        if row is None:
            raise CutoutNotFoundError(f"no cutout {cutout_id!r}")
        task = self._jobs.pop(cutout_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._db.execute("DELETE FROM skymap_cutout_pieces WHERE cutout_id = ?", (cutout_id,))
        await self._db.execute("DELETE FROM skymap_cutouts WHERE id = ?", (cutout_id,))
        await asyncio.to_thread(_remove_tree, self._root / str(row["source"]) / cutout_id)
        await self._publish()

    async def delete_all(self) -> int:
        """Every cached target-field image, unconditionally — including one
        a saved plan's target protects from ordinary LRU eviction. That
        protection is against an automatic reclaim silently discarding
        something a plan still points at; it is not a reason to refuse an
        explicit "delete everything" a viewer asked for by name. Returns
        the number of rows removed, for the confirm dialog's own record."""
        row = await self._db.fetch_one("SELECT COUNT(*) AS n FROM skymap_cutouts")
        count = int(row["n"]) if row is not None else 0
        await self._db.execute("DELETE FROM skymap_cutout_pieces")
        await self._db.execute("DELETE FROM skymap_cutouts")
        await asyncio.to_thread(_remove_tree, self._root)
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        await self._publish()
        return count

    async def import_cutout(
        self,
        *,
        cache_key: str,
        source_id: str,
        ra_deg: float,
        dec_deg: float,
        size_deg: float,
        bands: tuple[str, ...],
        pieces: list[ImportPiece],
        publish: bool = True,
    ) -> str | None:
        """Persist an already-fetched cutout from an export archive
        (``ssc.skymap.export_import``) exactly as if it had just completed a
        live fetch: the row is written first, ``queued``, before any piece
        file (CLAUDE.md invariant 6), then each piece is written file-then-row
        in :meth:`_store_pieces`'s own order, then the row is finished
        ``complete`` — the same sequence :meth:`request` and :meth:`_run`
        produce for a real fetch, minus the network call. Returns the new
        cutout id, or ``None`` when ``cache_key`` is already cached locally:
        imported data never overwrites a fresher local fetch, so the caller
        counts that as skipped and leaves the local copy untouched.

        ``publish`` defaults on for a single import; a bulk import loop
        passes ``False`` and publishes once itself, so restoring thousands
        of cutouts does not flood the WS with one whole-section event per
        row (the module docstring's "one section, one publisher" still
        holds — this only changes how often, not who)."""
        existing = await self.row_by_key(cache_key)
        if existing is not None:
            return None
        cutout_id = uuid.uuid4().hex[:12]
        now = self._now().isoformat()
        # Durable before acted on (invariant 6): the row exists before any
        # piece file is written, exactly as `request()` does for a live one.
        await self._db.execute(
            "INSERT INTO skymap_cutouts (id, cache_key, source, ra_deg, dec_deg, size_deg, "
            "bands, status, bytes, error, created_at, updated_at, fetched_at, last_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, ?)",
            (
                cutout_id,
                cache_key,
                source_id,
                ra_deg,
                dec_deg,
                size_deg,
                json.dumps(list(bands)) if bands else None,
                CutoutJobStatus.QUEUED,
                now,
                now,
                now,
            ),
        )
        total = 0
        directory = self._root / source_id / cutout_id
        for idx, piece in enumerate(pieces):
            raw_path = directory / f"piece{idx}.{piece.format}"
            await asyncio.to_thread(_write_atomically, raw_path, piece.data)
            total += len(piece.data)
            display_rel: str | None = None
            if piece.display_png is not None:
                display_path = directory / f"piece{idx}.png"
                await asyncio.to_thread(_write_atomically, display_path, piece.display_png)
                total += len(piece.display_png)
                display_rel = display_path.relative_to(self._root).as_posix()
            wcs = piece.wcs
            await self._db.execute(
                "INSERT INTO skymap_cutout_pieces (cutout_id, idx, band, format, path, bytes, "
                "width, height, crpix1, crpix2, crval1, crval2, cd1_1, cd1_2, cd2_1, cd2_2, "
                "ctype1, ctype2, stretch, display_png_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cutout_id,
                    idx,
                    piece.band,
                    piece.format,
                    raw_path.relative_to(self._root).as_posix(),
                    len(piece.data),
                    piece.width,
                    piece.height,
                    None if wcs is None else wcs.crpix1,
                    None if wcs is None else wcs.crpix2,
                    None if wcs is None else wcs.crval1,
                    None if wcs is None else wcs.crval2,
                    None if wcs is None else wcs.cd1_1,
                    None if wcs is None else wcs.cd1_2,
                    None if wcs is None else wcs.cd2_1,
                    None if wcs is None else wcs.cd2_2,
                    None if wcs is None else wcs.ctype1,
                    None if wcs is None else wcs.ctype2,
                    piece.stretch,
                    display_rel,
                ),
            )
        stamp = self._now().isoformat()
        await self._db.execute(
            "UPDATE skymap_cutouts SET status = ?, bytes = ?, updated_at = ?, fetched_at = ? "
            "WHERE id = ?",
            (CutoutJobStatus.COMPLETE, total, stamp, stamp, cutout_id),
        )
        if publish:
            await self._publish()
        return cutout_id

    async def list_covering(
        self, ra_deg: float, dec_deg: float, radius_deg: float
    ) -> list[CutoutState]:
        # A coarse bounding-box pre-filter (a cutout's own footprint can be up
        # to 3 deg across); refined below with a real angular separation.
        pad = radius_deg + 3.0
        rows = await self._db.fetch_all(
            "SELECT * FROM skymap_cutouts WHERE status = 'complete' AND "
            "ra_deg BETWEEN ? AND ? AND dec_deg BETWEEN ? AND ? ORDER BY updated_at DESC",
            (ra_deg - pad, ra_deg + pad, dec_deg - pad, dec_deg + pad),
        )
        out: list[CutoutState] = []
        for row in rows:
            state = _state_of(row)
            if _angular_separation_deg(ra_deg, dec_deg, state.ra_deg, state.dec_deg) <= radius_deg:
                out.append(state)
        return out

    async def list_complete_source(self, source: str) -> list[CutoutState]:
        """Every completed cached field for one source, newest first."""
        rows = await self._db.fetch_all(
            "SELECT * FROM skymap_cutouts WHERE status = 'complete' AND source = ? "
            "ORDER BY updated_at DESC",
            (source,),
        )
        return [_state_of(row) for row in rows]

    # -- eviction ------------------------------------------------------------ #

    async def _enforce_budget(self) -> None:
        if self._max_bytes <= 0:
            return
        total = await self._total_bytes()
        if total <= self._max_bytes:
            return
        protected = await self._plans.target_positions()
        rows = await self._db.fetch_all(
            "SELECT id, source, ra_deg, dec_deg, bytes FROM skymap_cutouts "
            "WHERE status = 'complete' ORDER BY last_used ASC"
        )
        freed = 0
        for row in rows:
            if total - freed <= self._max_bytes:
                break
            ra_deg, dec_deg = float(row["ra_deg"]), float(row["dec_deg"])
            if any(
                _angular_separation_deg(ra_deg, dec_deg, pra, pdec) <= PLAN_PROTECTION_RADIUS_DEG
                for pra, pdec in protected
            ):
                continue
            cutout_id = str(row["id"])
            await self._db.execute(
                "DELETE FROM skymap_cutout_pieces WHERE cutout_id = ?", (cutout_id,)
            )
            await self._db.execute("DELETE FROM skymap_cutouts WHERE id = ?", (cutout_id,))
            await asyncio.to_thread(_remove_tree, self._root / str(row["source"]) / cutout_id)
            freed += int(row["bytes"])
        if freed:
            _log.info("cutout cache over %d bytes: evicted %d bytes", self._max_bytes, freed)
            await self._publish()

    # -- storage --------------------------------------------------------------- #

    async def _store_piece(
        self,
        cutout_id: str,
        source_id: str,
        idx: int,
        piece: _Piece,
        *,
        source_url: str | None = None,
    ) -> int:
        """Write one piece's file(s) and its manifest row. ``source_url`` is
        the upstream URL it came from, when a source fetches many discrete
        files for one cutout (AllWISE's Atlas tiles) rather than one file
        per band -- read back by `_fetch_wise_incremental` on a retry to
        skip a tile already on disk (migration 0009)."""
        directory = self._root / source_id / cutout_id
        raw_path = directory / f"piece{idx}.{piece.format}"
        await asyncio.to_thread(_write_atomically, raw_path, piece.data)
        total = len(piece.data)
        stretch: str | None = None
        display_rel: str | None = None
        width = piece.image.width if piece.image is not None else piece.width
        height = piece.image.height if piece.image is not None else piece.height
        if piece.image is not None:
            display = _render_display(piece.image)
            if display is not None:
                display_path = directory / f"piece{idx}.png"
                await asyncio.to_thread(_write_atomically, display_path, display)
                total += len(display)
                stretch = describe_stretch()
                display_rel = display_path.relative_to(self._root).as_posix()
        wcs = piece.wcs
        await self._db.execute(
            "INSERT INTO skymap_cutout_pieces (cutout_id, idx, band, format, path, bytes, "
            "width, height, crpix1, crpix2, crval1, crval2, cd1_1, cd1_2, cd2_1, cd2_2, "
            "ctype1, ctype2, stretch, display_png_path, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cutout_id,
                idx,
                piece.band,
                piece.format,
                raw_path.relative_to(self._root).as_posix(),
                len(piece.data),
                width,
                height,
                None if wcs is None else wcs.crpix1,
                None if wcs is None else wcs.crpix2,
                None if wcs is None else wcs.crval1,
                None if wcs is None else wcs.crval2,
                None if wcs is None else wcs.cd1_1,
                None if wcs is None else wcs.cd1_2,
                None if wcs is None else wcs.cd2_1,
                None if wcs is None else wcs.cd2_2,
                None if wcs is None else wcs.ctype1,
                None if wcs is None else wcs.ctype2,
                stretch,
                display_rel,
                source_url,
            ),
        )
        return total

    async def _store_pieces(self, cutout_id: str, source_id: str, pieces: list[_Piece]) -> int:
        total = 0
        for idx, piece in enumerate(pieces):
            total += await self._store_piece(cutout_id, source_id, idx, piece)
        return total

    async def _fetched_source_urls(self, cutout_id: str) -> set[str]:
        """Every upstream URL already stored for this cutout -- what
        `_fetch_wise_incremental` skips on a retry so a resumed fetch never
        re-downloads a tile IRSA already served once."""
        rows = await self._db.fetch_all(
            "SELECT source_url FROM skymap_cutout_pieces "
            "WHERE cutout_id = ? AND source_url IS NOT NULL",
            (cutout_id,),
        )
        return {str(row["source_url"]) for row in rows}

    async def _next_piece_idx(self, cutout_id: str) -> int:
        """One past the highest ``idx`` already stored for this cutout, so a
        resumed fetch appends new tiles rather than colliding with
        (``cutout_id``, ``idx``)'s primary key."""
        row = await self._db.fetch_one(
            "SELECT COALESCE(MAX(idx), -1) AS max_idx FROM skymap_cutout_pieces "
            "WHERE cutout_id = ?",
            (cutout_id,),
        )
        return int(row["max_idx"]) + 1 if row is not None else 0

    async def _touch_progress(self, cutout_id: str) -> None:
        """Reflects a tile that just landed in the row's own ``bytes`` and
        ``updated_at``, then publishes -- so a client polling the manifest
        mid-fetch sees real, growing progress rather than a silent wait for
        up to tens of minutes (THIRD_PARTY_NOTICES.md "good citizen" reading;
        CLAUDE.md invariant 7)."""
        total = await self._stored_bytes(cutout_id)
        await self._db.execute(
            "UPDATE skymap_cutouts SET bytes = ?, updated_at = ? WHERE id = ?",
            (total, self._now().isoformat(), cutout_id),
        )
        await self._publish()

    # -- fetching ---------------------------------------------------------------- #

    async def _fetch(
        self,
        source: CutoutSource,
        ra_deg: float,
        dec_deg: float,
        size_deg: float,
        bands: tuple[str, ...],
    ) -> list[_Piece]:
        # "wise" is not dispatched here: `_run` calls
        # `_fetch_wise_incremental` for it directly, since AllWISE's tiles
        # are stored (and the row's progress touched) one at a time as they
        # land, not batched into one `list[_Piece]` this method could
        # return -- the other three sources fetch quickly enough that the
        # batch-then-store shape below is still the simpler, correct one.
        if source.id == "unwise":
            return await self._fetch_unwise(ra_deg, dec_deg, size_deg)
        if source.id == "sdss":
            return await self._fetch_sdss(ra_deg, dec_deg, size_deg, bands)
        if source.id == "legacy-surveys":
            return await self._fetch_legacy_surveys(ra_deg, dec_deg, size_deg)
        raise CutoutRefusedError(f"no fetcher for source {source.id!r}")  # pragma: no cover

    async def _get(
        self,
        url: str,
        *,
        host: str,
        params: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,  # noqa: ASYNC109 - per-call budget, SDSS's getjpeg needs 90s
    ) -> bytes:
        actual_host = urlsplit(url).hostname
        if actual_host != host:
            raise _UpstreamError(
                f"refusing to fetch from {actual_host!r}; {host!r} is the only host "
                "cleared for this source (THIRD_PARTY_NOTICES.md endpoint gate)"
            )
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        delay = _RETRY_BASE_DELAY_S
        async with lock:
            for attempt in range(_RETRY_ATTEMPTS):
                try:
                    response = await self._http.get(
                        url,
                        params=params,
                        headers={"User-Agent": self._user_agent()},
                        timeout=timeout or CUTOUT_TIMEOUT,
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    if attempt == _RETRY_ATTEMPTS - 1:
                        raise _OffFootprintError(f"cannot reach {host}: {exc}") from exc
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                except httpx.TimeoutException as exc:
                    raise _UpstreamError(
                        f"{host} did not answer in time: {exc}", timed_out=True
                    ) from exc
                except httpx.HTTPError as exc:
                    raise _UpstreamError(f"{host}: {exc}") from exc
                if response.status_code == 404:
                    raise _NotFoundError(response.content)
                if response.status_code != 200:
                    raise _UpstreamError(
                        f"{host} answered {response.status_code}", status_code=response.status_code
                    )
                return response.content
        raise AssertionError("unreachable")  # pragma: no cover - loop always returns or raises

    # -- WISE (AllWISE via IRSA) -------------------------------------------------- #

    async def _wise_access_urls(
        self, ra_deg: float, dec_deg: float, size_deg: float, band: str
    ) -> list[str]:
        # SIA v2's real circle-region syntax is `CIRCLE <ra> <dec> <radius>`,
        # space-separated -- a bare "ra,dec" pair (no shape keyword, no
        # radius) is rejected as a malformed POS, and BAND is a wavelength in
        # metres, not a bandpass name (THIRD_PARTY_NOTICES.md "AllWISE cutouts"
        # fix, verified live 2026-09-07; see WISE_BAND_WAVELENGTH_M).
        radius_deg = max(size_deg / 2.0, 0.01)
        params = {
            "COLLECTION": "wise_allwise",
            "POS": f"CIRCLE {ra_deg} {dec_deg} {radius_deg}",
            "BAND": str(WISE_BAND_WAVELENGTH_M[band]),
        }
        data = await self._get(
            "https://irsa.ipac.caltech.edu/SIA", host="irsa.ipac.caltech.edu", params=params
        )
        rows = _votable_rows(
            data, ("access_url", "dataproduct_subtype", "access_format", "s_ra", "s_dec")
        )
        # SIA lists the calibrated intensity image alongside a weight FITS,
        # an uncertainty FITS and several auxiliary tables for the very same
        # tile; only the intensity image is a cutout this module can read.
        science_rows = [
            row
            for row in rows
            if row.get("dataproduct_subtype") == "science"
            and row.get("access_format") == "image/fits"
            and "access_url" in row
        ]

        # SIA does not return rows in distance order (verified live,
        # 2026-09-09: an unsorted `WISE_MAX_ATLAS_TILES` slice for M31 picked
        # four Atlas tiles 2-3.7 deg from the requested centre and none
        # covering it at all -- a real cutout of the wrong patch of sky,
        # exactly the "misprojected is worse than missing" failure mode).
        # `s_ra`/`s_dec` (each tile's own centre, standard SIA v2 columns)
        # let this sort nearest-first before truncating, so a capped result
        # is the tiles that actually cover the target rather than whichever
        # happened to sort first in IRSA's own response.
        def distance(row: dict[str, str]) -> float:
            try:
                return _angular_separation_deg(
                    ra_deg, dec_deg, float(row["s_ra"]), float(row["s_dec"])
                )
            except (KeyError, ValueError):
                return math.inf  # pragma: no cover - s_ra/s_dec always present live

        science_rows.sort(key=distance)
        # A 6 deg box can genuinely need every one of its ~21 intersecting
        # tiles (measured live, 2026-09-09); `WISE_MAX_ATLAS_TILES` is now
        # only a defensive ceiling, not a coverage cap (see its own comment)
        # -- what makes fetching all of them livable is
        # `_fetch_wise_incremental` storing each one as it lands rather
        # than holding the whole mosaic in memory.
        return [row["access_url"] for row in science_rows[:WISE_MAX_ATLAS_TILES]]

    async def _fetch_wise_incremental(
        self,
        cutout_id: str,
        ra_deg: float,
        dec_deg: float,
        size_deg: float,
        bands: tuple[str, ...],
    ) -> int:
        """AllWISE's own fetch, restructured to persist (and publish) each
        Atlas tile the moment it lands rather than accumulating the whole
        mosaic in memory and writing it out at the end -- the only way a 6
        deg box's up to ~21 tiles/band (measured live, 2026-09-09: ~45 MB
        and ~100 s each, serialised behind IRSA's one-request-per-host lock)
        can be observed while it runs, interrupted without losing finished
        work, and resumed rather than restarted by the next retry
        (CLAUDE.md invariant 6; "good citizen of the upstream" -- never
        re-fetch a tile already on disk). Returns the total bytes stored
        for this cutout so far (this call's tiles plus any from an earlier,
        interrupted attempt)."""
        already = await self._fetched_source_urls(cutout_id)
        next_idx = await self._next_piece_idx(cutout_id)
        for band in bands:
            access_urls = await self._wise_access_urls(ra_deg, dec_deg, size_deg, band)
            for access_url in access_urls:
                if access_url in already:
                    continue  # a previous, interrupted attempt already has this one
                sep = "&" if "?" in access_url else "?"
                cutout_url = (
                    f"{access_url}{sep}center={ra_deg},{dec_deg}&size={size_deg}deg&gzip=true"
                )
                try:
                    raw = await self._get(cutout_url, host="irsa.ipac.caltech.edu")
                except _NotFoundError:
                    continue
                data = _maybe_gunzip(raw)
                try:
                    image = read_fits_image(data)
                    wcs = extract_wcs(image.header)
                except FitsError as exc:
                    raise _UpstreamError(
                        f"IRSA IBE cutout is not a usable FITS image: {exc}"
                    ) from exc
                piece = _Piece(band=band, format="fits", data=data, image=image, wcs=wcs)
                await self._store_piece(cutout_id, "wise", next_idx, piece, source_url=access_url)
                already.add(access_url)
                next_idx += 1
                await self._touch_progress(cutout_id)
        if not already:
            raise _OffFootprintError(
                f"AllWISE has no Atlas tile at ra={ra_deg:.4f} dec={dec_deg:.4f}"
            )
        return await self._stored_bytes(cutout_id)

    # -- SDSS (SkyServer field lookup + SAS frames, getjpeg fallback) ------------- #

    async def _sdss_field_lookup(self, ra_deg: float, dec_deg: float) -> _SdssField | None:
        pad = 0.2
        sql = (
            "SELECT TOP 1 run, rerun, camcol, field FROM Field WHERE "
            f"ra BETWEEN {ra_deg - pad:.6f} AND {ra_deg + pad:.6f} AND "
            f"dec BETWEEN {dec_deg - pad:.6f} AND {dec_deg + pad:.6f} "
            f"ORDER BY (ra - {ra_deg:.6f}) * (ra - {ra_deg:.6f}) + "
            f"(dec - {dec_deg:.6f}) * (dec - {dec_deg:.6f}) ASC"
        )
        try:
            data = await self._get(
                "https://skyserver.sdss.org/dr17/SkyServerWS/SearchTools/SqlSearch",
                host="skyserver.sdss.org",
                params={"cmd": sql, "format": "json"},
            )
        except _OffFootprintError:
            # SkyServer unreachable: unavailable with reason, no retry storm
            # (THIRD_PARTY_NOTICES.md endpoint gate, SDSS getjpeg row — applied
            # to the field lookup too, since it is the same host).
            raise
        rows = _sql_search_rows(data)
        if not rows:
            return None
        row = rows[0]
        return _SdssField(
            run=int(row["run"]),
            rerun=int(row.get("rerun", 301)),
            camcol=int(row["camcol"]),
            field=int(row["field"]),
        )

    async def _sdss_quicklook(self, ra_deg: float, dec_deg: float, size_deg: float) -> list[_Piece]:
        width = min(SDSS_CUTOUT.max_pixels, 512)
        scale = max(size_deg * 3600.0 / width, 0.01)
        params = {
            "ra": f"{ra_deg}",
            "dec": f"{dec_deg}",
            "scale": f"{scale}",
            "width": str(width),
            "height": str(width),
        }
        try:
            data = await self._get(
                "https://skyserver.sdss.org/dr17/SkyServerWS/ImgCutout/getjpeg",
                host="skyserver.sdss.org",
                params=params,
                timeout=SDSS_QUICKLOOK_TIMEOUT,
            )
        except _NotFoundError as exc:
            # HTTP 404-with-JPEG = off-footprint; keyed on status, the body
            # (if any) is not trusted (THIRD_PARTY_NOTICES.md endpoint gate).
            raise _OffFootprintError(
                f"SDSS has no imagery at ra={ra_deg:.4f} dec={dec_deg:.4f} (off-footprint)"
            ) from exc
        wcs = ImageWCS(
            crpix1=width / 2 + 0.5,
            crpix2=width / 2 + 0.5,
            crval1=ra_deg,
            crval2=dec_deg,
            cd1_1=-scale / 3600.0,
            cd1_2=0.0,
            cd2_1=0.0,
            cd2_2=scale / 3600.0,
            ctype1="RA---TAN",
            ctype2="DEC--TAN",
        )
        return [
            _Piece(
                band=None, format="jpg", data=data, image=None, wcs=wcs, width=width, height=width
            )
        ]

    async def _fetch_sdss(
        self, ra_deg: float, dec_deg: float, size_deg: float, bands: tuple[str, ...]
    ) -> list[_Piece]:
        field = await self._sdss_field_lookup(ra_deg, dec_deg)
        if field is None:
            # Off-footprint at the SQL step: getjpeg is the fallback, never
            # the only route (THIRD_PARTY_NOTICES.md endpoint gate).
            return await self._sdss_quicklook(ra_deg, dec_deg, size_deg)
        pieces: list[_Piece] = []
        for band in bands:
            url = (
                "https://data.sdss.org/sas/dr17/eboss/photoObj/frames/"
                f"{field.rerun}/{field.run}/{field.camcol}/"
                f"frame-{band}-{field.run:06d}-{field.camcol}-{field.field:04d}.fits.bz2"
            )
            try:
                raw = await self._get(url, host="data.sdss.org")
            except _NotFoundError:
                continue
            try:
                data = bz2.decompress(raw)
                image = read_fits_image(data)
                wcs = extract_wcs(image.header)
            except (OSError, FitsError) as exc:
                raise _UpstreamError(f"SDSS SAS frame {band} is not usable: {exc}") from exc
            pieces.append(_Piece(band=band, format="fits", data=data, image=image, wcs=wcs))
        if not pieces:
            return await self._sdss_quicklook(ra_deg, dec_deg, size_deg)
        return pieces

    # -- Legacy Surveys + unWISE (same host, same fits-cutout/jpeg-cutout) ------- #
    #
    # unWISE reaches the map through Legacy Surveys' own `unwise-neo7` layer
    # (THIRD_PARTY_NOTICES.md "unWISE resolved, 2026-09-07") -- same host, same
    # two endpoints Legacy Surveys' own optical layers already use, just a
    # different `layer=` value and no north/south DR9/DR10 split (unWISE is
    # one all-sky release; `legacy_layer(dec_deg)`'s hemisphere logic is a
    # Legacy Surveys-specific fact and does not apply to it). The fetch and
    # fallback mechanics below are shared; only the layer name, whether a
    # `bands` value is meaningful, and the off-footprint wording differ.

    async def _fetch_legacy_surveys(
        self, ra_deg: float, dec_deg: float, size_deg: float
    ) -> list[_Piece]:
        layer = legacy_layer(dec_deg)
        return await self._fetch_legacysurvey_layer(
            ra_deg,
            dec_deg,
            size_deg,
            layer=layer,
            bands="grz",
            max_pixels=LEGACY_SURVEYS_CUTOUT.max_pixels,
            off_footprint_message=f"Legacy Surveys {layer} has no imagery here",
        )

    async def _fetch_unwise(self, ra_deg: float, dec_deg: float, size_deg: float) -> list[_Piece]:
        # `jpeg-cutout`, not `fits-cutout` -- deliberately, not as an
        # error fallback. Verified live, 2026-09-07, identical field: the
        # `fits-cutout` W1+W2 float cube at `UNWISE_CUTOUT.max_pixels`
        # square is ~72 MB and took ~72 s; `_render_display` below only
        # ever draws one plane of a non-3-plane cube (a single grayscale
        # band -- its own doc comment), so that whole 72 MB round trip
        # bought nothing but a discarded W2 plane and a slow "Fetch"
        # button (reported live). `jpeg-cutout` for the same field is
        # Legacy Surveys' own W1+W2 colour composite already: ~2.8 MB,
        # ~12 s, and strictly better looking than the grayscale FITS
        # path ever was. No `bands` value would be meaningful on either
        # path -- the endpoint ignores it for `unwise-neo7` (verified
        # live: identical bytes for `bands=w1`, `bands=w2` and no
        # `bands` at all).
        #
        # Tiled into an n x n grid of `UNWISE_TILE_SIZE_DEG`-square
        # sub-requests whenever `size_deg` exceeds that (reported live
        # 2026-09-08): `max_pixels` is a *per-request* pixel budget, so one
        # `size_deg=6` request is stuck at 7.2"/px -- half the resolution a
        # 3 deg request gets from the identical budget. Four 3 deg tiles
        # covering the same 6x6 deg footprint each get their own full
        # budget, at 3.6"/px -- a genuine sharpness gain, not a workaround
        # for a size limit that does not actually exist server-side
        # (`CutoutRequest.size_deg`'s own ceiling is already 6.0 and a
        # single 6 deg request succeeds today). `size_deg <=
        # UNWISE_TILE_SIZE_DEG` is `n=1`: unchanged, one request, exactly as
        # before tiling existed.
        n = max(1, math.ceil(size_deg / UNWISE_TILE_SIZE_DEG))
        if n == 1:
            return await self._unwise_tile(ra_deg, dec_deg, size_deg)
        tile_size_deg = size_deg / n
        # `sky_tile_centers_deg` (ssc.astro.mosaic) does the RA/Dec grid
        # geometry -- cos(dec)-corrected RA offsets, RA wrap at 0/360 deg,
        # and pole clamping -- so this module never re-derives spherical
        # geometry that `ssc.astro` already owns (architecture policy invariant 3).
        centers = sky_tile_centers_deg(ra_deg, dec_deg, n, tile_size_deg)
        # One in-flight request per host is already serialised by `_get`'s
        # own per-host lock (`www.legacysurvey.org`, shared with Legacy
        # Surveys' own layers) -- `asyncio.gather` here would not add real
        # network concurrency, only bookkeeping, so tiles are fetched one at
        # a time. A genuine upstream failure on any one tile propagates
        # immediately and fails the whole job (architecture policy invariant 7 -- a
        # mosaic silently missing a corner is worse than an honest failure)
        # rather than this method swallowing it and returning a partial
        # 3-piece result.
        pieces: list[_Piece] = []
        for tile_ra_deg, tile_dec_deg in centers:
            pieces.extend(
                await self._unwise_tile(float(tile_ra_deg), float(tile_dec_deg), tile_size_deg)
            )
        return pieces

    async def _unwise_tile(self, ra_deg: float, dec_deg: float, size_deg: float) -> list[_Piece]:
        """One `jpeg-cutout` request for one unWISE tile -- `_fetch_unwise`
        calls this once directly (`size_deg <= UNWISE_TILE_SIZE_DEG`) or once
        per tile in its own grid."""
        max_pixels = UNWISE_CUTOUT.max_pixels
        pixscale = max(1.0, size_deg * 3600.0 / max_pixels)
        pixels = min(max_pixels, max(1, round(size_deg * 3600.0 / pixscale)))
        return await self._legacysurvey_quicklook(
            ra_deg,
            dec_deg,
            UNWISE_LAYER,
            pixscale,
            pixels,
            f"unWISE has no imagery at ra={ra_deg:.4f} dec={dec_deg:.4f}",
        )

    async def _fetch_legacysurvey_layer(
        self,
        ra_deg: float,
        dec_deg: float,
        size_deg: float,
        *,
        layer: str,
        bands: str | None,
        max_pixels: int,
        off_footprint_message: str,
    ) -> list[_Piece]:
        pixscale = max(1.0, size_deg * 3600.0 / max_pixels)
        pixels = min(max_pixels, max(1, round(size_deg * 3600.0 / pixscale)))
        params = {
            "ra": f"{ra_deg}",
            "dec": f"{dec_deg}",
            "layer": layer,
            "pixscale": f"{pixscale}",
            "size": str(pixels),
        }
        if bands is not None:
            params["bands"] = bands
        try:
            data = await self._get(
                "https://www.legacysurvey.org/viewer/fits-cutout",
                host="www.legacysurvey.org",
                params=params,
            )
        except _NotFoundError as exc:
            # Optional layer (Legacy Surveys' own): off-footprint is never
            # an error (registry: CutoutSource.optional). unWISE is
            # verified all-sky, so this path is not expected for it, but the
            # archive's own answer is still trusted over an assumption.
            raise _OffFootprintError(off_footprint_message) from exc
        except _UpstreamError:
            # `fits-cutout` (a multi-band FITS cube) 500s reproducibly for
            # some real fields (confirmed live: two distinct Legacy Surveys
            # targets, both DR9 and DR10, `jpeg-cutout` succeeding at the
            # identical coordinates every time) -- a server-side limitation
            # on their multi-plane FITS path, not a transient blip and not
            # anything wrong with this request. `jpeg-cutout` is the same
            # fallback role SDSS's own quick-look already plays for its own
            # server-side gaps; try it before giving up.
            return await self._legacysurvey_quicklook(
                ra_deg, dec_deg, layer, pixscale, pixels, off_footprint_message
            )
        try:
            image = read_fits_image(data)
            wcs = extract_wcs(image.header)
        except FitsError as exc:
            raise _UpstreamError(f"{layer} cutout is not a usable FITS image: {exc}") from exc
        return [_Piece(band=None, format="fits", data=data, image=image, wcs=wcs)]

    async def _legacysurvey_quicklook(
        self,
        ra_deg: float,
        dec_deg: float,
        layer: str,
        pixscale: float,
        pixels: int,
        off_footprint_message: str,
    ) -> list[_Piece]:
        """`jpeg-cutout`: a colour composite, no WCS header of its own -- a
        synthetic TAN WCS from the request geometry, the same technique
        `_sdss_quicklook` already uses for SDSS's own JPEG fallback."""
        params = {
            "ra": f"{ra_deg}",
            "dec": f"{dec_deg}",
            "layer": layer,
            "pixscale": f"{pixscale}",
            "width": str(pixels),
            "height": str(pixels),
        }
        try:
            data = await self._get(
                "https://www.legacysurvey.org/viewer/jpeg-cutout",
                host="www.legacysurvey.org",
                params=params,
            )
        except _NotFoundError as exc:
            raise _OffFootprintError(off_footprint_message) from exc
        wcs = ImageWCS(
            crpix1=pixels / 2 + 0.5,
            crpix2=pixels / 2 + 0.5,
            crval1=ra_deg,
            crval2=dec_deg,
            cd1_1=-pixscale / 3600.0,
            cd1_2=0.0,
            cd2_1=0.0,
            cd2_2=pixscale / 3600.0,
            ctype1="RA---TAN",
            ctype2="DEC--TAN",
        )
        return [
            _Piece(
                band=None, format="jpg", data=data, image=None, wcs=wcs, width=pixels, height=pixels
            )
        ]

def _state_of(row: Any) -> CutoutState:
    return CutoutState(
        id=str(row["id"]),
        source=str(row["source"]),
        ra_deg=float(row["ra_deg"]),
        dec_deg=float(row["dec_deg"]),
        size_deg=float(row["size_deg"]),
        status=CutoutJobStatus(str(row["status"])),
        bytes=int(row["bytes"]),
        error=None if row["error"] is None else str(row["error"]),
        error_detail=None if row["error_detail"] is None else str(row["error_detail"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _piece_of(row: Any) -> CutoutPieceRecord:
    has_wcs = row["crpix1"] is not None
    wcs = (
        ImageWCS(
            crpix1=float(row["crpix1"]),
            crpix2=float(row["crpix2"]),
            crval1=float(row["crval1"]),
            crval2=float(row["crval2"]),
            cd1_1=float(row["cd1_1"]),
            cd1_2=float(row["cd1_2"]),
            cd2_1=float(row["cd2_1"]),
            cd2_2=float(row["cd2_2"]),
            ctype1=str(row["ctype1"]),
            ctype2=str(row["ctype2"]),
        )
        if has_wcs
        else None
    )
    return CutoutPieceRecord(
        idx=int(row["idx"]),
        band=None if row["band"] is None else str(row["band"]),
        format=str(row["format"]),
        path=str(row["path"]),
        bytes=int(row["bytes"]),
        width=None if row["width"] is None else int(row["width"]),
        height=None if row["height"] is None else int(row["height"]),
        wcs=wcs,
        stretch=None if row["stretch"] is None else str(row["stretch"]),
        display_png_path=None if row["display_png_path"] is None else str(row["display_png_path"]),
    )


__all__ = [
    "AUTOFETCH_DEBOUNCE_S",
    "AUTOFETCH_SIZE_DEG",
    "AUTOFETCH_SOURCE_ID",
    "CUTOUT_HISTORY",
    "PLAN_PROTECTION_RADIUS_DEG",
    "CutoutNotFoundError",
    "CutoutPieceRecord",
    "CutoutRefusedError",
    "CutoutService",
    "ImportPiece",
    "cutout_cache_key",
    "now_utc",
    "release_tag",
]
