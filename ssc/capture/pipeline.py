# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Orchestrating one capture and recovering from a killed process.

Two entry points:

* :func:`capture_frame` — write one exposure durably. The catalogue row is
  inserted as ``'writing'`` *before* any FITS byte reaches disk (CLAUDE.md
  invariant 6); only once the file has been fsynced and renamed into place
  is the row marked ``'complete'``.
* :func:`reconcile_incomplete` — recover a structurally valid final FITS
  matching its catalogue row after a crash between rename and completion.
  Unverified final files are preserved and reported; only missing-file
  placeholders are discarded.

Neither function talks to a device — ``frame`` arrives already decoded and
already read (``ssc.capture.frame.RawFrame``); this module only ever writes
what it is handed.
"""

from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ssc.capture.fits import FitsWriteError, _fits_ascii, write_fits_file
from ssc.capture.fits_rows import FitsRowSource
from ssc.capture.frame import FrameType, RawFrame
from ssc.capture.pathtemplate import (
    DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    capture_filename_template_without_target,
    render_capture_filename,
    render_capture_path,
)
from ssc.store.capture import CaptureFrameRecord, CaptureRepository

_UTC_TZ = ZoneInfo("UTC")


def frame_filename(
    frame: RawFrame,
    *,
    date_obs: datetime,
    sequence: int,
    template: str = DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    tz: ZoneInfo = _UTC_TZ,
) -> str:
    """Render the configured, human-readable filename (including ``.fits``).

    Never load-bearing for the catalogue (DESIGN.md §9.5.4: sessions are
    derived from ``DATE-OBS``, never from a filename or directory) — this
    only has to be unique and roughly sortable for a human browsing the
    folder directly.
    """
    effective_template = template
    if frame.metadata.frame_type is not FrameType.LIGHT:
        effective_template = capture_filename_template_without_target(template)
    stem = render_capture_filename(
        effective_template,
        metadata=frame.metadata,
        date_obs=date_obs,
        sequence=sequence,
        tz=tz,
    )
    if frame.metadata.frame_type is not FrameType.LIGHT:
        # Calibration matching is catalogue/FITS driven, never filename
        # parsing. Still make the files self-describing for a human who
        # copies them elsewhere. Respect explicit template choices and only
        # add whichever compatibility fields the template omitted.
        compatibility: list[str] = []
        if "%EXPOSURE%" not in effective_template:
            compatibility.append(
                render_capture_filename(
                    "%EXPOSURE%",
                    metadata=frame.metadata,
                    date_obs=date_obs,
                    sequence=sequence,
                    tz=tz,
                )
            )
        if "%GAIN%" not in effective_template:
            rendered_gain = render_capture_filename(
                "%GAIN%",
                metadata=frame.metadata,
                date_obs=date_obs,
                sequence=sequence,
                tz=tz,
            )
            compatibility.append(f"g{rendered_gain}")
        if compatibility:
            stem = f"{stem}_{'_'.join(compatibility)}"
    return f"{stem}.fits"


async def capture_frame(
    *,
    repo: CaptureRepository,
    folder: Path,
    frame: RawFrame,
    date_obs: datetime,
    template: str,
    tz: ZoneInfo,
    filename_template: str = DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    sequence: int = 0,
    run_id: str | None = None,
    block_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Write ``frame`` into ``folder`` and catalogue it. Returns the new
    catalogue row's id.

    ``date_obs`` is the caller's own record of when the exposure started
    (see ``ssc.store.capture.CaptureRepository.begin_frame`` for why this is
    independent of ``frame.metadata.date_obs``, which may be ``None``).

    ``template``/``tz`` place the file beneath a subdirectory rendered by
    ``ssc.capture.pathtemplate.render_capture_path`` — the templated layout
    (``ssc.settings.models.CaptureSettings.folder_template``). ``folder``
    itself is the validated root (``ssc.settings.capture_folder``); every
    subdirectory below it is SSC's own and is created here, on demand — the
    root's existence/writability/free-space checks are not repeated for it.

    ``filename_template`` renders the filename stem from the same tag
    vocabulary. If a rendered name already exists on disk or in the
    catalogue, ``-2``, ``-3``, ... is appended instead of overwriting it.

    ``run_id``/``block_id``: forwarded to ``CaptureRepository.begin_frame``
    unchanged — ``None``/``None`` for a manually triggered capture, the
    scheduled run/item id for a ``take_exposures`` block
    (``ssc.scheduler.executor.CapturePort``).
    """
    if frame.pixels.ndim != 2:
        raise FitsWriteError(f"only a single 2-D plane is written; got shape {frame.pixels.shape}")
    height, width = frame.pixels.shape
    subdir = render_capture_path(
        template, metadata=frame.metadata, date_obs=date_obs, sequence=sequence, tz=tz
    )
    directory = folder / subdir
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    name = frame_filename(
        frame,
        date_obs=date_obs,
        sequence=sequence,
        template=filename_template,
        tz=tz,
    )
    path = directory / name
    collision = 2
    while path.exists() or await repo.path_exists(path):
        path = directory / f"{Path(name).stem}-{collision}.fits"
        collision += 1

    frame_id = await repo.begin_frame(
        path=path,
        date_obs=date_obs,
        object_name=frame.metadata.object_name,
        filter_name=frame.metadata.filter_name,
        frame_type=frame.metadata.frame_type,
        exposure_s=frame.metadata.exposure_s,
        gain=frame.metadata.gain,
        ccd_temp_c=frame.metadata.ccd_temp_c,
        width=int(width),
        height=int(height),
        run_id=run_id,
        block_id=block_id,
        now=now,
    )
    write_fits_file(path, frame.pixels, frame.metadata)
    await repo.complete_frame(frame_id)
    return frame_id


