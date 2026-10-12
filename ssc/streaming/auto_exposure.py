# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Bounded exposure feedback for the low-rate wide-camera stream."""

from __future__ import annotations

import math
from typing import Final

import numpy as np
from numpy.typing import NDArray

WIDE_STREAM_MAX_EXPOSURE_S: Final = 4.0

_TARGET_LEVEL_FRACTION: Final = 0.25
_LOW_HYSTERESIS: Final = 0.8
_HIGH_HYSTERESIS: Final = 1.25
_MIN_STEP_FACTOR: Final = 0.5
_MAX_STEP_FACTOR: Final = 2.0
_SAMPLE_BUDGET: Final = 65_536


def next_wide_stream_exposure_s(
    pixels: NDArray[np.generic],
    current_exposure_s: float,
    *,
    minimum_exposure_s: float,
    maximum_exposure_s: float = WIDE_STREAM_MAX_EXPOSURE_S,
) -> float:
    """Choose the next exposure from a robust bright-scene percentile.

    The 75th percentile follows the broad sky/cloud brightness rather than
    stars or a sparse hot-pixel population. Square-root feedback and a 2x
    per-frame step limit prevent alternating bright/dark frames when a light
    enters the field.
    """

    values = np.asarray(pixels)
    if values.size == 0:
        return _bounded(current_exposure_s, minimum_exposure_s, maximum_exposure_s)
    stride = max(1, math.ceil(math.sqrt(values.size / _SAMPLE_BUDGET)))
    sample = values.ravel()[::stride].astype(np.float64, copy=False)
    sample = sample[np.isfinite(sample)]
    if sample.size == 0:
        return _bounded(current_exposure_s, minimum_exposure_s, maximum_exposure_s)

    if np.issubdtype(values.dtype, np.integer):
        sensor_max = float(np.iinfo(values.dtype).max)
    else:
        sensor_max = max(float(np.percentile(sample, 99.99)), 1.0)
    observed = max(float(np.percentile(sample, 75.0)), sensor_max * 0.001)
    ratio = (sensor_max * _TARGET_LEVEL_FRACTION) / observed
    current = _bounded(current_exposure_s, minimum_exposure_s, maximum_exposure_s)
    if _LOW_HYSTERESIS <= ratio <= _HIGH_HYSTERESIS:
        return current
    factor = (
        _MIN_STEP_FACTOR
        if observed >= sensor_max * 0.9
        else min(_MAX_STEP_FACTOR, max(_MIN_STEP_FACTOR, math.sqrt(ratio)))
    )
    return _bounded(current * factor, minimum_exposure_s, maximum_exposure_s)


def _bounded(exposure_s: float, minimum_exposure_s: float, maximum_exposure_s: float) -> float:
    return min(maximum_exposure_s, max(minimum_exposure_s, exposure_s))


__all__ = [
    "WIDE_STREAM_MAX_EXPOSURE_S",
    "next_wide_stream_exposure_s",
]
