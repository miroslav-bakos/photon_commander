# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``POST /api/capture/exposure``'s own service: the 202/409/422/501
boundary around ``ssc.api.capture_service``, plus the catalogue reads
``GET /api/capture/frames``/``GET /api/capture/sessions`` want.

``ssc.api.capture_service.drive_exposure`` is pinned and verified against
live hardware (module docstring there); this module does not repeat its
metadata-reading logic. It only decides *when* to call it and *what HTTP
status a failure to start one becomes*:

* **409** — no capture folder configured, a capture is already running, or
  the configured capture folder template uses ``%DATEOFCAPTURE%`` and the
  active location has no resolvable timezone
  (:class:`CaptureTimezoneUnknownError`). The first two are checked with no
  device I/O at all; the last needs no device I/O either, but is only
  checked once the camera is known to exist and be idle, so it never masks
  the more specific 503/already-running answer.
* **503** — this process hosts no camera (a substituted gateway/test, or
  ``ssc.alpaca``'s ``Camera`` passthrough not yet landed) — mirrors
  ``ssc.api.run_service.RunDeviceUnavailableError``'s own precedent.
* **422** — the requested duration is outside ``Camera.ExposureMin``/
  ``Camera.ExposureMax``, read live rather than assumed (CLAUDE.md
  invariant 11's sibling rule against hardcoding what a device must answer
  for itself).
* **501** — ``Camera.StartExposure`` answered ``NotImplemented`` (the 8.46
  path capture_service's own docstring describes).

Every one of those is decided *before* the request returns, because each is
a fast, single Alpaca round trip — unlike the exposure itself, which is
never awaited here: once ``StartExposure`` has been accepted,
:meth:`CaptureService.request_exposure` hands the rest (poll, read, write,
catalogue) to a task the caller does not wait on, and returns ``202``. That
split is exactly what ``ssc.api.capture_service.begin_exposure`` was pulled
out of :func:`~ssc.api.capture_service.drive_exposure` to make possible
without commanding ``StartExposure`` twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import random
import shutil
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol, cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
from numpy.typing import NDArray

from ssc.api.capture_service import (
    CameraDevice,
    CaptureMetadataError,
    Device,
    ExposureContext,
    ExposureNotAvailableError,
    begin_exposure,
    capture_one_exposure,
    drive_exposure,
    exposure_metadata_fallbacks,
    light_flag,
)
from ssc.api.external_tools import ConfiguredAstap
from ssc.api.plans_service import PlanTimezoneUnknownError, resolve_timezone
from ssc.api.tasks import TaskRegistry
from ssc.astro.events import Ephemeris
from ssc.astro.frames import frame_offset_arcsec
from ssc.capture import (
    CalibrationMasterError,
    FrameMetadata,
    FrameQuality,
    FrameType,
    Session,
    bayer_pattern,
    build_calibration_master,
    debayer,
    derive_sessions,
    measure_frame_quality,
    render_capture_filename,
)
from ssc.capture.archive import (
    FITS_SUFFIXES,
    ArchiveDeleteResult,
    ArchiveEntry,
    ArchivePathError,
    ArchiveTransferResult,
    archive_directory_stats,
    ensure_preview_safe,
    remove_archive_directory,
    resolve_archive_destination,
    resolve_archive_directory,
    resolve_archive_key,
    scan_archive,
    scan_archive_directories,
    write_archive_selection_zip,
    write_archive_zip,
)
from ssc.capture.calibration import (
    analyse_flat_frame,
    choose_next_flat_exposure,
    is_dark_filter_name,
    is_flat_eligible_filter_name,
)
from ssc.capture.fits_rows import FitsRowSource
from ssc.capture.master_catalogue import (
    CalibrationMasterCatalogueError,
    CalibrationMasterRecord,
    MasterStatus,
    move_calibration_master,
    scan_calibration_masters,
)
from ssc.capture.masters import calibration_optic_id, calibration_optics_match
from ssc.client.errors import AlpacaClientError, AlpacaRemoteError
from ssc.platesolve import (
    PlateSolveResult,
    SolverAvailability,
    SolverUnavailableError,
    astap_availability,
    astap_solve,
)
from ssc.settings.store import SettingsStore
from ssc.skymap.fits import parse_header, read_fits_image
from ssc.skymap.png import (
    balance_rgb_for_display,
    encode_grayscale_png,
    encode_rgb_png,
    nina_autostretch,
    suppress_excess_green_for_display,
)
from ssc.stacking.live import (
    LiveStackEngine,
    LiveStackError,
    LiveStackFrameRejectedError,
    LiveStackRun,
)
from ssc.state.events import CaptureChanged, MountChanged
from ssc.state.models import (
    CalibrationRunKind,
    CalibrationRunState,
    CaptureStage,
    CaptureState,
    FlatProbeState,
)
from ssc.state.store import StateStore
from ssc.store.capture import CaptureFrameNotFoundError, CaptureFrameRecord, CaptureRepository
from ssc.streaming import (
    RtspConfig,
    RtspFrame,
    RtspStatus,
    RtspStreamer,
    YoutubePublisher,
    YoutubeStreamError,
    YoutubeStreamStatus,
)
from ssc.streaming.auto_exposure import (
    WIDE_STREAM_MAX_EXPOSURE_S,
    next_wide_stream_exposure_s,
)
from ssc.system_metrics import default_live_stack_binning

_log = logging.getLogger(__name__)

#: Passed to ``ssc.capture.pipeline.capture_frame`` when the configured
#: template contains no ``%DATEOFCAPTURE%`` tag at all — a real ``ZoneInfo``
#: is required by that call's own type, but nothing renders through it in
#: that case (:meth:`CaptureService._resolve_template_timezone`), so there is
#: no location to consult and no reason to ask for one.
_UNUSED_TZ: Final = ZoneInfo("UTC")

#: A thumbnail's longer side, in pixels — small enough for a timeline strip,
#: large enough to still recognise the frame. Chosen, not measured; there is
#: no requirement pinning this number.
_THUMBNAIL_MAX_DIM: Final = 320

_BAYER_TILES: Final[dict[str, tuple[tuple[str, str], tuple[str, str]]]] = {
    "RGGB": (("R", "G"), ("G", "B")),
    "GRBG": (("G", "R"), ("B", "G")),
    "GBRG": (("G", "B"), ("R", "G")),
    "BGGR": (("B", "G"), ("G", "R")),
}

#: A dither is not complete when its device command merely returns. Wait for
#: fresh mount polls to show both no slew and a stable position before opening
#: the shutter again. The poll interval is deliberately below the gateway's
#: one-second cadence so the first new reading is noticed promptly.
_DITHER_SETTLE_TIMEOUT_S: Final = 60.0
_DITHER_SETTLE_POLL_S: Final = 0.25
_DITHER_COORDINATE_RETRIES: Final = 60
_DITHER_COORDINATE_RETRY_DELAY_S: Final = 1.0
_DITHER_STABLE_ARCSEC: Final = 5.0
_DITHER_STABLE_DURATION_S: Final = 30.0


@dataclass(frozen=True, slots=True)
class _CaptureLoopSpec:
    """Inputs retained by the server for a browser-independent capture loop."""

    duration_s: float
    gain: int | None
    object_name: str | None
    frame_type: FrameType
    optic_id: str | None
    flat_source: str | None
    frame_limit: int | None = None


def _thumbnail_factor(height: int, width: int) -> int:
    """Integer box-downsample factor that brings the longer side to roughly
    :data:`_THUMBNAIL_MAX_DIM`. ``1`` (no-op) for anything already smaller."""
    return max(1, max(height, width) // _THUMBNAIL_MAX_DIM)


def _downsample_mean(pixels: NDArray[np.uint8], factor: int) -> NDArray[np.uint8]:
    """Box-downsample already-stretched ``(h, w)`` or ``(h, w, 3)`` uint8
    pixels by ``factor``, mean-pooling — not the raw float plane, since this
    is a display thumbnail, not a resampled science image. Any leftover rows/
    columns that don't fill a whole ``factor``-sized box are cropped off
    rather than averaged in lopsided."""
    if factor <= 1:
        return pixels
    height, width = pixels.shape[:2]
    cropped = pixels[: height - height % factor, : width - width % factor]
    new_height, new_width = cropped.shape[0] // factor, cropped.shape[1] // factor
    if cropped.ndim == 2:
        reshaped = cropped.reshape(new_height, factor, new_width, factor)
    else:
        reshaped = cropped.reshape(new_height, factor, new_width, factor, cropped.shape[2])
    return reshaped.mean(axis=(1, 3)).astype(np.uint8)


def _bayer_thumbnail_rgb(mosaic: np.ndarray, pattern: str) -> np.ndarray:
    """Reduce a Bayer mosaic to thumbnail RGB before allocating colour planes."""
    tile = _BAYER_TILES.get(pattern.upper())
    if tile is None:
        return debayer(mosaic.astype(np.float64), pattern)
    factor = _thumbnail_factor(*mosaic.shape)
    if factor <= 1:
        return debayer(mosaic.astype(np.float64), pattern)
    # Whole, even blocks contain equal numbers of each Bayer-tile position,
    # preserving colour while reducing a 4K sensor plane to preview size.
    factor += factor % 2
    height = mosaic.shape[0] // factor * factor
    width = mosaic.shape[1] // factor * factor
    blocks = mosaic[:height, :width].reshape(height // factor, factor, width // factor, factor)
    channels: list[np.ndarray] = []
    for channel in ("R", "G", "B"):
        positions = [
            blocks[:, row::2, :, column::2].mean(axis=(1, 3), dtype=np.float32)
            for row in range(2)
            for column in range(2)
            if tile[row][column] == channel
        ]
        channels.append(sum(positions, np.zeros_like(positions[0])) / len(positions))
    return np.stack(channels, axis=-1)


def _render_frame_png(raw: bytes, *, thumbnail: bool) -> bytes:
    """Synchronous body of :meth:`CaptureService.get_frame_image` — decode,
    debayer, stretch, downsample, encode. Kept as a plain function (rather
    than inlined) so it can run through :func:`asyncio.to_thread` as one
    unit; see that method's docstring for why it must not run on the event
    loop."""
    image = read_fits_image(raw)
    plane = image.data if image.data.ndim == 2 else image.data[0]
    pattern = image.header.get("BAYERPAT")
    if image.data.ndim == 2 and isinstance(pattern, str):
        rgb = _bayer_thumbnail_rgb(plane, pattern) if thumbnail else debayer(plane, pattern)
        pixels_rgb = suppress_excess_green_for_display(
            nina_autostretch(balance_rgb_for_display(rgb))
        )
        if thumbnail and max(pixels_rgb.shape[:2]) > _THUMBNAIL_MAX_DIM:
            pixels_rgb = _downsample_mean(
                pixels_rgb, _thumbnail_factor(pixels_rgb.shape[0], pixels_rgb.shape[1])
            )
        return encode_rgb_png(pixels_rgb)
    if image.data.ndim == 3 and image.data.shape[0] == 3:
        rgb = np.moveaxis(image.data.astype(np.float64), 0, -1)
        pixels_rgb = suppress_excess_green_for_display(
            nina_autostretch(balance_rgb_for_display(rgb))
        )
        if thumbnail:
            pixels_rgb = _downsample_mean(
                pixels_rgb, _thumbnail_factor(pixels_rgb.shape[0], pixels_rgb.shape[1])
            )
        return encode_rgb_png(pixels_rgb)
    pixels = nina_autostretch(plane)
    if thumbnail:
        pixels = _downsample_mean(pixels, _thumbnail_factor(*pixels.shape))
    return encode_grayscale_png(pixels)


def _measure_frame_quality(raw: bytes) -> FrameQuality:
    """Synchronous body of :meth:`CaptureService.get_frame_quality` — decode,
    debayer/green-channel-reduce, measure. Kept as a plain function, run
    through :func:`asyncio.to_thread`, for the same reason
    :func:`_render_frame_png` is: this is CPU work that must not serialise
    behind it on the event loop.

    Debayers and takes the green channel whenever ``BAYERPAT`` is present —
    the star detector wants one luminance-like plane, and green carries most
    of a Bayer sensor's luminance information (the same reasoning
    ``ssc.skymap.cutouts`` and ``_render_frame_png`` apply to make an RGB
    preview; here it is one channel, not a stack, since the detector has no
    use for colour). ``XPIXSZ``/``FOCALLEN`` are read straight off the FITS
    header exactly like :func:`ssc.capture.fits.write_frame_header` wrote
    them; either being absent means arcsec fields come back ``None``
    (invariant 7), never a guessed pixel scale.
    """
    image = read_fits_image(raw)
    plane = image.data if image.data.ndim == 2 else image.data[0]
    pattern = image.header.get("BAYERPAT")
    if image.data.ndim == 2 and isinstance(pattern, str):
        rgb = debayer(plane, pattern)
        plane = rgb[..., 1]
    elif image.data.ndim == 3 and image.data.shape[0] == 3:
        plane = image.data[1]
    pixel_size_um = image.header.get("XPIXSZ")
    focal_length_mm = image.header.get("FOCALLEN")
    return measure_frame_quality(
        plane.astype(np.float64),
        pixel_size_um=float(pixel_size_um) if isinstance(pixel_size_um, int | float) else None,
        focal_length_mm=float(focal_length_mm)
        if isinstance(focal_length_mm, int | float)
        else None,
    )


class CaptureFolderNotConfiguredError(Exception):
    """No capture folder is set (Settings > Capture) — the one place SSC
    writes gigabytes a night must be chosen before the first frame, not
    guessed (``ssc.settings.capture_folder`` module docstring)."""


class CaptureAlreadyRunningError(Exception):
    """A capture is already in progress; SSC drives one exposure at a time."""


class ArchiveDeleteBusyError(Exception):
    """Archive bytes are in use by capture or live stacking."""


class CalibrationRunNotRunningError(Exception):
    """Cancellation was requested with no automatic calibration run active."""


class CalibrationConfigurationError(Exception):
    """The requested calibration cannot be made safely with current hardware/state."""


class _CalibrationCancelledError(Exception):
    """Internal cooperative stop, checked only between complete exposures."""


class CaptureDeviceUnavailableError(Exception):
    """This process hosts no camera to expose."""


class CaptureDurationOutOfRangeError(Exception):
    """The requested duration is outside the camera's own reported limits."""

    def __init__(self, duration_s: float, minimum_s: float, maximum_s: float) -> None:
        super().__init__(
            f"duration {duration_s}s is outside this camera's own limits "
            f"[{minimum_s}, {maximum_s}]s (Camera.ExposureMin/ExposureMax)"
        )
        self.duration_s = duration_s
        self.minimum_s = minimum_s
        self.maximum_s = maximum_s


class CaptureTimezoneUnknownError(Exception):
    """The configured capture folder template uses ``%DATEOFCAPTURE%``, but
    the active observing location has no resolvable timezone
    (``ssc.api.plans_service.resolve_timezone``'s own error, renamed here:
    a capture-time failure, not a plan-compile one). Like
    :class:`CaptureFolderNotConfiguredError`, this is a configuration gap
    surfaced before anything is commanded, not a device error."""


class CaptureFrameNotReadyError(Exception):
    """The requested frame's row is still ``'writing'`` — its bytes are not
    yet fsynced into their final place (``CaptureRepository.complete_frame``
    has not run), so there is nothing safe to serve yet. Never serve a
    possibly-incomplete file; make the caller wait and ask again."""


class CaptureDitherSettleError(Exception):
    """The dither command returned, but fresh mount telemetry did not confirm
    that movement stopped and the pointing settled before the timeout."""


class CaptureDitherProbeCleanupError(Exception):
    """Diagnostic camera ownership cannot be released until idle is verified."""


class CaptureDitherStoppedError(CaptureDitherSettleError):
    """A durable run Stop interrupts scheduled dither recovery."""


class CaptureDitherWindowEndedError(CaptureDitherSettleError):
    """The science window ended; a stopped mount can hand off to cleanup."""


class CaptureSolverUnavailableError(Exception):
    """No plate solver can be used for a catalogued frame right now."""

    def __init__(self, availability: SolverAvailability) -> None:
        super().__init__(availability.reason or "plate solving is unavailable")
        self.availability = availability


class GainSettableCamera(CameraDevice, Protocol):
    """``CameraDevice`` plus the one write this module needs of its own:
    ``Camera.Gain`` is a standard read/write ASCOM member, but
    ``capture_service.CameraDevice`` only ever *reads* it (for the FITS
    header, after the fact) — setting it before the exposure starts is this
    module's own concern, not the pinned driver's."""

    async def put(self, member: str, **params: object) -> object: ...


class FilterWheelDevice(Device, Protocol):
    async def put(self, member: str, **params: object) -> object: ...


@dataclass(frozen=True, slots=True)
class CalibrationSourceSet:
    """One contiguous, metadata-compatible set of retained raw frames."""

    records: tuple[CaptureFrameRecord, ...]
    frame_type: FrameType
    gain: int | None
    exposure_s: float | None
    filter_name: str | None
    width: int
    height: int
    optic_id: str | None = None

    @property
    def started_at(self) -> datetime:
        return self.records[0].date_obs

    @property
    def ended_at(self) -> datetime:
        return self.records[-1].date_obs


def _calibration_source_sets(
    records: list[CaptureFrameRecord],
) -> list[CalibrationSourceSet]:
    calibration = [
        record
        for record in records
        if record.status == "complete"
        and record.frame_type in {"bias", "dark", "flat"}
        and record.path.is_file()
    ]
    groups: list[CalibrationSourceSet] = []
    for session in derive_sessions(calibration):
        compatible: dict[
            tuple[str, int | None, float | None, str | None, int, int, str | None],
            list[CaptureFrameRecord],
        ] = {}
        for record in session.frames:
            try:
                optic = calibration_optic_id(FitsRowSource.open(record.path).header)
            except (OSError, ValueError):
                continue
            key = (
                record.frame_type,
                record.gain,
                record.exposure_s,
                record.filter_name,
                record.width,
                record.height,
                optic,
            )
            compatible.setdefault(key, []).append(record)
        for key, grouped in compatible.items():
            frame_type, gain, exposure_s, filter_name, width, height, optic = key
            groups.append(
                CalibrationSourceSet(
                    records=tuple(sorted(grouped, key=lambda item: item.date_obs)),
                    frame_type=FrameType(frame_type),
                    gain=gain,
                    exposure_s=exposure_s,
                    filter_name=filter_name,
                    width=width,
                    height=height,
                    optic_id=optic,
                )
            )
    return sorted(groups, key=lambda group: group.ended_at, reverse=True)


def _best_bias_source_set(
    groups: list[CalibrationSourceSet],
    *,
    gain: int | None,
    width: int,
    height: int,
    optic_id: str | None = None,
) -> CalibrationSourceSet | None:
    return next(
        (
            group
            for group in groups
            if group.frame_type is FrameType.BIAS
            and group.gain == gain
            and group.width == width
            and group.height == height
            and (group.optic_id or "tele") == (optic_id or "tele")
            and len(group.records) >= 10
        ),
        None,
    )


def _canonical_filter_name(value: object) -> str | None:
    """Normalise equivalent Seestar filter labels across FITS producers."""
    if not isinstance(value, str):
        return None
    token = "".join(character for character in value.casefold() if character.isalnum())
    if token in {"ir", "ircut", "uvir", "uvircut"}:
        return "uvircut"
    return token or None


def _canonical_optic_id(header: dict[str, object]) -> str | None:
    """Read an optic from SSC's OPTICID or Seestar's WIDECAM convention."""
    optic = header.get("OPTICID")
    if isinstance(optic, str) and (token := optic.strip().casefold()):
        if token in {"main", "primary", "tele"}:
            return "tele"
        if token in {"secondary", "wide"}:
            return "wide"
        return token
    wide_camera = header.get("WIDECAM")
    if isinstance(wide_camera, bool):
        return "wide" if wide_camera else "tele"
    if isinstance(wide_camera, int | float) and wide_camera in {0, 1}:
        return "wide" if bool(wide_camera) else "tele"
    return None


async def _ensure_connected(device: Device) -> None:
    if not await device.is_connected():
        await device.connect()


def _as_float(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CaptureMetadataError(f"{label} returned {value!r}, not a number")
    return float(value)


def _as_positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CaptureMetadataError(f"{label} returned {value!r}, not a positive integer")
    return value


def _equatorial_separation_arcsec(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Small-angle separation for nearby ``(RA hours, Dec degrees)`` points."""
    ra_delta_h = abs(first[0] - second[0]) % 24.0
    ra_delta_deg = min(ra_delta_h, 24.0 - ra_delta_h) * 15.0
    mean_dec_rad = math.radians((first[1] + second[1]) / 2.0)
    return (
        math.hypot(
            ra_delta_deg * math.cos(mean_dec_rad),
            first[1] - second[1],
        )
        * 3600.0
    )


def _dither_due(
    *, frame_type: FrameType, enabled: bool, completed_lights: int, every_n_frames: int
) -> bool:
    """Whether the next light starts with a configured cadence dither."""
    return (
        frame_type is FrameType.LIGHT
        and enabled
        and completed_lights > 0
        and completed_lights % every_n_frames == 0
    )


def _ssc_version() -> str:
    try:
        return importlib.metadata.version("ssc")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def initial_capture_state(
    *, last: CaptureFrameRecord | None, folder_configured: bool
) -> CaptureState:
    """What the very first snapshot carries for a fresh process.

    The preview is intentionally session-local: a frame left in a capture
    folder from an earlier run must never be presented as the current preview.
    The catalogue still retains every frame, and a new capture publishes its
    id immediately for the preview to follow.
    """
    del last
    return CaptureState(
        in_progress=False,
        last_frame_id=None,
        last_captured_at=None,
        last_error=None,
        folder_configured=folder_configured,
    )


class CaptureQualityBusyError(Exception):
    """The bounded quality-analysis queue is full; callers may retry later."""


# Separate from asyncio's general I/O pool. One CPU-heavy raw-FITS analysis
# globally, even while old/new CaptureService instances overlap at shutdown.
_QUALITY_COMPUTE_LOCK = threading.Lock()
_QUALITY_ALGORITHM_VERSION = 1
_QUALITY_MAX_PENDING = 16


def _quality_identity(path: Path) -> str:
    stat = path.stat()
    return f"{_QUALITY_ALGORITHM_VERSION}:{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"


class _FrameQualityCache:
    def __init__(self, repo: CaptureRepository) -> None:
        self._repo = repo
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="frame-quality")
        self._pending: dict[tuple[Path, str], asyncio.Task[FrameQuality]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def get(self, path: Path) -> FrameQuality:
        if self._closed:
            raise CaptureQualityBusyError("Frame quality analysis is shutting down; retry shortly.")
        path = await asyncio.to_thread(path.resolve)
        identity = await asyncio.to_thread(_quality_identity, path)
        payload = await self._repo.get_quality_cache(path, identity)
        if payload is not None:
            # Validate on every warm read too: never serve a deleted/replaced file.
            if await asyncio.to_thread(_quality_identity, path) == identity:
                return FrameQuality(**json.loads(payload))
            raise CaptureFrameNotReadyError("frame changed while reading cached quality; retry")
        key = (path, identity)
        task = self._pending.get(key)
        if task is None:
            if self._closed or len(self._pending) >= _QUALITY_MAX_PENDING:
                raise CaptureQualityBusyError("Frame quality analysis is busy; retry shortly.")
            task = asyncio.create_task(self._compute(path, identity), name="frame-quality")
            self._pending[key] = task

            def finished(done: asyncio.Task[FrameQuality]) -> None:
                self._pending.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _compute(self, path: Path, identity: str) -> FrameQuality:
        async with self._lock:
            # Another request/service may have completed this file while queued.
            payload = await self._repo.get_quality_cache(path, identity)
            if payload is not None and await asyncio.to_thread(_quality_identity, path) == identity:
                return FrameQuality(**json.loads(payload))
            if await asyncio.to_thread(_quality_identity, path) != identity:
                raise CaptureFrameNotReadyError("frame changed before quality analysis; retry")
            def measure() -> FrameQuality:
                with _QUALITY_COMPUTE_LOCK:
                    return _measure_frame_quality(path.read_bytes())

            future = asyncio.get_running_loop().run_in_executor(self._worker, measure)
            try:
                quality = await asyncio.shield(future)
            except asyncio.CancelledError:
                # Cancelling an asyncio waiter cannot stop its CPU thread. Keep
                # the permit until it really finishes, including during shutdown.
                with contextlib.suppress(Exception):
                    await asyncio.shield(future)
                raise
            if await asyncio.to_thread(_quality_identity, path) != identity:
                raise CaptureFrameNotReadyError("frame changed during quality analysis; retry")
            await self._repo.put_quality_cache(path, identity, json.dumps(asdict(quality)))
            return quality

    async def aclose(self) -> None:
        self._closed = True
        pending = tuple(self._pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # All submitted work has finished; shutdown cannot block the loop.
        self._worker.shutdown(wait=False, cancel_futures=True)


class CaptureService:
    """Drives ``ssc.api.capture_service`` from a request, publishing the
    ``capture`` snapshot section around it. See the module docstring."""

    def __init__(
        self,
        *,
        camera: GainSettableCamera | None,
        wide_camera: GainSettableCamera | None = None,
        telescope: Device | None,
        filterwheel: FilterWheelDevice | None,
        focuser: Device | None = None,
        repo: CaptureRepository,
        settings: SettingsStore,
        state: StateStore,
        tasks: TaskRegistry,
        section: CaptureState,
        solve: Callable[[Path], Awaitable[PlateSolveResult]] = astap_solve,
        availability: Callable[[], Awaitable[SolverAvailability]] = astap_availability,
        dither: Callable[[float, float], Awaitable[None]] | None = None,
        dither_abort: Callable[[], Awaitable[None]] | None = None,
        dither_recovery_recorder: Callable[[], Awaitable[bool]] | None = None,
        dither_pixel_scale_arcsec: float | None = None,
        recenter: Callable[[float, float], Awaitable[None]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        scheduled_targets: Callable[[], tuple[str | None, str | None, datetime | None]] = lambda: (
            None,
            None,
            None,
        ),
        solve_dither_probe: Callable[[], Awaitable[PlateSolveResult]] | None = None,
        cleanup_dither_probe: Callable[[], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        auxiliary_camera_busy: Callable[[], bool] = lambda: False,
        ephemeris: Ephemeris | None = None,
    ) -> None:
        self._cleanup_dither_probe = cleanup_dither_probe
        self._dither_probe_cleanup_pending = False
        self._monotonic = monotonic
        self._solve_dither_probe = solve_dither_probe
        self._auxiliary_camera_busy = auxiliary_camera_busy
        self._wide_cleanup_error: str | None = None
        self._camera = camera
        self._wide_camera = wide_camera
        self._telescope = telescope
        self._filterwheel = filterwheel
        self._focuser = focuser
        self._repo = repo
        self._quality_cache = _FrameQualityCache(repo)
        self._settings = settings
        self._state = state
        self._tasks = tasks
        self._section = section
        configured_solver = ConfiguredAstap(settings)
        self._solve = configured_solver.solve if solve is astap_solve else solve
        self._availability = (configured_solver.availability
                              if availability is astap_availability else availability)
        self._dither = dither
        self._dither_recovery_recorder = dither_recovery_recorder
        self._dither_abort = dither_abort
        self._dither_pixel_scale_arcsec = dither_pixel_scale_arcsec
        self._recenter = recenter
        self._now = now
        self._sleep = sleep
        self._scheduled_targets = scheduled_targets
        self._ephemeris = ephemeris
        self._sequence = 0
        self._light_sequence = 0
        self._manual_dither_group_id = uuid4().hex
        self._manual_dither_group_index = 0
        self._dither_center_j2000: tuple[float, float] | None = None
        self._dither_center_explicit = False
        self._last_dither_target_j2000: tuple[float, float] | None = None
        self._dither_object_name: str | None = None
        self._center_reference: PlateSolveResult | None = None
        self._manual_camera_wide = False
        self._live_stack: LiveStackEngine | None = None
        self._live_stack_seen: set[Path] = set()
        self._live_stack_pending_sizes: dict[Path, int] = {}
        self._live_stack_watch_folder: Path | None = None
        self._live_stack_watch_recursive = False
        self._live_stack_external = False
        self._live_stack_preview_task: asyncio.Task[bytes] | None = None
        self._live_stack_preview_key: tuple[int, int, int, bool] | None = None
        self._live_stack_preview_cache: tuple[tuple[int, int, int, bool], bytes] | None = None
        self._live_stack_lock = asyncio.Lock()
        self._wide_capture_lock = asyncio.Lock()
        self._live_stack_processing_file: Path | None = None
        self._live_stack_pending_count = 0
        self._scheduled_live_stack_queue: asyncio.Queue[int] | None = None
        self._scheduled_live_stack_worker: asyncio.Task[None] | None = None
        self._scheduled_live_stack_baseline_paths: tuple[Path, ...] = ()
        self._scheduled_live_stack_scope: tuple[str, str, int | None] | None = None

        self._live_stack_task: asyncio.Task[None] | None = None
        self._live_stack_active = False
        self._live_stack_error: str | None = None
        self._live_stack_save_error: str | None = None
        self._live_stack_rejected_count = 0
        self._live_stack_last_rejection_reason: str | None = None
        self._live_stack_rejected_folder: Path | None = None
        self._live_stack_optic_id: str | None = None
        self._live_stack_master_selection_message: str | None = None
        self._live_stack_auto_select_masters = True
        self._rtsp = RtspStreamer(
            self._live_stack_rtsp_frame,
            task_factory=lambda coro: self._tasks.spawn(coro, name="live-stack-rtsp"),
        )
        self._youtube = YoutubePublisher(
            task_factory=lambda coro: self._tasks.spawn(coro, name="youtube-publisher")
        )
        self._wide_rtsp_pixels: np.ndarray | None = None
        self._wide_rtsp_bayer_pattern: str | None = None
        self._wide_rtsp_temperature_c: float | None = None
        self._wide_rtsp_exposure_s: float | None = None
        self._wide_rtsp_min_exposure_s: float | None = None
        self._wide_rtsp_max_exposure_s: float | None = None
        self._wide_rtsp_last_exposure_s: float | None = None
        self._wide_rtsp_version = 0
        self._wide_rtsp_task: asyncio.Task[None] | None = None
        self._rtsp_automation_task: asyncio.Task[None] | None = None
        self._rtsp_automatic = False
        self._calibration_task: asyncio.Task[None] | None = None
        self._calibration_cancel_requested = False
        self._calibration_target_count: int | None = None
        self._capture_loop: _CaptureLoopSpec | None = None
        self._archive_render_cache: dict[tuple[str, int, int, bool], bytes] = {}
        self._archive_render_tasks: dict[tuple[str, int, int, bool], asyncio.Task[bytes]] = {}
        self._unsupported_metadata_members: set[str] = set()
        self._wide_unsupported_metadata_members: set[str] = set()
        self._metadata_connection_key: tuple[object, ...] | None = None

    def metadata_capabilities(self, *, wide: bool = False) -> set[str]:
        """Share proven NotImplemented reads until the connection/firmware changes."""
        snapshot = self._state.snapshot
        device, link = snapshot.device, snapshot.link
        key = (None if device is None else (device.host, device.firmware, device.serial),
               None if link is None else (link.endpoint, link.changed_at))
        if key != self._metadata_connection_key:
            self._unsupported_metadata_members.clear()
            self._wide_unsupported_metadata_members.clear()
            self._metadata_connection_key = key
        return self._wide_unsupported_metadata_members if wide else self._unsupported_metadata_members

    def set_dither_pixel_scale_arcsec(self, value: float | None) -> None:
        """Switch model-specific dither geometry when the active scope changes."""
        self._dither_pixel_scale_arcsec = value

    def start_stream_automation(self) -> None:
        """Watch authoritative mount state for the persisted idle stream policy."""
        if self._rtsp_automation_task is None or self._rtsp_automation_task.done():
            self._rtsp_automation_task = self._tasks.spawn(
                self._run_stream_automation(), name="wide-camera-stream-automation"
            )

    async def _run_stream_automation(self) -> None:
        subscription = self._state.subscribe()
        try:
            await self.reconcile_stream_automation()
            while True:
                item = await subscription.queue.get()
                if isinstance(item.event, MountChanged):
                    await self.reconcile_stream_automation()
        finally:
            self._state.unsubscribe(subscription)

    async def reconcile_stream_automation(self) -> None:
        """Apply the persisted broadcasting policy to current mount/stack state."""
        settings = self._settings.streaming
        mount = self._state.snapshot.mount
        unparked = mount is not None and mount.at_park is False
        requested = settings.broadcast_when_unparked and unparked
        if not requested:
            if self._rtsp_automatic and self._rtsp.status.active:
                # Starting YouTube is an explicit user action. It temporarily
                # promotes an automatically-started RTSP stream into a source
                # the user owns: parking or disabling idle automation must not
                # tear the relay out from under that manual broadcast. Once
                # YouTube is stopped, ``stop_youtube_stream`` reconciles again
                # and releases RTSP if the automatic condition is still false.
                if self._youtube.status.active:
                    return
                await self.stop_live_stack_rtsp()
            return
        if not self._rtsp.status.active:
            try:
                await self.start_live_stack_rtsp(
                    self._configured_rtsp_config(),
                    automatic=True,
                )
            except Exception as exc:
                # A transport/metadata timeout must not kill the policy watcher.
                # Cancellation is BaseException and remains authoritative.
                _log.warning("automatic wide-camera stream could not start: %s: %s", type(exc).__name__, exc)
        else:
            self._ensure_wide_rtsp_polling()

    def _configured_rtsp_config(self) -> RtspConfig:
        settings = self._settings.streaming
        return RtspConfig(
            host=settings.rtsp_host,
            port=settings.rtsp_port,
            path=settings.rtsp_path,
            width=settings.rtsp_width,
            height=settings.rtsp_height,
            fps=settings.rtsp_fps,
            bitrate_kbps=settings.rtsp_bitrate_kbps,
            update_interval_s=settings.rtsp_update_interval_s,
            auto_crop=settings.rtsp_auto_crop,
            # Source selection is deterministic: wide camera until a stack
            # exists, then stack, then wide camera again.
            wide_camera_until_stacked=True,
        )

    @property
    def section(self) -> CaptureState:
        return self._section

    def set_folder_configured(self, configured: bool) -> CaptureState:
        """Mirror a folder-setting change into the snapshot immediately —
        called by ``ssc.api.routes.settings`` right after
        ``SettingsStore.set_capture_folder`` returns, the same "apply to the
        live service, not only the store" rule ``set_metno_contact`` and
        ``set_object_info_enabled`` already follow."""
        return self._publish(folder_configured=configured)

    def _publish(self, **updates: object) -> CaptureState:
        self._section = self._section.model_copy(update=updates)
        self._state.apply(CaptureChanged(capture=self._section))
        return self._section

    async def request_exposure(
        self,
        *,
        duration_s: float,
        gain: int | None,
        object_name: str | None,
        frame_type: FrameType,
        optic_id: str | None = None,
        flat_source: str | None = None,
    ) -> CaptureState:
        """Validate and command ``StartExposure`` synchronously (all of it a
        fast Alpaca round trip — module docstring), then hand the poll/read/
        write/catalogue tail to a background task and return. Raises one of
        this module's own errors, or :class:`ExposureNotAvailableError`
        (``ssc.api.capture_service``), for the route to map to a status.
        """
        if self._section.loop_active:
            raise CaptureAlreadyRunningError(
                "a capture loop is already running; stop it before starting another capture"
            )
        self.clear_scheduled_dither_center()
        return await self._request_exposure(
            duration_s=duration_s,
            gain=gain,
            object_name=object_name,
            frame_type=frame_type,
            optic_id=optic_id,
            flat_source=flat_source,
            loop_automation=False,
        )

    async def start_capture_loop(
        self,
        *,
        duration_s: float,
        gain: int | None,
        object_name: str | None,
        frame_type: FrameType,
        optic_id: str | None = None,
        flat_source: str | None = None,
        frame_limit: int | None = None,
    ) -> CaptureState:
        """Start a repeating capture job owned entirely by this process."""
        await self._recover_failed_exposure()
        if self._section.in_progress or self._section.loop_active:
            raise CaptureAlreadyRunningError("a capture or capture loop is already running")
        self.clear_scheduled_dither_center()
        spec = _CaptureLoopSpec(
            duration_s=duration_s,
            gain=gain,
            object_name=object_name,
            frame_type=frame_type,
            optic_id=optic_id,
            flat_source=flat_source,
            frame_limit=frame_limit,
        )
        self._capture_loop = spec
        self._publish(
            loop_active=True, loop_completed_frames=0, loop_frame_limit=frame_limit, last_error=None
        )
        try:
            return await self._request_exposure(
                duration_s=spec.duration_s,
                gain=spec.gain,
                object_name=spec.object_name,
                frame_type=spec.frame_type,
                optic_id=spec.optic_id,
                flat_source=spec.flat_source,
                loop_automation=True,
            )
        except Exception:
            self._capture_loop = None
            self._publish(loop_active=False)
            raise

    def stop_capture_loop(self) -> CaptureState:
        """Prevent another loop frame; an exposure already accepted finishes."""
        if not self._section.loop_active:
            raise CaptureAlreadyRunningError("no capture loop is running")
        self._capture_loop = None
        return self._publish(loop_active=False)

    async def _request_exposure(
        self,
        *,
        duration_s: float,
        gain: int | None,
        object_name: str | None,
        frame_type: FrameType,
        optic_id: str | None,
        flat_source: str | None,
        loop_automation: bool,
    ) -> CaptureState:
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError(
                "no capture folder is configured; set one in Settings > Capture "
                "before starting a capture"
            )
        wide = optic_id is not None and optic_id.casefold() == "wide"
        camera = self._wide_camera if wide else self._camera
        if camera is None:
            raise CaptureDeviceUnavailableError(
                f"this ssc-core hosts no {'Wide' if wide else 'Tele'} camera, so a capture cannot be started"
            )
        if wide and frame_type in {FrameType.DARK, FrameType.BIAS}:
            raise ExposureNotAvailableError(
                "Wide dark/bias capture is unavailable: no verified Wide shutter or dark filter"
            )
        if (
            wide
            and loop_automation
            and frame_type is FrameType.LIGHT
            and self._settings.capture.dither_enabled
        ):
            raise ExposureNotAvailableError(
                "Wide loop dithering is unavailable without a calibrated Wide pixel scale; "
                "disable dithering before starting a Wide loop"
            )
        await self._recover_failed_exposure()
        if self._wide_preview_busy():
            raise CaptureAlreadyRunningError(
                "camera acquisition is already running; only one exposure runs at a time"
            )
        # Claimed synchronously, with no ``await`` between the check above
        # and this — closes the race a second near-simultaneous request
        # would otherwise find open (module docstring: mutation here is
        # synchronous and single-threaded, mirroring ``ssc.state.store``'s
        # own discipline).
        self._publish(
            in_progress=True,
            last_error=None,
            # A new ordinary exposure ends the previous calibration result's
            # ownership of the Capture preview. Its generated master remains
            # durable in Archive; live preview follows this exposure again.
            calibration=CalibrationRunState(),
        )
        try:
            await self.prepare_main_capture()
            minimum_s, maximum_s = await self._read_limits(camera)
            # A physical camera cannot expose for literal zero time. The API's
            # zero is an explicit bias sentinel; resolve it to this camera's
            # live shortest supported exposure rather than hardcoding a value
            # or asking the device to violate its own ExposureMin.
            actual_duration_s = minimum_s if frame_type is FrameType.BIAS else duration_s
            if not (minimum_s <= actual_duration_s <= maximum_s):
                raise CaptureDurationOutOfRangeError(actual_duration_s, minimum_s, maximum_s)
            # Resolved here, not earlier: every check above is instant and
            # device-free, but this one is still cheap and still belongs
            # before ``StartExposure`` is ever commanded — never mid-capture.
            template = self._settings.capture.folder_template
            filename_template = self._settings.capture.filename_template
            tz = self._resolve_template_timezone(f"{template}/{filename_template}")
            if gain is not None:
                await camera.put("gain", Gain=gain)
            if frame_type in {FrameType.BIAS, FrameType.DARK}:
                await self._select_dark_filter_if_available()
            capture_number = self._light_sequence
            dither_settings = self._settings.capture
            dither_applied = False
            if loop_automation and frame_type is FrameType.LIGHT:
                self._remember_dither_center(object_name)
            if (
                loop_automation
                and self._dither is not None
                and _dither_due(
                    frame_type=frame_type,
                    enabled=dither_settings.dither_enabled,
                    completed_lights=capture_number,
                    every_n_frames=dither_settings.dither_every_n_frames,
                )
            ):
                self._publish(
                    stage=CaptureStage.DITHERING,
                    object_name=object_name,
                    frame_type=frame_type,
                    gain=gain,
                )
                pixels = random.uniform(
                    dither_settings.dither_min_pixels, dither_settings.dither_max_pixels
                )
                target_ra_h, target_dec_deg = self._next_dither_target(
                    pixels=pixels,
                    max_pixels=dither_settings.dither_max_pixels,
                )
                command_from_poll = self._state.snapshot.mount
                command_from_polled_at = (
                    None if command_from_poll is None else command_from_poll.polled_at
                )
                await self._dither(target_ra_h, target_dec_deg)
                await self._wait_for_dither_settle(
                    target_j2000=(target_ra_h, target_dec_deg),
                    after_polled_at=command_from_polled_at,
                )
                dither_applied = True
                self._manual_dither_group_index += 1
            started_at = await begin_exposure(
                camera=camera,
                telescope=self._telescope,
                filterwheel=None if wide else self._filterwheel,
                duration_s=actual_duration_s,
                light=light_flag(frame_type),
                now=self._now,
            )
        except Exception:
            self._publish(
                in_progress=False,
                stage=None,
                duration_s=None,
                started_at=None,
                object_name=None,
                frame_type=None,
                gain=None,
            )
            raise

        # Different optics have different boresights. Keep failed requests
        # from changing the reference; the accepted first frame of the new
        # camera establishes its own drift reference when it is solved.
        if wide != self._manual_camera_wide:
            self._center_reference = None
            self._manual_camera_wide = wide

        # All six known synchronously, right after the device actually
        # started exposing — the first point a client can be told what is
        # running and for how long (module docstring: stage/duration_s/etc.
        # describe *this* exposure and are cleared together in `_finish`).
        self._publish(
            stage=CaptureStage.EXPOSING,
            duration_s=actual_duration_s,
            started_at=started_at,
            object_name=object_name,
            frame_type=frame_type,
            gain=gain,
        )

        if frame_type is FrameType.LIGHT:
            self._light_sequence += 1
        self._sequence += 1
        context = replace(
            self._context(object_name, optic_id=optic_id or "tele", flat_source=flat_source),
            wide_camera=wide,
            dither_group_id=(
                f"{self._manual_dither_group_id}:{self._manual_dither_group_index}"
                if loop_automation and frame_type is FrameType.LIGHT and dither_settings.dither_enabled
                else None
            ),
        )
        self._tasks.spawn(
            self._finish(
                camera=camera,
                wide=wide,
                started_at=started_at,
                duration_s=actual_duration_s,
                context=context,
                frame_type=frame_type,
                folder_template=template,
                filename_template=filename_template,
                tz=tz,
                sequence=self._sequence,
                folder=folder,
                dither_applied=dither_applied,
                center_after_drift=loop_automation,
                loop_automation=loop_automation,
            ),
            name=f"capture-exposure-{self._sequence}",
        )
        return self._section

    def set_dither_center(self, ra_j2000_h: float, dec_j2000_deg: float) -> None:
        """Pin a successfully optically verified scheduled panel's intended center."""
        self._dither_center_j2000 = (ra_j2000_h, dec_j2000_deg)
        self._last_dither_target_j2000 = self._dither_center_j2000
        self._dither_center_explicit = True

    def clear_scheduled_dither_center(self) -> None:
        """Release scheduled panel ownership before manual capture or stack cleanup."""
        if self._dither_center_explicit:
            self._dither_center_j2000 = None
            self._last_dither_target_j2000 = None
            self._dither_object_name = None
            self._dither_center_explicit = False

    def _remember_dither_center(self, object_name: str | None) -> None:
        """Anchor a light sequence to its intended, pre-dither sky position."""
        if self._dither_center_explicit:
            self._dither_object_name = object_name
            return
        mount = self._state.snapshot.mount
        if mount is None or mount.ra_j2000_h is None or mount.dec_j2000_deg is None:
            return
        if self._dither_center_j2000 is None or object_name != self._dither_object_name:
            position = (mount.ra_j2000_h, mount.dec_j2000_deg)
            self._dither_center_j2000 = position
            self._last_dither_target_j2000 = position
            self._dither_object_name = object_name

    def _next_dither_target(self, *, pixels: float, max_pixels: float) -> tuple[float, float]:
        """Choose an absolute random offset around the sequence center.

        Absolute targets prevent the classic dither random walk: every point
        is bounded by the configured radius around the original field center,
        regardless of all earlier directions.
        """
        mount = self._state.snapshot.mount
        if mount is None or mount.stale or mount.ra_j2000_h is None or mount.dec_j2000_deg is None:
            raise CaptureDitherSettleError(
                "cannot dither safely without fresh J2000 mount coordinates"
            )
        if self._dither_pixel_scale_arcsec is None or self._dither_pixel_scale_arcsec <= 0.0:
            raise CaptureDitherSettleError(
                "cannot dither safely without the active telephoto optic's pixel scale"
            )
        pixel_scale_deg = self._dither_pixel_scale_arcsec / 3600.0

        current = (mount.ra_j2000_h, mount.dec_j2000_deg)
        last_target = self._last_dither_target_j2000
        if self._dither_center_j2000 is None:
            self._dither_center_j2000 = current
        elif last_target is not None and not self._dither_center_explicit:
            moved_arcsec = _equatorial_separation_arcsec(current, last_target)
            allowed_arcsec = max_pixels * pixel_scale_deg * 3600.0 + 60.0
            if moved_arcsec > allowed_arcsec:
                # A real user/centering slew starts a new field even when the
                # optional object label did not change.
                self._dither_center_j2000 = current

        center_ra_h, center_dec_deg = self._dither_center_j2000
        angle_rad = random.random() * math.tau
        radius_deg = pixels * pixel_scale_deg
        dec_deg = center_dec_deg + radius_deg * math.sin(angle_rad)
        cos_dec = math.cos(math.radians(center_dec_deg))
        if abs(cos_dec) < 1e-6 or not -90.0 <= dec_deg <= 90.0:
            raise CaptureDitherSettleError("cannot calculate a safe dither target near the pole")
        ra_h = (center_ra_h + radius_deg * math.cos(angle_rad) / (15.0 * cos_dec)) % 24.0
        target = (ra_h, dec_deg)
        self._last_dither_target_j2000 = target
        return target

    async def recover_scheduled_dither(self, object_name: str | None) -> None:
        """Reconcile unknown motion completion without repeating an offset.

        The old process may have died before or after dispatch. Neither its
        random target nor its completion can be inferred from mount coordinates.
        Consume this ambiguous cadence only after independent stability proof;
        the executor then verifies the persisted planned field before science.
        """
        active_run = self._state.snapshot.run
        run_id = None if active_run is None else active_run.run_id
        self._publish(in_progress=True, stage=CaptureStage.DITHERING,
                      object_name=object_name, last_error="Recovering ambiguous dither without another offset")
        mount = self._state.snapshot.mount
        after_polled_at = None if mount is None else mount.polled_at
        abort_sent = False
        try:
            while True:
                self._check_dither_run_stop(run_id)
                try:
                    mount = self._state.snapshot.mount
                    if mount is not None and mount.slewing is True and not abort_sent:
                        abort = getattr(self, "_dither_abort", None)
                        if abort is None:
                            raise CaptureDitherSettleError("cannot stop ambiguous dither motion")
                        await abort()
                        abort_sent = True
                    await self._wait_for_fresh_dither_coordinates(stop_run_id=run_id)
                    await self._restore_dither_tracking(
                        after_polled_at=after_polled_at, stop_run_id=run_id
                    )
                    mount = self._state.snapshot.mount
                    if mount is None or mount.ra_j2000_h is None or mount.dec_j2000_deg is None:
                        raise CaptureDitherSettleError("fresh J2000 coordinates required for recovery")
                    await self._wait_for_image_dither_settle(
                        target_j2000=(mount.ra_j2000_h, mount.dec_j2000_deg),
                        after_polled_at=after_polled_at, stop_run_id=run_id,
                    )
                    return
                except (CaptureDitherStoppedError, CaptureDitherWindowEndedError):
                    raise
                except (CaptureDitherSettleError, AlpacaClientError) as exc:
                    self._publish(last_error=f"Ambiguous dither recovery: {exc}")
                    await self._sleep(_DITHER_SETTLE_POLL_S)
        except CaptureDitherWindowEndedError:
            await self._finish_expired_dither(run_id, after_polled_at=after_polled_at)
            raise
        finally:
            self._publish(in_progress=False, stage=None, duration_s=None, started_at=None,
                          object_name=None, frame_type=None, gain=None)

    async def dither_scheduled(
        self, pixels: float, max_pixels: float, object_name: str | None
    ) -> None:
        """Apply one scheduler dither through the verified absolute-slew path."""
        if self._dither is None:
            raise CaptureDitherSettleError("mount dithering is unavailable")
        self._publish(
            in_progress=True,
            last_error=None,
            stage=CaptureStage.DITHERING,
            duration_s=None,
            started_at=None,
            object_name=object_name,
            frame_type=FrameType.LIGHT,
            gain=None,
        )
        active_run = self._state.snapshot.run
        stop_run_id = None if active_run is None else active_run.run_id
        motion_polled_at = None
        tracking_reset = False
        try:
            while True:
                self._check_dither_run_stop(stop_run_id)
                try:
                    await self._wait_for_fresh_dither_coordinates(stop_run_id=stop_run_id)
                    break
                except (CaptureDitherStoppedError, CaptureDitherWindowEndedError):
                    raise
                except CaptureDitherSettleError as exc:
                    # No target or physical move exists yet. A telemetry outage
                    # must not escape to the executor's non-replayable DITHER
                    # failure path; stay visibly waiting and honour durable Stop.
                    self._publish(last_error=f"Dither recovery: {exc}")
                    await self._sleep(_DITHER_SETTLE_POLL_S)
            self._check_dither_run_stop(stop_run_id)
            self._remember_dither_center(object_name)
            target = self._next_dither_target(pixels=pixels, max_pixels=max_pixels)
            mount = self._state.snapshot.mount
            polled_at = None if mount is None else mount.polled_at
            motion_polled_at = polled_at
            await self._dither(*target)
            while True:
                self._check_dither_run_stop(stop_run_id)
                try:
                    await self._restore_dither_tracking(
                        after_polled_at=polled_at, stop_run_id=stop_run_id
                    )
                    await self._wait_for_dither_settle(
                        target_j2000=target, after_polled_at=polled_at, stop_run_id=stop_run_id
                    )
                    break
                except (CaptureDitherStoppedError, CaptureDitherWindowEndedError):
                    raise
                except CaptureDitherSettleError as exc:
                    # Keep the panel alive and recheck the stopped mount. Never
                    # issue another offset or expose through uncertain motion.
                    _log.warning("Dither settling delayed; waiting for fresh stable mount telemetry: %s", exc)
                    self._publish(last_error=f"Dither recovery: {exc}")
                    if "position changed" in str(exc) and getattr(self, "_solve_dither_probe", None) is not None:
                        try:
                            await self._wait_for_image_dither_settle(
                                target_j2000=target, after_polled_at=polled_at,
                                stop_run_id=stop_run_id,
                            )
                            break
                        except (CaptureDitherStoppedError, CaptureDitherWindowEndedError):
                            raise
                        except CaptureDitherSettleError as image_error:
                            self._publish(last_error=f"Dither image verification: {image_error}")
                    if not tracking_reset and "position changed" in str(exc):
                        tracking_reset = await self._recover_dither_tracking(stop_run_id)
                        if tracking_reset:
                            mount = self._state.snapshot.mount
                            polled_at = None if mount is None else mount.polled_at
                    await self._sleep(_DITHER_SETTLE_POLL_S)
            # The next scheduler effect may be calibration or cleanup rather
            # than another light exposure. Release capture ownership here.
            self._publish(
                in_progress=False, last_error=None, stage=None,
                duration_s=None, started_at=None, object_name=None,
                frame_type=None, gain=None,
            )
        except CaptureDitherWindowEndedError:
            try:
                await self._finish_expired_dither(stop_run_id, after_polled_at=motion_polled_at)
            except CaptureDitherStoppedError:
                self._publish(in_progress=False, stage=None, duration_s=None, started_at=None,
                              object_name=None, frame_type=None, gain=None,
                              last_error="scheduled dither interrupted by durable run Stop")
                raise
            self._publish(
                in_progress=False, stage=None, duration_s=None, started_at=None,
                object_name=None, frame_type=None, gain=None,
                last_error="observing window ended during dither recovery",
            )
            raise
        except Exception as exc:
            self._publish(
                in_progress=False,
                last_error=f"{type(exc).__name__}: {exc}",
                stage=None,
                duration_s=None,
                started_at=None,
                object_name=None,
                frame_type=None,
                gain=None,
            )
            raise

    def _check_dither_run_stop(self, run_id: str | None, *, check_window: bool = True) -> None:
        run = self._state.snapshot.run
        if run_id is not None and run is not None and run.run_id == run_id and run.status == "stopped":
            raise CaptureDitherStoppedError("scheduled dither interrupted by durable run Stop")
        if check_window and run_id is not None and run is not None and run.run_id == run_id:
            item = next((item for item in run.items if item.item_id == run.current_item_id), None)
            if item is not None and item.end_utc is not None and self._now() >= item.end_utc:
                raise CaptureDitherWindowEndedError("observing window ended during dither recovery")

    async def _finish_expired_dither(self, run_id: str | None, *, after_polled_at: datetime | None) -> None:
        """Hand off only after fresh telemetry proves the mount stopped."""
        aborted = False
        while True:
            self._check_dither_run_stop(run_id, check_window=False)
            mount = self._state.snapshot.mount
            if (mount is not None and not mount.stale and mount.slewing is False
                    and mount.polled_at is not None
                    and (after_polled_at is None or mount.polled_at > after_polled_at)):
                return
            error = "observing window ended; waiting for a safely stopped mount"
            if not aborted and self._dither_abort is not None:
                try:
                    await self._dither_abort()
                    aborted = True
                except Exception as exc:
                    # A transport outage cannot prove motion stopped or kill
                    # the panel. Reassess telemetry before retrying cleanup.
                    error += f"; stop confirmation unavailable: {type(exc).__name__}: {exc}"
            self._publish(last_error=error)
            await self._sleep(_DITHER_SETTLE_POLL_S)

    async def _recover_dither_tracking(self, run_id: str | None) -> bool:
        """One durable controller reset, preserving the target and strict settle gate."""
        mount = self._state.snapshot.mount
        recorder = getattr(self, "_dither_recovery_recorder", None)
        if (recorder is None or self._telescope is None or mount is None
                or mount.stale or mount.slewing is not False):
            return False
        put = getattr(self._telescope, "put", None)
        if put is None:
            return False
        self._check_dither_run_stop(run_id)
        try:
            if not await recorder():
                # A previously journalled reset may already have taken effect.
                return True
            self._check_dither_run_stop(run_id)
            await put("tracking", Tracking=False)
            self._check_dither_run_stop(run_id)
            await put("tracking", Tracking=True)
            if await self._telescope.get("tracking") is not True:
                self._publish(last_error="Dither recovery: tracking reset not yet confirmed")
            return True
        except (CaptureDitherStoppedError, CaptureDitherWindowEndedError):
            raise
        except Exception as exc:
            self._publish(last_error=f"Dither recovery: tracking reset waiting: {type(exc).__name__}: {exc}")
            return False

    async def _restore_dither_tracking(
        self, *, after_polled_at: datetime | None, stop_run_id: str | None = None
    ) -> None:
        """Restore tracking only after the async move has stopped, then verify it."""
        deadline = asyncio.get_running_loop().time() + _DITHER_SETTLE_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            self._check_dither_run_stop(stop_run_id)
            mount = self._state.snapshot.mount
            if (mount is not None and not mount.stale and mount.slewing is False
                    and mount.polled_at is not None
                    and (after_polled_at is None or mount.polled_at > after_polled_at)):
                if self._telescope is None:
                    raise CaptureDitherSettleError("tracking control unavailable after dither")
                try:
                    # Reissuing Tracking=True can restart the mount's tracking
                    # controller. Recovery must preserve already active tracking.
                    if mount.tracking is not True:
                        put_tracking = getattr(self._telescope, "put", None)
                        if put_tracking is None:
                            raise CaptureDitherSettleError("tracking control unavailable after dither")
                        await put_tracking("tracking", Tracking=True)
                    if await self._telescope.get("tracking") is True:
                        _log.info("Dither tracking restored and confirmed")
                        return
                except AlpacaClientError as exc:
                    raise CaptureDitherSettleError(f"tracking recovery after dither: {exc}") from exc
                raise CaptureDitherSettleError("mount did not confirm tracking after dither")
            await self._sleep(_DITHER_SETTLE_POLL_S)
        raise CaptureDitherSettleError("waiting for stopped mount before restoring dither tracking")

    async def _wait_for_fresh_dither_coordinates(self, *, stop_run_id: str | None = None) -> None:
        """Absorb the short stale-telemetry window after a scheduled exposure."""
        for attempt in range(_DITHER_COORDINATE_RETRIES + 1):
            self._check_dither_run_stop(stop_run_id)
            mount = self._state.snapshot.mount
            if (
                mount is not None
                and not mount.stale
                and mount.ra_j2000_h is not None
                and mount.dec_j2000_deg is not None
            ):
                return
            if attempt < _DITHER_COORDINATE_RETRIES:
                await self._sleep(_DITHER_COORDINATE_RETRY_DELAY_S)
        raise CaptureDitherSettleError(
            "cannot dither safely without fresh J2000 mount coordinates after "
            "waiting 60 seconds for telemetry to recover"
        )

    async def _recover_dither_probe_camera(self, stop_run_id: str | None) -> None:
        """Retain ownership even when Stop/window expiry awaits camera cleanup."""
        self._dither_probe_cleanup_pending = True
        cleanup = getattr(self, "_cleanup_dither_probe", None)
        while True:
            self._publish(in_progress=True, stage=CaptureStage.DITHERING,
                          last_error="Dither diagnostic camera cleanup pending; exposure blocked")
            if cleanup is not None:
                try:
                    await cleanup()
                except Exception as exc:
                    _log.warning("Dither camera idle recovery pending: %s", exc)
                else:
                    self._dither_probe_cleanup_pending = False
                    self._check_dither_run_stop(stop_run_id)
                    return
            # No new probe, offset, science or calibration while idle is unknown.
            await self._sleep(_DITHER_SETTLE_POLL_S)

    async def _wait_for_image_dither_settle(
        self, *, target_j2000: tuple[float, float],
        after_polled_at: datetime | None, stop_run_id: str | None,
    ) -> None:
        """Independent image confirmation; no slew or science catalogue writes.

        The injected probe must clean up its camera exposure before completing
        cancellation. Every intervening mount event is checked, not just the
        latest snapshot. A replay gap invalidates the whole interval.
        """
        from ssc.state.store import Gap

        probe = self._solve_dither_probe
        if probe is None:
            raise CaptureDitherSettleError("image verification unavailable")
        seq = self._state.seq
        monotonic = getattr(self, "_monotonic", time.monotonic)
        deadline = monotonic() + 120.0
        first_solved_mono: float | None = None
        successful_probes = 0
        anchor: tuple[float, float] | None = None
        first_solved_at: datetime | None = None

        def guard() -> None:
            nonlocal seq
            self._check_dither_run_stop(stop_run_id)
            replay = self._state.since(seq)
            if isinstance(replay, Gap):
                raise CaptureDitherSettleError("mount event gap during image verification")
            samples = [entry.event.mount for entry in replay.events
                       if entry.event.type == "mount.changed"]
            samples.append(self._state.snapshot.mount)
            for sample in samples:
                if (sample is None or sample.stale or sample.slewing is not False
                        or sample.tracking is not True or sample.polled_at is None
                        or (after_polled_at is not None and sample.polled_at <= after_polled_at)
                        or not 0 <= (self._now() - sample.polled_at).total_seconds() <= 10):
                    raise CaptureDitherSettleError("fresh stopped tracking telemetry required for image verification")
            seq = self._state.seq

        while monotonic() < deadline:
            guard()
            probe_started_mono = monotonic()
            probe_started_at = self._now()
            task: asyncio.Future[PlateSolveResult] = asyncio.ensure_future(probe())
            try:
                while not task.done():
                    guard()
                    if monotonic() >= deadline:
                        raise CaptureDitherSettleError("image verification timed out")
                    await asyncio.sleep(0.1)
                solved = await task
                guard()
            except BaseException as exc:
                cleanup_failed = isinstance(exc, CaptureDitherProbeCleanupError)
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except CaptureDitherProbeCleanupError:
                        cleanup_failed = True
                if cleanup_failed:
                    await self._recover_dither_probe_camera(stop_run_id)
                    if isinstance(exc, (CaptureDitherStoppedError, CaptureDitherWindowEndedError,
                                        asyncio.CancelledError)):
                        raise
                    # Restart the full proof only after positively confirmed idle.
                    raise CaptureDitherSettleError("diagnostic camera idle recovered; restart image verification") from exc
                if isinstance(exc, (CaptureDitherStoppedError, CaptureDitherWindowEndedError,
                                    CaptureDitherSettleError, asyncio.CancelledError)):
                    raise
                raise CaptureDitherSettleError(f"image probe failed: {exc}") from exc
            position = (float(solved.coord.ra_h), float(solved.coord.dec_deg))
            if not all(math.isfinite(value) for value in position):
                raise CaptureDitherSettleError("non-finite solved image coordinates")
            if _equatorial_separation_arcsec(position, target_j2000) > 60.0:
                raise CaptureDitherSettleError("image dither target residual exceeds 60 arcsec")
            successful_probes += 1
            if anchor is None:
                anchor = position
                first_solved_at = self._now()
                first_solved_mono = monotonic()
            elif _equatorial_separation_arcsec(position, anchor) > _DITHER_STABLE_ARCSEC:
                raise CaptureDitherSettleError("image position changed beyond 5 arcsec")
            elif (successful_probes >= 3 and first_solved_at is not None
                  and first_solved_mono is not None
                  and probe_started_mono - first_solved_mono >= _DITHER_STABLE_DURATION_S
                  and (probe_started_at - first_solved_at).total_seconds() >= _DITHER_STABLE_DURATION_S):
                _log.info("Dither sampled image centroids stable over 30 seconds within 5 arcsec; target within 60 arcsec")
                return
            await self._sleep(_DITHER_SETTLE_POLL_S)
        raise CaptureDitherSettleError("image verification timed out")

    async def _wait_for_dither_settle(
        self,
        *,
        target_j2000: tuple[float, float],
        after_polled_at: datetime | None,
        stop_run_id: str | None = None,
    ) -> None:
        """Wait for two distinct, stable post-command mount readings.

        Re-reading one telemetry sample never counts as settling. A reported
        slew, stale/partial data, or a position jump resets confirmation and
        keeps the capture visibly in ``dithering`` until the mount is safe.
        """
        monotonic = getattr(self, "_monotonic", time.monotonic)
        deadline = monotonic() + _DITHER_SETTLE_TIMEOUT_S
        previous_polled_at: datetime | None = None
        previous_position: tuple[float, float] | None = None
        stable_since: datetime | None = None
        stable_samples = 0
        reset_reason = "no fresh post-command sample"

        while monotonic() < deadline:
            self._check_dither_run_stop(stop_run_id)
            mount = self._state.snapshot.mount
            reading_changed = (
                mount is not None
                and mount.polled_at is not None
                and mount.polled_at != previous_polled_at
            )
            reading_is_settled = (
                mount is not None
                and not mount.stale
                and mount.slewing is False
                and mount.tracking is True
                and mount.polled_at is not None
                and (after_polled_at is None or mount.polled_at > after_polled_at)
                and mount.polled_at != previous_polled_at
                and mount.ra_hours is not None
                and mount.dec_deg is not None
                and mount.ra_j2000_h is not None
                and mount.dec_j2000_deg is not None
            )
            if reading_changed and reading_is_settled:
                assert mount is not None and mount.polled_at is not None
                assert mount.ra_hours is not None and mount.dec_deg is not None
                assert mount.ra_j2000_h is not None and mount.dec_j2000_deg is not None
                position = (mount.ra_hours, mount.dec_deg)
                if (
                    previous_position is None
                    or _equatorial_separation_arcsec(position, previous_position)
                    > _DITHER_STABLE_ARCSEC
                ):
                    if previous_position is not None:
                        separation = _equatorial_separation_arcsec(position, previous_position)
                        reset_reason = f"position changed {separation:.1f} arcsec"
                    stable_samples = 1
                    stable_since = mount.polled_at
                    previous_position = position
                else:
                    stable_samples += 1
                previous_polled_at = mount.polled_at
                # Keep the start-of-interval anchor: several small successive
                # moves must not add up to a falsely "stable" drifting mount.
                if (
                    stable_samples >= 2
                    and stable_since is not None
                    and (mount.polled_at - stable_since).total_seconds()
                    >= _DITHER_STABLE_DURATION_S
                ):
                    residual = _equatorial_separation_arcsec(
                        (mount.ra_j2000_h, mount.dec_j2000_deg), target_j2000
                    )
                    _log.info("Dither mount settled; target residual %.1f arcsec", residual)
                    return
            elif reading_changed:
                # A new stale, slewing, off-target, or incomplete sample breaks
                # the settle interval. Repeated reads of the same sample do not.
                assert mount is not None and mount.polled_at is not None
                reset_reason = (
                    f"stale={mount.stale}, slewing={mount.slewing}, tracking={mount.tracking}, "
                    f"coordinates_complete={all(value is not None for value in (mount.ra_hours, mount.dec_deg, mount.ra_j2000_h, mount.dec_j2000_deg))}"
                )
                previous_polled_at = mount.polled_at
                previous_position = None
                stable_since = None
                stable_samples = 0
            await self._sleep(_DITHER_SETTLE_POLL_S)

        abort = getattr(self, "_dither_abort", None)
        mount = self._state.snapshot.mount
        # A settle timeout is not proof of motion. AbortSlew can disable
        # tracking even when already stopped, creating another settle failure
        # on every retry. Retain the fail-safe for moving or unknown telemetry.
        confirmed_stopped = (
            mount is not None and not mount.stale and mount.slewing is False
            and mount.polled_at is not None
            and (after_polled_at is None or mount.polled_at > after_polled_at)
        )
        if abort is not None and not confirmed_stopped:
            try:
                await abort()
            except Exception:
                _log.exception("could not stop mount after dither settle timeout")

        raise CaptureDitherSettleError(
            "dither command completed, but the mount did not report a stopped, "
            "stable position within 60 seconds; "
            f"the next exposure was not started; last reset: {reset_reason}; "
            f"stable samples: {stable_samples}"
        )

    def begin_scheduled_exposure(
        self,
        *,
        duration_s: float,
        gain: int,
        object_name: str | None,
        frame_type: FrameType,
    ) -> None:
        """Publish scheduler-driven exposures on the same Capture surface as manual ones."""
        self._publish(
            in_progress=True,
            last_error=None,
            stage=CaptureStage.EXPOSING,
            duration_s=duration_s,
            started_at=self._now(),
            object_name=object_name,
            frame_type=frame_type,
            gain=gain,
        )

    def update_scheduled_exposure_stage(self, stage: CaptureStage) -> None:
        self._publish(stage=stage)

    def complete_scheduled_exposure(self, frame_id: int) -> None:
        self._publish(
            in_progress=False,
            last_frame_id=frame_id,
            last_captured_at=self._now(),
            last_error=None,
            stage=None,
            duration_s=None,
            started_at=None,
            object_name=None,
            frame_type=None,
            gain=None,
        )

    def fail_scheduled_exposure(self, exc: Exception) -> None:
        self._failed_exposure_camera = self._camera
        self._publish(
            in_progress=True,
            last_error=f"{type(exc).__name__}: {exc}; camera idle unconfirmed; acquisition blocked",
            stage=None,
            duration_s=None,
            started_at=None,
            object_name=None,
            frame_type=None,
            gain=None,
        )

    async def start_calibration(
        self,
        *,
        kind: CalibrationRunKind,
        count: int,
        duration_s: float | None,
        gain: int | None,
        filter_positions: list[int],
        optic_id: str | None,
        flat_source: str | None,
        generate_master: bool,
        keep_only_master: bool,
        covered_lens_confirmed: bool = False,
        run_id: str | None = None,
        block_id: str | None = None,
        continue_guard: Callable[[], Awaitable[bool]] | None = None,
    ) -> CaptureState:
        """Start manual per-lens sets; scheduled calibration remains Tele-only."""
        optic_id = optic_id or "tele"
        if optic_id not in {"tele", "wide", "both"}:
            raise CalibrationConfigurationError("calibration optic must be tele, wide or both")
        if (run_id is not None or block_id is not None) and optic_id != "tele":
            raise CalibrationConfigurationError("scheduled calibration supports Tele only")
        if optic_id in {"wide", "both"} and kind in {"bias", "dark"} and not covered_lens_confirmed:
            raise CalibrationConfigurationError("Confirm the Wide lens is physically covered for dark/bias calibration")
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError(
                "no capture folder is configured; set one in Settings > Capture first"
            )
        if self._camera is None:
            raise CaptureDeviceUnavailableError("this ssc-core hosts no camera")
        if continue_guard is not None and not await continue_guard():
            raise _CalibrationCancelledError
        if run_id is not None or block_id is not None:
            if not run_id or not block_id:
                raise CalibrationConfigurationError("scheduled calibration requires run and item identity")
            if self._calibration_task is not None and not self._calibration_task.done():
                raise CaptureAlreadyRunningError("calibration already owns the camera")
            if self._section.in_progress and getattr(self, "_failed_exposure_camera", None) is None:
                raise CaptureAlreadyRunningError("another acquisition owns the camera")
            # A previous process may have died with an exposure still owned by
            # the camera. Confirm idle before permitting catalogue continuation.
            self._failed_exposure_camera = self._camera
        await self._recover_failed_exposure()
        if continue_guard is not None and not await continue_guard():
            raise _CalibrationCancelledError
        await self.drain_wide_preview()
        if self._wide_preview_busy():
            raise CaptureAlreadyRunningError("camera acquisition is already in progress")
        if self._section.loop_active:
            raise CaptureAlreadyRunningError("a capture loop is already in progress")

        runners = []
        for lens in (("tele", "wide") if optic_id == "both" else (optic_id,)):
            runners.append(await self._prepare_calibration_set(
                kind=kind, count=count, duration_s=duration_s, gain=gain,
                filter_positions=filter_positions, optic_id=lens, flat_source=flat_source,
                generate_master=generate_master, keep_only_master=keep_only_master,
                run_id=run_id, block_id=block_id, continue_guard=continue_guard,
            ))
        if self._section.in_progress:
            raise CaptureAlreadyRunningError("another acquisition owns the camera")
        self._calibration_cancel_requested = False
        self._calibration_target_count = count
        self._publish(in_progress=True, last_error=None, calibration=CalibrationRunState(
            status="running", kind=kind, phase="preparing", requested_count=count,
            total_frames=count, generate_master=generate_master, keep_only_master=keep_only_master,
            message=f"Preparing {optic_id} calibration run"))
        self._calibration_task = self._tasks.spawn(
            self._run_calibration_sets(runners), name=f"capture-calibration-{kind}")
        return self._section

    async def _run_calibration_sets(self, runners: list[Callable[[], Awaitable[None]]]) -> None:
        master_paths: list[str] = []
        removed_frames = 0
        try:
            for runner in runners:
                if self._calibration_cancel_requested:
                    self._publish(calibration=self._section.calibration.model_copy(
                        update={"status": "cancelled", "message": "Calibration capture cancelled"}))
                    break
                await runner()
                master_paths.extend(self._section.calibration.master_paths)
                removed_frames += self._section.calibration.removed_source_frames
                self._publish(calibration=self._section.calibration.model_copy(update={
                    "master_paths": tuple(master_paths), "removed_source_frames": removed_frames}))
                if self._section.calibration.status != "completed":
                    break
        finally:
            self._calibration_task = None
            self._calibration_cancel_requested = False
            self._calibration_target_count = None
            self._publish(in_progress=getattr(self, "_failed_exposure_camera", None) is not None,
                          stage=None, duration_s=None, started_at=None, object_name=None,
                          frame_type=None, gain=None)

    async def _prepare_calibration_set(
        self,
        *,
        kind: CalibrationRunKind,
        count: int,
        duration_s: float | None,
        gain: int | None,
        filter_positions: list[int],
        optic_id: str | None,
        flat_source: str | None,
        generate_master: bool,
        keep_only_master: bool,
        run_id: str | None = None,
        block_id: str | None = None,
        continue_guard: Callable[[], Awaitable[bool]] | None = None,
    ) -> Callable[[], Awaitable[None]]:
        folder = self._settings.capture.folder
        assert folder is not None
        camera = self._wide_camera if optic_id == "wide" else self._camera
        if camera is None:
            raise CaptureDeviceUnavailableError(f"this ssc-core hosts no {optic_id} camera")
        minimum_s, maximum_s = await self._read_limits(camera)
        actual_gain = await self._calibration_gain(kind=kind, requested=gain, camera=camera)
        filter_names: tuple[str, ...] = ()
        max_adu: float | None = None
        if kind == "flat" and optic_id != "wide":
            if self._filterwheel is None:
                raise CalibrationConfigurationError(
                    "per-filter flats require a connected filter wheel"
                )
            await _ensure_connected(self._filterwheel)
            raw_names = await self._filterwheel.get("names")
            if not isinstance(raw_names, list) or not all(
                isinstance(name, str) for name in raw_names
            ):
                raise CalibrationConfigurationError(
                    f"FilterWheel.Names returned {raw_names!r}, not a string list"
                )
            if any(position >= len(raw_names) for position in filter_positions):
                raise CalibrationConfigurationError(
                    f"filter positions {filter_positions!r} exceed the reported "
                    f"{len(raw_names)} slots"
                )
            filter_names = tuple(
                raw_names[position].strip() or f"Slot {position + 1}"
                for position in filter_positions
            )
            rejected_names = tuple(
                name for name in filter_names if not is_flat_eligible_filter_name(name)
            )
            if rejected_names:
                raise CalibrationConfigurationError(
                    "the Dark shutter position cannot be used for flat frames; "
                    "select only illuminated filter positions"
                )
        if kind == "flat":
            if flat_source is None:
                raise CalibrationConfigurationError("flat capture requires panel or sky source")
            if optic_id == "wide":
                filter_names = ("Unfiltered",)
                filter_positions = [0]
            max_adu = _as_float(await camera.get("maxadu"), label="Camera.MaxADU")
            if generate_master:
                try:
                    # Full-sensor geometry is authoritative here. NumX/NumY
                    # are subframe controls, which SSC's native Seestar bridge
                    # deliberately refuses because the camera ignores them.
                    width_raw, height_raw = await asyncio.gather(
                        camera.get("cameraxsize"), camera.get("cameraysize")
                    )
                    width = _as_positive_int(width_raw, label="Camera.CameraXSize")
                    height = _as_positive_int(height_raw, label="Camera.CameraYSize")
                except (AlpacaClientError, KeyError, CaptureMetadataError) as exc:
                    raise CalibrationConfigurationError(
                        "cannot verify the flat master bias dependency because the camera "
                        "did not report its current image dimensions"
                    ) from exc
                groups = await self.list_calibration_source_sets()
                if (
                    _best_bias_source_set(groups, gain=actual_gain, width=width, height=height, optic_id=optic_id)
                    is None
                ):
                    raise CalibrationConfigurationError(
                        "a flat master requires at least 10 retained bias frames at "
                        f"gain {actual_gain} and {width}x{height}; capture a bias set first. "
                        "No flat frames were captured."
                    )

        if kind == "dark":
            assert duration_s is not None
            if not minimum_s <= duration_s <= maximum_s:
                raise CaptureDurationOutOfRangeError(duration_s, minimum_s, maximum_s)
        capture_settings = self._settings.capture
        folder_template = {
            "bias": capture_settings.calibration_bias_folder_template,
            "dark": capture_settings.calibration_dark_folder_template,
            "flat": capture_settings.calibration_flat_folder_template,
        }[kind]
        folder_template = f"{folder_template}/{optic_id}"
        filename_template = (
            capture_settings.calibration_filename_template or capture_settings.filename_template
        )
        tz = self._resolve_template_timezone(f"{folder_template}/{filename_template}")
        retained = await self._resume_calibration_frames(
            run_id=run_id, block_id=block_id, kind=kind, gain=actual_gain,
            exposure_s=minimum_s if kind == "bias" else duration_s,
            filter_names=filter_names,
        )
        total = count if kind in {"bias", "dark"} else count * len(filter_positions)
        calibration = CalibrationRunState(
            status="running",
            kind=kind,
            phase="preparing",
            requested_count=count,
            filter_names=filter_names,
            completed_frames=sum(len(ids) for ids in retained.values()),
            total_frames=total,
            gain=actual_gain,
            generate_master=generate_master,
            keep_only_master=keep_only_master,
            message=f"Preparing {optic_id} calibration run",
        )
        async def run_set() -> None:
            self._publish(in_progress=True, last_error=None, frame_type=FrameType(kind),
                          gain=actual_gain, calibration=calibration)
            await self._run_calibration(
                camera=camera,
                kind=kind,
                count=count,
                requested_duration_s=duration_s,
                gain=actual_gain,
                filter_positions=tuple(filter_positions),
                filter_names=filter_names,
                optic_id=optic_id,
                flat_source=flat_source,
                generate_master=generate_master,
                keep_only_master=keep_only_master,
                minimum_s=minimum_s,
                maximum_s=maximum_s,
                max_adu=max_adu,
                folder=folder,
                folder_template=folder_template,
                filename_template=filename_template,
                tz=tz,
                run_id=run_id,
                block_id=block_id,
                retained=retained,
                continue_guard=continue_guard,
            )
        return run_set

    async def _resume_calibration_frames(
        self, *, run_id: str | None, block_id: str | None,
        kind: CalibrationRunKind, gain: int, exposure_s: float | None,
        filter_names: tuple[str, ...],
    ) -> dict[str | None, list[int]]:
        retained: dict[str | None, list[int]] = {}
        if run_id is None or block_id is None:
            return retained
        records = await self._repo.list_block_frames(run_id=run_id, block_id=block_id)
        for record in records:
            if record.status == "writing":
                # Camera idle was independently verified above. An unfinished
                # write is not a saved exposure and cannot advance the count.
                path = await self._repo.discard_frame(record.id)
                await asyncio.to_thread(path.unlink, missing_ok=True)
                continue
            expected_filter = record.filter_name if kind == "flat" else None
            if (record.frame_type != kind or record.gain != gain
                    or (kind == "flat" and record.filter_name not in filter_names)
                    or (kind != "flat" and record.exposure_s != exposure_s)):
                raise CalibrationConfigurationError("saved scheduled calibration parameters differ from intent")
            try:
                image = await asyncio.to_thread(lambda path=record.path: read_fits_image(path.read_bytes()))
                if (image.header.get("GAIN") != gain
                        or image.header.get("EXPTIME") != record.exposure_s
                        or str(image.header.get("IMAGETYP", "")).casefold() != kind
                        or image.header.get("FILTER") != record.filter_name
                        or image.data.shape != (record.height, record.width)):
                    raise ValueError("FITS metadata disagrees with durable catalogue")
            except Exception as exc:
                raise CalibrationConfigurationError(
                    f"cannot verify retained calibration frame {record.id}: {exc}") from exc
            retained.setdefault(expected_filter, []).append(record.id)
        _log.info("scheduled calibration resume run=%s item=%s verified_frames=%s",
                  run_id, block_id, sum(map(len, retained.values())))
        return retained

    async def list_calibration_source_sets(self) -> list[CalibrationSourceSet]:
        """Group retained calibration raws into rebuildable capture sets."""
        records = await self._repo.list_complete()
        return await asyncio.to_thread(_calibration_source_sets, records)

    async def cancel_calibration(self) -> CaptureState:
        if self._calibration_task is None or self._calibration_task.done():
            raise CalibrationRunNotRunningError("no automatic calibration run is active")
        self._calibration_cancel_requested = True
        self._publish(
            calibration=self._section.calibration.model_copy(
                update={
                    "status": "cancelling",
                    "message": "Stopping after the current exposure is safely downloaded",
                }
            )
        )
        return self._section

    async def update_active_calibration_count(self, count: int) -> CaptureState:
        """Change a running calibration target after the current exposure."""
        if count < 1:
            raise CalibrationConfigurationError("calibration count must be at least 1")
        if self._calibration_task is None or self._calibration_task.done():
            raise CalibrationRunNotRunningError("no automatic calibration run is active")
        self._calibration_target_count = count
        filters = max(1, len(self._section.calibration.filter_names))
        self._publish(
            calibration=self._section.calibration.model_copy(
                update={
                    "requested_count": count,
                    "total_frames": count * filters,
                    "message": f"Calibration target updated to {count} frame(s)",
                }
            )
        )
        return self._section

    async def wait_for_calibration(self) -> CaptureState:
        """Wait for the active server-owned run; used by the scheduler adapter."""
        task = self._calibration_task
        if task is None:
            raise CalibrationRunNotRunningError("no automatic calibration run is active")
        # The calibration is owned by the server, not by a scheduler/request
        # waiting for its result. Only cancel_calibration may request a stop.
        await asyncio.shield(task)
        return self._section

    async def _calibration_gain(self, *, kind: CalibrationRunKind, requested: int | None, camera: GainSettableCamera | None = None) -> int:
        camera = camera if camera is not None else self._camera
        assert camera is not None
        if requested is not None:
            return requested
        member = "gainmin" if kind == "flat" else "gain"
        raw = await camera.get(member)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise CalibrationConfigurationError(
                f"Camera.{member.title()} returned {raw!r}, not an integer; specify gain explicitly"
            )
        return raw

    async def _select_filter(self, position: int) -> None:
        assert self._filterwheel is not None
        await _ensure_connected(self._filterwheel)
        await self._filterwheel.put("position", Position=position)
        for _ in range(100):
            current = await self._filterwheel.get("position")
            if current == position:
                return
            await self._sleep(0.1)
        raise CalibrationConfigurationError(
            f"filter wheel did not reach position {position} within 10 seconds"
        )

    async def _select_dark_filter_if_available(self) -> None:
        """Move a shutterless camera to its named opaque position when present.

        ``Light=False`` remains part of the ASCOM camera command, but Seestar
        exposes its physical cover as a filter-wheel slot.  Other supported
        cameras may have a real shutter and no such slot, so absence is a
        valid no-op rather than a configuration error.
        """
        if self._filterwheel is None:
            return
        await _ensure_connected(self._filterwheel)
        raw_names = await self._filterwheel.get("names")
        if not isinstance(raw_names, list) or not all(isinstance(name, str) for name in raw_names):
            raise CalibrationConfigurationError(
                f"FilterWheel.Names returned {raw_names!r}, not a string list"
            )
        dark_position = next(
            (position for position, name in enumerate(raw_names) if is_dark_filter_name(name)),
            None,
        )
        if dark_position is not None:
            await self._select_filter(dark_position)

    def _calibration_stage(self, stage: CaptureStage) -> None:
        self._publish(stage=stage)

    async def _save_calibration_frame(
        self,
        *,
        camera: GainSettableCamera,
        duration_s: float,
        frame_type: FrameType,
        context: ExposureContext,
        folder: Path,
        folder_template: str,
        filename_template: str,
        tz: ZoneInfo,
        run_id: str | None = None,
        block_id: str | None = None,
    ) -> int:
        self._sequence += 1
        return await capture_one_exposure(
            camera=camera,
            telescope=self._telescope,
            filterwheel=None if context.optic_id == "wide" else self._filterwheel,
            focuser=None if context.optic_id == "wide" else self._focuser,
            repo=self._repo,
            folder=folder,
            duration_s=duration_s,
            context=context,
            frame_type=frame_type,
            folder_template=folder_template,
            filename_template=filename_template,
            tz=tz,
            sequence=self._sequence,
            now=self._now,
            sleep=self._sleep,
            on_stage=self._calibration_stage,
            unsupported_members=self.metadata_capabilities(wide=context.optic_id == "wide"),
            run_id=run_id,
            block_id=block_id,
        )

    async def _check_calibration_cancel(
        self, continue_guard: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        if self._calibration_cancel_requested or (
                continue_guard is not None and not await continue_guard()):
            raise _CalibrationCancelledError

    async def _run_calibration(
        self,
        *,
        camera: GainSettableCamera,
        kind: CalibrationRunKind,
        count: int,
        requested_duration_s: float | None,
        gain: int,
        filter_positions: tuple[int, ...],
        filter_names: tuple[str, ...],
        optic_id: str | None,
        flat_source: str | None,
        generate_master: bool,
        keep_only_master: bool,
        minimum_s: float,
        maximum_s: float,
        max_adu: float | None,
        folder: Path,
        folder_template: str,
        filename_template: str,
        tz: ZoneInfo,
        run_id: str | None = None,
        block_id: str | None = None,
        retained: dict[str | None, list[int]] | None = None,
        continue_guard: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        try:
            await self.prepare_main_capture()
            await self._check_calibration_cancel(continue_guard)
            if kind in {"bias", "dark"} and optic_id != "wide":
                await self._select_dark_filter_if_available()
            await camera.put("gain", Gain=gain)
            frame_ids_by_filter = {key: list(ids) for key, ids in (retained or {}).items()}
            if kind in {"bias", "dark"}:
                context = replace(self._context(None, optic_id=optic_id), wide_camera=optic_id == "wide")
                exposure_s = minimum_s if kind == "bias" else cast(float, requested_duration_s)
                frame_type = FrameType(kind)
                while len(frame_ids_by_filter.get(None, ())) < (
                    self._calibration_target_count or count
                ):
                    await self._check_calibration_cancel(continue_guard)
                    self._publish(
                        duration_s=exposure_s,
                        started_at=self._now(),
                        calibration=self._section.calibration.model_copy(
                            update={
                                "phase": "capturing",
                                "exposure_s": exposure_s,
                                "message": f"Capturing {optic_id} {kind} frames",
                            }
                        ),
                    )
                    frame_id = await self._save_calibration_frame(
                        camera=camera,
                        duration_s=exposure_s,
                        frame_type=frame_type,
                        context=context,
                        folder=folder,
                        folder_template=folder_template,
                        filename_template=filename_template,
                        tz=tz,
                        run_id=run_id,
                        block_id=block_id,
                    )
                    frame_ids_by_filter.setdefault(None, []).append(frame_id)
                    self._publish(
                        calibration=self._section.calibration.model_copy(
                            update={
                                "completed_frames": self._section.calibration.completed_frames + 1
                            }
                        )
                    )
            else:
                assert max_adu is not None and optic_id is not None and flat_source is not None
                for filter_index, (position, filter_name) in enumerate(
                    zip(filter_positions, filter_names, strict=True)
                ):
                    await self._check_calibration_cancel(continue_guard)
                    if optic_id != "wide":
                        await self._select_filter(position)
                    exposure_s = minimum_s
                    context = replace(self._context(None, optic_id=optic_id, flat_source=flat_source), wide_camera=optic_id == "wide")
                    for probe_number in range(1, 11):
                        await self._check_calibration_cancel(continue_guard)
                        self._publish(
                            duration_s=exposure_s,
                            started_at=self._now(),
                            calibration=self._section.calibration.model_copy(
                                update={
                                    "phase": "probing",
                                    "current_filter": filter_name,
                                    "current_filter_index": filter_index,
                                    "exposure_s": exposure_s,
                                    "message": f"Measuring {optic_id} {filter_name} flat illumination",
                                }
                            ),
                        )
                        frame, _ = await drive_exposure(
                            camera=camera,
                            telescope=self._telescope,
                            filterwheel=None if optic_id == "wide" else self._filterwheel,
                            duration_s=exposure_s,
                            context=context,
                            frame_type=FrameType.FLAT,
                            now=self._now,
                            sleep=self._sleep,
                            on_stage=self._calibration_stage,
                            unsupported_members=self.metadata_capabilities(wide=optic_id == "wide"),
                        )
                        metrics = await asyncio.to_thread(
                            analyse_flat_frame, frame.pixels, max_adu=max_adu
                        )
                        decision = choose_next_flat_exposure(
                            metrics,
                            exposure_s=exposure_s,
                            minimum_s=minimum_s,
                            maximum_s=maximum_s,
                            source=cast(Literal["panel", "sky"], flat_source),
                            probe_number=probe_number,
                        )
                        probe = FlatProbeState(
                            filter_name=filter_name,
                            exposure_s=exposure_s,
                            median_fraction=metrics.median_fraction,
                            low_fraction=metrics.low_fraction,
                            high_fraction=metrics.high_fraction,
                            clipped_pixel_fraction=metrics.clipped_pixel_fraction,
                            banding_fraction=metrics.banding_fraction,
                            banding_axis=metrics.banding_axis,
                            decision=decision.action,
                            reason=decision.reason,
                        )
                        self._publish(
                            calibration=self._section.calibration.model_copy(
                                update={"probes": (*self._section.calibration.probes, probe)}
                            )
                        )
                        if decision.action == "accept":
                            break
                        if decision.action != "probe" or decision.next_exposure_s is None:
                            raise CalibrationConfigurationError(decision.reason)
                        exposure_s = decision.next_exposure_s
                    else:  # pragma: no cover - chooser fails on probe 10
                        raise CalibrationConfigurationError("flat exposure did not converge")

                    while len(frame_ids_by_filter.get(filter_name, ())) < (
                        self._calibration_target_count or count
                    ):
                        await self._check_calibration_cancel(continue_guard)
                        self._publish(
                            duration_s=exposure_s,
                            started_at=self._now(),
                            calibration=self._section.calibration.model_copy(
                                update={
                                    "phase": "capturing",
                                    "exposure_s": exposure_s,
                                    "message": f"Capturing {optic_id} {filter_name} flats",
                                }
                            ),
                        )
                        frame_id = await self._save_calibration_frame(
                        camera=camera,
                            duration_s=exposure_s,
                            frame_type=FrameType.FLAT,
                            context=context,
                            folder=folder,
                            folder_template=folder_template,
                            filename_template=filename_template,
                            tz=tz,
                            run_id=run_id,
                            block_id=block_id,
                        )
                        frame_ids_by_filter.setdefault(filter_name, []).append(frame_id)
                        self._publish(
                            calibration=self._section.calibration.model_copy(
                                update={
                                    "completed_frames": self._section.calibration.completed_frames
                                    + 1
                                }
                            )
                        )
            await self._check_calibration_cancel(continue_guard)
            if generate_master:
                await self._generate_calibration_masters(
                    kind=kind,
                    gain=gain,
                    frame_ids_by_filter=frame_ids_by_filter,
                    keep_only_master=keep_only_master,
                )
            master_count = len(self._section.calibration.master_paths)
            removed_count = self._section.calibration.removed_source_frames
            final = self._section.calibration.model_copy(
                update={
                    "status": "completed",
                    "message": (
                        (
                            f"Calibration completed; {master_count} master(s) passed verification; "
                            f"{removed_count} source frame(s) removed"
                        )
                        if generate_master and keep_only_master
                        else f"Calibration completed; {master_count} master(s) passed verification"
                        if generate_master
                        else "Calibration capture completed"
                    ),
                }
            )
            self._publish(calibration=final, last_error=None)
        except _CalibrationCancelledError:
            self._publish(
                calibration=self._section.calibration.model_copy(
                    update={"status": "cancelled", "message": "Calibration capture cancelled"}
                )
            )
        except Exception as exc:
            _log.exception("automatic calibration capture failed")
            if self._section.calibration.phase != "integrating":
                self._failed_exposure_camera = camera
            message = f"{type(exc).__name__}: {exc}"
            self._publish(
                last_error=message,
                calibration=self._section.calibration.model_copy(
                    update={"status": "failed", "error": str(exc), "message": None}
                ),
            )

    async def _generate_calibration_masters(
        self,
        *,
        kind: CalibrationRunKind,
        gain: int,
        frame_ids_by_filter: dict[str | None, list[int]],
        keep_only_master: bool,
    ) -> None:
        """Integrate this run only, then optionally remove its verified inputs."""
        folder = self._settings.capture.folder
        assert folder is not None
        updates: dict[str, object] = {
            # The last raw frame is fully saved before integration begins.
            # Clear its operation fields now so clients do not keep showing
            # an expired "Saving" stage throughout a long master build.
            "stage": None,
            "duration_s": None,
            "started_at": None,
            "object_name": None,
            "frame_type": None,
            "gain": None,
            "calibration": self._section.calibration.model_copy(
                update={"phase": "integrating", "message": "Building calibration master"}
            ),
        }
        self._publish(**updates)
        master_paths: list[str] = []
        stamp = self._now().astimezone(UTC).strftime("%Y%m%d-%H%M%S")
        for filter_name, frame_ids in frame_ids_by_filter.items():
            records = [await self._repo.get_frame(frame_id) for frame_id in frame_ids]
            max_adu = await self._master_max_adu(records) if kind == "flat" else None
            result = await self._build_one_calibration_master(
                kind=kind,
                gain=gain,
                records=records,
                filter_name=filter_name,
                stamp=stamp,
                max_adu=max_adu,
            )
            master_paths.append(result)

        removed_source_frames = 0
        if keep_only_master:
            self._publish(
                calibration=self._section.calibration.model_copy(
                    update={
                        "master_paths": tuple(master_paths),
                        "message": "Masters verified; removing individual source frames",
                    }
                )
            )
            for frame_ids in frame_ids_by_filter.values():
                for frame_id in frame_ids:
                    path = await self._repo.delete_complete_frame(frame_id)
                    with contextlib.suppress(FileNotFoundError):
                        await asyncio.to_thread(path.unlink)
                    removed_source_frames += 1

        self._publish(
            calibration=self._section.calibration.model_copy(
                update={
                    "master_paths": tuple(master_paths),
                    "removed_source_frames": removed_source_frames,
                    "message": (
                        f"Built {len(master_paths)} calibration master(s); removed "
                        f"{removed_source_frames} source frame(s)"
                        if keep_only_master
                        else f"Built {len(master_paths)} calibration master(s); raw frames retained"
                    ),
                }
            )
        )

    async def _master_max_adu(self, records: list[CaptureFrameRecord]) -> float:
        path = records[0].path
        first_image = await asyncio.to_thread(lambda: read_fits_image(path.read_bytes()))
        stored = first_image.header.get("MAXADU")
        if isinstance(stored, int | float) and not isinstance(stored, bool) and stored > 0:
            return float(stored)
        camera = self._wide_camera if calibration_optic_id(first_image.header) == "wide" else self._camera
        if camera is None:
            raise CalibrationConfigurationError(
                "the retained flats predate MAXADU metadata; connect their camera to "
                "verify illumination before rebuilding the master"
            )
        return _as_float(await camera.get("maxadu"), label="Camera.MaxADU")

    async def _build_one_calibration_master(
        self,
        *,
        kind: CalibrationRunKind,
        gain: int,
        records: list[CaptureFrameRecord],
        filter_name: str | None,
        stamp: str,
        max_adu: float | None,
    ) -> str:
        folder = self._settings.capture.folder
        assert folder is not None
        first = records[0]
        optic = calibration_optic_id((await asyncio.to_thread(FitsRowSource.open, first.path)).header)
        lens = optic or "tele"
        suffix_filter = ""
        if filter_name is not None:
            safe_filter = (
                "".join(
                    char if char.isalnum() or char in "-_" else "-" for char in filter_name.strip()
                ).strip("-")
                or "unknown"
            )
            suffix_filter = f"_{safe_filter}"
        exposure = first.exposure_s
        exposure_text = (
            "unknown" if exposure is None else f"{exposure:.6f}".rstrip("0").rstrip(".") + "s"
        )
        temperature_text = ""
        if kind == "dark":
            temperatures = [
                record.ccd_temp_c for record in records if record.ccd_temp_c is not None
            ]
            representative_temperature = float(np.median(temperatures)) if temperatures else None
            temperature_text = (
                "_unknownC"
                if representative_temperature is None
                else f"_{representative_temperature:+.1f}C"
            )
        type_folder = {"bias": "bias", "dark": "darks", "flat": "flats"}[kind]
        output = (
            folder
            / "Calibration"
            / type_folder
            / f"master_{kind}_{lens}{suffix_filter}_{exposure_text}_g{gain}{temperature_text}_{stamp}.fits"
        )
        collision = 2
        while output.exists():
            output = output.with_name(f"{output.stem}-{collision}.fits")
            collision += 1

        bias_paths: tuple[Path, ...] = ()
        if kind == "flat":
            groups = await self.list_calibration_source_sets()
            bias_group = _best_bias_source_set(
                groups, gain=gain, width=first.width, height=first.height, optic_id=optic
            )
            if bias_group is None:
                raise CalibrationMasterError(
                    "flat master needs at least 10 compatible retained bias frames at "
                    f"gain {gain} and {first.width}x{first.height}"
                )
            bias_paths = tuple(record.path for record in bias_group.records)
        result = await asyncio.to_thread(
            build_calibration_master,
            frame_type=FrameType(kind),
            source_paths=tuple(record.path for record in records),
            output_path=output,
            bias_paths=bias_paths,
            ssc_version=_ssc_version(),
            max_adu=max_adu,
        )
        return str(result.path)

    async def start_calibration_master_build(self, *, frame_ids: list[int]) -> CaptureState:
        """Rebuild a master from one exact retained source-set candidate."""
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError(
                "no capture folder is configured; set one in Settings > Capture first"
            )
        if self._section.in_progress:
            raise CaptureAlreadyRunningError("a capture or calibration run is already in progress")
        requested = frozenset(frame_ids)
        groups = await self.list_calibration_source_sets()
        source = next(
            (
                group
                for group in groups
                if frozenset(record.id for record in group.records) == requested
            ),
            None,
        )
        if source is None:
            raise CalibrationConfigurationError(
                "the selected retained calibration set changed; refresh and select it again"
            )
        if len(source.records) < 10:
            raise CalibrationConfigurationError(
                "a verified master requires at least 10 retained source frames"
            )
        if source.gain is None:
            raise CalibrationConfigurationError("the retained calibration set has no gain metadata")
        if source.frame_type is FrameType.FLAT:
            bias_group = _best_bias_source_set(
                groups,
                gain=source.gain,
                width=source.width,
                height=source.height,
                optic_id=source.optic_id,
            )
            if bias_group is None:
                raise CalibrationConfigurationError(
                    "this flat set needs at least 10 retained compatible bias frames at "
                    f"gain {source.gain} and {source.width}x{source.height}"
                )
        records = list(source.records)
        max_adu = (
            await self._master_max_adu(records) if source.frame_type is FrameType.FLAT else None
        )
        calibration = CalibrationRunState(
            status="running",
            kind=cast(CalibrationRunKind, source.frame_type.value),
            phase="integrating",
            requested_count=len(records),
            filter_names=(() if source.filter_name is None else (source.filter_name,)),
            current_filter=source.filter_name,
            completed_frames=len(records),
            total_frames=len(records),
            gain=source.gain,
            generate_master=True,
            message=f"Building master from {len(records)} retained frames",
        )
        self._calibration_cancel_requested = False
        self._publish(
            in_progress=True,
            last_error=None,
            frame_type=source.frame_type,
            gain=source.gain,
            calibration=calibration,
        )
        self._calibration_task = self._tasks.spawn(
            self._run_calibration_master_build(source=source, max_adu=max_adu),
            name=f"capture-calibration-rebuild-{source.frame_type.value}",
        )
        return self._section

    async def _run_calibration_master_build(
        self, *, source: CalibrationSourceSet, max_adu: float | None
    ) -> None:
        try:
            path = await self._build_one_calibration_master(
                kind=cast(CalibrationRunKind, source.frame_type.value),
                gain=cast(int, source.gain),
                records=list(source.records),
                filter_name=source.filter_name,
                stamp=self._now().astimezone(UTC).strftime("%Y%m%d-%H%M%S"),
                max_adu=max_adu,
            )
            self._publish(
                calibration=self._section.calibration.model_copy(
                    update={
                        "status": "completed",
                        "master_paths": (path,),
                        "message": (
                            f"Built master from {len(source.records)} retained frames; "
                            "raw frames retained"
                        ),
                    }
                ),
                last_error=None,
            )
        except Exception as exc:
            _log.exception("calibration master rebuild failed")
            self._publish(
                last_error=f"{type(exc).__name__}: {exc}",
                calibration=self._section.calibration.model_copy(
                    update={"status": "failed", "error": str(exc), "message": None}
                ),
            )
        finally:
            self._calibration_task = None
            self._publish(
                in_progress=False,
                stage=None,
                frame_type=None,
                gain=None,
            )

    async def list_calibration_masters(self) -> list[CalibrationMasterRecord]:
        return await asyncio.to_thread(scan_calibration_masters, self.live_stack_master_folder())

    async def get_calibration_master_image(
        self, *, frame_type: FrameType, name: str
    ) -> tuple[bytes, str]:
        """Render one active generated master through Archive's safe path seam."""
        records = await self.list_calibration_masters()
        record = next(
            (
                candidate
                for candidate in records
                if candidate.status == "active"
                and candidate.frame_type is frame_type
                and candidate.name == name
            ),
            None,
        )
        if record is None:
            raise CalibrationMasterCatalogueError("active calibration master does not exist")
        folder = self._settings.capture.folder
        assert folder is not None
        key = record.path.relative_to(folder).as_posix()
        return await self.get_archive_image(key, thumbnail=False)

    async def move_calibration_master(
        self, *, frame_type: FrameType, name: str, destination: MasterStatus
    ) -> CalibrationMasterRecord:
        return await asyncio.to_thread(
            move_calibration_master,
            self.live_stack_master_folder(),
            frame_type=frame_type,
            name=name,
            destination=destination,
        )

    async def _read_limits(
        self, camera: GainSettableCamera | None = None
    ) -> tuple[float, float]:
        camera = camera if camera is not None else self._camera
        assert camera is not None
        await _ensure_connected(camera)
        minimum_s = _as_float(await camera.get("exposuremin"), label="Camera.ExposureMin")
        maximum_s = _as_float(await camera.get("exposuremax"), label="Camera.ExposureMax")
        return minimum_s, maximum_s

    def _resolve_template_timezone(self, template: str) -> ZoneInfo:
        """Only resolved when the template actually needs a calendar date:
        ``%DATEOFCAPTURE%`` is the one tag that depends on the site's
        timezone (CLAUDE.md invariant 5). A user who never touches the
        default template, or who removes that one tag, must not be blocked
        on setting a location just to start a dark."""
        if "%DATEOFCAPTURE%" not in template:
            return _UNUSED_TZ
        location = self._settings.active_location()
        if location is None:
            raise CaptureTimezoneUnknownError(
                "the capture folder template uses %DATEOFCAPTURE%, which needs the "
                "active observing location's timezone: set one in Settings > Location "
                "before starting a capture, or edit the template to remove that tag"
            )
        try:
            return resolve_timezone(location)
        except PlanTimezoneUnknownError as exc:
            raise CaptureTimezoneUnknownError(str(exc)) from exc

    def _context(
        self,
        object_name: str | None,
        *,
        optic_id: str | None = None,
        flat_source: str | None = None,
    ) -> ExposureContext:
        device = self._state.snapshot.device
        return ExposureContext(
            object_name=object_name,
            # No persisted setting carries this yet (architecture policy invariant 7:
            # absent, never fabricated) — a real optic/profile pairing is
            # ``ssc.astro``'s planning-tab concept, not a capture-time one.
            focal_length_mm=None,
            telescope_name=self._settings.device.label,
            ssc_version=_ssc_version(),
            device_firmware=None if device is None else device.firmware,
            ephemeris=self._ephemeris,
            optic_id=optic_id,
            flat_source=flat_source,
            **exposure_metadata_fallbacks(
                self._state.snapshot.mount,
                self._settings.active_location(),
                now=self._now(),
            ),
        )

    async def _finish(
        self,
        *,
        camera: GainSettableCamera,
        wide: bool,
        started_at: datetime,
        duration_s: float,
        context: ExposureContext,
        frame_type: FrameType,
        folder_template: str,
        filename_template: str,
        tz: ZoneInfo,
        sequence: int,
        folder: Path,
        dither_applied: bool,
        center_after_drift: bool,
        loop_automation: bool,
    ) -> None:
        def on_stage(stage: CaptureStage) -> None:
            # Synchronous callback (`_publish` never awaits) — safe to call
            # straight from inside `drive_exposure`/`capture_one_exposure`'s
            # own control flow. `_publish` returns the new `CaptureState`,
            # which this callback's own contract (-> None) discards.
            self._publish(stage=stage)

        try:
            frame_id = await capture_one_exposure(
                camera=camera,
                telescope=self._telescope,
                filterwheel=None if wide else self._filterwheel,
                focuser=None if wide else self._focuser,
                repo=self._repo,
                folder=folder,
                duration_s=duration_s,
                context=context,
                frame_type=frame_type,
                folder_template=folder_template,
                filename_template=filename_template,
                tz=tz,
                sequence=sequence,
                started_at=started_at,
                now=self._now,
                sleep=self._sleep,
                on_stage=on_stage,
                unsupported_members=self.metadata_capabilities(wide=wide),
            )
            center = self._settings.capture
            if (
                center_after_drift
                and frame_type is FrameType.LIGHT
                and center.center_after_drift_enabled
            ):
                solved = await self.solve_frame(frame_id)
                drift_pixels = self._drift_pixels(solved, dither_applied=dither_applied)
                if (
                    drift_pixels is not None
                    and self._recenter is not None
                    and drift_pixels > center.drift_threshold_pixels
                ):
                    assert self._center_reference is not None
                    await self._recenter(
                        self._center_reference.coord.ra_h,
                        self._center_reference.coord.dec_deg,
                    )
        except Exception as exc:
            _log.exception("capture exposure failed")
            if loop_automation:
                self._capture_loop = None
            self._failed_exposure_camera = camera
            self._publish(
                in_progress=True,
                loop_active=False if loop_automation else self._section.loop_active,
                last_error=f"{type(exc).__name__}: {exc}; camera idle unconfirmed; acquisition blocked",
                stage=None,
                duration_s=None,
                started_at=None,
                object_name=None,
                frame_type=None,
                gain=None,
            )
            return
        completed = self._section.loop_completed_frames + (1 if loop_automation else 0)
        self._publish(
            in_progress=False,
            last_frame_id=frame_id,
            last_captured_at=self._now(),
            last_error=None,
            stage=None,
            duration_s=None,
            started_at=None,
            object_name=None,
            frame_type=None,
            gain=None,
            loop_completed_frames=completed,
        )
        spec = self._capture_loop
        if loop_automation and self._section.loop_active and spec is not None:
            if spec.frame_limit is not None and completed >= spec.frame_limit:
                self._capture_loop = None
                self._publish(loop_active=False)
                return
            try:
                await self._request_exposure(
                    duration_s=spec.duration_s,
                    gain=spec.gain,
                    object_name=spec.object_name,
                    frame_type=spec.frame_type,
                    optic_id=spec.optic_id,
                    flat_source=spec.flat_source,
                    loop_automation=True,
                )
            except Exception as exc:
                _log.exception("capture loop could not start its next exposure")
                self._capture_loop = None
                self._publish(
                    loop_active=False,
                    last_error=f"{type(exc).__name__}: {exc}",
                )

    def _drift_pixels(self, solved: PlateSolveResult, *, dither_applied: bool) -> float | None:
        """Return uncommanded drift; an intentional dither establishes a new baseline."""
        if self._center_reference is None or dither_applied:
            # A dither is deliberate pointing motion. The first solved frame
            # after it becomes the new reference, regardless of whether the
            # dither was larger than the 30″ centering tolerance.
            self._center_reference = solved
            return None
        return float(frame_offset_arcsec(self._center_reference.coord, solved.coord)) / float(
            solved.pixel_scale_arcsec
        )

    # -- catalogue reads ------------------------------------------------------- #

    async def list_frames(self, *, limit: int) -> list[CaptureFrameRecord]:
        return await self._repo.list_recent(limit=limit)

    async def frame_record(self, frame_id: int) -> CaptureFrameRecord:
        return await self._repo.get_frame(frame_id)

    def live_stack_master_folder(self) -> Path:
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        path = folder / "Calibration"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def store_live_stack_master(self, *, name: str, frame_type: FrameType, content: bytes) -> Path:
        if not name.lower().endswith((".fit", ".fits")):
            raise LiveStackError("calibration masters must be FITS files (.fit or .fits)")
        safe_name = Path(name).name
        if safe_name != name:
            raise LiveStackError("calibration master filename must not contain directories")
        type_folder = {
            FrameType.BIAS: "bias",
            FrameType.DARK: "darks",
            FrameType.FLAT: "flats",
        }[frame_type]
        path = self.live_stack_master_folder() / type_folder / safe_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def live_stack_master_files(self, frame_type: FrameType) -> list[Path]:
        """Return supplied masters of one type, independent of their filename."""
        type_folder = {
            FrameType.BIAS: "bias",
            FrameType.DARK: "darks",
            FrameType.FLAT: "flats",
        }.get(frame_type)
        if type_folder is None:
            return []
        folder = self.live_stack_master_folder() / type_folder
        return sorted(
            (
                path
                for path in folder.glob("*")
                if path.is_file() and path.suffix.lower() in {".fit", ".fits"}
            ),
            key=lambda path: path.name.casefold(),
        )

    def reset_live_stack(
        self,
        *,
        dark_master: Path | None = None,
        bias_master: Path | None = None,
        flat_master: Path | None = None,
        auto_select_masters: bool = True,
        watch_folder: str | None = None,
        watch_recursive: bool = False,
        optic_id: str | None = None,
        binning: Literal[1, 2] | None = None,
    ) -> LiveStackRun:
        if self._live_stack_save_error is not None:
            raise LiveStackError(f"unsaved integration retained; retry Save before reset: {self._live_stack_save_error}")
        if self._live_stack_lock.locked():
            raise LiveStackError("integration processing is still owned; wait before reset")
        if binning is None:
            existing = self.live_stack_run()
            binning = existing.binning if existing is not None else default_live_stack_binning()
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        if watch_folder is not None:
            selected = Path(watch_folder).expanduser().resolve()
            if not selected.is_dir():
                raise LiveStackError(f"watch folder is not a readable directory: {selected}")
            self._live_stack_watch_folder = selected
            self._live_stack_watch_recursive = watch_recursive
            self._live_stack_external = True
            light_files = self._watched_fits(selected, recursive=watch_recursive)
        else:
            light_files = self._light_files(folder)
            if not light_files:
                raise LiveStackError("no capture-session lights folder exists yet")
            self._live_stack_watch_folder = max(
                light_files, key=lambda path: path.stat().st_mtime
            ).parent
            self._live_stack_watch_recursive = False
            self._live_stack_external = False
            light_files = self._light_files(self._live_stack_watch_folder, recursive=False)
        self._live_stack_optic_id = optic_id
        self._live_stack_auto_select_masters = auto_select_masters
        self._live_stack_master_selection_message = None
        selection_light: Path | None = None
        if auto_select_masters and light_files:
            selection_light = max(light_files, key=lambda path: path.stat().st_mtime)
            automatic = self._auto_select_live_stack_masters(
                selection_light,
                optic_id=optic_id,
            )
            if dark_master is None and bias_master is None:
                dark_master = automatic[0]
                bias_master = automatic[1]
            flat_master = flat_master or automatic[2]
        if self._live_stack is None:
            self._live_stack = LiveStackEngine(folder)
        # A new/reset stack means a new integration of the selected data set,
        # not merely watching for files created after the button was pressed.
        # Queue the existing lights first, then continue watching for arrivals.
        self._live_stack_seen.clear()
        self._scheduled_live_stack_scope = None
        self._live_stack_pending_sizes.clear()
        self._live_stack_processing_file = None
        self._live_stack_pending_count = len(light_files)
        self._live_stack_rejected_count = 0
        self._live_stack_last_rejection_reason = None
        self._live_stack_rejected_folder = None
        run = self._live_stack.reset(
            dark_master=dark_master,
            bias_master=bias_master,
            flat_master=flat_master,
            binning=binning,
        )
        if selection_light is not None:
            self._live_stack_master_selection_message = self._master_selection_message(
                selection_light, run
            )
        elif auto_select_masters:
            self._live_stack_master_selection_message = (
                "No existing light frame was available for automatic master selection; "
                "this run will continue without calibration masters."
            )
        else:
            self._live_stack_master_selection_message = "Automatic master selection is disabled."
        self._scheduled_live_stack_queue = None
        if self._scheduled_live_stack_worker is not None:
            self._scheduled_live_stack_worker.cancel()
            self._scheduled_live_stack_worker = None
        if self._live_stack_task is not None:
            self._live_stack_task.cancel()
        self._live_stack_active = True
        self._live_stack_error = None
        self._live_stack_task = self._tasks.spawn(
            self._watch_live_stack(), name="capture-live-stack"
        )
        self._ensure_wide_rtsp_polling()
        return run

    def reset_scheduled_live_stack(
        self, first_frame: Path, *, seed_folder_baseline: bool = True, **preferences: object
    ) -> LiveStackRun:
        """Start a stack at ``first_frame`` without importing older panel data.

        The ordinary reset intentionally queues every FITS already in the chosen
        folder. A scheduled panel instead owns only frames captured after its
        start, even when its target/date folder already contains an earlier run.
        Seed the watcher's seen set with that existing folder snapshot; the
        scheduler then explicitly adds ``first_frame`` and every later frame.
        """
        folder = first_frame.resolve().parent
        run = self.reset_live_stack(watch_folder=str(folder), watch_recursive=False, **preferences)
        ordered_existing = self._watched_fits(folder, recursive=False)
        first_frame = first_frame.resolve()
        existing = {path for path in ordered_existing if path != first_frame}
        self._live_stack_seen.update(existing)
        history = [path for path in ordered_existing if path != first_frame]
        if len(history) > 8:
            indices = np.linspace(0, len(history) - 1, num=8, dtype=int)
            history = [history[int(index)] for index in indices]
        self._scheduled_live_stack_baseline_paths = tuple(history) if seed_folder_baseline else ()
        self._live_stack_pending_count = 0
        self._live_stack_external = False
        if self._live_stack_task is not None:
            self._live_stack_task.cancel()
            self._live_stack_task = None
        if self._scheduled_live_stack_worker is not None:
            self._scheduled_live_stack_worker.cancel()
        self._scheduled_live_stack_queue = asyncio.Queue(maxsize=2)
        self._scheduled_live_stack_worker = self._tasks.spawn(
            self._run_scheduled_live_stack(), name="scheduled-live-stack"
        )
        return run

    async def restore_scheduled_live_stack(
        self, run_id: str, block_id: str, panel_index: int | None = None,
        start_index: int | None = 0, *, prepare_only: bool = False,
    ) -> bool:
        """Rebuild a lost RAM integration from this exact persisted science item.

        Raw frames remain authoritative. Saved preferences pin the resolution
        and calibration masters across a deployment; folder neighbours never
        become members of the recovered integration.
        """
        scope = (run_id, block_id, panel_index)
        if getattr(self, "_scheduled_live_stack_scope", None) == scope and self.live_stack_run() is not None:
            return True
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        key = hashlib.sha256(f"{run_id}\0{block_id}\0{panel_index}".encode()).hexdigest()
        checkpoint = folder / f".scheduled-stack-{key}.json"
        preferences: dict[str, object] = {}
        if checkpoint.exists():
            payload = json.loads(await asyncio.to_thread(checkpoint.read_text))
            if payload.get("scope") != list(scope) or payload.get("binning") not in (None, 1, 2):
                raise LiveStackError("invalid scheduled integration checkpoint")
            if payload.get("binning") is not None:
                preferences = {name: Path(payload[name]) if payload[name] else None
                               for name in ("dark_master", "bias_master", "flat_master")}
                preferences.update(binning=payload["binning"], auto_select_masters=False)
            start_index = payload["start_index"]
        if start_index is None:
            raise LiveStackError("scheduled panel checkpoint missing; cannot safely mix mosaic panels")
        if not isinstance(start_index, int) or start_index < 0:
            raise LiveStackError("invalid scheduled panel frame boundary")
        if prepare_only:
            if not checkpoint.exists():
                await asyncio.to_thread(self._persist_stack_checkpoint, checkpoint,
                                        {"scope": list(scope), "start_index": start_index, "binning": None})
            return False
        records = [record for record in await self._repo.list_block_frames(
            run_id, block_id, include_writing=False
        ) if record.frame_type == "light"]
        records = records[start_index:]
        if not records:
            return False
        for record in records:
            if not record.path.is_file():
                raise LiveStackError(f"saved science frame is missing: {record.path}")
        run = self.reset_scheduled_live_stack(records[0].path, seed_folder_baseline=False, **preferences)
        payload = {"scope": list(scope), "binning": run.binning, "start_index": start_index,
                   **{name: str(getattr(run, name)) if getattr(run, name) else None
                      for name in ("dark_master", "bias_master", "flat_master")}}
        await asyncio.to_thread(self._persist_stack_checkpoint, checkpoint, payload)
        self._scheduled_live_stack_baseline_paths = ()
        self._live_stack_seen.difference_update(record.path.resolve() for record in records)
        for record in records:
            try:
                await self.add_live_stack_frame(record.id)
            except LiveStackFrameRejectedError as exc:
                self._live_stack_rejected_count += 1
                self._live_stack_last_rejection_reason = str(exc)
        self._scheduled_live_stack_scope = scope
        _log.info("scheduled integration recovered run=%s item=%s catalogue_frames=%d", run_id, block_id, len(records))
        return True

    @staticmethod
    def _persist_stack_checkpoint(checkpoint: Path, payload: dict[str, object]) -> None:
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=checkpoint.parent, prefix=".scheduled-stack-", suffix=".part")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, checkpoint)
            directory = os.open(checkpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def queue_scheduled_live_stack_frame(self, frame_id: int) -> LiveStackRun | None:
        """Offer a saved frame without blocking capture on stack processing.

        Stop/reset controls detach this consumer. A full queue skips preview
        processing for this frame; the saved FITS remains available for stacking.
        """
        queue = self._scheduled_live_stack_queue
        worker = self._scheduled_live_stack_worker
        if queue is None or worker is None or worker.done() or not self._live_stack_active:
            return self.live_stack_run()
        try:
            queue.put_nowait(frame_id)
        except asyncio.QueueFull:
            _log.warning("live stack is busy; capture frame %d remains saved", frame_id)
            return self.live_stack_run()
        self._live_stack_pending_count = queue.qsize()
        return self.live_stack_run()

    async def _run_scheduled_live_stack(self) -> None:
        queue = self._scheduled_live_stack_queue
        assert queue is not None
        if self._live_stack is not None and self._scheduled_live_stack_baseline_paths:
            await asyncio.to_thread(
                self._live_stack.prime_quality_baseline,
                self._scheduled_live_stack_baseline_paths,
            )
        self._scheduled_live_stack_baseline_paths = ()
        while True:
            frame_id = await queue.get()
            self._live_stack_pending_count = queue.qsize()
            try:
                await self.add_live_stack_frame(frame_id)
            except LiveStackFrameRejectedError as exc:
                _log.warning("scheduled live stack rejected capture frame %d: %s", frame_id, exc)
                self._live_stack_last_rejection_reason = str(exc)
                self._live_stack_rejected_count += 1
            except Exception as exc:
                await self._repo.record_stack_outcome(frame_id, "error")
                _log.exception("scheduled live stack skipped capture frame %d", frame_id)
                self._live_stack_error = (
                    f"Skipped capture frame {frame_id}: {type(exc).__name__}: {exc}"
                )
                self._live_stack_rejected_count += 1
            finally:
                queue.task_done()
                self._live_stack_pending_count = queue.qsize()

    def _auto_select_live_stack_masters(
        self, light_path: Path, *, optic_id: str | None = None
    ) -> tuple[Path | None, Path | None, Path | None]:
        """Choose only metadata-compatible local masters for one light."""
        light = read_fits_image(light_path.read_bytes())
        header = light.header
        gain = header.get("GAIN")
        exposure = header.get("EXPTIME")
        temperature = header.get("CCD-TEMP")
        filter_name = _canonical_filter_name(header.get("FILTER"))
        matching_optic_id = (
            _canonical_optic_id({"OPTICID": optic_id})
            if optic_id is not None
            else _canonical_optic_id(header)
        )
        candidates: dict[FrameType, list[tuple[Path, dict[str, object]]]] = {
            FrameType.DARK: [],
            FrameType.BIAS: [],
            FrameType.FLAT: [],
        }
        for frame_type in (FrameType.DARK, FrameType.BIAS, FrameType.FLAT):
            for path in self.live_stack_master_files(frame_type):
                try:
                    image = read_fits_image(path.read_bytes())
                except (OSError, ValueError):
                    continue
                if image.data.shape != light.data.shape:
                    continue
                try:
                    if not calibration_optics_match(image.header, matching_optic_id):
                        continue
                except CalibrationMasterError:
                    continue
                image_type = image.header.get("IMAGETYP")
                if isinstance(image_type, str) and image_type.casefold() != frame_type.value:
                    continue
                candidates[frame_type].append((path, image.header))

        def newest(items: list[tuple[Path, dict[str, object]]]) -> Path | None:
            return max(items, key=lambda item: item[0].stat().st_mtime)[0] if items else None

        darks = [
            item
            for item in candidates[FrameType.DARK]
            if item[1].get("GAIN") == gain and item[1].get("EXPTIME") == exposure
        ]
        dark: Path | None = None
        if darks:

            def dark_score(item: tuple[Path, dict[str, object]]) -> tuple[float, float]:
                candidate_temp = item[1].get("CCD-TEMP")
                distance = (
                    abs(float(candidate_temp) - float(temperature))
                    if isinstance(candidate_temp, int | float)
                    and isinstance(temperature, int | float)
                    else float("inf")
                )
                return distance, -item[0].stat().st_mtime

            dark = min(darks, key=dark_score)[0]

        bias = None
        if dark is None:
            bias = newest(
                [item for item in candidates[FrameType.BIAS] if item[1].get("GAIN") == gain]
            )
        flat = newest(
            [
                item
                for item in candidates[FrameType.FLAT]
                if item[1].get("GAIN") == gain
                and _canonical_filter_name(item[1].get("FILTER")) == filter_name
            ]
        )
        return dark, bias, flat

    def _master_selection_message(self, light_path: Path, run: LiveStackRun) -> str:
        light = read_fits_image(light_path.read_bytes())
        header = light.header
        shape = "x".join(str(value) for value in light.data.shape)
        gain = header.get("GAIN", "unknown")
        exposure = header.get("EXPTIME", "unknown")
        filter_name = header.get("FILTER", "unknown")
        optic_id = self._live_stack_optic_id or header.get("OPTICID", "unspecified")
        pedestal_text = (
            f"dark {run.dark_master.name}"
            if run.dark_master is not None
            else f"bias {run.bias_master.name}"
            if run.bias_master is not None
            else (f"no compatible dark or bias (shape {shape}, gain {gain}, exposure {exposure}s)")
        )
        temperature_note = ""
        if run.dark_master is not None:
            dark_header = read_fits_image(run.dark_master.read_bytes()).header
            light_temperature = header.get("CCD-TEMP")
            dark_temperature = dark_header.get("CCD-TEMP")
            if isinstance(light_temperature, int | float) and isinstance(
                dark_temperature, int | float
            ):
                difference = abs(float(light_temperature) - float(dark_temperature))
                if difference > 0.05:
                    temperature_note = (
                        f" WARNING: dark temperature mismatch is {difference:.1f} C "
                        f"(light {float(light_temperature):+.1f} C, "
                        f"master {float(dark_temperature):+.1f} C)."
                    )
        flat_text = (
            f"flat {run.flat_master.name}"
            if run.flat_master is not None
            else (
                "no compatible flat "
                f"(shape {shape}, gain {gain}, filter {filter_name}, optic {optic_id})"
            )
        )
        bias_note = (
            "; bias not used because a dark is selected"
            if run.dark_master is not None and run.bias_master is not None
            else ""
        )
        return (
            f"Calibration for {light_path.name}: {pedestal_text}; "
            f"{flat_text}{bias_note}.{temperature_note}"
        )

    def live_stack_run(self) -> LiveStackRun | None:
        return None if self._live_stack is None else self._live_stack.run

    def scheduled_focus_quality(self) -> tuple[Path, float | None, int] | None:
        sample = None if self._live_stack is None else self._live_stack.latest_frame_quality
        if sample is None:
            return None
        path, quality = sample
        return path, quality.hfr_px, quality.star_count

    def live_stack_pixels(self, *, auto_crop: bool = False) -> tuple[np.ndarray, str | None] | None:
        if self._live_stack is None:
            return None
        pixels = self._live_stack.presentation_pixels(auto_crop=auto_crop)
        if pixels is None:
            return None
        return pixels, self._live_stack.bayer_pattern

    async def render_live_stack_preview(
        self,
        renderer: Callable[[np.ndarray, str | None], bytes],
        *,
        auto_crop: bool = False,
    ) -> bytes | None:
        """Share one full-resolution render; HTTP cancellation must not multiply workers."""
        while True:
            engine = self._live_stack
            run = None if engine is None else engine.run
            if engine is None or run is None or engine.pixels is None:
                return None
            key = (id(engine), run.sequence, len(run.frame_paths), auto_crop)
            if self._live_stack_preview_cache is not None:
                cached_key, cached = self._live_stack_preview_cache
                if cached_key == key:
                    return cached
            pending = self._live_stack_preview_task
            pending_key = self._live_stack_preview_key
            if pending is None or pending.done():
                published = self.live_stack_pixels(auto_crop=auto_crop)
                if published is None:
                    return None
                pixels, pattern = published
                pending = asyncio.create_task(
                    asyncio.to_thread(renderer, pixels, pattern),
                    name="live-stack-preview",
                )
                # Retrieve failures even if every HTTP consumer disconnects.
                pending.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception()
                )
                self._live_stack_preview_task = pending
                self._live_stack_preview_key = key
                pending_key = key
            rendered = await asyncio.shield(pending)
            assert pending_key is not None
            self._live_stack_preview_cache = (pending_key, rendered)
            if pending_key == key:
                return rendered
            # A crop/version change waits for the existing worker before
            # obtaining its pixel snapshot, keeping queued requests lightweight.

    @property
    def live_stack_active(self) -> bool:
        return self._live_stack_active

    @property
    def live_stack_error(self) -> str | None:
        return self._live_stack_error

    @property
    def live_stack_master_selection_message(self) -> str | None:
        return self._live_stack_master_selection_message

    @property
    def live_stack_auto_select_masters(self) -> bool:
        return self._live_stack_auto_select_masters

    @property
    def live_stack_watch_folder(self) -> Path | None:
        return self._live_stack_watch_folder

    @property
    def live_stack_watch_recursive(self) -> bool:
        return self._live_stack_watch_recursive

    @property
    def live_stack_processing_file(self) -> Path | None:
        return self._live_stack_processing_file

    @property
    def live_stack_pending_count(self) -> int:
        return self._live_stack_pending_count

    @property
    def live_stack_rejected_count(self) -> int:
        return self._live_stack_rejected_count

    @property
    def live_stack_last_rejection_reason(self) -> str | None:
        return self._live_stack_last_rejection_reason

    @property
    def live_stack_rejected_folder(self) -> Path | None:
        return self._live_stack_rejected_folder

    def live_stack_target_header(self) -> tuple[str | None, str | None, str | None]:
        scheduled_current, next_name, next_start = self._scheduled_targets()
        run = None if self._live_stack is None else self._live_stack.run
        current = None if run is None else run.object_name
        current = current or scheduled_current
        if next_name is None or next_start is None:
            return current, None, None
        location = self._settings.active_location()
        if location is not None and location.timezone is not None:
            with contextlib.suppress(ValueError, ZoneInfoNotFoundError):
                next_start = next_start.astimezone(ZoneInfo(location.timezone))
        return current, next_name, next_start.strftime("%H:%M")

    def live_stack_target_header_text(self) -> str | None:
        current, next_name, next_start = self.live_stack_target_header()
        if current is None:
            return None
        if next_name is None or next_start is None:
            return current
        return f"{current}  ·  next: {next_name} (starting {next_start})"

    async def stop_scheduled_live_stack(self, *, require_retained: bool = False) -> LiveStackRun | None:
        if self.live_stack_run() is None:
            if require_retained:
                raise LiveStackError("retained integration is missing after restart; saved raw FITS require reconstruction before cleanup can complete")
            return None
        return await self.stop_live_stack()

    async def _save_retained_live_stack(self, **kwargs: object) -> LiveStackRun:
        assert self._live_stack is not None
        try:
            saved = await asyncio.to_thread(self._live_stack.save, **kwargs)
        except Exception as exc:
            self._live_stack_save_error = f"{type(exc).__name__}: {exc}"
            self._live_stack_error = f"Save failed; integration retained: {self._live_stack_save_error}"
            raise LiveStackError(self._live_stack_error) from exc
        self._live_stack_save_error = None
        self._live_stack_error = None
        return saved

    async def stop_live_stack(self) -> LiveStackRun:
        self.clear_scheduled_dither_center()
        run = self.live_stack_run()
        if run is None:
            raise LiveStackError("no live stack has been started")
        queue = self._scheduled_live_stack_queue
        worker = self._scheduled_live_stack_worker
        self._scheduled_live_stack_queue = None
        self._live_stack_active = False
        if queue is not None:
            await queue.join()
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._scheduled_live_stack_queue = None
        self._scheduled_live_stack_worker = None
        self._live_stack_pending_count = 0
        self._live_stack_active = False
        if self._live_stack_task is not None:
            self._live_stack_task.cancel()
            self._live_stack_task = None
        assert self._live_stack is not None
        async with self._live_stack_lock:
            saved = await self._save_retained_live_stack()
        self._ensure_wide_rtsp_polling()
        await self.reconcile_stream_automation()
        return saved

    async def save_live_stack(self) -> LiveStackRun:
        if self._live_stack is None or self._live_stack.run is None:
            raise LiveStackError("no live stack has been started")
        try:
            return await self._export_live_stack()
        except Exception as exc:
            self._live_stack_save_error = f"{type(exc).__name__}: {exc}"
            self._live_stack_error = f"Save failed; integration retained: {self._live_stack_save_error}"
            raise LiveStackError(self._live_stack_error) from exc

    async def _export_live_stack(self) -> LiveStackRun:
        assert self._live_stack is not None
        async with self._live_stack_lock:
            run = self._live_stack.run
            destination = (self._live_stack_watch_folder or run.output_path.parent) / "livestack"
            await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
            saved_at = self._now()
            template = self._settings.capture.filename_template
            timezone = self._resolve_template_timezone(template)
            metadata = FrameMetadata(
                date_obs=saved_at,
                exposure_s=run.exposure_s,
                gain=run.gain,
                ccd_temp_c=run.ccd_temp_c,
                bayer_offset_x=None,
                bayer_offset_y=None,
                pixel_size_um=None,
                ra_deg=None,
                dec_deg=None,
                site_lat_deg=None,
                site_lon_deg=None,
                filter_name=run.filter_name,
                object_name=run.object_name,
                frame_type=FrameType.LIGHT,
                focal_length_mm=None,
                telescope_name="Photon Commander live stack",
                ssc_version=importlib.metadata.version("ssc"),
                device_firmware=None,
            )
            stack_template = template if "%TARGET%" in template else "%TARGET%_" + template
            stem = "live_" + render_capture_filename(
                stack_template,
                metadata=metadata,
                date_obs=saved_at,
                sequence=run.sequence,
                tz=timezone,
            )
            output_path = destination / f"{stem}.fits"
            collision = 2
            while output_path.exists():
                output_path = destination / f"{stem}-{collision}.fits"
                collision += 1
            return await self._save_retained_live_stack(
                output_path=output_path,
                parameters_path=output_path.with_suffix(".json"),
                date_obs=saved_at,
            )

    async def _watch_live_stack(self) -> None:
        while self._live_stack_active:
            await self._sleep(5.0)
            try:
                await self.poll_live_stack()
            except Exception as exc:
                _log.exception("live-stack watcher failed")
                self._live_stack_error = f"{type(exc).__name__}: {exc}"
                self._live_stack_active = False
                return

    async def add_live_stack_frame(self, frame_id: int) -> LiveStackRun:
        if self._live_stack is None:
            self.reset_live_stack()
        assert self._live_stack is not None
        record = await self._repo.get_frame(frame_id)
        if record.status != "complete":
            raise LiveStackError(f"capture frame {frame_id} is not complete")
        async with self._live_stack_lock:
            if record.path in self._live_stack_seen:
                run = self._live_stack.run
                if run is None:
                    raise LiveStackError("no live stack has been started")
                return run
            self._live_stack_processing_file = record.path
            try:
                try:
                    processing = asyncio.create_task(asyncio.to_thread(
                        self._live_stack.add_frame,
                        record.path,
                        measure_quality=True,
                    ))
                    try:
                        run = await asyncio.shield(processing)
                    except asyncio.CancelledError:
                        # A native worker cannot be cancelled. Keep ownership
                        # until its mutation ends before reset/recovery proceeds.
                        while not processing.done():
                            with contextlib.suppress(Exception, asyncio.CancelledError):
                                await asyncio.shield(processing)
                        raise
                except LiveStackFrameRejectedError:
                    await self._repo.record_stack_outcome(frame_id, "rejected")
                    await self._quarantine_live_stack_frame(record.path, frame_id=frame_id)
                    raise
                await self._repo.record_stack_outcome(frame_id, "accepted")
                self._live_stack_seen.add(record.path)
            finally:
                self._live_stack_processing_file = None
        await self.reconcile_stream_automation()
        return run

    async def poll_live_stack(self) -> LiveStackRun:
        if self._live_stack is None or self._live_stack_watch_folder is None:
            raise LiveStackError("start or reset the live stack before polling captures")
        async with self._live_stack_lock:
            session_paths = set(
                self._watched_fits(
                    self._live_stack_watch_folder, recursive=self._live_stack_watch_recursive
                )
                if self._live_stack_external
                else self._light_files(self._live_stack_watch_folder, recursive=False)
            )
            new_paths = sorted(session_paths - self._live_stack_seen)
            self._live_stack_pending_count = len(new_paths)
            for path in new_paths:
                size = path.stat().st_size
                if self._live_stack_pending_sizes.get(path) != size:
                    self._live_stack_pending_sizes[path] = size
                    continue
                self._live_stack_processing_file = path
                try:
                    await asyncio.to_thread(
                        self._live_stack.add_frame,
                        path,
                        measure_quality=True,
                    )
                except LiveStackFrameRejectedError as exc:
                    await self._quarantine_live_stack_frame(path)
                    _log.warning("live stack rejected %s: %s", path, exc)
                    self._live_stack_last_rejection_reason = str(exc)
                    self._live_stack_rejected_count += 1
                except Exception as exc:
                    # One malformed or incompatible input must not terminate
                    # watching every later file in an external producer's folder.
                    _log.exception("live stack skipped %s", path)
                    self._live_stack_error = f"Skipped {path.name}: {type(exc).__name__}: {exc}"
                    self._live_stack_rejected_count += 1
                finally:
                    self._live_stack_processing_file = None
                    self._live_stack_seen.add(path)
                    self._live_stack_pending_sizes.pop(path, None)
                    self._live_stack_pending_count -= 1
        assert self._live_stack.run is not None
        await self.reconcile_stream_automation()
        return self._live_stack.run

    async def _quarantine_live_stack_frame(
        self, source: Path, *, frame_id: int | None = None
    ) -> Path | None:
        if not self._settings.capture.keep_rejected_frames:
            await asyncio.to_thread(source.unlink)
            if frame_id is None:
                records = await self._repo.list_complete()
                record = next((item for item in records if item.path == source), None)
                frame_id = None if record is None else record.id
            if frame_id is not None:
                await self._repo.delete_complete_frame(frame_id)
                self._clear_deleted_last_frame({frame_id})
            self._live_stack_rejected_folder = None
            return None
        destination_folder = source.parent / "rejected"
        await asyncio.to_thread(destination_folder.mkdir, parents=True, exist_ok=True)
        destination = destination_folder / source.name
        suffix = 2
        while destination.exists():
            destination = destination_folder / f"{source.stem}-{suffix}{source.suffix}"
            suffix += 1
        await asyncio.to_thread(shutil.move, source, destination)
        if frame_id is None:
            records = await self._repo.list_complete()
            record = next((item for item in records if item.path == source), None)
            frame_id = None if record is None else record.id
        if frame_id is not None:
            try:
                await self._repo.move_complete_frame(frame_id, destination)
            except Exception:
                await asyncio.to_thread(shutil.move, destination, source)
                raise
        self._live_stack_rejected_folder = destination_folder
        return destination

    @property
    def live_stack_rtsp_status(self) -> RtspStatus:
        return self._rtsp.status

    async def start_live_stack_rtsp(
        self, config: RtspConfig, *, automatic: bool = False
    ) -> RtspStatus:
        stack_available = (
            self._live_stack_active
            and self._live_stack is not None
            and self._live_stack.pixels is not None
        )
        if not stack_available and not config.wide_camera_until_stacked:
            raise LiveStackError("live stacking needs at least one accepted frame before streaming")
        if not stack_available:
            mount = self._state.snapshot.mount
            if mount is None or mount.at_park is not False:
                raise LiveStackError("the wide-camera stream requires an unparked mount")
            if self._wide_camera is None:
                raise LiveStackError("this ssc-core hosts no wide camera")
            self._wide_rtsp_exposure_s = None
            self._wide_rtsp_last_exposure_s = None
            await self._capture_wide_rtsp_frame()
        status = await self._rtsp.start(config)
        self._rtsp_automatic = automatic
        self._ensure_wide_rtsp_polling()
        return status

    async def stop_live_stack_rtsp(self) -> RtspStatus:
        if self._youtube.status.active:
            await self._youtube.stop()
        if self._wide_rtsp_task is not None:
            self._wide_rtsp_task.cancel()
            self._wide_rtsp_task = None
        self._wide_rtsp_pixels = None
        self._wide_rtsp_temperature_c = None
        self._rtsp_automatic = False
        return await self._rtsp.stop()

    async def restart_live_stack_rtsp(self) -> RtspStatus:
        """Restart the active encoder from the latest persisted settings."""
        if not self._rtsp.status.active:
            raise LiveStackError("the RTSP broadcast is not running")
        if self._youtube.status.active:
            raise LiveStackError("stop the YouTube relay before restarting its RTSP source")
        automatic = self._rtsp_automatic
        config = self._configured_rtsp_config()
        await self.stop_live_stack_rtsp()
        return await self.start_live_stack_rtsp(config, automatic=automatic)

    @property
    def youtube_stream_status(self) -> YoutubeStreamStatus:
        return self._youtube.status

    async def start_youtube_stream(self, stream_key: str) -> YoutubeStreamStatus:
        rtsp = self._rtsp.status
        if not rtsp.active or rtsp.url is None:
            raise YoutubeStreamError(
                "start the Photon Commander RTSP broadcast before publishing to YouTube"
            )
        return await self._youtube.start(
            rtsp_url=rtsp.url,
            stream_key=stream_key,
            resolution=self._settings.streaming.youtube_resolution,
        )

    async def stop_youtube_stream(self) -> YoutubeStreamStatus:
        status = await self._youtube.stop()
        await self.reconcile_stream_automation()
        return status

    async def aclose(self) -> None:
        """Drain quality work and streams before the database/task registry closes."""
        await self._quality_cache.aclose()
        await self._youtube.stop()
        if self._rtsp.status.active:
            await self.stop_live_stack_rtsp()

    async def set_wide_companion_policy(self, enabled: bool) -> None:
        """Reserve preview ownership before persisting a changed capture policy."""
        self._wide_policy_transition = True
        try:
            await self.drain_wide_preview()
            await self._settings.set_wide_companion_enabled(enabled)
            self.refresh_wide_stream_policy()
        finally:
            self._wide_policy_transition = False

    def refresh_wide_stream_policy(self) -> None:
        if self._settings.capture.wide_companion_enabled:
            # Never replay an obsolete Wide preview as a live camera feed.
            self._wide_rtsp_pixels = None
        else:
            self._ensure_wide_rtsp_polling()

    def _ensure_wide_rtsp_polling(self) -> None:
        if self._settings.capture.wide_companion_enabled:
            return
        config = self._rtsp.status.config
        mount = self._state.snapshot.mount
        stack_available = (
            self._live_stack_active
            and self._live_stack is not None
            and self._live_stack.pixels is not None
        )
        wide_needed = config is not None and (
            (config.wide_camera_until_stacked and not stack_available)
            or (self._settings.streaming.wide_camera_picture_in_picture and stack_available)
        )
        if (
            not self._rtsp.status.active
            or not wide_needed
            or self._wide_camera is None
            or mount is None
            or mount.at_park is not False
        ):
            return
        if self._wide_rtsp_task is None or self._wide_rtsp_task.done():
            self._wide_rtsp_task = self._tasks.spawn(
                self._poll_wide_rtsp(), name="wide-camera-rtsp"
            )

    def _wide_preview_busy(self) -> bool:
        return (getattr(self, "_wide_policy_transition", False)
                or self._section.in_progress
                or getattr(self, "_auxiliary_camera_busy", lambda: False)())

    async def drain_wide_preview(self) -> None:
        """Wait for preview retrieval/cleanup before touching the main camera."""
        async with self._wide_capture_lock:
            if getattr(self, "_wide_cleanup_error", None) is not None:
                try:
                    main = getattr(self, "_camera", None)
                    wide = self._wide_camera
                    if (main is None or wide is None
                            or int(cast(int | str, await main.get("camerastate"))) != 0
                            or int(cast(int | str, await wide.get("camerastate"))) != 0):
                        raise CaptureAlreadyRunningError(self._wide_cleanup_error)
                except Exception as exc:
                    raise CaptureAlreadyRunningError(self._wide_cleanup_error) from exc
                previous_error = self._wide_cleanup_error
                self._wide_cleanup_error = None
                if getattr(self._section, "last_error", None) == previous_error:
                    self._publish(last_error=None)

    async def _recover_failed_exposure(self, *, during_recovery: bool = False) -> None:
        if getattr(self, "_scheduled_recovery_active", False) and not during_recovery:
            raise CaptureAlreadyRunningError("Scheduled recovery owns the camera")
        lock = getattr(self, "_failed_exposure_recovery_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._failed_exposure_recovery_lock = lock
        async with lock:
            await self._recover_failed_exposure_locked()

    async def _recover_failed_exposure_locked(self) -> None:
        """Keep ownership until the failed camera independently confirms idle.

        Called on subsequent acquisition requests, so an outage does not leave
        an unrecoverable software latch. Abort cannot remove catalogue files.
        """
        camera = getattr(self, "_failed_exposure_camera", None)
        if camera is None:
            return
        error = "Failed exposure cleanup pending; camera idle unconfirmed; acquisition blocked"
        try:
            async with asyncio.timeout(5):
                state = await camera.get("camerastate")
                if type(state) is not int or state != 0:
                    await camera.put("abortexposure")
                    for _ in range(25):
                        state = await camera.get("camerastate")
                        if type(state) is int and state == 0:
                            break
                        await self._sleep(0.1)
                    else:
                        raise CaptureAlreadyRunningError(error)
                if type(state) is not int or state != 0:
                    raise CaptureAlreadyRunningError(error)
        except Exception as exc:
            self._publish(in_progress=True, last_error=f"{error}: {exc}")
            raise CaptureAlreadyRunningError(error) from exc
        self._failed_exposure_camera = None
        self._publish(in_progress=False, stage=None)

    @property
    def scheduled_recovery_active(self) -> bool:
        """Allow durable Stop to interrupt lower-layer repair waits immediately."""
        return bool(getattr(self, "_scheduled_recovery_active", False))

    async def prepare_dawn_safety(self) -> None:
        """Prevent automatic acquisition before priority parking; retain saved raws.

        The caller bounds export time and proceeds with safety even if storage
        or a worker is stuck. Cancelling an unfinished calibration is intentional
        at this priority deadline, rather than waiting for another science frame.
        """
        self._capture_loop = None
        self._calibration_cancel_requested = True
        self._live_stack_active = False
        self._publish(loop_active=False)
        for task in (getattr(self, "_calibration_task", None),
                     getattr(self, "_live_stack_task", None),
                     getattr(self, "_wide_rtsp_task", None)):
            if task is not None and not task.done():
                task.cancel()
        try:
            await self.stop_scheduled_live_stack()
        finally:
            # Export may re-evaluate preview streaming; no preview exposure
            # should restart between safety preparation and physical parking.
            preview = getattr(self, "_wide_rtsp_task", None)
            if preview is not None and not preview.done():
                preview.cancel()

    async def recover_scheduled_operation(
        self, *, level: int, operation: str,
        ensure_link: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Repair lower layers without discarding frames/stack or moving a field.

        Reconnect is injected from the public device service and must honour
        explicit Disconnect. Planned optical re-centering and durable executor
        relaunch are higher layers; this method issues no positional command.
        """
        active_run = self._state.snapshot.run
        run_id = None if active_run is None else active_run.run_id
        self._check_dither_run_stop(run_id)
        stage = "ownership"
        claimed = False
        try:
            if (self._auxiliary_camera_busy() or self._section.loop_active
                    or getattr(self, "_scheduled_recovery_active", False)):
                raise CaptureAlreadyRunningError("Another camera owner blocks scheduled recovery")
            if (getattr(self, "_calibration_task", None) is not None
                    or (self._section.in_progress
                        and getattr(self, "_failed_exposure_camera", None) is None)):
                raise CaptureAlreadyRunningError("Active capture owner blocks scheduled recovery")
            self._scheduled_recovery_active = True
            claimed = True
            self._publish(in_progress=True)
            if level >= 2:
                stage = "link"
                _log.info("Scheduled recovery level=%s operation=%s stage=%s", level, operation, stage)
                self._publish(last_error=f"Recovery level {level}: verifying device connection")
                if ensure_link is None:
                    raise CaptureDeviceUnavailableError("connection recovery unavailable")
                await ensure_link()
                self._check_dither_run_stop(run_id)
            stage = "camera_idle"
            _log.info("Scheduled recovery level=%s operation=%s stage=%s", level, operation, stage)
            self._publish(last_error=f"Recovery level {level}: verifying camera idle")
            await self._recover_failed_exposure(during_recovery=True)
            self._publish(in_progress=True)
            await self.drain_wide_preview()
            if self._camera is None:
                raise CaptureDeviceUnavailableError("camera unavailable during recovery")
            async with asyncio.timeout(5):
                camera_state = await self._camera.get("camerastate")
            if type(camera_state) is not int or camera_state != 0:
                # Without our failed-owner latch, this may belong to another
                # client. Never abort unknown acquisition to repair this plan.
                raise CaptureAlreadyRunningError("Camera idle unconfirmed; unknown acquisition not aborted")
            self._check_dither_run_stop(run_id)
            # Centering owns slew -> stopped telemetry -> tracking -> solve.
            # After reconnect the mount can be pointing below the horizon;
            # enabling tracking here would prevent that ordered repair forever.
            if level >= 2 and operation in {"expose_frame", "dither", "autofocus"}:
                stage = "tracking"
                mount = self._state.snapshot.mount
                polled_at = None if mount is None else mount.polled_at
                _log.info("Scheduled recovery level=%s operation=%s stage=%s", level, operation, stage)
                self._publish(last_error=f"Recovery level {level}: verifying stopped tracking")
                await self._restore_dither_tracking(after_polled_at=polled_at, stop_run_id=run_id)
            _log.info("Scheduled recovery level=%s operation=%s stage=complete", level, operation)
        except Exception as exc:
            _log.warning("Scheduled recovery level=%s operation=%s stage=%s failed: %s: %s",
                         level, operation, stage, type(exc).__name__, exc)
            self._publish(last_error=f"Recovery level {level} {stage}: {type(exc).__name__}: {exc}")
            raise
        finally:
            if claimed:
                self._scheduled_recovery_active = False
                self._publish(in_progress=getattr(self, "_failed_exposure_camera", None) is not None)

    async def prepare_scheduled_capture(self) -> None:
        await self._recover_failed_exposure()
        if (self._section.in_progress or self._section.loop_active
                or getattr(self, "_calibration_task", None) is not None):
            raise CaptureAlreadyRunningError("Manual capture or calibration owns the camera")
        await self.prepare_main_capture()

    async def prepare_main_capture(self) -> None:
        await self._recover_failed_exposure()
        await self.drain_wide_preview()
        if self._auxiliary_camera_busy():
            raise CaptureAlreadyRunningError("Autofocus or polar alignment owns the camera")

    async def prepare_auxiliary_capture(self) -> None:
        await self._recover_failed_exposure()
        if self._section.in_progress:
            raise LiveStackError("Main capture is active; auxiliary acquisition cannot start")
        await self.drain_wide_preview()
        if self._section.in_progress:
            raise LiveStackError("Main capture started while waiting for Wide preview")

    async def _capture_wide_rtsp_frame(self) -> None:
        if self._settings.capture.wide_companion_enabled or self._wide_preview_busy():
            # Native camera/1 shares capture resources with camera/0.
            # Preserve the last preview; science acquisition has priority.
            return
        async with self._wide_capture_lock:
            if not self._settings.capture.wide_companion_enabled and not self._wide_preview_busy():
                if getattr(self, "_wide_cleanup_error", None) is not None:
                    raise LiveStackError(self._wide_cleanup_error)
                async with asyncio.timeout(WIDE_STREAM_MAX_EXPOSURE_S + 8):
                    await self._capture_wide_rtsp_frame_locked()

    async def _capture_wide_rtsp_frame_locked(self) -> None:
        assert self._wide_camera is not None
        await _ensure_connected(self._wide_camera)
        # Local ownership is insufficient after reconnect or another client.
        main = getattr(self, "_camera", None)
        if ((main is not None and int(cast(int | str, await main.get("camerastate"))) != 0)
                or int(cast(int | str, await self._wide_camera.get("camerastate"))) != 0):
            return
        if self._wide_rtsp_exposure_s is None:
            minimum_s = float(cast(str | float, await self._wide_camera.get("exposuremin")))
            device_maximum_s = float(cast(str | float, await self._wide_camera.get("exposuremax")))
            maximum_s = min(device_maximum_s, WIDE_STREAM_MAX_EXPOSURE_S)
            if (
                not math.isfinite(minimum_s)
                or not math.isfinite(maximum_s)
                or minimum_s <= 0
                or maximum_s < minimum_s
            ):
                raise LiveStackError(
                    "wide camera reported an invalid automatic-exposure range: "
                    f"{minimum_s:g}-{device_maximum_s:g} seconds"
                )
            await self._wide_camera.put("gain", Gain=200)
            self._wide_rtsp_min_exposure_s = minimum_s
            self._wide_rtsp_max_exposure_s = maximum_s
            self._wide_rtsp_exposure_s = minimum_s
        exposure_s = self._wide_rtsp_exposure_s
        assert self._wide_rtsp_min_exposure_s is not None
        assert self._wide_rtsp_max_exposure_s is not None
        if self._wide_preview_busy():
            return
        completed = False
        start_requested = False
        try:
            start_requested = True
            try:
                await self._wide_camera.start_exposure(exposure_s, light=True)
            except AlpacaRemoteError:
                # The device explicitly rejected this request. AbortExposure
                # would touch somebody else's capture, not one we started.
                start_requested = False
                raise
            poll_count = math.ceil((exposure_s + 5.0) / 0.1)
            for _ in range(poll_count):
                if await self._wide_camera.image_ready():
                    completed = True
                    frame = await self._wide_camera.image_array()
                    self._wide_rtsp_pixels = frame.array.T.copy()
                    self._wide_rtsp_last_exposure_s = exposure_s
                    self._wide_rtsp_exposure_s = next_wide_stream_exposure_s(
                        frame.array,
                        exposure_s,
                        minimum_exposure_s=self._wide_rtsp_min_exposure_s,
                        maximum_exposure_s=self._wide_rtsp_max_exposure_s,
                    )
                    offset_x = await self._wide_camera.get("bayeroffsetx")
                    offset_y = await self._wide_camera.get("bayeroffsety")
                    self._wide_rtsp_bayer_pattern = bayer_pattern(
                        int(cast(str | int, offset_x)), int(cast(str | int, offset_y))
                    )
                    with contextlib.suppress(Exception):
                        self._wide_rtsp_temperature_c = float(
                            cast(str | float, await self._wide_camera.get("ccdtemperature"))
                        )
                    self._wide_rtsp_version += 1
                    return
                await self._sleep(0.1)
            raise LiveStackError(
                f"wide camera did not produce a frame within {exposure_s + 5.0:g} seconds"
            )
        finally:
            if start_requested and not completed:
                cleanup = asyncio.create_task(self._stop_wide_exposure())
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                if cancelled:
                    raise asyncio.CancelledError

    async def _stop_wide_exposure(self) -> None:
        assert self._wide_camera is not None
        try:
            async with asyncio.timeout(5):
                await self._wide_camera.put("abortexposure")
                while int(cast(int | str, await self._wide_camera.get("camerastate"))) != 0:
                    await self._sleep(0.1)
        except Exception as exc:
            self._wide_cleanup_error = f"Wide exposure cleanup failed; acquisition blocked: {exc}"
            self._publish(last_error=self._wide_cleanup_error)
            _log.exception("%s", self._wide_cleanup_error)
            raise LiveStackError(self._wide_cleanup_error) from exc

    async def _poll_wide_rtsp(self) -> None:
        failures = 0
        while self._rtsp.status.active:
            if self._settings.capture.wide_companion_enabled:
                return
            config = self._rtsp.status.config
            mount = self._state.snapshot.mount
            stack_available = (
                self._live_stack_active
                and self._live_stack is not None
                and self._live_stack.pixels is not None
            )
            if mount is None or mount.at_park is not False:
                # A manually-started YouTube relay may keep RTSP alive while
                # parked. Repeat the last cached wide frame; do not keep taking
                # exposures from a camera whose arm is now parked.
                return
            if config is None or not (
                (config.wide_camera_until_stacked and not stack_available)
                or (self._settings.streaming.wide_camera_picture_in_picture and stack_available)
            ):
                return
            try:
                await self._capture_wide_rtsp_frame()
            except Exception as exc:
                # A transient timeout must not kill optional PiP until some
                # unrelated stream reconciliation happens. Preserve main
                # acquisition priority and recheck parked/disabled state after
                # each bounded retry delay.
                _log.warning("wide-camera stream frame unavailable: %s: %s",
                             type(exc).__name__, exc)
                self._wide_rtsp_pixels = None
                if (getattr(self, "_wide_cleanup_error", None) is not None
                        or (isinstance(exc, AlpacaRemoteError) and exc.is_not_implemented)):
                    return
                failures += 1
                await self._sleep((5, 15, 30)[min(failures - 1, 2)])
                continue
            failures = 0
            await self._sleep(1.0)

    def _live_stack_rtsp_frame(self) -> RtspFrame | None:
        if (
            not self._live_stack_active
            or self._live_stack is None
            or self._live_stack.pixels is None
        ):
            if self._settings.capture.wide_companion_enabled or self._wide_rtsp_pixels is None:
                return None
            now = self._stream_local_time()
            temperature = (
                "—"
                if self._wide_rtsp_temperature_c is None
                else f"{self._wide_rtsp_temperature_c:.1f}°C"
            )
            exposure = (
                "—"
                if self._wide_rtsp_last_exposure_s is None
                else f"{self._wide_rtsp_last_exposure_s:g}s"
            )
            return RtspFrame(
                pixels=self._wide_rtsp_pixels,
                bayer_pattern=self._wide_rtsp_bayer_pattern,
                overlay_lines=(
                    "Seestar S30 Wide Camera | waiting for first accepted live-stack frame",
                    "Wide preview paused during camera acquisition" if self._wide_preview_busy()
                    else f"auto exposure {exposure} | sensor {temperature}",
                    now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                ),
                version=f"wide:{self._wide_rtsp_version}",
                image_version=f"wide:{self._wide_rtsp_version}",
                header_text="Seestar S30 Wide Camera",
            )
        run = self._live_stack.run
        assert run is not None
        accepted = len(run.frame_paths)
        received = accepted + self._live_stack_rejected_count
        exposure = "—" if run.exposure_s is None else f"{run.exposure_s:g}s"
        gain = "—" if run.gain is None else str(run.gain)
        temperature = "—" if run.ccd_temp_c is None else f"{run.ccd_temp_c:.1f}°C"
        integration = self._format_live_integration(
            None if run.exposure_s is None else run.exposure_s * accepted
        )
        calibration = " / ".join(
            (
                f"dark {'yes' if run.dark_master else 'no'}",
                f"flat {'yes' if run.flat_master else 'no'}",
                f"bias {'yes' if run.bias_master else 'no'}",
            )
        )
        error = "OK" if self._live_stack_error is None else self._live_stack_error[:80]
        now = self._now()
        local_now = self._stream_local_time(now)
        lines = (
            f"LIVE  {run.object_name or 'Unknown target'} | {run.filter_name or 'No filter'} | integration {integration}",
            f"accepted {accepted} / received {received} / rejected {self._live_stack_rejected_count} | exposure {exposure} | gain {gain} | sensor {temperature}",
            f"HFR {'—' if run.hfr_px is None else f'{run.hfr_px:.2f}px'} | FWHM {'—' if run.fwhm_px is None else f'{run.fwhm_px:.2f}px'} | stars {'—' if run.star_count is None else run.star_count} | SNR est. {'—' if run.snr_estimate is None else f'{run.snr_estimate:.1f}'} | {calibration}",
            f"queue {self._live_stack_pending_count} | {error} | {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        )
        header = self.live_stack_target_header_text() or "Unknown target"
        config = self._rtsp.status.config
        interval = 5.0 if config is None else config.update_interval_s
        bucket = int(now.timestamp() / interval)
        version = (
            f"{run.sequence}:{accepted}:{self._live_stack_rejected_count}:"
            f"{self._live_stack_pending_count}:{self._live_stack_error}:{bucket}"
        )
        pixels = self._live_stack.presentation_pixels(
            auto_crop=False if config is None else config.auto_crop
        )
        assert pixels is not None
        return RtspFrame(
            pixels=pixels,
            bayer_pattern=self._live_stack.bayer_pattern,
            overlay_lines=lines,
            version=(
                f"{version}:wide:{self._wide_rtsp_version}"
                if (self._settings.streaming.wide_camera_picture_in_picture
                    and not self._settings.capture.wide_companion_enabled)
                else version
            ),
            # Overlay clocks and queue notices change independently of pixels.
            # Dither boundaries can republish integration without accepting a
            # new light, so they belong to the content key as well.
            image_version=(
                f"stack:{id(self._live_stack)}:{run.output_path}:{run.sequence}:"
                f"{accepted}:{len(run.dither_group_boundaries)}:"
                f"{False if config is None else config.auto_crop}"
            ),
            picture_in_picture_version=f"wide:{self._wide_rtsp_version}",
            header_text=header,
            picture_in_picture_pixels=(
                self._wide_rtsp_pixels
                if (self._settings.streaming.wide_camera_picture_in_picture
                    and not self._settings.capture.wide_companion_enabled)
                else None
            ),
            picture_in_picture_bayer_pattern=self._wide_rtsp_bayer_pattern,
        )

    def _stream_local_time(self, moment: datetime | None = None) -> datetime:
        current = self._now() if moment is None else moment
        location = self._settings.active_location()
        if location is not None and location.timezone is not None:
            with contextlib.suppress(ValueError, ZoneInfoNotFoundError):
                return current.astimezone(ZoneInfo(location.timezone))
        return current.astimezone(UTC)

    @property
    def live_stack_rtsp_source(self) -> Literal["wide_camera", "live_stack"] | None:
        if not self._rtsp.status.active:
            return None
        if (
            self._live_stack_active
            and self._live_stack is not None
            and self._live_stack.pixels is not None
        ):
            return "live_stack"
        return "wide_camera"

    async def live_stack_rtsp_preview_png(self) -> bytes:
        return await asyncio.to_thread(self._rtsp.preview_png)

    @staticmethod
    def _format_live_integration(seconds: float | None) -> str:
        if seconds is None:
            return "—"
        if seconds < 60:
            return f"{seconds:g}s"
        minutes = int(seconds // 60)
        if minutes < 60:
            return f"{minutes}m {seconds % 60:.0f}s"
        return f"{minutes // 60}h {minutes % 60}m"

    @staticmethod
    def _light_files(folder: Path, *, recursive: bool = True) -> list[Path]:
        candidates = folder.rglob("*") if recursive else folder.iterdir()
        return [
            path.resolve()
            for path in candidates
            if path.is_file()
            and path.parent.name.lower() == "lights"
            and path.suffix.lower() in {".fit", ".fits"}
            and not path.name.startswith("live_")
        ]

    @staticmethod
    def _watched_fits(folder: Path, *, recursive: bool) -> list[Path]:
        candidates = folder.rglob("*") if recursive else folder.iterdir()
        paths = [
            path.resolve()
            for path in candidates
            if path.is_file()
            and path.suffix.lower() in {".fit", ".fits"}
            and not path.name.startswith(("live_", "master_", "."))
        ]
        return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name.casefold()))

    async def list_sessions(self) -> list[Session]:
        return derive_sessions(await self._repo.list_complete())

    async def list_archive_entries(
        self, *, limit: int
    ) -> tuple[list[ArchiveEntry], list[str], bool]:
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        records = await self._repo.list_complete()
        entries = await asyncio.to_thread(scan_archive, folder, records, limit=limit + 1)
        directories = await asyncio.to_thread(scan_archive_directories, folder)
        return entries[:limit], directories, len(entries) > limit

    def _require_archive_delete_idle(self) -> None:
        if self._section.in_progress:
            raise ArchiveDeleteBusyError(
                "archive files cannot be deleted while capture or calibration is running"
            )
        if self._live_stack_active:
            raise ArchiveDeleteBusyError(
                "archive files cannot be deleted while live stacking is running"
            )

    async def delete_archive_files(self, keys: list[str]) -> ArchiveDeleteResult:
        """Delete an exact validated file set and its SSC catalogue rows."""
        self._require_archive_delete_idle()
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        paths = [resolve_archive_key(folder, key) for key in keys]
        records = await self._repo.list_complete()
        records_by_path = {record.path.resolve(): record for record in records}
        bytes_deleted = 0
        deleted_ids: set[int] = set()
        for path in paths:
            stat = await asyncio.to_thread(path.stat)
            await asyncio.to_thread(path.unlink)
            bytes_deleted += stat.st_size
            record = records_by_path.get(path.resolve())
            if record is not None:
                await self._repo.delete_complete_frame(record.id)
                deleted_ids.add(record.id)
        self._drop_archive_cache(paths)
        self._clear_deleted_last_frame(deleted_ids)
        return ArchiveDeleteResult(
            files_deleted=len(paths),
            catalogue_rows_deleted=len(deleted_ids),
            bytes_deleted=bytes_deleted,
        )

    async def delete_archive_folder(self, directory: str) -> ArchiveDeleteResult:
        """Delete one validated non-root folder and all of its contents."""
        self._require_archive_delete_idle()
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        path = resolve_archive_directory(folder, directory)
        records = await self._repo.list_complete()
        contained = [record for record in records if record.path.resolve().is_relative_to(path)]
        files_deleted, bytes_deleted = await asyncio.to_thread(remove_archive_directory, path)
        deleted_ids: set[int] = set()
        for record in contained:
            await self._repo.delete_complete_frame(record.id)
            deleted_ids.add(record.id)
        self._drop_archive_cache([path])
        self._clear_deleted_last_frame(deleted_ids)
        return ArchiveDeleteResult(
            files_deleted=files_deleted,
            catalogue_rows_deleted=len(deleted_ids),
            bytes_deleted=bytes_deleted,
            folder_deleted=directory,
        )

    async def transfer_archive_files(
        self,
        keys: list[str],
        directories: list[str],
        destination: str,
        mode: Literal["move", "copy"],
    ) -> ArchiveTransferResult:
        """Move or copy validated files and folders into an existing folder."""
        self._require_archive_delete_idle()
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        sources = [resolve_archive_key(folder, key) for key in keys]
        directory_sources = [resolve_archive_directory(folder, key) for key in directories]
        for directory_source in directory_sources:
            nested = [
                source
                for source in [*sources, *directory_sources]
                if source != directory_source and source.is_relative_to(directory_source)
            ]
            if nested:
                raise ArchivePathError("items inside a selected folder must not also be selected")
        destination_path = resolve_archive_destination(folder, destination)
        all_sources = [*sources, *directory_sources]
        targets = [destination_path / source.name for source in all_sources]
        if len({target.resolve() for target in targets}) != len(targets):
            raise ArchivePathError("selected items do not have unique destination names")
        for source, target in zip(all_sources, targets, strict=True):
            if source.resolve() == target.resolve():
                raise ArchivePathError("an item is already in the destination folder")
            if source.is_dir() and destination_path.resolve().is_relative_to(source.resolve()):
                raise ArchivePathError("a folder cannot be transferred into itself")
            if target.exists():
                raise ArchivePathError(f"destination item already exists: {target.name}")

        records = await self._repo.list_complete()
        records_by_path = {record.path.resolve(): record for record in records}
        stats = await asyncio.gather(*(asyncio.to_thread(path.stat) for path in sources))
        directory_stats = await asyncio.gather(
            *(asyncio.to_thread(archive_directory_stats, path) for path in directory_sources)
        )
        files_transferred = len(sources) + sum(item[0] for item in directory_stats)
        bytes_transferred = sum(stat.st_size for stat in stats) + sum(
            item[1] for item in directory_stats
        )
        updated = 0
        for source, target in zip(sources, targets[: len(sources)], strict=True):
            if mode == "copy":
                await asyncio.to_thread(shutil.copy2, source, target)
                continue
            await asyncio.to_thread(shutil.move, source, target)
            record = records_by_path.get(source.resolve())
            if record is not None:
                await self._repo.move_complete_frame(record.id, target)
                updated += 1
        directory_targets = targets[len(sources) :]
        for source, target in zip(directory_sources, directory_targets, strict=True):
            contained = [
                record for record in records if record.path.resolve().is_relative_to(source)
            ]
            if mode == "copy":
                await asyncio.to_thread(shutil.copytree, source, target, copy_function=shutil.copy2)
                continue
            await asyncio.to_thread(shutil.move, source, target)
            for record in contained:
                relative = record.path.resolve().relative_to(source)
                await self._repo.move_complete_frame(record.id, target / relative)
                updated += 1
        self._drop_archive_cache(all_sources)
        return ArchiveTransferResult(
            files_transferred=files_transferred,
            directories_transferred=len(directory_sources),
            bytes_transferred=bytes_transferred,
            catalogue_rows_updated=updated,
            destination=destination,
            mode=mode,
        )

    async def create_archive_zip(self, directory: str) -> tuple[Path, str]:
        """Create a temporary ZIP for one validated archive folder tree."""
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        source = resolve_archive_destination(folder, directory)
        with tempfile.NamedTemporaryFile(
            prefix="ssc-archive-", suffix=".zip", delete=False
        ) as handle:
            output = Path(handle.name)
        try:
            await asyncio.to_thread(write_archive_zip, source, output)
        except BaseException:
            await asyncio.to_thread(output.unlink, missing_ok=True)
            raise
        return output, f"{source.name}.zip"

    async def create_archive_selection_zip(
        self, keys: list[str], directories: list[str]
    ) -> tuple[Path, str]:
        """Create a temporary ZIP containing exactly the selected archive items."""
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        root = folder.expanduser().resolve()
        files = [resolve_archive_key(root, key) for key in keys]
        directory_paths = [resolve_archive_directory(root, key) for key in directories]
        with tempfile.NamedTemporaryFile(
            prefix="ssc-archive-selection-", suffix=".zip", delete=False
        ) as handle:
            output = Path(handle.name)
        try:
            await asyncio.to_thread(
                write_archive_selection_zip, root, files, directory_paths, output
            )
        except BaseException:
            await asyncio.to_thread(output.unlink, missing_ok=True)
            raise
        return output, f"{root.name}-selection.zip"

    async def get_archive_header(self, key: str) -> dict[str, object]:
        """Read the primary FITS header without loading its pixel payload."""
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        path = resolve_archive_key(folder, key)
        ensure_preview_safe(path)

        def read_header() -> dict[str, object]:
            with path.open("rb") as handle:
                raw = handle.read(1024 * 1024)
            return parse_header(raw)[0]

        return await asyncio.to_thread(read_header)

    def _drop_archive_cache(self, paths: list[Path]) -> None:
        roots = [str(path) for path in paths]
        self._archive_render_cache = {
            key: value
            for key, value in self._archive_render_cache.items()
            if not any(key[0] == root or key[0].startswith(f"{root}{os.sep}") for root in roots)
        }

    def _clear_deleted_last_frame(self, deleted_ids: set[int]) -> None:
        if self._section.last_frame_id in deleted_ids:
            self._publish(last_frame_id=None, last_captured_at=None)

    async def get_archive_image(self, key: str, *, thumbnail: bool) -> tuple[bytes, str]:
        """Render or return one file after resolving it inside the capture root."""
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        path = resolve_archive_key(folder, key)
        stat = ensure_preview_safe(path)
        suffix = path.suffix.casefold()
        if suffix not in FITS_SUFFIXES:
            media_type = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".tif": "image/tiff",
                ".tiff": "image/tiff",
            }[suffix]
            return await asyncio.to_thread(path.read_bytes), media_type

        cache_key = (str(path), stat.st_mtime_ns, stat.st_size, thumbnail)
        cached = self._archive_render_cache.get(cache_key)
        if cached is not None:
            return cached, "image/png"
        task = self._archive_render_tasks.get(cache_key)
        if task is None:
            async def render() -> bytes:
                raw = await asyncio.to_thread(path.read_bytes)
                return await asyncio.to_thread(_render_frame_png, raw, thumbnail=thumbnail)

            task = asyncio.create_task(render(), name="archive-render")
            self._archive_render_tasks[cache_key] = task

            def finished(completed: asyncio.Task[bytes]) -> None:
                self._archive_render_tasks.pop(cache_key, None)
                # Retrieve errors even when every HTTP waiter disconnected.
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finished)
        rendered = await asyncio.shield(task)
        if thumbnail:
            if len(self._archive_render_cache) >= 128:
                self._archive_render_cache.pop(next(iter(self._archive_render_cache)))
            self._archive_render_cache[cache_key] = rendered
        return rendered, "image/png"

    async def get_archive_quality(self, key: str) -> FrameQuality:
        folder = self._settings.capture.folder
        if folder is None:
            raise CaptureFolderNotConfiguredError("no capture folder is configured")
        path = resolve_archive_key(folder, key)
        ensure_preview_safe(path)
        if path.suffix.casefold() not in FITS_SUFFIXES:
            raise ArchivePathError("star quality is available for FITS images only")
        try:
            return await self._quality_cache.get(path)
        except (FileNotFoundError, CaptureFrameNotReadyError) as exc:
            raise ArchivePathError(str(exc)) from exc

    async def get_frame_image(self, frame_id: int, *, thumbnail: bool) -> bytes:
        """A rendered PNG of a catalogued frame's own FITS bytes — a full
        preview, or a downsampled thumbnail of the same rendering.

        Debayers whenever the FITS header carries ``BAYERPAT``, regardless of
        ``frame_type`` — purely a display concern, decided by header presence
        alone (a mono frame has no ``BAYERPAT`` and renders grayscale; CLAUDE.md
        invariant 7 forbids guessing a mosaic pattern that isn't there).
        :func:`ssc.capture.debayer` reconstructs display-preview RGB, then each
        channel is stretched independently and stacked — the same pattern
        ``ssc.skymap.cutouts._render_display`` uses for a 3-plane cube.

        Raises :class:`ssc.store.capture.CaptureFrameNotFoundError` for an
        unknown id, :class:`CaptureFrameNotReadyError` for a frame whose row
        is still ``'writing'``.

        The decode/debayer/stretch/encode pipeline below is pure CPU work —
        for a full-resolution frame it is tens of milliseconds, and the
        timeline can request a screenful of thumbnails at once. Run on the
        event loop directly, that serialises behind every other request this
        process is handling, `StartExposure` included: a capture "starting"
        for many seconds was traced to exactly this, the thumbnail strip
        painting in while a `POST /api/capture/exposure` sat queued behind
        it. The whole render — not just the file read — goes through
        `asyncio.to_thread` so it runs off the loop.
        """
        record = await self._repo.get_frame(frame_id)
        if record.status != "complete":
            raise CaptureFrameNotReadyError(
                f"capture frame {frame_id} is {record.status!r}, not yet complete"
            )
        raw = await self._read_complete_frame_bytes(record)
        return await asyncio.to_thread(_render_frame_png, raw, thumbnail=thumbnail)

    async def get_frame_quality(self, frame_id: int) -> FrameQuality:
        """The numpy-only HFR/FWHM star metric (``ssc.capture.quality``,
        ``ssc-bkv.8``) for a catalogued frame's own FITS bytes.

        Same not-ready/not-found handling as :meth:`get_frame_image`:
        :class:`ssc.store.capture.CaptureFrameNotFoundError` for an unknown
        id, :class:`CaptureFrameNotReadyError` for a frame whose row is still
        ``'writing'``. The whole decode/debayer/detect pipeline runs off the
        event loop in a dedicated bounded worker, with persistent raw-FITS
        metrics reused only while file identity and algorithm version match.
        """
        record = await self._repo.get_frame(frame_id)
        if record.status != "complete":
            raise CaptureFrameNotReadyError(
                f"capture frame {frame_id} is {record.status!r}, not yet complete"
            )
        try:
            return await self._quality_cache.get(record.path)
        except FileNotFoundError as exc:
            raise CaptureFrameNotFoundError(f"capture frame {frame_id} file is missing: {record.path}") from exc

    async def _read_complete_frame_bytes(self, record: CaptureFrameRecord) -> bytes:
        try:
            return await asyncio.to_thread(record.path.read_bytes)
        except FileNotFoundError as exc:
            raise CaptureFrameNotFoundError(
                f"capture frame {record.id} file is missing: {record.path}"
            ) from exc

    async def solve_frame(self, frame_id: int) -> PlateSolveResult:
        """Plate-solve one complete catalogued FITS without taking another exposure."""
        record = await self._repo.get_frame(frame_id)
        if record.status != "complete":
            raise CaptureFrameNotReadyError(
                f"capture frame {frame_id} is {record.status!r}, not yet complete"
            )
        availability = await self._availability()
        if not availability.available:
            raise CaptureSolverUnavailableError(availability)
        try:
            return await self._solve(record.path)
        except SolverUnavailableError as exc:
            raise CaptureSolverUnavailableError(
                SolverAvailability(available=False, issue=exc.issue, reason=str(exc))
            ) from exc


__all__ = [
    "ArchiveDeleteBusyError",
    "CaptureAlreadyRunningError",
    "CaptureDeviceUnavailableError",
    "CaptureDitherSettleError",
    "CaptureDurationOutOfRangeError",
    "CaptureFolderNotConfiguredError",
    "CaptureFrameNotReadyError",
    "CaptureService",
    "CaptureSolverUnavailableError",
    "CaptureTimezoneUnknownError",
    "GainSettableCamera",
    "initial_capture_state",
]
