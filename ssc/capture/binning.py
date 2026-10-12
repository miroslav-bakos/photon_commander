# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Small, explicit software-binning helpers for transient solve frames."""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from ssc.capture.frame import RawFrame


def bin_2x2_for_solving(frame: RawFrame) -> RawFrame:
    """Reduce a raw 2-D solve frame to one quarter of its pixel count.

    The Seestar camera does not expose a verified hardware-binning control,
    so the full sensor has already been transferred when this runs. Averaging
    each complete Bayer tile produces a monochrome solve image with the same
    field of view. Metadata describes the doubled effective pixel pitch and
    deliberately drops the no-longer-applicable Bayer phase.

    Unknown/non-2-D and sub-2-pixel frames are returned unchanged so plate
    solving can still attempt the original image.
    """
    pixels = frame.pixels
    if pixels.ndim != 2 or pixels.shape[0] < 2 or pixels.shape[1] < 2:
        return frame
    height = pixels.shape[0] // 2 * 2
    width = pixels.shape[1] // 2 * 2
    cropped = pixels[:height, :width]
    mean = np.mean(
        np.stack(
            (
                cropped[0::2, 0::2],
                cropped[0::2, 1::2],
                cropped[1::2, 0::2],
                cropped[1::2, 1::2],
            )
        ),
        axis=0,
        dtype=np.float64,
    )
    if np.issubdtype(pixels.dtype, np.integer):
        mean = np.rint(mean)
    binned = mean.astype(pixels.dtype, copy=False)
    pixel_size_um = frame.metadata.pixel_size_um
    metadata = replace(
        frame.metadata,
        pixel_size_um=None if pixel_size_um is None else pixel_size_um * 2.0,
        bayer_offset_x=None,
        bayer_offset_y=None,
    )
    return RawFrame(pixels=binned, metadata=metadata)


__all__ = ["bin_2x2_for_solving"]
