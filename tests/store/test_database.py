"""The three pragmas that are not optional."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from ssc.store.database import Database, open_database


async def test_wal_is_on_for_a_file_database(database: Database) -> None:
    assert str(await database.pragma("journal_mode")).lower() == "wal"


async def test_busy_timeout_is_set(database: Database) -> None:
    assert int(str(await database.pragma("busy_timeout"))) == 5_000


async def test_foreign_keys_are_actually_enforced(database: Database) -> None:
    await database.executescript(
        """
        CREATE TABLE parent (id INTEGER PRIMARY KEY);
        CREATE TABLE child (id INTEGER PRIMARY KEY,
                            parent_id INTEGER NOT NULL REFERENCES parent(id));
        """
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await database.execute("INSERT INTO child (id, parent_id) VALUES (1, 99)")


async def test_the_parent_directory_is_created(tmp_path: Path) -> None:
    handle = await open_database(tmp_path / "nested" / "deeper" / "ssc.sqlite3")
    try:
        assert (tmp_path / "nested" / "deeper" / "ssc.sqlite3").exists()
    finally:
        await handle.aclose()
