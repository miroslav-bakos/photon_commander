"""An open, migrated database per test — mirrors ``tests/store/conftest.py``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ssc.store.database import Database, open_database
from ssc.store.migrations import migrate


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    handle = await open_database(tmp_path / "ssc.sqlite3")
    try:
        yield handle
    finally:
        await handle.aclose()


@pytest.fixture
async def migrated(database: Database) -> Database:
    await migrate(database)
    return database
