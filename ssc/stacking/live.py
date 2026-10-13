# SPDX-License-Identifier: GPL-3.0-only

"""Incremental, RAM-first live stacks built from durable captured FITS."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import numpy as np

from ssc.capture.fits import debayer, write_fits_file
from ssc.capture.frame import FrameMetadata, FrameType
from ssc.capture.motion import frame_motion_reason
from ssc.capture.quality import FrameQuality, measure_frame_quality
from ssc.skymap.fits import read_fits_image
from ssc.stacking.integration import IncrementalTrimmedMean
from ssc.stacking.registration import (
    MAX_RIGID_RMS_PX,
    MIN_RIGID_MATCH_FRACTION,
    MIN_RIGID_MATCHED_STARS,
    MIN_UNAMBIGUOUS_PEAK_RATIO,
    FrameObservation,
    register_translation,
)

_BAYER_OFFSETS = {"RGGB": (0, 0), "GRBG": (1, 0), "GBRG": (0, 1), "BGGR": (1, 1)}
_BAYER_TILES = {
    "RGGB": (("R", "G"), ("G", "B")),
    "GRBG": (("G", "R"), ("B", "G")),
    "GBRG": (("G", "B"), ("R", "G")),
    "BGGR": (("B", "G"), ("G", "R")),
}

_CLOUD_BASELINE_MIN_FRAMES = 3
_CLOUD_SOURCE_RATIO = 0.35
_CLOUD_CONTRAST_RATIO = 0.50
_QUALITY_HISTORY_LIMIT = 24


@dataclass(frozen=True, slots=True)
class LiveStackRun:
    sequence: int
    output_path: Path
    parameters_path: Path
    frame_paths: tuple[Path, ...]
    dark_master: Path | None
    bias_master: Path | None
    flat_master: Path | None
    binning: Literal[1, 2]
    integration_grouping: Literal["per_frame", "dither_boundary"] = "per_frame"
    dither_group_boundaries: tuple[tuple[str, int], ...] = ()
    last_shift_y_px: float | None = None
    last_shift_x_px: float | None = None
    registration_peak: float | None = None
    registration_peak_ratio: float | None = None
    last_rotation_deg: float | None = None
    object_name: str | None = None
    filter_name: str | None = None
    exposure_s: float | None = None
    gain: int | None = None
    ccd_temp_c: float | None = None
    hfr_px: float | None = None
    fwhm_px: float | None = None
    star_count: int | None = None
    detected_source_count: int | None = None
    frame_contrast_snr: float | None = None
    snr_estimate: float | None = None


class LiveStackError(ValueError):
    """The selected live-stack inputs cannot be combined safely."""


class LiveStackFrameRejectedError(LiveStackError):
    """A valid light frame was retained but excluded from integration."""


class LiveStackEngine:
    """Integrate each light once and persist only when explicitly saved."""

    def __init__(self, folder: Path) -> None:
        # ``add_frame`` runs in a worker thread while scheduled panel changes
        # can reset the engine from the event-loop thread.  Keep the complete
        # engine transition atomic: reset must never clear the integration
        # halfway through an add or save.
        self._lock = threading.RLock()
        self._folder = folder
        self._sequence = self._latest_sequence(folder)
        self._run: LiveStackRun | None = None
        self._working_parameters_path: Path | None = None
        self._registration_reference: np.ndarray | None = None
        self._registration_shift = (0.0, 0.0)
        self._integration: IncrementalTrimmedMean | None = None
        self._bayer_pattern: str | None = None
        self._source_bayer_pattern: str | None = None
        self._master_temperatures: dict[Path, float | None] = {}
        self._master_cache: dict[tuple[Path, tuple[int, int]], np.ndarray] = {}
        self._flat_cache: dict[tuple[Path, tuple[int, int], str | None], np.ndarray] = {}
        self._registrations: list[dict[str, object]] = []
        self._dither_header_groups = False
        self._first_header: dict[str, object] | None = None
        self._last_header: dict[str, object] | None = None
        self._total_exposure_s: float | None = 0.0
        self._uniform_exposure_s: float | None = None
        self._mixed_exposures = False
        self._quality_history: list[FrameQuality] = []
        self.latest_frame_quality: tuple[Path, FrameQuality] | None = None

    @staticmethod
    def _latest_sequence(folder: Path) -> int:
        latest = 0
        for path in (*folder.glob("live_*.fits"), *folder.glob("live_*.json")):
            match = re.fullmatch(r"live_(\d+)(?:_.+)?", path.stem)
            if match is not None:
                latest = max(latest, int(match.group(1)))
        return latest

    @property
    def run(self) -> LiveStackRun | None:
        return self._run

    @property
    def pixels(self) -> np.ndarray | None:
        """Published stack snapshot, replaced atomically after each frame."""
        return None if self._integration is None else self._integration.pixels

    def presentation_pixels(self, *, auto_crop: bool = False) -> np.ndarray | None:
        """Return a preview view without modifying the full integration."""
        if self._integration is None:
            return None
        pixels = self._integration.pixels
        if not auto_crop or self._integration.frame_count < 2:
            return pixels
        coverage = self._integration.coverage
        if coverage.ndim > 2:
            coverage = np.min(coverage, axis=tuple(range(2, coverage.ndim)))
        complete = coverage == self._integration.frame_count
        rows = np.flatnonzero(np.any(complete, axis=1))
        columns = np.flatnonzero(np.any(complete, axis=0))
        if rows.size == 0 or columns.size == 0:
            return pixels
        top, bottom = int(rows[0]), int(rows[-1]) + 1
        left, right = int(columns[0]), int(columns[-1]) + 1
        if self._bayer_pattern is not None:
            # Cropping an odd Bayer row/column changes the CFA phase. Trim at
            # most one additional pixel so the original pattern stays true.
            top += top % 2
            left += left % 2
            bottom -= (bottom - top) % 2
            right -= (right - left) % 2
        crop_height = bottom - top
        crop_width = right - left
        height, width = pixels.shape[:2]
        # A long dither random walk can leave only a sliver covered by every
        # single frame. That is not a useful automatic crop; retain the full
        # stack instead of rendering an apparently broken vertical strip.
        if (
            crop_height < 16
            or crop_width < 16
            or crop_height < height // 2
            or crop_width < width // 2
        ):
            return pixels
        return pixels[top:bottom, left:right]

    @property
    def bayer_pattern(self) -> str | None:
        return self._bayer_pattern

    @property
    def memory_bytes(self) -> int:
        """Long-lived numeric working memory, excluding cached masters."""
        reference = (
            0 if self._registration_reference is None else self._registration_reference.nbytes
        )
        integration = 0 if self._integration is None else self._integration.memory_bytes
        return reference + integration

    def reset(
        self,
        *,
        dark_master: Path | None = None,
        bias_master: Path | None = None,
        flat_master: Path | None = None,
        binning: Literal[1, 2] = 1,
        group_by_dither: bool = False,
    ) -> LiveStackRun:
        with self._lock:
            return self._reset(
                dark_master=dark_master,
                bias_master=bias_master,
                flat_master=flat_master,
                binning=binning,
                group_by_dither=group_by_dither,
            )

    def _reset(
        self,
        *,
        dark_master: Path | None = None,
        bias_master: Path | None = None,
        flat_master: Path | None = None,
        binning: Literal[1, 2] = 1,
        group_by_dither: bool = False,
    ) -> LiveStackRun:
        if binning not in {1, 2}:
            raise LiveStackError("live-stack binning must be 1 or 2")
        self._sequence += 1
        prefix = f"live_{self._sequence:03d}"
        self._run = LiveStackRun(
            self._sequence,
            self._folder / f"{prefix}.fits",
            self._folder / f"{prefix}.json",
            (),
            dark_master,
            bias_master,
            flat_master,
            binning,
            integration_grouping="dither_boundary" if group_by_dither else "per_frame",
        )
        self._working_parameters_path = self._run.parameters_path
        self._registration_reference = None
        self._registration_shift = (0.0, 0.0)
        self._integration = None
        self._bayer_pattern = None
        self._source_bayer_pattern = None
        self._master_cache.clear()
        self._master_temperatures.clear()
        self._flat_cache.clear()
        self._registrations.clear()
        self._dither_header_groups = False
        self._first_header = None
        self._last_header = None
        self._total_exposure_s = 0.0
        self._uniform_exposure_s = None
        self._mixed_exposures = False
        self._quality_history.clear()
        self.latest_frame_quality = None
        return self._run

    def begin_dither_group(self, boundary_id: str) -> LiveStackRun:
        """Record one successful dither before its first accepted science frame.

        Call reset(group_by_dither=True) before acquisition. The caller must
        serialize this with its frame queue at the successful physical boundary;
        use a stable journal/operation ID so a recovery replay cannot split a
        block twice. This method neither moves the mount nor changes raw frames.
        """
        with self._lock:
            if self._run is None or self._run.integration_grouping != "dither_boundary":
                raise LiveStackError("dither boundaries require a boundary-grouped live stack")
            if not boundary_id:
                raise LiveStackError("dither boundary ID must not be empty")
            if any(saved_id == boundary_id for saved_id, _ in self._run.dither_group_boundaries):
                return self._run
            if self._integration is not None:
                self._integration.begin_group()
            self._run = replace(
                self._run,
                dither_group_boundaries=(
                    *self._run.dither_group_boundaries,
                    (boundary_id, len(self._run.frame_paths)),
                ),
            )
            self._write_parameters()
            return self._run

    def prime_quality_baseline(self, frame_paths: tuple[Path, ...]) -> None:
        """Seed cloud detection from earlier lights without integrating them."""
        with self._lock:
            for frame_path in frame_paths:
                try:
                    candidate, header = self._calibrated(frame_path)
                    quality = self._measure_candidate_quality(candidate, header)
                except (OSError, ValueError, LiveStackError):
                    continue
                if quality.detected_source_count > 0 and quality.contrast_snr is not None:
                    self._quality_history.append(quality)
            self._quality_history = self._quality_history[-_QUALITY_HISTORY_LIMIT:]

    def set_calibration_masters(
        self,
        *,
        dark_master: Path | None,
        bias_master: Path | None,
        flat_master: Path | None,
    ) -> LiveStackRun:
        with self._lock:
            return self._set_calibration_masters(
                dark_master=dark_master,
                bias_master=bias_master,
                flat_master=flat_master,
            )

    def _set_calibration_masters(
        self,
        *,
        dark_master: Path | None,
        bias_master: Path | None,
        flat_master: Path | None,
    ) -> LiveStackRun:
        """Set masters after reset, but before the first light is integrated."""
        if self._run is None:
            raise LiveStackError("start or reset the live stack before selecting masters")
        if self._run.frame_paths or self._integration is not None:
            raise LiveStackError("calibration masters cannot change after stacking has started")
        self._run = replace(
            self._run,
            dark_master=dark_master,
            bias_master=bias_master,
            flat_master=flat_master,
        )
        self._master_cache.clear()
        self._master_temperatures.clear()
        self._flat_cache.clear()
        return self._run

    def add_frame(self, frame_path: Path, *, measure_quality: bool = False) -> LiveStackRun:
        with self._lock:
            return self._add_frame(frame_path, measure_quality=measure_quality)

    def _add_frame(self, frame_path: Path, *, measure_quality: bool = False) -> LiveStackRun:
        if self._run is None:
            self.reset()
        assert self._run is not None
        if frame_path in self._run.frame_paths:
            return self._run
        candidate, header = self._calibrated(frame_path)
        value = header.get("BAYERPAT")
        pattern = value.upper() if isinstance(value, str) else None
        cfa_state = header.get("CFASTATE")
        if cfa_state == "unknown" or (cfa_state == "bayer" and pattern is None):
            raise LiveStackError(
                "live-stack frame Bayer provenance is missing; raw frame retained, "
                "waiting for a frame with measured sensor metadata"
            )
        if cfa_state == "unsupported":
            raise LiveStackError("live-stack frame sensor family is unsupported; raw frame retained")
        if pattern is not None and pattern not in _BAYER_TILES:
            raise LiveStackError("live-stack frame has unsupported Bayer provenance")
        if self._registration_reference is not None and pattern != self._source_bayer_pattern:
            if pattern is None:
                raise LiveStackError(
                    "live-stack frame Bayer provenance is missing; raw frame retained, "
                    "existing stack unchanged"
                )
            raise LiveStackError("live-stack frames must use the same Bayer pattern")
        group_id = self._header_string(header, "DITHGRP")
        if group_id is not None:
            if self._run.integration_grouping == "per_frame":
                if self._run.frame_paths:
                    raise LiveStackError("cannot mix marked dither groups after unmarked frames")
                self._run = replace(self._run, integration_grouping="dither_boundary")
            if (self._run.dither_group_boundaries
                    and group_id != self._run.dither_group_boundaries[-1][0]
                    and any(saved == group_id for saved, _ in self._run.dither_group_boundaries)):
                raise LiveStackError("dither group frames arrived out of chronological order")
            self._dither_header_groups = True
            self.begin_dither_group(group_id)
        elif self._dither_header_groups:
            raise LiveStackError("dither-grouped frames require DITHGRP provenance")
        quality = self._measure_candidate_quality(candidate, header) if measure_quality else None
        if quality is not None:
            self.latest_frame_quality = (frame_path, quality)
            motion_plane = self._debayer_green(candidate, pattern) if isinstance(pattern, str) and pattern in _BAYER_TILES else candidate
            motion_reason = frame_motion_reason(motion_plane)
            del motion_plane
            if motion_reason is not None:
                raise LiveStackFrameRejectedError(motion_reason)
        if self._run.binning == 2:
            candidate = self._bin_2x2(candidate, pattern)
        elif pattern in _BAYER_TILES:
            candidate = self._debayer_full_resolution(candidate, pattern)
        if quality is not None:
            rejection = self._cloud_rejection_reason(quality)
            if rejection is not None:
                raise LiveStackFrameRejectedError(rejection)
        if self._registration_reference is None:
            self._registration_reference = candidate.copy()
            self._integration = IncrementalTrimmedMean(
                candidate.shape,
                group_size=None if self._run.integration_grouping == "dither_boundary" else 1,
            )
            self._source_bayer_pattern = pattern
            self._bayer_pattern = None
            aligned, valid = candidate, np.isfinite(candidate)
            shift_y = shift_x = 0.0
            peak = ratio = None
            rotation_deg = 0.0
            matched_star_count = None
            star_match_fraction = None
            star_rms_px = None
        else:
            if candidate.shape != self._registration_reference.shape:
                raise LiveStackError("live-stack frames must have identical dimensions")
            try:
                assert self._registration_reference is not None
                reference_plane = self._registration_plane(self._registration_reference)
                candidate_plane = self._registration_plane(candidate)
                registration = register_translation(
                    FrameObservation(reference_plane, None, None),
                    FrameObservation(candidate_plane, None, None),
                )
            except ValueError as exc:
                raise LiveStackError(f"could not register live-stack frame: {exc}") from exc
            if registration.matched_star_count is not None and (
                registration.matched_star_count < MIN_RIGID_MATCHED_STARS
                or (registration.star_match_fraction or 0.0) < MIN_RIGID_MATCH_FRACTION
                or (registration.star_rms_px or float("inf")) > MAX_RIGID_RMS_PX
            ):
                raise LiveStackFrameRejectedError(
                    "could not register live-stack star geometry "
                    f"({registration.matched_star_count} matches, "
                    f"{(registration.star_match_fraction or 0.0):.0%} of the smaller field, "
                    f"{(registration.star_rms_px or 0.0):.2f}px RMS; requires at least "
                    f"{MIN_RIGID_MATCHED_STARS} matches, {MIN_RIGID_MATCH_FRACTION:.0%}, "
                    f"and at most {MAX_RIGID_RMS_PX:.2f}px RMS)"
                )
            if (
                registration.matched_star_count is None
                and registration.peak_ratio < MIN_UNAMBIGUOUS_PEAK_RATIO
            ):
                raise LiveStackError(
                    "could not register live-stack frame unambiguously "
                    f"(peak ratio {registration.peak_ratio:.2f})"
                )
            del reference_plane, candidate_plane
            shift_y = registration.shift_y_px
            shift_x = registration.shift_x_px
            if registration.matched_star_count is None:
                applied_y, applied_x = round(shift_y), round(shift_x)
                aligned, valid = self._translate_without_wrap(candidate, applied_y, applied_x)
                shift_y = float(applied_y)
                shift_x = float(applied_x)
            else:
                aligned, valid = self._rigid_without_wrap(
                    candidate,
                    shift_y,
                    shift_x,
                    registration.rotation_deg,
                )
            peak = registration.correlation_peak
            ratio = registration.peak_ratio
            rotation_deg = registration.rotation_deg
            matched_star_count = registration.matched_star_count
            star_match_fraction = registration.star_match_fraction
            star_rms_px = registration.star_rms_px
        assert self._integration is not None
        self._integration.add(aligned, valid)
        del candidate, aligned, valid
        snr_estimate = self._estimate_field_snr(
            self._integration.pixels,
            self._integration.coverage == self._integration.frame_count,
        )
        self._registration_shift = (shift_y, shift_x)
        self._registrations.append(
            {
                "frame": str(frame_path),
                "shift_y_px": shift_y,
                "shift_x_px": shift_x,
                "correlation_peak": peak,
                "peak_ratio": ratio,
                "rotation_deg": rotation_deg,
                "matched_star_count": matched_star_count,
                "star_match_fraction": star_match_fraction,
                "star_rms_px": star_rms_px,
            }
        )
        if self._first_header is None:
            self._first_header = header.copy()
            self._uniform_exposure_s = self._header_float(header, "EXPTIME")
        elif self._uniform_exposure_s != self._header_float(header, "EXPTIME"):
            self._mixed_exposures = True
        self._last_header = header.copy()
        exposure_s = self._header_float(header, "EXPTIME")
        if exposure_s is None:
            self._total_exposure_s = None
        elif self._total_exposure_s is not None:
            self._total_exposure_s += exposure_s
        self._run = replace(
            self._run,
            frame_paths=(*self._run.frame_paths, frame_path),
            last_shift_y_px=shift_y,
            last_shift_x_px=shift_x,
            registration_peak=peak,
            registration_peak_ratio=ratio,
            last_rotation_deg=rotation_deg,
            object_name=self._header_string(header, "OBJECT"),
            filter_name=self._header_string(header, "FILTER"),
            exposure_s=self._header_float(header, "EXPTIME"),
            gain=self._header_int(header, "GAIN"),
            ccd_temp_c=self._header_float(header, "CCD-TEMP"),
            hfr_px=None if quality is None else quality.hfr_px,
            fwhm_px=None if quality is None else quality.fwhm_px,
            star_count=None if quality is None else quality.star_count,
            detected_source_count=(
                None if quality is None else quality.detected_source_count
            ),
            frame_contrast_snr=None if quality is None else quality.contrast_snr,
            snr_estimate=snr_estimate,
        )
        if quality is not None:
            self._quality_history.append(quality)
            self._quality_history = self._quality_history[-_QUALITY_HISTORY_LIMIT:]
        return self._run

    def _measure_candidate_quality(
        self, candidate: np.ndarray, header: dict[str, object]
    ) -> FrameQuality:
        value = header.get("BAYERPAT")
        pattern = value.upper() if isinstance(value, str) else None
        plane = (
            self._debayer_green(candidate, pattern)
            if pattern in _BAYER_TILES
            else candidate
        )
        return measure_frame_quality(
            plane,
            pixel_size_um=self._header_float(header, "XPIXSZ"),
            focal_length_mm=self._header_float(header, "FOCALLEN"),
        )

    def _cloud_rejection_reason(self, quality: FrameQuality) -> str | None:
        usable = [
            sample
            for sample in self._quality_history
            if sample.detected_source_count > 0 and sample.contrast_snr is not None
        ]
        if len(usable) < _CLOUD_BASELINE_MIN_FRAMES or quality.contrast_snr is None:
            return None
        source_baseline = float(
            np.percentile([sample.detected_source_count for sample in usable], 75)
        )
        contrast_baseline = float(
            np.percentile([sample.contrast_snr for sample in usable], 75)
        )
        if source_baseline < 40 or contrast_baseline <= 0:
            return None
        source_ratio = quality.detected_source_count / source_baseline
        contrast_ratio = quality.contrast_snr / contrast_baseline
        if source_ratio >= _CLOUD_SOURCE_RATIO or contrast_ratio >= _CLOUD_CONTRAST_RATIO:
            return None
        return (
            "cloud quality gate: detected sources "
            f"{quality.detected_source_count} vs {source_baseline:.0f} baseline "
            f"({source_ratio:.0%}); contrast SNR {quality.contrast_snr:.1f} vs "
            f"{contrast_baseline:.1f} ({contrast_ratio:.0%})"
        )

    @staticmethod
    def _estimate_field_snr(pixels: np.ndarray, complete_coverage: np.ndarray) -> float | None:
        """Estimate live-stack field contrast relative to background noise.

        A whole astronomical image has no single photometric SNR: every star
        and extended-object region has its own signal.  For a useful live
        progress indicator we report the robust 99.9th-percentile luminance
        contrast above the median sky, divided by the sky MAD sigma.  This is
        deliberately labelled an estimate by callers.  Only pixels covered
        by every registered light participate, so dither borders cannot make
        the value jump.  Uniform sampling caps the per-frame calculation while
        retaining pixels across the complete field.
        """
        plane = LiveStackEngine._registration_plane(pixels)
        coverage = complete_coverage
        if coverage.ndim > 2:
            coverage = np.all(coverage, axis=tuple(range(2, coverage.ndim)))
        finite = np.isfinite(plane) & coverage
        values = plane[finite]
        if values.size < 256:
            return None
        stride = max(1, (values.size + 262_143) // 262_144)
        sample = values[::stride].astype(np.float64, copy=False)
        background = float(np.median(sample))
        noise = 1.4826 * float(np.median(np.abs(sample - background)))
        if not np.isfinite(noise) or noise <= 0:
            return None
        signal = float(np.percentile(sample, 99.9)) - background
        if not np.isfinite(signal):
            return None
        return max(0.0, signal / noise)

    @staticmethod
    def _header_string(header: dict[str, object], key: str) -> str | None:
        value = header.get(key)
        return value if isinstance(value, str) else None

    @staticmethod
    def _header_float(header: dict[str, object], key: str) -> float | None:
        value = header.get(key)
        return float(value) if isinstance(value, int | float) else None

    @staticmethod
    def _header_int(header: dict[str, object], key: str) -> int | None:
        value = header.get(key)
        return int(value) if isinstance(value, int | float) else None

    @staticmethod
    def _header_bool(header: dict[str, object], key: str) -> bool | None:
        value = header.get(key)
        return bool(value) if isinstance(value, bool | int) else None

    @staticmethod
    def _header_datetime(header: dict[str, object], key: str) -> datetime | None:
        value = header.get(key)
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    @staticmethod
    def _registration_plane(pixels: np.ndarray) -> np.ndarray:
        return pixels if pixels.ndim == 2 else np.mean(pixels, axis=2, dtype=np.float32)

    @staticmethod
    def _bin_2x2(pixels: np.ndarray, bayer_pattern: str | None) -> np.ndarray:
        """One Bayer tile to one RGB pixel, or average a monochrome 2x2 block."""
        height = pixels.shape[0] // 2 * 2
        width = pixels.shape[1] // 2 * 2
        cropped = pixels[:height, :width]
        if bayer_pattern not in _BAYER_TILES:
            return cast(
                np.ndarray,
                (
                cropped[0::2, 0::2]
                + cropped[0::2, 1::2]
                + cropped[1::2, 0::2]
                + cropped[1::2, 1::2]
                )
                * np.float32(0.25),
            )
        tile = _BAYER_TILES[bayer_pattern]
        channels: list[np.ndarray] = []
        for channel in ("R", "G", "B"):
            planes = [
                cropped[row::2, column::2]
                for row in range(2)
                for column in range(2)
                if tile[row][column] == channel
            ]
            channels.append(sum(planes, np.zeros_like(planes[0])) / np.float32(len(planes)))
        return np.stack(channels, axis=2)

    @staticmethod
    def _debayer_full_resolution(pixels: np.ndarray, pattern: str) -> np.ndarray:
        """Keep the existing demosaic arithmetic, with bounded temporary storage.

        Two halo rows keep the Bayer phase even and supply the complete 3x3
        neighbourhood. Only physical image borders reach the edge padding.
        Conversion to float32 happens as each stripe enters the final RGB array.
        """
        height, width = pixels.shape
        result = np.empty((height, width, 3), dtype=np.float32)
        for start in range(0, height, 128):
            stop = min(start + 128, height)
            source_start = max(0, start - 2)
            source_stop = min(height, stop + 2)
            stripe = debayer(
                pixels[source_start:source_stop].astype(np.float64), pattern
            )
            result[start:stop] = stripe[start - source_start:stop - source_start]
        return result

    @staticmethod
    def _debayer_green(pixels: np.ndarray, bayer_pattern: str) -> np.ndarray:
        """Interpolate only green for quality metrics without allocating RGB."""
        tile = _BAYER_TILES[bayer_pattern]
        mask = np.zeros(pixels.shape, dtype=bool)
        for row in range(2):
            for column in range(2):
                if tile[row][column] == "G":
                    mask[row::2, column::2] = True
        known = np.where(mask, pixels, np.float32(0.0))
        padded = np.pad(known, 1, mode="edge")
        padded_mask = np.pad(mask, 1, mode="edge")
        summed = np.zeros_like(pixels, dtype=np.float32)
        counts = np.zeros(pixels.shape, dtype=np.uint8)
        height, width = pixels.shape
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                summed += padded[dy : dy + height, dx : dx + width]
                counts += padded_mask[dy : dy + height, dx : dx + width]
        np.divide(summed, np.maximum(counts, 1), out=summed)
        return summed

    @staticmethod
    def _translate_without_wrap(
        pixels: np.ndarray, shift_y: int, shift_x: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """Translate a frame while marking newly exposed borders invalid."""
        height, width = pixels.shape[:2]
        if abs(shift_y) >= height or abs(shift_x) >= width:
            raise LiveStackError("registration shift lies outside the image")
        aligned = np.zeros_like(pixels)
        valid = np.zeros(pixels.shape, dtype=bool)
        source_y = slice(max(0, -shift_y), min(height, height - shift_y))
        source_x = slice(max(0, -shift_x), min(width, width - shift_x))
        target_y = slice(max(0, shift_y), min(height, height + shift_y))
        target_x = slice(max(0, shift_x), min(width, width + shift_x))
        aligned[target_y, target_x, ...] = pixels[source_y, source_x, ...]
        valid[target_y, target_x, ...] = np.isfinite(aligned[target_y, target_x, ...])
        return aligned, valid

    @staticmethod
    def _rigid_without_wrap(
        pixels: np.ndarray,
        shift_y: float,
        shift_x: float,
        rotation_deg: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Bilinearly resample a rigid candidate-to-reference transform."""
        height, width = pixels.shape[:2]
        aligned = np.zeros_like(pixels)
        valid_2d = np.zeros((height, width), dtype=bool)
        radians = np.radians(rotation_deg)
        cosine, sine = float(np.cos(radians)), float(np.sin(radians))
        output_x = np.arange(width, dtype=np.float64)[None, :] - shift_x
        for start in range(0, height, 128):
            stop = min(height, start + 128)
            output_y = np.arange(start, stop, dtype=np.float64)[:, None] - shift_y
            source_y = cosine * output_y - sine * output_x
            source_x = sine * output_y + cosine * output_x
            y0 = np.floor(source_y).astype(np.intp)
            x0 = np.floor(source_x).astype(np.intp)
            chunk_valid = (y0 >= 0) & (y0 + 1 < height) & (x0 >= 0) & (x0 + 1 < width)
            if not np.any(chunk_valid):
                continue
            ys, xs = np.nonzero(chunk_valid)
            source_y0 = y0[ys, xs]
            source_x0 = x0[ys, xs]
            fraction_y = (source_y[ys, xs] - source_y0).astype(np.float32)
            fraction_x = (source_x[ys, xs] - source_x0).astype(np.float32)
            values = pixels
            if pixels.ndim == 2:
                interpolated = (
                    values[source_y0, source_x0] * (1.0 - fraction_y) * (1.0 - fraction_x)
                    + values[source_y0 + 1, source_x0] * fraction_y * (1.0 - fraction_x)
                    + values[source_y0, source_x0 + 1] * (1.0 - fraction_y) * fraction_x
                    + values[source_y0 + 1, source_x0 + 1] * fraction_y * fraction_x
                )
                aligned[start + ys, xs] = interpolated
            else:
                fy = fraction_y[:, None]
                fx = fraction_x[:, None]
                interpolated = (
                    values[source_y0, source_x0, :] * (1.0 - fy) * (1.0 - fx)
                    + values[source_y0 + 1, source_x0, :] * fy * (1.0 - fx)
                    + values[source_y0, source_x0 + 1, :] * (1.0 - fy) * fx
                    + values[source_y0 + 1, source_x0 + 1, :] * fy * fx
                )
                aligned[start + ys, xs, :] = interpolated
            valid_2d[start + ys, xs] = True
        valid = valid_2d if pixels.ndim == 2 else np.repeat(valid_2d[:, :, None], pixels.shape[2], axis=2)
        valid &= np.isfinite(aligned)
        return aligned, valid

    def save(
        self,
        *,
        output_path: Path | None = None,
        parameters_path: Path | None = None,
        date_obs: datetime | None = None,
    ) -> LiveStackRun:
        with self._lock:
            return self._save(
                output_path=output_path,
                parameters_path=parameters_path,
                date_obs=date_obs,
            )

    def _save(
        self,
        *,
        output_path: Path | None = None,
        parameters_path: Path | None = None,
        date_obs: datetime | None = None,
    ) -> LiveStackRun:
        """Atomically persist the current integration and its manifest."""
        if self._run is None:
            raise LiveStackError("no live stack has been started")
        if output_path is None and re.fullmatch(r"live_\d+", self._run.output_path.stem):
            target = re.sub(r"[^\w.-]+", "_", self._run.object_name or "unknown").strip("._")[:100] or "unknown"
            output_path = self._run.output_path.with_stem(f"{self._run.output_path.stem}_{target}")
        saved_run = self._run
        if output_path is not None:
            saved_run = replace(
                self._run,
                output_path=output_path,
                parameters_path=parameters_path or output_path.with_suffix(".json"),
            )
        pixels = self.pixels
        if pixels is not None:
            offsets = _BAYER_OFFSETS.get(self._bayer_pattern or "")
            first = self._first_header or {}
            last = self._last_header or {}
            metadata = FrameMetadata(
                date_obs=self._header_datetime(first, "DATE-OBS") or date_obs,
                date_end=self._header_datetime(last, "DATE-EXP"),
                exposure_s=None if self._mixed_exposures else self._uniform_exposure_s,
                total_exposure_s=self._total_exposure_s,
                stack_count=len(self._run.frame_paths),
                gain=self._run.gain,
                ccd_temp_c=self._run.ccd_temp_c,
                bayer_offset_x=None if offsets is None else offsets[0],
                bayer_offset_y=None if offsets is None else offsets[1],
                pixel_size_um=self._header_float(first, "XPIXSZ"),
                pixel_size_y_um=self._header_float(first, "YPIXSZ"),
                ra_deg=self._header_float(first, "RA"),
                dec_deg=self._header_float(first, "DEC"),
                site_lat_deg=self._header_float(first, "SITELAT"),
                site_lon_deg=self._header_float(first, "SITELONG"),
                filter_name=self._run.filter_name,
                object_name=self._run.object_name,
                frame_type=FrameType.LIGHT,
                focal_length_mm=self._header_float(first, "FOCALLEN"),
                aperture_diameter_mm=self._header_float(first, "APTDIA"),
                focal_ratio=self._header_float(first, "APERTURE"),
                sensor_name=self._header_string(first, "INSTRUME"),
                optic_id=self._header_string(first, "OPTICID"),
                focuser_position=self._header_int(first, "FOCUSPOS"),
                alignment_mode=self._header_int(first, "EQMODE"),
                wide_camera=self._header_bool(first, "WIDECAM"),
                bin_x=(self._header_int(first, "XBINNING") or 1) * self._run.binning
                if self._run.binning > 1 or "XBINNING" in first else None,
                bin_y=(self._header_int(first, "YBINNING") or 1) * self._run.binning
                if self._run.binning > 1 or "YBINNING" in first else None,
                telescope_name=self._header_string(first, "TELESCOP") or "Photon Commander live stack",
                ssc_version="0.0.0",
                device_firmware=None,
            )
            persisted = pixels if pixels.ndim == 2 else np.moveaxis(pixels, -1, 0)
            write_fits_file(
                saved_run.output_path,
                persisted,
                metadata,
                allow_cube=persisted.ndim == 3,
            )
        self._write_parameters(saved_run.parameters_path)
        self._run = saved_run
        # Subsequent acquisition boundaries update working provenance only.
        # An exported FITS and sidecar remain a matching snapshot until Save.
        self._working_parameters_path = self._folder / f".live_{self._sequence:03d}.working.json"
        return self._run

    def _calibrated(self, path: Path) -> tuple[np.ndarray, dict[str, object]]:
        image = read_fits_image(path.read_bytes())
        values = image.data.astype(np.float32)
        if values.ndim != 2:
            raise LiveStackError("live-stack frames must be two-dimensional FITS images")
        assert self._run is not None
        pedestal = self._run.dark_master or self._run.bias_master
        if pedestal is not None:
            values -= self._aligned_master(pedestal, values.shape)
        if self._run.flat_master is not None:
            value = image.header.get("BAYERPAT")
            pattern = value.upper() if isinstance(value, str) else None
            key = (self._run.flat_master, values.shape, pattern)
            flat = self._flat_cache.get(key)
            if flat is None:
                flat = self._normalized_flat(
                    self._aligned_master(self._run.flat_master, values.shape), pattern
                )
                self._flat_cache[key] = flat
            values /= flat
        return values, image.header

    @staticmethod
    def _normalized_flat(flat: np.ndarray, bayer_pattern: str | None) -> np.ndarray:
        if bayer_pattern not in _BAYER_TILES:
            mean = float(np.mean(flat))
            if not np.isfinite(mean) or mean <= 0:
                raise LiveStackError("flat master has no positive signal")
            return flat / mean
        normalized = flat.astype(np.float32, copy=True)
        tile = _BAYER_TILES[bayer_pattern]
        for channel in ("R", "G", "B"):
            planes = [
                flat[row::2, column::2]
                for row in range(2)
                for column in range(2)
                if tile[row][column] == channel
            ]
            finite = np.concatenate([plane[np.isfinite(plane)] for plane in planes])
            level = float(np.mean(finite)) if finite.size else 0.0
            if level <= 0:
                raise LiveStackError(f"flat master has no positive {channel} CFA signal")
            for row in range(2):
                for column in range(2):
                    if tile[row][column] == channel:
                        normalized[row::2, column::2] /= level
        return normalized

    def _aligned_master(self, path: Path, shape: tuple[int, int]) -> np.ndarray:
        key = (path, shape)
        cached = self._master_cache.get(key)
        if cached is not None:
            return cached
        image = read_fits_image(path.read_bytes())
        self._master_temperatures[path] = self._header_float(image.header, "CCD-TEMP")
        master = image.data.astype(np.float32)
        if master.shape == shape:
            aligned = master
        elif master.shape == shape[::-1]:
            aligned = master.T
        else:
            raise LiveStackError(
                f"master {path.name} has shape {master.shape}, expected {shape} or its transpose"
            )
        self._master_cache[key] = aligned
        return aligned

    def _write_parameters(self, path: Path | None = None) -> None:
        assert self._run is not None
        reference_temp = self._header_float(self._first_header or {}, "CCD-TEMP")
        dark_temp = (self._master_temperatures.get(self._run.dark_master)
                     if self._run.dark_master is not None else None)
        payload = {
            "reference_ccd_temp_c": reference_temp,
            "dark_ccd_temp_c": dark_temp,
            # Absolute measured mismatch; no temperature scaling is implied.
            "dark_temperature_delta_c": (
                abs(reference_temp - dark_temp)
                if reference_temp is not None and dark_temp is not None else None
            ),
            "sequence": self._run.sequence,
            "frames": [str(path) for path in self._run.frame_paths],
            "dark_master": None if self._run.dark_master is None else str(self._run.dark_master),
            "bias_master": None if self._run.bias_master is None else str(self._run.bias_master),
            "flat_master": None if self._run.flat_master is None else str(self._run.flat_master),
            "integration": "incremental trimmed mean",
            "integration_grouping": self._run.integration_grouping,
            "dither_group_boundaries": [
                {"id": boundary_id, "accepted_frame_index": index}
                for boundary_id, index in self._run.dither_group_boundaries
            ],
            "rejection": "one lowest and highest sample per pixel after three samples",
            "registration": "star-matched rigid rotation and translation to first frame; phase-correlation fallback for sparse fields",
            "registrations": self._registrations,
            "binning": f"{self._run.binning}x{self._run.binning}",
        }
        path = path or self._working_parameters_path
        assert path is not None
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


__all__ = ["LiveStackEngine", "LiveStackError", "LiveStackRun"]
