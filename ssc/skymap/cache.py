# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The on-disk tile cache: files under ``data_dir/tiles/<survey>/``, indexed in SQLite.

The files mirror the upstream pyramid's layout, so a survey's cache directory
is itself a valid tile root. SQLite holds only what a directory walk would be
too slow to answer on a Pi at 3 a.m.: bytes per survey, LRU order, which
orders are completely covered, and which tiles upstream does not have.

Every file operation goes through ``asyncio.to_thread``; every index
operation through ``aiosqlite``. Nothing here blocks the loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from ssc.skymap.surveys import Survey, local_tile_path, tile_count
from ssc.store.database import Database

_log = logging.getLogger(__name__)

TileStatus = Literal["cached", "missing"]

#: ``tile_order`` of a survey's ``properties`` file in the index.
PROPERTIES_ORDER: Final = -1

#: A hit only rewrites ``last_used`` when the stored value is older than this;
#: panning a map touches hundreds of tiles a second and none of them needs a
#: write per view to stay recently-used.
_TOUCH_INTERVAL: Final = timedelta(hours=1)


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class CachedTile:
    survey: str
    order: int
    npix: int
    ext: str
    status: TileStatus
    bytes: int
    pinned: bool
    fetched_at: datetime
    last_used: datetime


@dataclass(frozen=True, slots=True)
class SurveyStats:
    cached_tiles: int
    cached_bytes: int
    missing_tiles: int


