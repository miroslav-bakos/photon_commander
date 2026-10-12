# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Read-only discovery of image files beneath the configured capture root.

The archive deliberately does not import files into ``capture_frame``: that
table is the durable journal of exposures SSC itself commanded. Files copied
from a Seestar or another application retain honest ``filesystem`` provenance
instead. Relative keys are resolved again at the serving boundary, so neither
a crafted URL nor a symlink can escape the configured root.
"""

from __future__ import annotations

import heapq
import os
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from ssc.skymap.fits import FitsError, parse_header
from ssc.store.capture import CaptureFrameRecord

FITS_SUFFIXES: Final = frozenset({".fit", ".fits", ".fts"})
STANDARD_IMAGE_SUFFIXES: Final = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff"})
ARCHIVE_SUFFIXES: Final = FITS_SUFFIXES | STANDARD_IMAGE_SUFFIXES
MAX_ARCHIVE_ENTRIES: Final = 5_000
MAX_PREVIEW_BYTES: Final = 512 * 1024 * 1024
_MAX_FITS_HEADER_BYTES: Final = 1024 * 1024
_FITS_BLOCK_BYTES: Final = 2880

ArchiveKind = Literal["fits", "image"]
DateSource = Literal["capture_catalogue", "fits_header", "filesystem_mtime"]


class ArchivePathError(ValueError):
    """An archive key is invalid, missing, or leaves the configured root."""


class ArchiveResourceError(ValueError):
    """A real archive file is too large or otherwise unsafe to render."""


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    key: str
    name: str
    directory: str
    kind: ArchiveKind
    size_bytes: int
    modified_at: datetime
    date_obs: datetime
    date_source: DateSource
    catalogue_id: int | None = None
    object_name: str | None = None
    filter_name: str | None = None
    frame_type: str | None = None
    exposure_s: float | None = None
    gain: int | None = None
    ccd_temp_c: float | None = None
    width: int | None = None
    height: int | None = None
    bayer_pattern: str | None = None
    preview_available: bool = True


@dataclass(frozen=True, slots=True)
class ArchiveDeleteResult:
    files_deleted: int
    catalogue_rows_deleted: int
    bytes_deleted: int
    folder_deleted: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveTransferResult:
    files_transferred: int
    directories_transferred: int
    bytes_transferred: int
    catalogue_rows_updated: int
    destination: str
    mode: Literal["move", "copy"]


def resolve_archive_key(root: Path, key: str) -> Path:
    """Resolve one POSIX relative key and prove it remains below ``root``."""
    resolved_root = root.expanduser().resolve()
    relative = Path(key)
    if relative.is_absolute() or key.strip() == "" or ".." in relative.parts:
        raise ArchivePathError("archive path must be a non-empty relative path")
    unresolved = resolved_root / relative
    cursor = resolved_root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ArchivePathError("archive path contains a symbolic link")
    candidate = unresolved.resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ArchivePathError("archive path leaves the configured capture folder")
    if not candidate.is_file():
        raise ArchivePathError("archive file does not exist")
    if candidate.suffix.casefold() not in ARCHIVE_SUFFIXES:
        raise ArchivePathError("archive file type is not supported")
    return candidate


def resolve_archive_directory(root: Path, key: str) -> Path:
    """Resolve one deletable directory without permitting the archive root.

    Parent components and the directory itself may not be symlinks. Nested
    symlinks are safe when the directory is removed: ``shutil.rmtree`` unlinks
    them and never traverses their targets.
    """
    resolved_root = root.expanduser().resolve()
    relative = Path(key)
    if relative.is_absolute() or key.strip() in {"", "."} or ".." in relative.parts:
        raise ArchivePathError("archive folder must be a non-root relative path")
    cursor = resolved_root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ArchivePathError("archive folder path contains a symbolic link")
    candidate = (resolved_root / relative).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ArchivePathError("archive folder leaves the configured capture folder")
    if not candidate.is_dir():
        raise ArchivePathError("archive folder does not exist")
    return candidate


def resolve_archive_destination(root: Path, key: str) -> Path:
    """Resolve an existing destination folder, permitting the archive root."""
    resolved_root = root.expanduser().resolve()
    if key.strip() == ".":
        if not resolved_root.is_dir():
            raise ArchivePathError("configured capture folder does not exist")
        return resolved_root
    return resolve_archive_directory(root, key)


def scan_archive_directories(root: Path) -> list[str]:
    """Return every real directory as a POSIX key, including empty folders."""
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise ArchivePathError("configured capture folder does not exist")
    directories = ["."]
    for directory, dirnames, _filenames in os.walk(resolved_root, followlinks=False):
        current = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and not (current / name).is_symlink()
        )
        directories.extend(
            (current / name).relative_to(resolved_root).as_posix() for name in dirnames
        )
    return directories


def remove_archive_directory(path: Path) -> tuple[int, int]:
    """Remove a previously validated folder and report file count and bytes."""
    file_count = 0
    byte_count = 0
    for directory, _dirnames, filenames in os.walk(path, followlinks=False):
        current = Path(directory)
        for name in filenames:
            candidate = current / name
            file_count += 1
            try:
                if not candidate.is_symlink():
                    byte_count += candidate.stat().st_size
            except OSError:
                pass
    shutil.rmtree(path)
    return file_count, byte_count


def archive_directory_stats(path: Path) -> tuple[int, int]:
    """Count real files and bytes below a validated archive directory."""
    file_count = 0
    byte_count = 0
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        current = Path(directory)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            candidate = current / name
            if candidate.is_symlink():
                continue
            file_count += 1
            byte_count += candidate.stat().st_size
    return file_count, byte_count


def write_archive_zip(source: Path, output: Path) -> None:
    """Write one folder tree to a ZIP without following symbolic links."""
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory, dirnames, filenames in os.walk(source, followlinks=False):
            current = Path(directory)
            dirnames[:] = sorted(name for name in dirnames if not (current / name).is_symlink())
            relative = current.relative_to(source.parent)
            if not filenames and not dirnames:
                archive.writestr(f"{relative.as_posix()}/", b"")
            for name in sorted(filenames):
                candidate = current / name
                if not candidate.is_symlink():
                    archive.write(candidate, (relative / name).as_posix())


def write_archive_selection_zip(
    root: Path, files: list[Path], directories: list[Path], output: Path
) -> None:
    """Write selected files and recursive folder trees, preserving root-relative keys."""
    written: set[str] = set()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for candidate in files:
            relative = candidate.relative_to(root).as_posix()
            if relative not in written:
                archive.write(candidate, relative)
                written.add(relative)
        for source in directories:
            for directory, dirnames, filenames in os.walk(source, followlinks=False):
                current = Path(directory)
                dirnames[:] = sorted(
                    name for name in dirnames if not (current / name).is_symlink()
                )
                relative_directory = current.relative_to(root).as_posix()
                if not filenames and not dirnames:
                    entry = f"{relative_directory}/"
                    if entry not in written:
                        archive.writestr(entry, b"")
                        written.add(entry)
                for name in sorted(filenames):
                    candidate = current / name
                    relative = candidate.relative_to(root).as_posix()
                    if not candidate.is_symlink() and relative not in written:
                        archive.write(candidate, relative)
                        written.add(relative)


def ensure_preview_safe(path: Path) -> os.stat_result:
    stat = path.stat()
    if stat.st_size > MAX_PREVIEW_BYTES:
        raise ArchiveResourceError(
            f"archive preview is limited to {MAX_PREVIEW_BYTES // 1024**2} MiB"
        )
    return stat


def scan_archive(
    root: Path, records: list[CaptureFrameRecord], *, limit: int = MAX_ARCHIVE_ENTRIES
) -> list[ArchiveEntry]:
    """Return supported files, newest first, without following symlink dirs."""
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise ArchivePathError("configured capture folder does not exist")
    catalogue = {record.path.resolve(): record for record in records}
    if limit <= 0:
        return []
    # Bound retained metadata, not traversal: an alphabetically early folder
    # must never hide today's captures in a later folder.
    newest: list[tuple[datetime, str, ArchiveEntry]] = []
    for directory, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        current = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and not (current / name).is_symlink()
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = current / name
            suffix = path.suffix.casefold()
            if suffix not in ARCHIVE_SUFFIXES or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            record = catalogue.get(path.resolve())
            entry = _entry_of(resolved_root, path, stat, record)
            candidate = (entry.date_obs, entry.key, entry)
            if len(newest) < limit:
                heapq.heappush(newest, candidate)
            elif candidate[:2] > newest[0][:2]:
                heapq.heapreplace(newest, candidate)
    return [entry for _, _, entry in sorted(newest, reverse=True)]


def _entry_of(
    root: Path, path: Path, stat: os.stat_result, record: CaptureFrameRecord | None
) -> ArchiveEntry:
    relative = path.relative_to(root).as_posix()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    kind: ArchiveKind = "fits" if path.suffix.casefold() in FITS_SUFFIXES else "image"
    header = _fits_header(path) if kind == "fits" else {}

    header_date = _header_datetime(header.get("DATE-OBS"))
    source: DateSource
    if record is not None:
        observed, source = record.date_obs.astimezone(UTC), "capture_catalogue"
    elif header_date is not None:
        observed, source = header_date, "fits_header"
    else:
        observed, source = modified, "filesystem_mtime"

    return ArchiveEntry(
        key=relative,
        name=path.name,
        directory=path.parent.relative_to(root).as_posix(),
        kind=kind,
        size_bytes=stat.st_size,
        modified_at=modified,
        date_obs=observed,
        date_source=source,
        catalogue_id=None if record is None else record.id,
        object_name=_record_or_text(record, "object_name", header.get("OBJECT")),
        filter_name=_record_or_text(record, "filter_name", header.get("FILTER")),
        frame_type=(
            record.frame_type if record is not None else _optional_text(header.get("IMAGETYP"))
        ),
        exposure_s=(
            record.exposure_s if record is not None else _optional_float(header.get("EXPTIME"))
        ),
        gain=record.gain if record is not None else _optional_int(header.get("GAIN")),
        ccd_temp_c=(
            record.ccd_temp_c
            if record is not None
            else _optional_float(
                header["CCD-TEMP"] if "CCD-TEMP" in header else header.get("CCD_TEMP")
            )
        ),
        width=record.width if record is not None else _optional_int(header.get("NAXIS1")),
        height=record.height if record is not None else _optional_int(header.get("NAXIS2")),
        bayer_pattern=_optional_text(header.get("BAYERPAT")),
        preview_available=stat.st_size <= MAX_PREVIEW_BYTES,
    )


def _fits_header(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            blocks = bytearray()
            while len(blocks) < _MAX_FITS_HEADER_BYTES:
                block = handle.read(_FITS_BLOCK_BYTES)
                if len(block) != _FITS_BLOCK_BYTES:
                    return {}
                blocks.extend(block)
                if any(
                    block[offset : offset + 8] == b"END     "
                    for offset in range(0, _FITS_BLOCK_BYTES, 80)
                ):
                    return parse_header(bytes(blocks))[0]
        return {}
    except (OSError, FitsError):
        return {}


def _header_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(
            value.rstrip("Z") + ("+00:00" if value.endswith("Z") else "")
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _record_or_text(
    record: CaptureFrameRecord | None,
    attribute: Literal["object_name", "filter_name"],
    fallback: object,
) -> str | None:
    value = None if record is None else getattr(record, attribute)
    return value if value is not None else _optional_text(fallback)


__all__ = [
    "ARCHIVE_SUFFIXES",
    "MAX_ARCHIVE_ENTRIES",
    "ArchiveDeleteResult",
    "ArchiveEntry",
    "ArchivePathError",
    "ArchiveResourceError",
    "ensure_preview_safe",
    "remove_archive_directory",
    "resolve_archive_directory",
    "resolve_archive_key",
    "scan_archive",
    "scan_archive_directories",
]
