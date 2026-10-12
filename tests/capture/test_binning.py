# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np

from ssc.capture import FrameMetadata, FrameType, RawFrame, bin_2x2_for_solving


def metadata(**changes: object) -> FrameMetadata:
    base = FrameMetadata(
        date_obs=datetime(2026, 9, 12, tzinfo=UTC),
        exposure_s=1.0,
        gain=200,
        ccd_temp_c=None,
        bayer_offset_x=1,
        bayer_offset_y=0,
        pixel_size_um=2.9,
        ra_deg=None,
        dec_deg=None,
        site_lat_deg=None,
        site_lon_deg=None,
        filter_name="IR",
        object_name="solve",
        frame_type=FrameType.LIGHT,
        focal_length_mm=250.0,
        telescope_name="Seestar",
        ssc_version="test",
        device_firmware="test",
    )
    return replace(base, **changes)


def test_2x2_solve_binning_averages_tiles_and_updates_metadata() -> None:
    pixels = np.array(
        [[0, 2, 10, 12], [4, 6, 14, 16], [20, 22, 30, 32], [24, 26, 34, 36]],
        dtype=np.uint16,
    )

    result = bin_2x2_for_solving(RawFrame(pixels=pixels, metadata=metadata()))

    np.testing.assert_array_equal(result.pixels, np.array([[3, 13], [23, 33]], np.uint16))
    assert result.metadata.pixel_size_um == 5.8
    assert result.metadata.bayer_offset_x is None
    assert result.metadata.bayer_offset_y is None


def test_solve_binning_falls_back_for_non_2d_frame() -> None:
    frame = RawFrame(pixels=np.zeros((4, 4, 3), dtype=np.uint16), metadata=metadata())

    assert bin_2x2_for_solving(frame) is frame
