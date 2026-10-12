# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Recoverable filesystem catalogue for generated calibration masters."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ssc.capture.frame import FrameType

MasterStatus = Literal["active", "archived"]

_TYPE_FOLDERS = {
    FrameType.BIAS: "bias",
    FrameType.DARK: "darks",
    FrameType.FLAT: "flats",
}


class CalibrationMasterCatalogueError(ValueError):
    """A requested master lifecycle transition is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class CalibrationMasterRecord:
    name: str
    path: Path
    frame_type: FrameType
    status: MasterStatus
    provenance_available: bool


def _master_name(name: str) -> str:
    candidate = Path(name)
    if candidate.name != name or candidate.suffix.casefold() not in {".fit", ".fits"}:
        raise CalibrationMasterCatalogueError("master name must be a FITS basename")
    return name


def _folder(root: Path, frame_type: FrameType, status: MasterStatus) -> Path:
    try:
        folder = root / _TYPE_FOLDERS[frame_type]
    except KeyError as exc:
        raise CalibrationMasterCatalogueError("only bias, dark, and flat masters are managed") from exc
    return folder if status == "active" else folder / "archived"


def _provenance_paths(master: Path) -> tuple[Path, Path]:
    """Return the current sidecar path and the legacy ``.fits.json`` path."""
    return master.with_suffix(".json"), master.with_suffix(master.suffix + ".json")


def scan_calibration_masters(root: Path) -> list[CalibrationMasterRecord]:
    records: list[CalibrationMasterRecord] = []
    for frame_type in (FrameType.BIAS, FrameType.DARK, FrameType.FLAT):
        for status in ("active", "archived"):
            folder = _folder(root, frame_type, status)
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if not path.is_file() or path.suffix.casefold() not in {".fit", ".fits"}:
                    continue
                records.append(
                    CalibrationMasterRecord(
                        name=path.name,
                        path=path,
                        frame_type=frame_type,
                        status=status,
                        provenance_available=any(
                            sidecar.is_file() for sidecar in _provenance_paths(path)
                        ),
                    )
                )
    return sorted(records, key=lambda record: (record.status, record.frame_type.value, record.name))


def move_calibration_master(
    root: Path,
    *,
    frame_type: FrameType,
    name: str,
    destination: MasterStatus,
) -> CalibrationMasterRecord:
    safe_name = _master_name(name)
    source_status: MasterStatus = "archived" if destination == "active" else "active"
    source = _folder(root, frame_type, source_status) / safe_name
    target_folder = _folder(root, frame_type, destination)
    target = target_folder / safe_name
    if not source.is_file() or source.is_symlink():
        raise CalibrationMasterCatalogueError(f"{source_status} master does not exist")
    sidecar_pairs = tuple(zip(_provenance_paths(source), _provenance_paths(target), strict=True))
    if target.exists() or any(
        source_sidecar.exists() and target_sidecar.exists()
        for source_sidecar, target_sidecar in sidecar_pairs
    ):
        raise CalibrationMasterCatalogueError(f"a {destination} master with that name already exists")
    target_folder.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    for source_sidecar, target_sidecar in sidecar_pairs:
        if source_sidecar.is_file() and not source_sidecar.is_symlink():
            os.replace(source_sidecar, target_sidecar)
    return CalibrationMasterRecord(
        name=target.name,
        path=target,
        frame_type=frame_type,
        status=destination,
        provenance_available=any(sidecar.is_file() for sidecar in _provenance_paths(target)),
    )
