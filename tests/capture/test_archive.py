# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from __future__ import annotations

import io
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

import ssc.capture.archive as archive_module
from ssc.capture.archive import (
    ArchivePathError,
    ArchiveResourceError,
    ensure_preview_safe,
    remove_archive_directory,
    resolve_archive_directory,
    resolve_archive_key,
    scan_archive,
    scan_archive_directories,
)
from ssc.capture.fits import write_fits_file
from ssc.capture.frame import FrameMetadata, FrameType


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


def test_scan_archive_reads_fits_provenance_and_standard_images(tmp_path: Path) -> None:
    folder = tmp_path / "M31" / "2026-09-12" / "lights"
    folder.mkdir(parents=True)
    write_fits_file(
        folder / "light.fits", np.arange(64, dtype=np.uint16).reshape(8, 8), _metadata()
    )
    (tmp_path / "preview.jpg").write_bytes(b"jpeg")

    entries = scan_archive(tmp_path, [])

    fits = next(entry for entry in entries if entry.name == "light.fits")
    assert fits.key == "M31/2026-09-12/lights/light.fits"
    assert fits.date_source == "fits_header"
    assert fits.object_name == "M31"
    assert fits.bayer_pattern == "GRBG"
    assert fits.width == 8 and fits.height == 8
    assert next(entry for entry in entries if entry.name == "preview.jpg").kind == "image"


def test_archive_limit_selects_newest_frames_across_all_folders(tmp_path: Path) -> None:
    old = tmp_path / "Calibration"
    new = tmp_path / "NGC-7023"
    old.mkdir()
    new.mkdir()
    for index in range(3):
        write_fits_file(old / f"old-{index}.fits", np.zeros((8, 8), dtype=np.uint16), _metadata())
    write_fits_file(
        new / "current.fits", np.zeros((8, 8), dtype=np.uint16),
        replace(_metadata(), date_obs=datetime(2026, 10, 9, 18, tzinfo=UTC)),
    )

    entries = scan_archive(tmp_path, [], limit=2)

    assert len(entries) == 2
    assert entries[0].key == "NGC-7023/current.fits"
    assert entries[0].date_source == "fits_header"


def test_archive_reads_only_fits_header_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = b"COMMENT ".ljust(80) * 36
    second = b"OBJECT  = 'M31'".ljust(80) + b"END".ljust(80)
    payload = first + second.ljust(2880) + b"\0" * 1024 * 1024

    class TrackedFile(io.BytesIO):
        bytes_read = 0

        def read(self, size: int = -1) -> bytes:
            result = super().read(size)
            self.bytes_read += len(result)
            return result

    opened: list[TrackedFile] = []

    def open_tracked(_path: Path, _mode: str) -> TrackedFile:
        handle = TrackedFile(payload)
        opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", open_tracked)

    assert archive_module._fits_header(tmp_path / "image.fits")["OBJECT"] == "M31"
    assert opened[0].bytes_read == 2 * 2880


def test_archive_resolution_rejects_traversal_and_symlinks(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.fits"
    outside.write_bytes(b"not fits")
    (tmp_path / "escape.fits").symlink_to(outside)
    real_folder = tmp_path / "real"
    real_folder.mkdir()
    (real_folder / "inside.fits").write_bytes(b"not fits")
    (tmp_path / "linked-folder").symlink_to(real_folder, target_is_directory=True)

    assert [entry.key for entry in scan_archive(tmp_path, [])] == ["real/inside.fits"]
    for key in (
        "../outside.fits",
        "escape.fits",
        "linked-folder/inside.fits",
        str(outside),
    ):
        try:
            resolve_archive_key(tmp_path, key)
        except ArchivePathError:
            pass
        else:
            raise AssertionError(f"unsafe archive key was accepted: {key}")


def test_archive_preview_refuses_files_over_the_resource_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "large.fits"
    image.write_bytes(b"1234")
    monkeypatch.setattr(archive_module, "MAX_PREVIEW_BYTES", 3)

    with pytest.raises(ArchiveResourceError, match="limited"):
        ensure_preview_safe(image)


def test_directory_scan_includes_empty_real_folders_and_ignores_symlinks(tmp_path: Path) -> None:
    (tmp_path / "M31" / "lights").mkdir(parents=True)
    (tmp_path / "empty").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "M31", target_is_directory=True)

    assert scan_archive_directories(tmp_path) == [".", "M31", "empty", "M31/lights"]


def test_archive_directory_resolution_refuses_root_traversal_and_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)

    assert resolve_archive_directory(tmp_path, "real") == real
    for key in (".", "", "../outside", "linked", str(real)):
        with pytest.raises(ArchivePathError):
            resolve_archive_directory(tmp_path, key)


def test_remove_archive_directory_does_not_follow_nested_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    folder = tmp_path / "session"
    folder.mkdir()
    (folder / "image.fits").write_bytes(b"image")
    (folder / "escape").symlink_to(outside)

    count, size = remove_archive_directory(folder)

    assert (count, size) == (2, 5)
    assert not folder.exists()
    assert outside.read_bytes() == b"outside"
