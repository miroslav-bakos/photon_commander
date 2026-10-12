# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

import numpy as np
import pytest

from ssc.streaming.auto_exposure import (
    WIDE_STREAM_MAX_EXPOSURE_S,
    next_wide_stream_exposure_s,
)

MINIMUM_S = 0.00003


def test_dark_wide_frame_increases_exposure_by_at_most_two_times() -> None:
    pixels = np.full((256, 256), 500, dtype=np.uint16)

    assert next_wide_stream_exposure_s(
        pixels, 0.2, minimum_exposure_s=MINIMUM_S
    ) == pytest.approx(0.4)
    assert next_wide_stream_exposure_s(
        pixels, 3.0, minimum_exposure_s=MINIMUM_S
    ) == WIDE_STREAM_MAX_EXPOSURE_S


def test_bright_wide_frame_reduces_exposure_without_oscillating() -> None:
    bright = np.full((256, 256), 60_000, dtype=np.uint16)
    on_target = np.full((256, 256), 16_384, dtype=np.uint16)

    assert next_wide_stream_exposure_s(
        bright, 1.0, minimum_exposure_s=MINIMUM_S
    ) == pytest.approx(0.5)
    assert next_wide_stream_exposure_s(
        on_target, 1.0, minimum_exposure_s=MINIMUM_S
    ) == pytest.approx(1.0)


def test_auto_exposure_ignores_sparse_hot_pixels_and_honours_lower_bound() -> None:
    pixels = np.full((256, 256), 500, dtype=np.uint16)
    pixels.ravel()[:100] = np.iinfo(np.uint16).max

    assert next_wide_stream_exposure_s(
        pixels, 0.2, minimum_exposure_s=MINIMUM_S
    ) == pytest.approx(0.4)
    assert next_wide_stream_exposure_s(
        np.full((8, 8), 65_535, dtype=np.uint16),
        0.00001,
        minimum_exposure_s=MINIMUM_S,
    ) == MINIMUM_S
