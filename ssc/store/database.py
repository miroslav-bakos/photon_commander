# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""SQLite behind an async wrapper, opened the way this app needs it every time.

Three pragmas are not optional and are therefore not configurable:

* ``journal_mode=WAL`` — the executor writes while the API reads. Without WAL the
  two block each other and a status poll can stall a slew.
* ``foreign_keys=ON`` — SQLite defaults this *off* per connection. A schema with
  unenforced foreign keys is worse than one without them, because it looks safe.
* ``busy_timeout`` — a lock contention must wait, not raise. One writer is the
  rule, but WAL checkpoints and the migration path still contend briefly.

Every call goes through ``aiosqlite``, so nothing here blocks the event loop.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import aiosqlite

_log = logging.getLogger(__name__)

DEFAULT_BUSY_TIMEOUT_MS: Final = 5_000

#: An in-memory database, shared between the connections of one process. Used by
#: tests; a real run always names a file so the data outlives the process.
MEMORY_URI: Final = "file:ssc-memory?mode=memory&cache=shared"

SqlParams = Sequence[object] | Mapping[str, object]


class StoreError(Exception):
    """The store could not be opened, migrated or read."""


class Database:
    """One owned ``aiosqlite`` connection with the SSC pragmas applied."""

    def __init__(self, connection: aiosqlite.Connection, *, path: str) -> None:
        self._connection = connection
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    @property
    def raw(self) -> aiosqlite.Connection:
        return self._connection

    async def execute(self, sql: str, params: SqlParams = ()) -> None:
        await self._connection.execute(sql, params)
        await self._connection.commit()

    async def executescript(self, script: str) -> None:
        await self._connection.executescript(script)
        await self._connection.commit()

    async def fetch_one(self, sql: str, params: SqlParams = ()) -> aiosqlite.Row | None:
        async with self._connection.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return row

    async def fetch_all(self, sql: str, params: SqlParams = ()) -> list[aiosqlite.Row]:
        async with self._connection.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return list(rows)

    async def pragma(self, name: str) -> object:
        row = await self.fetch_one(f"PRAGMA {name}")
        if row is None:
            raise StoreError(f"PRAGMA {name} returned no row")
        return row[0]

    async def aclose(self) -> None:
        await self._connection.close()


async def open_database(
    path: Path | str,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Database:
    """Open (creating the parent directory if needed) and apply the pragmas."""
    uri = str(path)
    is_uri = uri.startswith("file:")
    if not is_uri:
        Path(uri).parent.mkdir(parents=True, exist_ok=True)

    connection = await aiosqlite.connect(uri, uri=is_uri, isolation_level=None)
    connection.row_factory = aiosqlite.Row
    database = Database(connection, path=uri)
    try:
        await _apply_pragmas(database, busy_timeout_ms=busy_timeout_ms)
    except BaseException:
        await database.aclose()
        raise
    return database


async def _apply_pragmas(database: Database, *, busy_timeout_ms: int) -> None:
    await database.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    await database.execute("PRAGMA foreign_keys = ON")
    # An in-memory database cannot use WAL; a file-backed one must.
    if "mode=memory" not in database.path:
        mode = await database.pragma("journal_mode = WAL")
        if str(mode).lower() != "wal":
            raise StoreError(
                f"SQLite refused WAL on {database.path!r} (journal_mode={mode!r}). "
                "The filesystem probably does not support it; move the data "
                "directory off a network or FUSE mount."
            )
    foreign_keys = await database.pragma("foreign_keys")
    if int(str(foreign_keys)) != 1:
        raise StoreError("SQLite refused PRAGMA foreign_keys=ON")


@asynccontextmanager
async def database(path: Path | str) -> AsyncIterator[Database]:
    """Open, yield, close — for scripts and tests, not for the app lifespan."""
    handle = await open_database(path)
    try:
        yield handle
    finally:
        await handle.aclose()


__all__ = [
    "MEMORY_URI",
    "Database",
    "StoreError",
    "database",
    "open_database",
]
