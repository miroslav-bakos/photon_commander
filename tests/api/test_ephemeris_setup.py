"""Authenticated setup reports the running astronomy service, not file existence."""
from pathlib import Path

import pytest

from ssc.astro.events import KERNEL_FILENAME, KERNEL_SHA256, KERNEL_SIZE_BYTES, KERNEL_URL
from tests.api.conftest import TestClient, services_of


@pytest.mark.parametrize("config", [True], indirect=True)
def test_ephemeris_destination_requires_session(client: TestClient) -> None:
    response = client.get("/api/settings/ephemeris")
    assert response.status_code == 401
    assert "kernel_path" not in response.json()


@pytest.mark.parametrize("config", [True], indirect=True)
def test_existing_but_unloaded_kernel_is_not_ready(
    paired: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "host-assets" / KERNEL_FILENAME
    path.parent.mkdir()
    path.write_bytes(b"truncated")
    monkeypatch.setattr("ssc.api.routes.settings.default_kernel_path", lambda: path)
    astro = services_of(paired).astro
    monkeypatch.setattr(astro, "ephemeris", None)
    monkeypatch.setattr(astro, "ephemeris_error", "Kernel is truncated; restart after replacing it.")
    response = paired.get("/api/settings/ephemeris")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "loaded": False,
        "error": "Kernel is truncated; restart after replacing it.",
        "folder_path": str(path.parent),
        "kernel_path": str(path),
        "filename": KERNEL_FILENAME,
        "download_url": KERNEL_URL,
        "expected_size_bytes": KERNEL_SIZE_BYTES,
        "expected_sha256": KERNEL_SHA256,
    }
    assert path.read_bytes() == b"truncated"


@pytest.mark.parametrize("config", [True], indirect=True)
def test_loaded_service_is_ready_without_rescanning_disk(
    paired: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "bundled" / KERNEL_FILENAME
    monkeypatch.setattr("ssc.api.routes.settings.default_kernel_path", lambda: path)
    astro = services_of(paired).astro
    sentinel = object()
    monkeypatch.setattr(astro, "ephemeris", sentinel)
    monkeypatch.setattr(astro, "ephemeris_error", "old error")
    response = paired.get("/api/settings/ephemeris")
    assert response.json()["loaded"] is True
    assert response.json()["error"] is None
    assert response.json()["kernel_path"] == str(path)
    assert astro.ephemeris is sentinel
    assert not path.exists()


@pytest.mark.parametrize("config", [False], indirect=True)
def test_loginless_setup_uses_existing_settings_policy(client: TestClient) -> None:
    response = client.get("/api/settings/ephemeris")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert isinstance(response.json()["loaded"], bool)
