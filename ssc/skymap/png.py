# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""A minimal PNG encoder, and the deterministic stretch that turns a FITS
plane into 8-bit pixels for it. Pure; no I/O.

Lifted and generalised from ``scripts/build-wham-hips.py`` (which wrote an
8-bit palette PNG for one bundled H-alpha layer) because a FITS cutout piece
now needs a **display PNG** generated server-side so the browser never parses
FITS (M11 spec: "WWT overlays need an image + WCS"). No PIL/astropy
dependency is added for this — check the deps before reaching for one.

Only what SSC's own cutout pieces need: 8-bit grayscale (colour type 0) and
8-bit RGB (colour type 2), uncompressed-then-deflated the same way the WHAM
build script did (``zlib``, the smallest of three per-row filters).

Two stretches, not one, added 2026-09-09 when the second use case showed the
first one does not generalise: :func:`asinh_stretch` derives its black/white
point from the pixels it is given, correct for a cutout (one contiguous
image) but wrong for one tile of a pyramid (``ssc.skymap.service``'s
``Survey.fits_stretch``), where every tile must agree on what a given
intensity looks like or the pyramid reads as a mosaic of mismatched patches.
:func:`asinh_stretch_reference` takes that black/white point as an argument
instead of deriving it.
"""

from __future__ import annotations

import math
import struct
import zlib
from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray

#: The stretch this module applies, named so a piece's manifest can say
#: exactly which one produced its display PNG (never bare pixels with no
#: explanation of how they got there).
STRETCH_NAME: Final = "asinh-percentile-clip"
#: Percentile clip before the asinh stretch: robust to a handful of hot
#: pixels or cosmic rays without needing per-band tuning.
LOW_PERCENTILE: Final = 1.0
HIGH_PERCENTILE: Final = 99.5
#: The knee of the asinh curve, in units of the clipped range — keeps faint
#: structure visible without letting the bright core blow out the image the
#: way a linear stretch would.
SOFTENING: Final = 0.05

#: N.I.N.A.'s default display-autostretch settings. These affect preview
#: pixels only; captured and stacked FITS values are never altered.
NINA_TARGET_BACKGROUND: Final = 0.2
NINA_BLACK_CLIPPING: Final = -2.8
NINA_MAD_SCALE: Final = 1.4826
NINA_DEFAULT_WHITE_LEVEL: Final = 65535.0


def describe_stretch() -> str:
    """The stretch parameters, as a string worth storing on a piece."""
    return f"{STRETCH_NAME}(p{LOW_PERCENTILE:g}..p{HIGH_PERCENTILE:g}, softening={SOFTENING:g})"


def asinh_stretch(plane: NDArray[np.float64]) -> NDArray[np.uint8]:
    """One band's pixels -> ``[0, 255]``, deterministically.

    NaN/Inf (a FITS blank-pixel marker, or a division upstream) map to 0
    (fully transparent-looking, never a guessed value — CLAUDE.md invariant
    7 in image form). The percentile clip is computed on the finite pixels
    only, so a handful of NaNs cannot skew the stretch of the rest.

    **Only for a cutout** — one contiguous image with nothing else to compare
    it to, where deriving the black/white point from its own pixels is
    correct. A *tile pyramid* must use :func:`asinh_stretch_reference`
    instead: two neighbouring tiles stretched independently by this function
    map the same real sky brightness to different output greys, which reads
    as a hard edge at the tile boundary (found live in ``nsns-halpha``,
    2026-09-09 — see that function's docstring).
    """
    finite = plane[np.isfinite(plane)]
    if finite.size == 0:
        return np.zeros(plane.shape, dtype=np.uint8)
    low, high = np.percentile(finite, [LOW_PERCENTILE, HIGH_PERCENTILE])
    if high <= low:
        high = low + 1.0
    return _asinh_curve(plane, low, high, SOFTENING)


def neutralize_rgb_background(pixels: NDArray[np.float64]) -> NDArray[np.float64]:
    """Remove per-channel background offsets from an RGB display image.

    The lowest channel median is the neutral reference and only excess
    offsets are subtracted.  This is deliberately additive rather than a
    channel gain/SCNR operation: colour differences above the background
    survive, including genuine green OIII structure.  The caller receives a
    copy; calibrated or stacked science pixels are never modified.
    """
    if pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError(f"RGB background neutralization needs a (h, w, 3) array, got {pixels.shape}")
    corrected = pixels.astype(np.float64, copy=True)
    medians = np.array(
        [
            np.median(channel[np.isfinite(channel)])
            if np.isfinite(channel).any()
            else np.nan
            for channel in np.moveaxis(corrected, -1, 0)
        ],
        dtype=np.float64,
    )
    finite_medians = medians[np.isfinite(medians)]
    if finite_medians.size == 0:
        return corrected
    target = float(np.min(finite_medians))
    for index, median in enumerate(medians):
        if np.isfinite(median):
            corrected[..., index] -= max(0.0, float(median) - target)
    return corrected


def balance_rgb_for_display(
    pixels: NDArray[np.float64], *, signal_percentile: float = 99.5
) -> NDArray[np.float64]:
    """Balance RGB backgrounds and robust bright-signal ranges for display.

    Independent black offsets neutralize the sky. A high percentile above
    each background provides a white-balance scale without the noise-driven
    green boost produced by fully unlinked stretching. The returned array is
    a copy; this is display balancing, not photometric calibration.
    """
    if pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError(f"RGB display balance needs a (h, w, 3) array, got {pixels.shape}")
    if not 50.0 < signal_percentile < 100.0:
        raise ValueError("signal_percentile must be between 50 and 100")
    balanced = pixels.astype(np.float64, copy=True)
    medians = np.empty(3, dtype=np.float64)
    signals = np.empty(3, dtype=np.float64)
    for channel in range(3):
        finite = balanced[..., channel][np.isfinite(balanced[..., channel])]
        if finite.size == 0:
            return balanced
        medians[channel] = np.median(finite)
        signals[channel] = np.percentile(finite, signal_percentile) - medians[channel]
    if np.any(signals <= np.finfo(np.float64).eps):
        return neutralize_rgb_background(balanced)
    target_signal = float(np.exp(np.mean(np.log(signals))))
    scales = np.clip(target_signal / signals, 0.25, 4.0)
    common_background = float(np.min(medians))
    balanced -= medians
    balanced *= scales
    balanced += common_background
    return balanced


def suppress_excess_green_for_display(pixels: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Remove a clear green-only cast from an already stretched RGB preview.

    Leave neutral background noise and cyan/yellow signal alone. Only pixels
    whose green exceeds both other channels by more than eight display levels
    are capped; scientific FITS pixels are never changed.
    """
    if pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError(f"green suppression needs a (h, w, 3) array, got {pixels.shape}")
    result = pixels.copy()
    other = np.maximum(result[..., 0], result[..., 2])
    excess = result[..., 1].astype(np.int16) - other.astype(np.int16) > 8
    result[..., 1] = np.where(excess, other, result[..., 1])
    return result


def nina_autostretch(
    pixels: NDArray[np.float64],
    *,
    white_level: float = NINA_DEFAULT_WHITE_LEVEL,
    target_background: float = NINA_TARGET_BACKGROUND,
    black_clipping: float = NINA_BLACK_CLIPPING,
) -> NDArray[np.uint8]:
    """Apply N.I.N.A.'s median/MAD midtones display stretch.

    The defaults mirror N.I.N.A.'s ``ImageSettings`` and its
    ``ImageUtility.GetStretchMap`` calculation: robust median and scaled MAD
    set the black point, then a midtones transfer maps the background median
    to 20%. Passing an RGB array computes one set of statistics across all
    channels, giving the linked stretch used to preserve channel balance.

    This is exclusively a display conversion. NaN maps to black, infinities
    clip to the corresponding endpoint, and the source array is untouched.
    """
    if white_level <= 0:
        raise ValueError("white_level must be positive")
    finite = pixels[np.isfinite(pixels)]
    if finite.size == 0:
        return np.zeros(pixels.shape, dtype=np.uint8)

    normalized = finite.astype(np.float64, copy=False) / white_level
    median = float(np.median(normalized, overwrite_input=True))
    deviations = np.abs(normalized - median)
    mad = float(np.median(deviations, overwrite_input=True))
    del normalized, deviations
    if mad <= np.finfo(np.float64).eps:
        low = float(np.min(finite))
        high = float(np.max(finite))
        if high <= low:
            return np.zeros(pixels.shape, dtype=np.uint8)
        del finite
        result = np.empty(pixels.shape, dtype=np.uint8)
        for start in range(0, pixels.shape[0], 128):
            stripe = pixels[start : start + 128]
            result[start : start + 128] = _display_u8((stripe - low) / (high - low))
        return result

    shadows = 0.0
    highlights = 1.0
    if median > 0.5:
        highlights = median - black_clipping * mad * NINA_MAD_SCALE
        balance_input = 1.0 - (highlights - median)
    else:
        shadows = median + black_clipping * mad * NINA_MAD_SCALE
        balance_input = median - shadows
    midtones = float(_midtones_transfer(target_background, balance_input).item())
    del finite
    result = np.empty(pixels.shape, dtype=np.uint8)
    for start in range(0, pixels.shape[0], 128):
        stripe = pixels[start : start + 128]
        stretched = _midtones_transfer(
            midtones, stripe / white_level + 1.0 - highlights - shadows
        )
        result[start : start + 128] = _display_u8(stretched)
    return result


def nina_autostretch_unlinked_rgb(
    pixels: NDArray[np.float64],
    *,
    white_level: float = NINA_DEFAULT_WHITE_LEVEL,
    target_background: float = NINA_TARGET_BACKGROUND,
    black_clipping: float = NINA_BLACK_CLIPPING,
) -> NDArray[np.uint8]:
    """Apply the display autostretch independently to each RGB channel.

    OSC channels commonly have different background offsets *and* response
    scales.  A linked transform preserves both and therefore preserves a
    strong green or magenta cast.  Unlinked display stretching maps each
    channel's own robust background to the same target while the scientific
    RGB/FITS values remain untouched.
    """
    if pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError(f"unlinked RGB autostretch needs a (h, w, 3) array, got {pixels.shape}")
    return np.stack(
        [
            nina_autostretch(
                pixels[..., channel],
                white_level=white_level,
                target_background=target_background,
                black_clipping=black_clipping,
            )
            for channel in range(3)
        ],
        axis=-1,
    )


def _midtones_transfer(balance: float, values: float | NDArray[np.float64]) -> NDArray[np.float64]:
    """N.I.N.A./PixInsight midtones transfer, vectorised and endpoint-safe."""
    x = np.asarray(values, dtype=np.float64)
    clipped = np.clip(x, 0.0, 1.0)
    denominator = (2.0 * balance - 1.0) * clipped - balance
    with np.errstate(divide="ignore", invalid="ignore"):
        transformed = (balance - 1.0) * clipped / denominator
    return np.where(clipped <= 0.0, 0.0, np.where(clipped >= 1.0, 1.0, transformed))


def _display_u8(values: NDArray[np.float64]) -> NDArray[np.uint8]:
    safe = np.nan_to_num(np.clip(values, 0.0, 1.0), nan=0.0, posinf=1.0, neginf=0.0)
    return np.round(safe * 255.0).astype(np.uint8)


def asinh_stretch_reference(
    plane: NDArray[np.float64],
    black_point: float,
    white_point: float,
    *,
    softening: float = SOFTENING,
) -> NDArray[np.uint8]:
    """One band's pixels -> ``[0, 255]`` against a **fixed** black/white point.

    For a tile *pyramid* (as opposed to :func:`asinh_stretch`'s single
    cutout), every tile is one slice of the same continuous sky, so every
    tile must map a given physical intensity to the same output grey — this
    function never looks at ``plane``'s own statistics, only at
    ``black_point``/``white_point``, which are the survey's constants
    (``Survey.fits_stretch_black``/``fits_stretch_white``), the same on every
    tile.

    Found necessary live 2026-09-09: ``nsns-halpha`` order-6 npix 21439 (on
    the Orion Nebula's core, real Rayleigh values up to ~140000 from the
    Trapezium) stretched by :func:`asinh_stretch` to a mean display value of
    11.5, while its actual-fainter neighbour npix 21480 stretched to 190 —
    each tile's own percentile clip absorbed *its* dynamic range, so a
    brighter tile came out darker than a fainter one. A fixed reference
    reverses this: the same neighbours come out continuous (roughly 82-155
    at the constants chosen for this survey — see the registry entry).

    NaN/Inf handling and the asinh curve are otherwise identical to
    :func:`asinh_stretch`.
    """
    if white_point <= black_point:
        raise ValueError(f"white_point ({white_point}) must exceed black_point ({black_point})")
    return _asinh_curve(plane, black_point, white_point, softening)


def _asinh_curve(
    plane: NDArray[np.float64], low: float, high: float, softening: float
) -> NDArray[np.uint8]:
    """Shared asinh knee, given an already-decided ``[low, high]``."""
    span = high - low
    clipped = np.clip((plane - low) / span, 0.0, 1.0)
    knee = math.asinh(1.0 / softening)
    v = np.arcsinh(clipped / softening) / knee
    v = np.nan_to_num(np.clip(v, 0.0, 1.0), nan=0.0, posinf=1.0, neginf=0.0)
    return np.round(v * 255.0).astype(np.uint8)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _filtered_rows(
    pixels: NDArray[np.uint8], filter_type: int, *, bytes_per_pixel: int
) -> bytes:
    """``pixels`` is ``(height, width * channels)``, one byte per sample."""
    if filter_type == 0:
        delta = pixels
    elif filter_type == 1:  # Sub
        delta = pixels.copy()
        # PNG Sub predicts from the corresponding channel of the preceding
        # pixel, not from the preceding byte.  Using one byte here happened
        # to be correct for grayscale but made standards-compliant decoders
        # reconstruct RGB scanlines as rainbow noise whenever zlib selected
        # this filter for a live-stack frame.
        delta[:, bytes_per_pixel:] = (
            pixels[:, bytes_per_pixel:].astype(np.int16)
            - pixels[:, :-bytes_per_pixel]
        ) & 0xFF
    elif filter_type == 2:  # Up
        delta = pixels.copy()
        delta[1:, :] = (pixels[1:, :].astype(np.int16) - pixels[:-1, :]) & 0xFF
    else:
        raise ValueError(filter_type)
    tag = np.full((pixels.shape[0], 1), filter_type, dtype=np.uint8)
    return np.concatenate([tag, delta.astype(np.uint8)], axis=1).tobytes()


def _encode(pixels: NDArray[np.uint8], *, colour_type: Literal[0, 2]) -> bytes:
    height, width = pixels.shape[0], pixels.shape[1]
    channels = 1 if pixels.ndim == 2 else pixels.shape[2]
    flat = pixels.reshape(height, width * channels)
    header = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    best = min(
        (
            zlib.compress(_filtered_rows(flat, f, bytes_per_pixel=channels), 9)
            for f in (0, 1, 2)
        ),
        key=len,
    )
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", header),
            _png_chunk(b"IDAT", best),
            _png_chunk(b"IEND", b""),
        ]
    )


