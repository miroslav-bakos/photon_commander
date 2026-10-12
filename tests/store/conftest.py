"""An open, migrated database per test, and a keyring that never touches the OS."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest

from ssc.settings.store import SettingsStore
from ssc.store.database import Database, open_database
from ssc.store.migrations import migrate


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1.0

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._data:
            raise keyring.errors.PasswordDeleteError(username)
        del self._data[(service, username)]


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


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


@pytest.fixture
async def settings(migrated: Database) -> SettingsStore:
    store = SettingsStore(migrated)
    await store.load()
    return store