class TileCache:
    """Files on disk, index in SQLite, one writer (the caller's loop)."""

    def __init__(
        self, database: Database, root: Path, *, now: Callable[[], datetime] = now_utc
    ) -> None:
        self._db = database
        self._root = root
        self._now = now

    @property
    def root(self) -> Path:
        return self._root

    @property
    def database(self) -> Database:
        """Shared with the prefetch job table; one connection, one writer."""
        return self._db

    # -- paths --------------------------------------------------------------- #

    def path_for(self, survey: Survey, order: int, npix: int, ext: str) -> Path:
        if order == PROPERTIES_ORDER:
            return self._root / survey.id / "properties"
        return self._root / survey.id / local_tile_path(survey, order, npix, ext)

    # -- lookups ------------------------------------------------------------- #

    async def lookup(self, survey_id: str, order: int, npix: int) -> CachedTile | None:
        row = await self._db.fetch_one(
            "SELECT survey, tile_order, npix, ext, status, bytes, pinned, fetched_at, last_used "
            "FROM skymap_tiles WHERE survey = ? AND tile_order = ? AND npix = ?",
            (survey_id, order, npix),
        )
        if row is None:
            return None
        tile = CachedTile(
            survey=str(row["survey"]),
            order=int(row["tile_order"]),
            npix=int(row["npix"]),
            ext=str(row["ext"]),
            status="missing" if row["status"] == "missing" else "cached",
            bytes=int(row["bytes"]),
            pinned=bool(row["pinned"]),
            fetched_at=datetime.fromisoformat(str(row["fetched_at"])),
            last_used=datetime.fromisoformat(str(row["last_used"])),
        )
        now = self._now()
        if now - tile.last_used > _TOUCH_INTERVAL:
            await self._db.execute(
                "UPDATE skymap_tiles SET last_used = ? "
                "WHERE survey = ? AND tile_order = ? AND npix = ?",
                (now.isoformat(), survey_id, order, npix),
            )
        return tile

    async def read(self, survey: Survey, tile: CachedTile) -> bytes | None:
        """The tile's bytes, or ``None`` if the file has gone from under the
        index (the index row is dropped so the next request refetches)."""
        path = self.path_for(survey, tile.order, tile.npix, tile.ext)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            _log.warning("tile index row without a file, dropping it: %s", path)
            await self._db.execute(
                "DELETE FROM skymap_tiles WHERE survey = ? AND tile_order = ? AND npix = ?",
                (survey.id, tile.order, tile.npix),
            )
            return None

    # -- writes -------------------------------------------------------------- #

    async def store(
        self, survey: Survey, order: int, npix: int, ext: str, data: bytes, *, pinned: bool
    ) -> None:
        path = self.path_for(survey, order, npix, ext)
        await asyncio.to_thread(_write_atomically, path, data)
        await self._upsert(survey.id, order, npix, ext, "cached", len(data), pinned=pinned)

    async def store_missing(
        self, survey: Survey, order: int, npix: int, ext: str, *, pinned: bool
    ) -> None:
        await self._upsert(survey.id, order, npix, ext, "missing", 0, pinned=pinned)

    async def _upsert(
        self,
        survey_id: str,
        order: int,
        npix: int,
        ext: str,
        status: TileStatus,
        size: int,
        *,
        pinned: bool,
    ) -> None:
        stamp = self._now().isoformat()
        # A tile a pack once pinned stays pinned if browsing refetches it.
        await self._db.execute(
            "INSERT INTO skymap_tiles "
            "(survey, tile_order, npix, ext, status, bytes, pinned, fetched_at, last_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (survey, tile_order, npix) DO UPDATE SET "
            "ext = excluded.ext, status = excluded.status, bytes = excluded.bytes, "
            "pinned = MAX(skymap_tiles.pinned, excluded.pinned), "
            "fetched_at = excluded.fetched_at, last_used = excluded.last_used",
            (survey_id, order, npix, ext, status, size, int(pinned), stamp, stamp),
        )

    async def clear(self, survey: Survey) -> int:
        """Delete every file and index row of one survey. Returns bytes freed."""
        row = await self._db.fetch_one(
            "SELECT COALESCE(SUM(bytes), 0) AS total FROM skymap_tiles WHERE survey = ?",
            (survey.id,),
        )
        freed = int(row["total"]) if row is not None else 0
        await self._db.execute("DELETE FROM skymap_tiles WHERE survey = ?", (survey.id,))
        await asyncio.to_thread(_remove_tree, self._root / survey.id)
        return freed

    # -- accounting ---------------------------------------------------------- #

    async def total_bytes(self) -> int:
        row = await self._db.fetch_one("SELECT COALESCE(SUM(bytes), 0) AS total FROM skymap_tiles")
        return int(row["total"]) if row is not None else 0

    async def survey_stats(self, survey_id: str) -> SurveyStats:
        row = await self._db.fetch_one(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status = 'cached' AND tile_order >= 0 THEN 1 END), 0) AS n, "
            "COALESCE(SUM(bytes), 0) AS b, "
            "COALESCE(SUM(CASE WHEN status = 'missing' THEN 1 END), 0) AS m "
            "FROM skymap_tiles WHERE survey = ?",
            (survey_id,),
        )
        if row is None:
            return SurveyStats(0, 0, 0)
        return SurveyStats(int(row["n"]), int(row["b"]), int(row["m"]))

    async def mean_tile_bytes(self, survey_id: str) -> int | None:
        """Measured, from what is cached. ``None`` when nothing is — never a guess."""
        row = await self._db.fetch_one(
            "SELECT AVG(bytes) AS mean FROM skymap_tiles "
            "WHERE survey = ? AND status = 'cached' AND tile_order >= 0",
            (survey_id,),
        )
        if row is None or row["mean"] is None:
            return None
        return round(float(row["mean"]))

    async def order_counts(self, survey_id: str) -> dict[int, int]:
        """Tiles (cached or known missing) per order.

        Deliberately **not** ``self._db.fetch_all`` — this GROUP BY is
        answered by SQLite streaming off the ``(survey, tile_order, npix)``
        primary-key index (``EXPLAIN QUERY PLAN`` shows a covering-index
        search), which spans more than one ``sqlite3_step()``.
        ``Database.fetch_all`` submits the initial ``execute()`` and the
        later ``fetchall()`` as two separate jobs on aiosqlite's connection
        thread; a concurrent write queued between them (``evict()``, a
        parallel tile ``store()``) can land mid-scan and hand back a row
        with ``tile_order`` read back as ``NULL`` from a column the schema
        declares ``NOT NULL``. Reproduced directly against ``aiosqlite``
        (concurrent evictions racing this query on a STRICT table with this
        exact index) and confirmed as the cause of the 2026-09-10
        ``skymap-publish`` crash on SIT (``TypeError: int() argument must be
        ... not 'NoneType'`` in this dict comprehension). ``execute_fetchall``
        runs the execute-and-drain as one job on that thread, so no other
        statement can interleave mid-scan; the same stress that produced
        hundreds of ``NULL`` rows through ``fetch_all`` produced none through
        this path.
        """
        rows = await self._db.raw.execute_fetchall(
            "SELECT tile_order, COUNT(*) AS n FROM skymap_tiles "
            "WHERE survey = ? AND tile_order >= 0 GROUP BY tile_order",
            (survey_id,),
        )
        return {int(row["tile_order"]): int(row["n"]) for row in rows}

    async def pack_complete_order(self, survey: Survey) -> int | None:
        """The deepest ``k`` with every tile of orders ``0..k`` present."""
        counts = await self.order_counts(survey.id)
        complete: int | None = None
        for order in range(survey.max_order + 1):
            if counts.get(order, 0) < tile_count(survey, order):
                break
            complete = order
        return complete

    async def is_cached(self, survey_id: str, order: int, npix: int) -> bool:
        row = await self._db.fetch_one(
            "SELECT 1 FROM skymap_tiles WHERE survey = ? AND tile_order = ? AND npix = ?",
            (survey_id, order, npix),
        )
        return row is not None

    # -- eviction ------------------------------------------------------------ #

    async def evict(
        self, *, max_bytes: int, surveys: dict[str, Survey], protected_orders: dict[str, int | None]
    ) -> int:
        """Drop least-recently-used browsing tiles until the cache fits.

        Never touches a pinned tile, nor any tile of an order the survey's
        pack has completed (``protected_orders[survey] = k`` protects
        ``0..k``). Returns bytes freed; the caller decides what to say if the
        cache still does not fit, because that means a pack is bigger than
        the budget and only the user may delete a pack.
        """
        total = await self.total_bytes()
        freed = 0
        if total <= max_bytes:
            return 0
        rows = await self._db.fetch_all(
            "SELECT survey, tile_order, npix, ext, bytes FROM skymap_tiles "
            "WHERE pinned = 0 AND status = 'cached' AND tile_order >= 0 ORDER BY last_used ASC"
        )
        for row in rows:
            if total - freed <= max_bytes:
                break
            survey_id = str(row["survey"])
            order = int(row["tile_order"])
            floor = protected_orders.get(survey_id)
            if floor is not None and order <= floor:
                continue
            survey = surveys.get(survey_id)
            npix = int(row["npix"])
            if survey is not None:
                path = self.path_for(survey, order, npix, str(row["ext"]))
                await asyncio.to_thread(_unlink_quietly, path)
            await self._db.execute(
                "DELETE FROM skymap_tiles WHERE survey = ? AND tile_order = ? AND npix = ?",
                (survey_id, order, npix),
            )
            freed += int(row["bytes"])
        return freed


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _unlink_quietly(path: Path) -> None:
    path.unlink(missing_ok=True)


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "PROPERTIES_ORDER",
    "CachedTile",
    "SurveyStats",
    "TileCache",
    "TileStatus",
    "now_utc",
]
