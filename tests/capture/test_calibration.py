# SPDX-License-Identifier: GPL-3.0-only
"""Pure flat-probe quality and exposure-selection tests."""

from __future__ import annotations

import numpy as np
import pytest

from ssc.capture.calibration import (
    analyse_flat_frame,
    choose_next_flat_exposure,
    is_dark_filter_name,
    is_flat_eligible_filter_name,
)

MAX_ADU = 65535.0


@pytest.mark.parametrize("name", ["Dark", " dark ", "DARK"])
def test_dark_shutter_name_is_not_eligible_for_flats(name: str) -> None:
    assert is_dark_filter_name(name)
    assert not is_flat_eligible_filter_name(name)


@pytest.mark.parametrize("name", ["IR", "LP", "Dark Red", "Slot 1"])
def test_illuminated_filter_names_are_eligible_for_flats(name: str) -> None:
    assert not is_dark_filter_name(name)
    assert is_flat_eligible_filter_name(name)


def _flat(level: float, *, rows: int = 256, columns: int = 320) -> np.ndarray:
    yy, xx = np.mgrid[:rows, :columns]
    vignette = 1.0 - 0.08 * ((xx - columns / 2) / columns) ** 2 - 0.08 * (
        (yy - rows / 2) / rows
    ) ** 2
    return np.clip(level * vignette, 0, MAX_ADU).astype(np.uint16)


def test_clean_mid_histogram_flat_is_accepted() -> None:
    metrics = analyse_flat_frame(_flat(MAX_ADU * 0.54), max_adu=MAX_ADU)
    decision = choose_next_flat_exposure(
        metrics,
        exposure_s=0.5,
        minimum_s=0.00003,
        maximum_s=30.0,
        source="panel",
        probe_number=2,
    )
    assert metrics.target_brightness
    assert not metrics.clipped
    assert not metrics.banded
    assert decision.action == "accept"


def test_dim_flat_scales_the_next_probe_up_with_a_bound() -> None:
    metrics = analyse_flat_frame(_flat(MAX_ADU * 0.10), max_adu=MAX_ADU)
    decision = choose_next_flat_exposure(
        metrics,
        exposure_s=0.1,
        minimum_s=0.00003,
        maximum_s=30.0,
        source="panel",
        probe_number=1,
    )
    assert decision.action == "probe"
    assert decision.next_exposure_s == 0.4


def test_clipping_at_minimum_requests_a_dimmer_source() -> None:
    metrics = analyse_flat_frame(np.full((128, 128), 65535, dtype=np.uint16), max_adu=MAX_ADU)
    decision = choose_next_flat_exposure(
        metrics,
        exposure_s=0.00003,
        minimum_s=0.00003,
        maximum_s=30.0,
        source="panel",
        probe_number=1,
    )
    assert metrics.clipped
    assert decision.action == "adjust_source"
    assert "shortest exposure" in decision.reason


def test_periodic_rows_are_reported_as_likely_banding() -> None:
    pixels = _flat(MAX_ADU * 0.52).astype(np.float64)
    rows = np.arange(pixels.shape[0])
    pixels *= (1.0 + 0.08 * np.sin(rows * 2 * np.pi / 12))[:, None]
    metrics = analyse_flat_frame(pixels.astype(np.uint16), max_adu=MAX_ADU)
    assert metrics.banded
    assert metrics.banding_axis == "rows"


def test_bayer_phase_is_not_mistaken_for_panel_banding() -> None:
    pixels = _flat(MAX_ADU * 0.52).astype(np.float64)
    # Deliberately strong Seestar-like raw CFA response differences. Direct
    # row/column profiles report these as 2-pixel stripes; a complete 2x2 CFA
    # cell has stable total response and is valid illumination.
    pixels[0::2, 0::2] *= 0.68
    pixels[0::2, 1::2] *= 1.28
    pixels[1::2, 0::2] *= 0.76
    pixels[1::2, 1::2] *= 1.28

    metrics = analyse_flat_frame(np.clip(pixels, 0, MAX_ADU).astype(np.uint16), max_adu=MAX_ADU)

    assert not metrics.banded


def test_good_short_panel_flat_is_accepted() -> None:
    metrics = analyse_flat_frame(_flat(MAX_ADU * 0.52), max_adu=MAX_ADU)
    decision = choose_next_flat_exposure(
        metrics,
        # The Seestar factory IR master uses about 0.045 s. Exposure length is
        # not itself a defect when the histogram and illumination are sound.
        exposure_s=0.045,
        minimum_s=0.00003,
        maximum_s=30.0,
        source="panel",
        probe_number=1,
    )
    assert decision.action == "accept"
