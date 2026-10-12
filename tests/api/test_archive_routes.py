# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from __future__ import annotations

import struct
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from ssc.api import capture_orchestrator
from ssc.capture.fits import write_fits_file
from ssc.capture.frame import FrameMetadata, FrameType
from ssc.store.capture import CaptureRepository
from tests.api.conftest import TestClient, services_of


def _terminal_operation(client: TestClient, response: object) -> dict[str, object]:
    operation_id = response.json()["id"]
    for _ in range(100):
        operation = client.get(f"/api/operations/{operation_id}").json()
        if operation["status"] in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(0.01)
    return operation


def _metadata() -> FrameMetadata:
    return FrameMetadata(
        date_obs=datetime(2026, 9, 12, 21, 30, tzinfo=UTC),
        exposure_s=30.0,
        gain=80,
        ccd_temp_c=17.5,
        bayer_offset_x=1,
        bayer_offset_y=0,
        pixel_size_um=2.0,
        ra_deg=None,
        dec_deg=None,
        site_lat_deg=None,
        site_lon_deg=None,
        filter_name="IR",
        object_name="M31",
        frame_type=FrameType.LIGHT,
        focal_length_mm=160.0,
        telescope_name="Seestar S30 Pro",
        ssc_version="test",
        device_firmware=None,
    )


