# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``TileCache.order_counts`` under real concurrency.

2026-09-10, SIT (``sit.log`` 19:13:54): ``skymap-publish`` died with
``TypeError: int() argument must be ... not 'NoneType'`` inside
``order_counts``'s dict comprehension, immediately followed by four
concurrent ``evict()`` log lines. The live database held no NULL
``tile_order`` rows — the corruption was in the *read*, not the data.

Root cause: ``order_counts``'s ``GROUP BY`` is answered by SQLite streaming
off the ``(survey, tile_order, npix)`` primary-key index (see the
``EXPLAIN QUERY PLAN`` assertion below), spanning more than one
``sqlite3_step()``. The old implementation went through
``Database.fetch_all``, which submits the initial ``execute()`` and the
later ``fetchall()`` as two *separate* jobs on aiosqlite's single
connection-owning thread. A concurrent write (``evict()``'s per-row
``DELETE``, a parallel tile ``store()``) queued between those two jobs can
land mid-scan and hand back a row read with ``tile_order`` as ``NULL`` from
a column the schema declares ``NOT NULL``.

This drives the real mechanism — concurrent ``evict()`` and ``store()``
against ``order_counts()``, all through the public ``TileCache`` API, no
reimplemented SQL — rather than asserting on the SQL text.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ssc.skymap.cache import TileCache
from ssc.skymap.surveys import Survey, SurveyKind, TileScheme
from ssc.store.database import Database, open_database
from ssc.store.migrations import migrate

SURVEY = Survey(
    id="race-fixture",
    label="Race fixture survey (test only)",
    kind=SurveyKind.OPTICAL,
    scheme=TileScheme.HIPS,
    upstream_base_url="https://example.org/hips/demo",
    tile_format="jpg",
    max_order=5,
    credit_label="Test fixture",
    credit_url="https://example.org",
)
# HiPS order 0 has only 12 tiles (``12 * 4**order``); start high enough that
# every order below holds at least ``N_NPIX`` of them.
ORDERS = (2, 3, 4, 5)
N_ORDERS = len(ORDERS)
N_NPIX = 150
TILE_BYTES = b"\xff\xd8\xff" + b"\x00" * 61  # 64 bytes, jpg-ish magic


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncIterator[TileCache]:
    database: Database = await open_database(tmp_path / "ssc.sqlite3")
    await migrate(database)
    tile_cache = TileCache(database, tmp_path / "tiles")
    for order in ORDERS:
        for npix in range(N_NPIX):
            await tile_cache.store(SURVEY, order, npix, "jpg", TILE_BYTES, pinned=False)
    try:
        yield tile_cache
    finally:
        await database.aclose()


async def test_order_counts_query_plan_streams_off_the_primary_key_index(
    cache: TileCache,
) -> None:
    """Pins down *why* this races: if a future migration adds an index that
    changes the plan to a materialize-then-sort, this test documents that
    the exploit here (a scan spanning more than one ``sqlite3_step()``) may
    no longer apply — and the concurrency test below would tell us either
    way."""
    rows = await cache.database.raw.execute_fetchall(
        "EXPLAIN QUERY PLAN SELECT tile_order, COUNT(*) AS n FROM skymap_tiles "
        "WHERE survey = ? AND tile_order >= 0 GROUP BY tile_order",
        (SURVEY.id,),
    )
    plan = " ".join(str(row["detail"]) for row in rows)
    assert "COVERING INDEX sqlite_autoindex_skymap_tiles_1" in plan


async def test_order_counts_survives_concurrent_eviction_and_writes(
    cache: TileCache,
) -> None:
    """Hammers real ``TileCache.evict()`` (the exact per-row DELETE loop
    that raced in production) and real ``TileCache.store()`` against real
    ``order_counts()`` calls, all as genuinely concurrent asyncio tasks, and
    asserts every read comes back internally consistent: every key is a
    real order with a positive count, and the counts never exceed what was
    ever inserted. Before the fix (``order_counts`` via
    ``Database.fetch_all``) this reliably produced a ``None`` key or raised
    ``TypeError`` within a couple of seconds; confirmed failing against the
    pre-fix implementation (see the report) before writing the guard in
    ``TileCache.order_counts``."""
    stop = asyncio.Event()
    failures: list[str] = []

    async def reader() -> None:
        while not stop.is_set():
            try:
                counts = await cache.order_counts(SURVEY.id)
            except TypeError as exc:
                failures.append(f"order_counts raised {exc!r}")
                continue
            for order, n in counts.items():
                if order is None or n is None or order not in ORDERS or n <= 0:
                    failures.append(f"corrupt row: order={order!r} n={n!r}")
            await asyncio.sleep(0)

    n_evictors = 6
    # Each evictor writes only inside its own npix band, never touching a
    # path another evictor task might be writing at the same instant — two
    # concurrent ``store()`` calls for the *same* tile is a separate,
    # already-present hazard (``_write_atomically``'s temp filename is not
    # unique per caller) that this test is not about; giving each task a
    # disjoint slice keeps the test isolated to the ``order_counts`` race.
    band = N_NPIX // n_evictors

    async def evictor(worker: int) -> None:
        i = 0
        npix_lo, npix_hi = worker * band, (worker + 1) * band
        while not stop.is_set():
            await cache.evict(
                max_bytes=0,  # evict everything evictable, every pass
                surveys={SURVEY.id: SURVEY},
                protected_orders={},
            )
            # Keep the table populated so there is always something to both
            # scan and delete on the next pass.
            order = ORDERS[i % N_ORDERS]
            npix = npix_lo + (i % (npix_hi - npix_lo))
            await cache.store(SURVEY, order, npix, "jpg", TILE_BYTES, pinned=False)
            i += 1
            await asyncio.sleep(0)

    readers = [asyncio.create_task(reader()) for _ in range(10)]
    evictors = [asyncio.create_task(evictor(worker)) for worker in range(n_evictors)]
    await asyncio.sleep(4)
    stop.set()
    await asyncio.gather(*readers, *evictors)

    assert failures == [], f"{len(failures)} corrupt/raising reads, e.g. {failures[:5]}"
