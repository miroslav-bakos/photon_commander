# SPDX-License-Identifier: GPL-3.0-only

from pathlib import Path

import pytest

from ssc.capture import FrameType
from ssc.capture.master_catalogue import (
    CalibrationMasterCatalogueError,
    move_calibration_master,
    scan_calibration_masters,
)


def test_archive_and_restore_master_with_provenance(tmp_path: Path) -> None:
    root = tmp_path / "Calibration"
    master = root / "darks" / "master_dark.fits"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"FITS")
    master.with_suffix(".json").write_text("{}")

    archived = move_calibration_master(
        root, frame_type=FrameType.DARK, name=master.name, destination="archived"
    )
    assert archived.status == "archived"
    assert archived.provenance_available
    assert not master.exists()
    assert [record.status for record in scan_calibration_masters(root)] == ["archived"]

    restored = move_calibration_master(
        root, frame_type=FrameType.DARK, name=master.name, destination="active"
    )
    assert restored.path == master
    assert master.read_bytes() == b"FITS"
    assert master.with_suffix(".json").is_file()


def test_legacy_fits_json_provenance_remains_supported(tmp_path: Path) -> None:
    root = tmp_path / "Calibration"
    master = root / "bias" / "master_bias.fits"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"FITS")
    master.with_suffix(".fits.json").write_text("{}")

    [record] = scan_calibration_masters(root)
    assert record.provenance_available

    archived = move_calibration_master(
        root, frame_type=FrameType.BIAS, name=master.name, destination="archived"
    )
    assert archived.provenance_available
    assert archived.path.with_suffix(".fits.json").is_file()


@pytest.mark.parametrize("name", ["../master.fits", "nested/master.fits", "master.txt"])
def test_rejects_unsafe_master_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(CalibrationMasterCatalogueError):
        move_calibration_master(
            tmp_path, frame_type=FrameType.FLAT, name=name, destination="archived"
        )


def test_archived_masters_are_reported_separately(tmp_path: Path) -> None:
    active = tmp_path / "bias" / "master_bias.fit"
    archived = tmp_path / "flats" / "archived" / "master_flat.fits"
    active.parent.mkdir(parents=True)
    archived.parent.mkdir(parents=True)
    active.write_bytes(b"active")
    archived.write_bytes(b"archived")

    records = scan_calibration_masters(tmp_path)
    assert {(record.frame_type, record.status) for record in records} == {
        (FrameType.BIAS, "active"),
        (FrameType.FLAT, "archived"),
    }