class CaptureRecoveryError(RuntimeError):
    """Unverified final files were retained for explicit recovery."""


def _validate_and_sync_final(record: CaptureFrameRecord) -> None:
    source = FitsRowSource.open(record.path)
    if source.header.get("SIMPLE") is not True:
        raise ValueError("not a standard primary FITS image")
    if source.shape != (record.height, record.width):
        raise ValueError("FITS dimensions disagree with the capture catalogue")
    payload_end = source.data_start + record.height * record.width * source.dtype.itemsize
    expected_size = ((payload_end + 2879) // 2880) * 2880
    if record.path.stat().st_size != expected_size:
        raise ValueError("FITS payload or padding is incomplete or unexpected")
    expected = {
        "IMAGETYP": record.frame_type,
        "EXPTIME": record.exposure_s,
        "GAIN": record.gain,
        "CCD-TEMP": record.ccd_temp_c,
        "OBJECT": record.object_name,
        "FILTER": record.filter_name,
    }
    for key, value in expected.items():
        if value is None:
            continue
        actual = source.header.get(key)
        if isinstance(value, (int, float)):
            matches = isinstance(actual, (int, float)) and math.isclose(actual, value, rel_tol=1e-9, abs_tol=1e-6)
        else:
            # Catalogue strings retain Unicode; FITS cards use the writer's
            # ASCII transliteration, and the header reader trims padding.
            matches = actual == _fits_ascii(value).strip()
        if not matches:
            raise ValueError(f"FITS {key} is absent or disagrees with the capture catalogue")
    # Re-establish durability before counting a file whose process died between
    # its atomic rename and the database completion update.
    with record.path.open("rb") as stream:
        os.fsync(stream.fileno())
    if os.name != "nt":
        directory = os.open(record.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


async def reconcile_incomplete(repo: CaptureRepository) -> list[Path]:
    """Recover verified finals; discard only placeholders with no final file.

    Unknown, malformed or mismatched final files and their writing rows remain
    untouched. Startup fails explicitly so they cannot be silently deleted or
    presented as completed captures.
    """
    removed: list[Path] = []
    unresolved: list[str] = []
    for record in await repo.list_writing():
        if not record.path.exists():
            removed.append(await repo.discard_frame(record.id))
            continue
        try:
            await asyncio.to_thread(_validate_and_sync_final, record)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            unresolved.append(f"frame {record.id}: {exc}")
            continue
        await repo.complete_frame(record.id)
    if unresolved:
        raise CaptureRecoveryError("Capture recovery preserved unverified final files: " + "; ".join(unresolved))
    return removed


__all__ = ["CaptureRecoveryError", "capture_frame", "frame_filename", "reconcile_incomplete"]
