# SPDX-License-Identifier: GPL-3.0-only
"""Flat-frame exposure selection and illumination-quality measurements.

Pure array maths: device orchestration lives in ``ssc.api.capture_orchestrator``.
The target and limits follow SharpCap's published flat guidance (mid-histogram,
no meaningful clipping), while the row/column high-pass measurement detects
the narrow stripes typical of a PWM panel or display refresh interacting with
a rolling shutter. It deliberately says *likely* banding: dust and vignetting
are wanted flat-field structure and must not be diagnosed as a display fault.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

TARGET_FRACTION = 0.52
TARGET_LOW_FRACTION = 0.42
TARGET_HIGH_FRACTION = 0.62
BRIGHT_LIMIT_FRACTION = 0.90
CLIP_FRACTION = 0.98
MAX_CLIPPED_PIXEL_FRACTION = 0.0001
BANDING_LIMIT_FRACTION = 0.01
MAX_PROBES = 10


def is_dark_filter_name(name: str) -> bool:
    """Whether a filter-wheel slot is the opaque ``Dark`` shutter."""
    return name.strip().casefold() == "dark"


def is_flat_eligible_filter_name(name: str) -> bool:
    """Whether a filter-wheel slot admits light for a flat exposure.

    Seestar exposes its closed shutter as a normal slot named ``Dark``.  Its
    numeric position varies by device/driver, so eligibility follows the live
    semantic name rather than assuming slot zero.
    """
    return not is_dark_filter_name(name)


@dataclass(frozen=True, slots=True)
class FlatFrameMetrics:
    median_fraction: float
    low_fraction: float
    high_fraction: float
    clipped_pixel_fraction: float
    banding_fraction: float
    banding_axis: Literal["rows", "columns"] | None

    @property
    def clipped(self) -> bool:
        return (
            self.high_fraction >= BRIGHT_LIMIT_FRACTION
            or self.clipped_pixel_fraction > MAX_CLIPPED_PIXEL_FRACTION
        )

    @property
    def banded(self) -> bool:
        return self.banding_fraction >= BANDING_LIMIT_FRACTION

    @property
    def target_brightness(self) -> bool:
        return TARGET_LOW_FRACTION <= self.median_fraction <= TARGET_HIGH_FRACTION


@dataclass(frozen=True, slots=True)
class FlatExposureDecision:
    action: Literal["accept", "probe", "adjust_source", "fail"]
    next_exposure_s: float | None
    reason: str


def _profile_banding(profile: NDArray[np.float64], *, scale: float) -> float:
    """High-frequency robust amplitude after removing broad illumination.

    The smoothing window is large enough to retain PWM/readout stripes and
    small enough to remove normal vignette curvature. Edge samples are
    discarded because convolution padding is not an illumination feature.
    """
    if profile.size < 24 or scale <= 0:
        return 0.0
    window = max(9, (profile.size // 16) | 1)
    kernel = np.full(window, 1.0 / window, dtype=np.float64)
    smooth = np.convolve(profile, kernel, mode="same")
    edge = window // 2
    residual = profile[edge:-edge] - smooth[edge:-edge]
    if residual.size == 0:
        return 0.0
    centred = residual - np.median(residual)
    return float(np.percentile(np.abs(centred), 95) / scale)


def _cfa_neutral_plane(plane: NDArray[np.float64]) -> NDArray[np.float64]:
    """Collapse each 2x2 Bayer cell before measuring illumination structure.

    Raw flats legitimately alternate by row and column because the four CFA
    photosites have different spectral responses.  A profile taken directly
    from that mosaic mistakes the Bayer phase for high-frequency panel
    banding.  Every 2x2 cell contains one complete Bayer tile regardless of
    which named pattern the sensor uses, so its mean removes that phase while
    retaining illumination gradients and wider refresh bands.
    """
    height = plane.shape[0] - plane.shape[0] % 2
    width = plane.shape[1] - plane.shape[1] % 2
    if height == 0 or width == 0:
        return plane
    return plane[:height, :width].reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3))


def analyse_flat_frame(pixels: NDArray[np.generic], *, max_adu: float) -> FlatFrameMetrics:
    """Measure brightness, clipping, and likely axis-aligned refresh banding."""
    if pixels.ndim != 2 or pixels.size == 0:
        raise ValueError(f"flat probe must be a non-empty 2-D image, got {pixels.shape}")
    if not np.isfinite(max_adu) or max_adu <= 0:
        raise ValueError(f"Camera.MaxADU must be positive, got {max_adu!r}")
    plane = pixels.astype(np.float64, copy=False)
    if not np.isfinite(plane).all():
        raise ValueError("flat probe contains non-finite pixels")

    low, median, high = np.percentile(plane, (0.1, 50.0, 99.9))
    clipped = float(np.count_nonzero(plane >= max_adu * CLIP_FRACTION) / plane.size)

    # Remove the 2x2 colour-filter-array phase first. Native Seestar IR/LP
    # masters otherwise look severely row-banded even though their broad
    # structure is valid flat-field signal. Then bound profile work on 8 MP
    # frames by striding the already phase-neutral plane.
    illumination = _cfa_neutral_plane(plane)
    row_stride = max(1, illumination.shape[0] // 1024)
    col_stride = max(1, illumination.shape[1] // 1024)
    sample = illumination[::row_stride, ::col_stride]
    row_strength = _profile_banding(np.median(sample, axis=1), scale=float(median))
    col_strength = _profile_banding(np.median(sample, axis=0), scale=float(median))
    strength = max(row_strength, col_strength)
    axis: Literal["rows", "columns"] | None = None
    if strength >= BANDING_LIMIT_FRACTION:
        axis = "rows" if row_strength >= col_strength else "columns"

    return FlatFrameMetrics(
        median_fraction=float(median / max_adu),
        low_fraction=float(low / max_adu),
        high_fraction=float(high / max_adu),
        clipped_pixel_fraction=clipped,
        banding_fraction=strength,
        banding_axis=axis,
    )


def choose_next_flat_exposure(
    metrics: FlatFrameMetrics,
    *,
    exposure_s: float,
    minimum_s: float,
    maximum_s: float,
    source: Literal["panel", "sky"],
    probe_number: int,
) -> FlatExposureDecision:
    """Accept, adjust the illumination, or choose the next bounded probe."""
    if probe_number >= MAX_PROBES:
        return FlatExposureDecision(
            "fail", None, f"could not converge on a safe flat exposure in {MAX_PROBES} probes"
        )
    if metrics.clipped and exposure_s <= minimum_s * 1.001:
        return FlatExposureDecision(
            "adjust_source",
            None,
            "the flat clips at the camera's shortest exposure; dim or diffuse the light source",
        )
    if metrics.target_brightness and not metrics.clipped:
        if metrics.banded:
            return FlatExposureDecision(
                "adjust_source",
                None,
                f"likely {metrics.banding_axis} PWM/refresh banding was detected; dim the "
                "panel or phone so a longer exposure can average more refresh cycles",
            )
        return FlatExposureDecision("accept", None, "brightness and illumination are suitable")

    median = max(metrics.median_fraction, 1e-6)
    proposed = exposure_s * TARGET_FRACTION / median
    # Avoid wild extrapolation from a nearly black first frame while still
    # converging much faster than one fixed increment at a time.
    proposed = min(exposure_s * 4.0, max(exposure_s / 4.0, proposed))
    proposed = min(maximum_s, max(minimum_s, proposed))
    if abs(proposed - exposure_s) <= max(1e-9, exposure_s * 0.001):
        direction = "dim" if metrics.clipped or median > TARGET_HIGH_FRACTION else "brighten"
        return FlatExposureDecision(
            "adjust_source",
            None,
            f"cannot {direction} the flat within this camera's exposure limits; adjust the source",
        )
    return FlatExposureDecision("probe", proposed, "another exposure is needed")


__all__ = [
    "BANDING_LIMIT_FRACTION",
    "MAX_PROBES",
    "FlatExposureDecision",
    "FlatFrameMetrics",
    "analyse_flat_frame",
    "choose_next_flat_exposure",
    "is_dark_filter_name",
    "is_flat_eligible_filter_name",
]
