"""Keyring-backed secrets: values in, never out (CLAUDE.md invariant 8)."""

from __future__ import annotations

import logging

import pytest

from ssc.settings.secrets import SERVICE_NAME, SecretStore, validate_name
from tests.store.conftest import MemoryKeyring


async def test_round_trip(memory_keyring: MemoryKeyring) -> None:
    store = SecretStore()
    await store.set("owm", "k3y")
    assert await store.get("owm") == "k3y"
    assert await store.has("owm") is True
    assert memory_keyring.get_password(SERVICE_NAME, "owm") == "k3y"


async def test_an_unset_secret_is_none_not_empty(memory_keyring: MemoryKeyring) -> None:
    assert await SecretStore().get("meteoblue") is None
    assert await SecretStore().has("meteoblue") is False


async def test_deleting_reports_whether_there_was_anything_to_delete(
    memory_keyring: MemoryKeyring,
) -> None:
    store = SecretStore()
    assert await store.delete("owm") is False
    await store.set("owm", "k3y")
    assert await store.delete("owm") is True
    assert await store.get("owm") is None


async def test_an_empty_value_is_refused_rather_than_stored(memory_keyring: MemoryKeyring) -> None:
    with pytest.raises(ValueError, match="empty secret"):
        await SecretStore().set("owm", "")


@pytest.mark.parametrize("name", ["", "Owm", "with space", "x" * 65, "../etc/passwd"])
async def test_bad_names_are_refused(name: str, memory_keyring: MemoryKeyring) -> None:
    with pytest.raises(ValueError):
        validate_name(name)


async def test_no_secret_value_reaches_the_log(
    memory_keyring: MemoryKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="ssc.settings.secrets"):
        store = SecretStore()
        await store.set("owm", "super-secret-value")
        await store.get("owm")
        await store.delete("owm")
    assert "super-secret-value" not in caplog.text
    assert "owm" in caplog.text