def encode_grayscale_png(pixels: NDArray[np.uint8]) -> bytes:
    """``(height, width)`` 8-bit samples -> a grayscale PNG (colour type 0)."""
    if pixels.ndim != 2:
        raise ValueError(f"grayscale PNG needs a 2-D array, got shape {pixels.shape}")
    return _encode(pixels, colour_type=0)


def encode_rgb_png(pixels: NDArray[np.uint8]) -> bytes:
    """``(height, width, 3)`` 8-bit samples -> a truecolor PNG (colour type 2)."""
    if pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError(f"RGB PNG needs a (h, w, 3) array, got shape {pixels.shape}")
    return _encode(pixels, colour_type=2)


__all__ = [
    "HIGH_PERCENTILE",
    "LOW_PERCENTILE",
    "NINA_BLACK_CLIPPING",
    "NINA_DEFAULT_WHITE_LEVEL",
    "NINA_MAD_SCALE",
    "NINA_TARGET_BACKGROUND",
    "SOFTENING",
    "STRETCH_NAME",
    "asinh_stretch",
    "asinh_stretch_reference",
    "balance_rgb_for_display",
    "describe_stretch",
    "encode_grayscale_png",
    "encode_rgb_png",
    "neutralize_rgb_background",
    "nina_autostretch",
    "nina_autostretch_unlinked_rgb",
    "suppress_excess_green_for_display",
]