def _png_shape(data: bytes) -> tuple[int, int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _depth, colour = struct.unpack(">IIBB", data[16:26])
    return width, height, colour


def test_archive_lists_and_renders_external_capture_files(
    paired: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "M31" / "2026-09-12" / "lights"
    folder.mkdir(parents=True)
    key = "M31/2026-09-12/lights/light.fits"
    write_fits_file(
        folder / "light.fits", np.arange(64, dtype=np.uint16).reshape(8, 8), _metadata()
    )
    response = paired.put("/api/settings/capture-folder", json={"folder": str(tmp_path)})
    assert response.status_code == 200, response.text

    listed = paired.get("/api/capture/archive")
    assert listed.status_code == 200, listed.text
    entry = listed.json()["entries"][0]
    assert entry["key"] == key
    assert entry["date_source"] == "fits_header"
    assert entry["bayer_pattern"] == "GRBG"
    assert entry["catalogue_id"] is None

    render_calls = 0
    original_render = capture_orchestrator._render_frame_png

    def counted_render(raw: bytes, *, thumbnail: bool) -> bytes:
        nonlocal render_calls
        render_calls += 1
        return original_render(raw, thumbnail=thumbnail)

    monkeypatch.setattr(capture_orchestrator, "_render_frame_png", counted_render)
    image = paired.get("/api/capture/archive/image", params={"key": key, "thumbnail": True})
    assert image.status_code == 200, image.text
    assert image.headers["content-type"] == "image/png"
    assert _png_shape(image.content) == (8, 8, 2)
    repeated = paired.get("/api/capture/archive/image", params={"key": key, "thumbnail": True})
    assert repeated.content == image.content
    assert render_calls == 1

    quality = paired.get("/api/capture/archive/quality", params={"key": key})
    assert quality.status_code == 200, quality.text
    assert quality.json()["star_count"] == 0


def test_archive_does_not_serve_paths_outside_the_capture_root(
    paired: TestClient, tmp_path: Path
) -> None:
    response = paired.put("/api/settings/capture-folder", json={"folder": str(tmp_path)})
    assert response.status_code == 200, response.text

    escaped = paired.get("/api/capture/archive/image", params={"key": "../secret.fits"})
    assert escaped.status_code == 404


def test_archive_returns_standard_images_without_rewriting_them(
    paired: TestClient, tmp_path: Path
) -> None:
    original = b"\x89PNG\r\n\x1a\noriginal-image-bytes"
    (tmp_path / "export.png").write_bytes(original)
    response = paired.put("/api/settings/capture-folder", json={"folder": str(tmp_path)})
    assert response.status_code == 200, response.text

    image = paired.get("/api/capture/archive/image", params={"key": "export.png"})

    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == original


def test_archive_deletes_selected_files_and_catalogue_rows(
    paired: TestClient, tmp_path: Path
) -> None:
    path = tmp_path / "M31" / "lights" / "light.fits"
    path.parent.mkdir(parents=True)
    write_fits_file(path, np.arange(64, dtype=np.uint16).reshape(8, 8), _metadata())
    assert (
        paired.put("/api/settings/capture-folder", json={"folder": str(tmp_path)}).status_code
        == 200
    )
    services = services_of(paired)

    async def plant() -> int:
        repo = CaptureRepository(services.database)
        frame_id = await repo.begin_frame(
            path=path,
            date_obs=datetime(2026, 9, 12, 21, 30, tzinfo=UTC),
            object_name="M31",
            filter_name="IR",
            frame_type="light",
            exposure_s=30,
            gain=200,
            ccd_temp_c=18,
            width=8,
            height=8,
        )
        await repo.complete_frame(frame_id)
        return frame_id

    paired.portal.call(plant)  # type: ignore[attr-defined]
    response = paired.post(
        "/api/capture/archive/delete-files",
        json={"keys": ["M31/lights/light.fits"]},
    )

    assert response.status_code == 202, response.text
    result = _terminal_operation(paired, response)["result"]
    assert result["files_deleted"] == 1
    assert result["catalogue_rows_deleted"] == 1
    assert not path.exists()

    async def remaining() -> int:
        return len(await CaptureRepository(services.database).list_complete())

    assert paired.portal.call(remaining) == 0  # type: ignore[attr-defined]


def test_archive_deletes_whole_folder_without_following_symlinks(
    paired: TestClient, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "keep-me.txt"
    outside.write_bytes(b"safe")
    folder = tmp_path / "M42"
    folder.mkdir()
    (folder / "preview.png").write_bytes(b"png")
    (folder / "notes.txt").write_bytes(b"notes")
    (folder / "outside-link").symlink_to(outside)
    assert (
        paired.put("/api/settings/capture-folder", json={"folder": str(tmp_path)}).status_code
        == 200
    )

    response = paired.post("/api/capture/archive/delete-folder", json={"directory": "M42"})

    assert response.status_code == 202, response.text
    assert _terminal_operation(paired, response)["result"] == {
        "files_deleted": 3,
        "catalogue_rows_deleted": 0,
        "bytes_deleted": 8,
        "folder_deleted": "M42",
    }
    assert not folder.exists()
    assert outside.read_bytes() == b"safe"


def test_archive_moves_and_copies_files_without_losing_catalogue_provenance(
    paired: TestClient, tmp_path: Path
) -> None:
    source = tmp_path / "incoming"
    destination = tmp_path / "sorted"
    source.mkdir()
    destination.mkdir()
    fits_path = source / "light.fits"
    png_path = source / "preview.png"
    write_fits_file(fits_path, np.arange(64, dtype=np.uint16).reshape(8, 8), _metadata())
    png_path.write_bytes(b"png")
    assert (
        paired.put("/api/settings/capture-folder", json={"folder": str(tmp_path)}).status_code
        == 200
    )
    services = services_of(paired)

    async def plant() -> int:
        repo = CaptureRepository(services.database)
        frame_id = await repo.begin_frame(
            path=fits_path,
            date_obs=datetime(2026, 9, 12, 21, 30, tzinfo=UTC),
            object_name="M31",
            filter_name="IR",
            frame_type="light",
            exposure_s=30,
            gain=200,
            ccd_temp_c=18,
            width=8,
            height=8,
        )
        await repo.complete_frame(frame_id)
        return frame_id

    frame_id = paired.portal.call(plant)  # type: ignore[attr-defined]
    moved = paired.post(
        "/api/capture/archive/transfer",
        json={"keys": ["incoming/light.fits"], "destination": "sorted", "mode": "move"},
    )
    assert moved.status_code == 202, moved.text
    assert _terminal_operation(paired, moved)["result"] == {
        "files_transferred": 1,
        "directories_transferred": 0,
        "bytes_transferred": (destination / "light.fits").stat().st_size,
        "catalogue_rows_updated": 1,
        "destination": "sorted",
        "mode": "move",
    }

    async def moved_record_path() -> Path:
        return (await CaptureRepository(services.database).get_frame(frame_id)).path

    assert paired.portal.call(moved_record_path) == destination / "light.fits"  # type: ignore[attr-defined]

    copied = paired.post(
        "/api/capture/archive/transfer",
        json={"keys": ["incoming/preview.png"], "destination": ".", "mode": "copy"},
    )
    assert copied.status_code == 202, copied.text
    result = _terminal_operation(paired, copied)["result"]
    assert result["files_transferred"] == 1
    assert result["catalogue_rows_updated"] == 0
    assert (tmp_path / "preview.png").read_bytes() == b"png"
    assert png_path.read_bytes() == b"png"


def test_archive_transfers_folders_and_downloads_complete_zip(
    paired: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "M31"
    nested = source / "lights"
    destination = tmp_path / "sorted"
    temporary = tmp_path / "temporary"
    nested.mkdir(parents=True)
    destination.mkdir()
    temporary.mkdir()
    monkeypatch.setattr(capture_orchestrator.tempfile, "tempdir", str(temporary))
    (source / "preview.png").write_bytes(b"preview")
    (nested / "light.fits").write_bytes(b"fits")
    assert (
        paired.put("/api/settings/capture-folder", json={"folder": str(tmp_path)}).status_code
        == 200
    )

    zipped = paired.get("/api/capture/archive/folder-zip", params={"directory": "M31"})
    assert zipped.status_code == 200, zipped.text
    assert list(temporary.glob("ssc-archive-*.zip")) == []
    archive_path = tmp_path / "download.zip"
    archive_path.write_bytes(zipped.content)
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {"M31/preview.png", "M31/lights/light.fits"}

    root_preview = tmp_path / "root-preview.png"
    root_preview.write_bytes(b"root")
    selected = paired.post(
        "/api/capture/archive/selection-zip",
        json={"keys": ["root-preview.png"], "directories": ["M31"]},
    )
    assert selected.status_code == 200, selected.text
    assert list(temporary.glob("ssc-archive-selection-*.zip")) == []
    selected_path = tmp_path / "selection.zip"
    selected_path.write_bytes(selected.content)
    with zipfile.ZipFile(selected_path) as archive:
        assert set(archive.namelist()) == {
            "root-preview.png",
            "M31/preview.png",
            "M31/lights/light.fits",
        }

    moved = paired.post(
        "/api/capture/archive/transfer",
        json={"directories": ["M31"], "destination": "sorted", "mode": "move"},
    )
    assert moved.status_code == 202, moved.text
    operation = _terminal_operation(paired, moved)
    assert operation["status"] == "succeeded", str(operation.get("error"))
    result = operation["result"]
    assert result["directories_transferred"] == 1
    assert result["files_transferred"] == 2
    assert result["bytes_transferred"] == 11
    assert (destination / "M31" / "lights" / "light.fits").read_bytes() == b"fits"


def test_archive_returns_sanitized_fits_header_and_storage_capacity(
    paired: TestClient, tmp_path: Path
) -> None:
    path = tmp_path / "light.fits"
    write_fits_file(path, np.arange(64, dtype=np.uint16).reshape(8, 8), _metadata())
    assert (
        paired.put("/api/settings/capture-folder", json={"folder": str(tmp_path)}).status_code
        == 200
    )

    listed = paired.get("/api/capture/archive")
    assert listed.status_code == 200
    assert listed.json()["storage_total_bytes"] > 0
    assert listed.json()["storage_free_bytes"] > 0
    header = paired.get("/api/capture/archive/header", params={"key": "light.fits"})
    assert header.status_code == 200, header.text
    assert header.json()["OBJECT"] == "M31"
    assert header.json()["NAXIS1"] == 8


@pytest.mark.parametrize("destination", ["../outside", "/tmp"])
def test_archive_refuses_unsafe_transfer_destination(
    paired: TestClient, tmp_path: Path, destination: str
) -> None:
    (tmp_path / "preview.png").write_bytes(b"png")
    assert (
        paired.put("/api/settings/capture-folder", json={"folder": str(tmp_path)}).status_code
        == 200
    )
    response = paired.post(
        "/api/capture/archive/transfer",
        json={"keys": ["preview.png"], "destination": destination, "mode": "copy"},
    )
    assert response.status_code == 202
    assert _terminal_operation(paired, response)["status"] == "failed"


@pytest.mark.parametrize("directory", [".", "../outside", "/tmp"])
def test_archive_refuses_unsafe_folder_delete(
    paired: TestClient, tmp_path: Path, directory: str
) -> None:
    assert (
        paired.put("/api/settings/capture-folder", json={"folder": str(tmp_path)}).status_code
        == 200
    )

    response = paired.post("/api/capture/archive/delete-folder", json={"directory": directory})

    assert response.status_code == 202
    operation = _terminal_operation(paired, response)
    assert operation["status"] == "failed"
