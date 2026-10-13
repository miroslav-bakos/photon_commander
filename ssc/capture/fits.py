# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""FITS writing: the counterpart to ``ssc.skymap.fits``'s pure reader.

ImageBytes carries no metadata at all (DESIGN.md §9.5.2) — SSC authors every
header card itself, once, from :class:`ssc.capture.frame.FrameMetadata`. A
field that could not be read is absent from the header, never written as
zero (CLAUDE.md invariant 7); this module never back-fills or defaults a
card, and it never talks to a device — it only ever sees what it is handed.

**BAYERPAT is derived from ``BayerOffsetX``/``BayerOffsetY``, never from
``SensorType``'s name.** ASCOM's ``SensorType`` enumerates a *family* (mono,
Bayer-RGGB-style CMOS, …); it does not say which of the four phases this
particular sensor's readout actually has.

**Live measurement, firmware's native Alpaca on :32323, telephoto
``camera/0``, 2026-09-10, corrected against a Seestar-app FITS on
2026-09-12.** ImageBytes declares ``Dimension1=2160`` (X) and
``Dimension2=3840`` (Y). Its decoded ``array[x, y]`` is transposed at the
capture boundary into this writer's ``pixels[y, x]``, producing the same
portrait ``NAXIS1=2160`` columns and ``NAXIS2=3840`` rows as the app.
The 0.1 s dark's sub-lattice means in FITS row/column orientation are::

    row even, col even: 1086.70   row even, col odd: 1073.59
    row odd,  col even: 1040.91   row odd,  col odd:  1086.80

The diagonal pair agrees to 0.1 ADU while the off-diagonal pair differs by
33 — the two green photosites — so **green sits at decoded-array (0, 0) and
(1, 1)**. That much is measured and solid; it does not by itself distinguish
GRBG from GBRG, since both put green on the diagonal. Two independent
sources agree it is specifically **GRBG** (red at FITS row 0, column 1): the device's own
8.46 ``get_camera_info`` reporting ``debayer_pattern: "GR"``
(``docs/M0-FINDINGS.md``), and the owner directly. Measured offsets on this
unit: ``BayerOffsetX = 1``, ``BayerOffsetY = 0``.

This module therefore reads ``BayerOffsetX`` along the FITS column axis
(``NAXIS1``) and ``BayerOffsetY`` along its row axis (``NAXIS2``), exactly
matching the device's X/Y labels. Measured offsets ``1/0`` yield GRBG, also
matching the Seestar app's own ``BAYERPAT`` card.

**Row order changes the effective phase.** A Bayer mosaic's phase depends on
which row is physically first. Flipping the array vertically before writing
would toggle the effective Y-offset parity whenever the image height is
even — as every Seestar frame's is — which silently swaps GRBG↔BGGR and
RGGB↔GBRG. This writer never flips: ``pixels[0]`` (row index 0 of the array
handed to :func:`write_fits_bytes`) is written as the FITS file's first row
of image data, unchanged, and ``ROWORDER = 'TOP-DOWN'`` records that choice
explicitly so Siril/ASTAP need not guess it from the data alone.
"""

from __future__ import annotations

import os
import tempfile
import unicodedata
from pathlib import Path
from typing import Final

import numpy as np
from numpy.typing import NDArray

from ssc.capture.frame import FrameMetadata

_BLOCK_SIZE: Final = 2880
_CARD_SIZE: Final = 80
#: A ``COMMENT`` card's fixed prefix; the remaining 72 columns are free text.
_COMMENT_PREFIX: Final = "COMMENT "

#: ``ROWORDER`` is not read from the device — it documents an SSC authoring
#: choice (see module docstring), so unlike every other card here it is
#: always known and always written.
ROW_ORDER: Final = "TOP-DOWN"

#: numpy dtype name -> (BITPIX, BZERO). Only the dtypes a raw sensor readout
#: or a calibration frame plausibly arrives as; anything else is refused
#: rather than silently coerced. ``uint16`` — the Seestar's own readout width
#: — is stored via the standard FITS unsigned-16 convention: BITPIX=16
#: (signed) with BZERO=32768 as the pedestal, which ``ssc.skymap.fits``'s
#: reader already applies on the way back in.
_BITPIX_FOR_DTYPE: Final[dict[str, tuple[int, float]]] = {
    "uint8": (8, 0.0),
    "int16": (16, 0.0),
    "uint16": (16, 32768.0),
    "int32": (32, 0.0),
    "float32": (-32, 0.0),
    "float64": (-64, 0.0),
}

#: (BayerOffsetX % 2, BayerOffsetY % 2) -> the 2x2 tile name, reading the
#: canonical RGGB tile row-major from the array's own (0, 0) once the offset
#: is applied. See the module docstring for the worked derivation.
_BAYER_PATTERNS: Final[dict[tuple[int, int], str]] = {
    (0, 0): "RGGB",
    (1, 0): "GRBG",
    (0, 1): "GBRG",
    (1, 1): "BGGR",
}


class FitsWriteError(ValueError):
    """The frame or its metadata cannot be written as FITS."""


def bayer_pattern(offset_x: int | None, offset_y: int | None) -> str | None:
    """The ``BAYERPAT`` value for these offsets, or ``None`` if either
    offset is unknown — never guessed from only one of the pair."""
    if offset_x is None or offset_y is None:
        return None
    return _BAYER_PATTERNS[(offset_x % 2, offset_y % 2)]


#: Each Bayer pattern name's 2x2 tile, read row-major from the array's own
#: (0, 0) — the same convention the module docstring derives and
#: :data:`_BAYER_PATTERNS` names (e.g. "GRBG" = G R / B G).
_PATTERN_TILES: Final[dict[str, tuple[tuple[str, str], tuple[str, str]]]] = {
    "RGGB": (("R", "G"), ("G", "B")),
    "GRBG": (("G", "R"), ("B", "G")),
    "GBRG": (("G", "B"), ("R", "G")),
    "BGGR": (("B", "G"), ("G", "R")),
}


def _channel_mask(pattern: str, channel: str, height: int, width: int) -> NDArray[np.bool_]:
    """Which pixels of a ``(height, width)`` mosaic belong to ``channel``,
    tiling ``pattern``'s 2x2 unit across the whole array."""
    tile = _PATTERN_TILES[pattern]
    tile_mask = np.array(
        [
            [tile[0][0] == channel, tile[0][1] == channel],
            [tile[1][0] == channel, tile[1][1] == channel],
        ]
    )
    reps = (height // 2 + 1, width // 2 + 1)
    return np.tile(tile_mask, reps)[:height, :width]


def _neighborhood_sum(arr: NDArray[np.float64]) -> NDArray[np.float64]:
    """Sum of each pixel's 3x3 neighbourhood, edge-padded so the border gets
    a (smaller-support) answer rather than special-cased separately."""
    padded = np.pad(arr, 1, mode="edge")
    total = np.zeros_like(arr)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            total += padded[dy : dy + arr.shape[0], dx : dx + arr.shape[1]]
    return total


def debayer(mosaic: NDArray[np.float64], pattern: str) -> NDArray[np.float64]:
    """Reconstruct an RGB image from a single-plane Bayer mosaic.

    A normalized 3x3 box filter, not full bilinear interpolation: each
    channel is reconstructed by summing that channel's known samples in
    every pixel's 3x3 neighbourhood and dividing by how many of those 3x3
    cells actually carry that channel (2, 4, or a border-clipped fewer of
    that). This is display-preview quality — good enough for a capture
    thumbnail/full preview — not the demosaic a stacking tool would use;
    scientific processing works from the raw mosaic, not this reconstruction.

    ``mosaic`` must be a single 2-D plane (one exposure's raw readout, per
    :func:`write_fits_bytes`'s own contract). Raises :class:`FitsWriteError`
    for a pattern this module does not know.
    """
    if pattern not in _PATTERN_TILES:
        raise FitsWriteError(f"unknown Bayer pattern {pattern!r}")
    if mosaic.ndim != 2:
        raise FitsWriteError(f"debayer needs a single 2-D plane; got shape {mosaic.shape}")
    height, width = mosaic.shape
    channels = []
    for channel in ("R", "G", "B"):
        mask = _channel_mask(pattern, channel, height, width)
        weight = mask.astype(np.float64)
        summed = _neighborhood_sum(np.where(mask, mosaic, 0.0))
        weight_summed = _neighborhood_sum(weight)
        channels.append(np.where(weight_summed > 0, summed / np.maximum(weight_summed, 1.0), 0.0))
    return np.stack(channels, axis=-1)


def debayer_green(mosaic: NDArray[np.float64], pattern: str) -> NDArray[np.float64]:
    """Return exactly ``debayer(mosaic, pattern)[..., 1]`` without RGB allocation.

    Focus measurements consume only green; retain the same full-resolution
    interpolation and border handling rather than changing the HFR metric.
    """
    if pattern not in _PATTERN_TILES:
        raise FitsWriteError(f"unknown Bayer pattern {pattern!r}")
    if mosaic.ndim != 2:
        raise FitsWriteError(f"debayer needs a single 2-D plane; got shape {mosaic.shape}")
    mask = _channel_mask(pattern, "G", *mosaic.shape)
    summed = _neighborhood_sum(np.where(mask, mosaic, 0.0))
    weight_summed = _neighborhood_sum(mask.astype(np.float64))
    return np.where(weight_summed > 0, summed / np.maximum(weight_summed, 1.0), 0.0)


def _format_card(key: str, value: object) -> str:
    if len(key) > 8:
        raise FitsWriteError(f"header keyword {key!r} is longer than 8 characters")
    if isinstance(value, bool):
        value_str = "T" if value else "F"
    elif isinstance(value, str):
        value_str = "'" + _fits_ascii(value).replace("'", "''") + "'"
    elif isinstance(value, int):
        value_str = str(value)
    elif isinstance(value, float):
        value_str = repr(value)
    else:
        raise FitsWriteError(f"header value for {key!r} has unsupported type {type(value)!r}")
    card = f"{key.ljust(8)}= {value_str}"
    if len(card) > _CARD_SIZE:
        raise FitsWriteError(f"header card for {key!r} exceeds {_CARD_SIZE} characters")
    return card.ljust(_CARD_SIZE)


_FITS_ASCII_TRANSLATION = str.maketrans(
    {
        "\N{DEGREE SIGN}": " deg",
        "\N{MICRO SIGN}": "u",
        "\N{GREEK SMALL LETTER MU}": "u",
        "\N{GREEK SMALL LETTER ALPHA}": "alpha",
        "\N{GREEK SMALL LETTER BETA}": "beta",
        "\N{MULTIPLICATION SIGN}": "x",
        "\N{EN DASH}": "-",
        "\N{EM DASH}": "-",
        "\N{MINUS SIGN}": "-",
        "\N{LEFT SINGLE QUOTATION MARK}": "'",
        "\N{RIGHT SINGLE QUOTATION MARK}": "'",
        "\N{LEFT DOUBLE QUOTATION MARK}": '"',
        "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
        "\N{HORIZONTAL ELLIPSIS}": "...",
    }
)


def _fits_ascii(text: str) -> str:
    """Return a meaningful ASCII representation for one FITS string value.

    User-facing metadata remains Unicode in the catalogue; only its FITS
    header copy is transliterated. Common astronomy symbols get readable
    spellings, accents are decomposed, and an otherwise unrepresentable
    character becomes ``?`` instead of discarding a completed exposure.
    """
    normalized = unicodedata.normalize("NFKD", text.translate(_FITS_ASCII_TRANSLATION))
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return without_marks.encode("ascii", errors="replace").decode("ascii")


def _format_comments(text: str) -> list[str]:
    """``COMMENT`` cards carrying ``text``, wrapped to the card width.

    A FITS comment card is ``COMMENT `` plus at most 72 characters, so a
    sentence longer than that becomes several cards rather than being
    truncated -- a half-sentence about provenance is worse than none.
    """
    text = _fits_ascii(text)
    body_width = _CARD_SIZE - len(_COMMENT_PREFIX)
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > body_width:
            if current:
                lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return [f"{_COMMENT_PREFIX}{line}".ljust(_CARD_SIZE) for line in lines]


def _pad(data: bytes, fill: bytes) -> bytes:
    remainder = len(data) % _BLOCK_SIZE
    if remainder == 0:
        return data
    return data + fill * (_BLOCK_SIZE - remainder)


def write_fits_bytes(
    pixels: NDArray[np.generic], metadata: FrameMetadata, *, allow_cube: bool = False
) -> bytes:
    """Encode one raw frame as a FITS primary HDU: header cards, then data,
    each padded to a 2880-byte block per the standard.

    ``pixels`` must be 2-D (a single plane) — a raw or mosaiced sensor
    readout, never a stack of planes; DESIGN.md §9.5.2 concerns exactly one
    exposure per FITS file.
    """
    if pixels.ndim != 2 and not (allow_cube and pixels.ndim == 3):
        raise FitsWriteError(f"only a single 2-D plane is written; got shape {pixels.shape}")
    if pixels.ndim == 2:
        height, width = pixels.shape
        planes = None
    else:
        planes, height, width = pixels.shape
    dtype_name = pixels.dtype.name
    if dtype_name not in _BITPIX_FOR_DTYPE:
        raise FitsWriteError(f"unsupported pixel dtype {dtype_name!r}")
    bitpix, bzero = _BITPIX_FOR_DTYPE[dtype_name]

    cards: list[str] = [
        _format_card("SIMPLE", True),
        _format_card("BITPIX", bitpix),
        _format_card("NAXIS", pixels.ndim),
        _format_card("NAXIS1", int(width)),
        _format_card("NAXIS2", int(height)),
    ]
    if planes is not None:
        cards.append(_format_card("NAXIS3", int(planes)))
    if bzero:
        cards.append(_format_card("BZERO", bzero))
        cards.append(_format_card("BSCALE", 1.0))

    if metadata.date_obs is not None:
        cards.append(_format_card("DATE-OBS", metadata.date_obs.isoformat()))
    if metadata.date_end is not None:
        cards.append(_format_card("DATE-EXP", metadata.date_end.isoformat()))
    if metadata.subframe_x is not None:
        cards.append(_format_card("XORGSUBF", int(metadata.subframe_x)))
    if metadata.subframe_y is not None:
        cards.append(_format_card("YORGSUBF", int(metadata.subframe_y)))
    if metadata.bin_x is not None:
        cards.append(_format_card("XBINNING", int(metadata.bin_x)))
        cards.append(_format_card("CCDXBIN", int(metadata.bin_x)))
    if metadata.bin_y is not None:
        cards.append(_format_card("YBINNING", int(metadata.bin_y)))
        cards.append(_format_card("CCDYBIN", int(metadata.bin_y)))
    if metadata.exposure_s is not None:
        # Both spellings: EXPTIME is what Siril, PixInsight, ASTAP and the
        # Seestar's own files use, EXPOSURE is the older SBIG-era key some
        # tools still look for. Writing one and not the other silently loses
        # the exposure length for half the ecosystem.
        cards.append(_format_card("EXPTIME", float(metadata.exposure_s)))
        cards.append(_format_card("EXPOSURE", float(metadata.exposure_s)))
    if metadata.total_exposure_s is not None:
        cards.append(_format_card("TOTALEXP", float(metadata.total_exposure_s)))
    if metadata.stack_count is not None:
        cards.append(_format_card("STACKCNT", int(metadata.stack_count)))
        cards.append(_format_card("NCOMBINE", int(metadata.stack_count)))
    # SSC's richer frame classification cannot be reconstructed from ASCOM's
    # Light flag (dark and bias are both false), so carry it explicitly.
    cards.append(_format_card("IMAGETYP", metadata.frame_type.value))
    if metadata.optic_id is not None:
        cards.append(_format_card("OPTICID", metadata.optic_id))
    if metadata.flat_source is not None:
        cards.append(_format_card("FLATSRC", metadata.flat_source))
    if metadata.timing_from_ssc:
        cards.extend(
            _format_comments(
                "DATE-OBS/EXPTIME are SSC's own record of the exposure it "
                "commanded; this device does not implement "
                "Camera.LastExposureStartTime/LastExposureDuration."
            )
        )
    if metadata.dither_group_id is not None:
        cards.append(_format_card("DITHGRP", metadata.dither_group_id))
    if metadata.gain is not None:
        cards.append(_format_card("GAIN", int(metadata.gain)))
    if metadata.ccd_temp_c is not None:
        cards.append(_format_card("CCD-TEMP", float(metadata.ccd_temp_c)))
    pattern = bayer_pattern(metadata.bayer_offset_x, metadata.bayer_offset_y)
    if metadata.sensor_type is not None:
        cards.append(_format_card("SENSTYPE", metadata.sensor_type))
        if metadata.sensor_type != 2:
            # Mono offsets may be zero placeholders; other sensor families
            # cannot be interpreted with an RGGB-family demosaic.
            pattern = None
    if metadata.sensor_metadata_checked:
        cfa_state = (
            "mono" if metadata.sensor_type == 0
            else "unsupported" if metadata.sensor_type not in (None, 2)
            else "bayer" if pattern is not None
            else "unknown"
        )
        cards.append(_format_card("CFASTATE", cfa_state))
    if pattern is not None:
        cards.append(_format_card("BAYERPAT", pattern))
    cards.append(_format_card("ROWORDER", ROW_ORDER))
    if metadata.pixel_size_um is not None:
        cards.append(_format_card("XPIXSZ", float(metadata.pixel_size_um)))
    if metadata.pixel_size_y_um is not None:
        cards.append(_format_card("YPIXSZ", float(metadata.pixel_size_y_um)))
    if metadata.ra_deg is not None:
        cards.append(_format_card("RA", float(metadata.ra_deg)))
    if metadata.dec_deg is not None:
        cards.append(_format_card("DEC", float(metadata.dec_deg)))
    if metadata.ra_deg is not None and metadata.dec_deg is not None:
        cards.append(_format_card("RADESYS", "ICRS"))
        cards.append(_format_card("EQUINOX", 2000.0))
    if metadata.site_lat_deg is not None:
        cards.append(_format_card("SITELAT", float(metadata.site_lat_deg)))
    if metadata.site_lon_deg is not None:
        cards.append(_format_card("SITELONG", float(metadata.site_lon_deg)))
    if metadata.filter_name is not None:
        cards.append(_format_card("FILTER", metadata.filter_name))
    if metadata.object_name is not None:
        cards.append(_format_card("OBJECT", metadata.object_name))
    if metadata.sensor_name is not None:
        cards.append(_format_card("INSTRUME", metadata.sensor_name))
    if metadata.camera_offset is not None:
        cards.append(_format_card("OFFSET", int(metadata.camera_offset)))
    if metadata.max_adu is not None:
        cards.append(_format_card("MAXADU", float(metadata.max_adu)))
    if metadata.readout_mode is not None:
        cards.append(_format_card("READOUTM", int(metadata.readout_mode)))
    if metadata.focal_length_mm is not None:
        cards.append(_format_card("FOCALLEN", float(metadata.focal_length_mm)))
    if metadata.aperture_diameter_mm is not None:
        cards.append(_format_card("APTDIA", float(metadata.aperture_diameter_mm)))
    if metadata.focal_ratio is not None:
        cards.append(_format_card("APERTURE", float(metadata.focal_ratio)))
    if metadata.alignment_mode is not None:
        cards.append(_format_card("EQMODE", int(metadata.alignment_mode)))
    if metadata.wide_camera is not None:
        cards.append(_format_card("WIDECAM", int(metadata.wide_camera)))
    if metadata.focuser_position is not None:
        cards.append(_format_card("FOCUSPOS", int(metadata.focuser_position)))
    if metadata.focuser_temperature_c is not None:
        cards.append(_format_card("FOCTEMP", float(metadata.focuser_temperature_c)))
    if metadata.telescope_name is not None:
        cards.append(_format_card("TELESCOP", metadata.telescope_name))
    program = f"Photon Commander {metadata.ssc_version}"
    if metadata.device_firmware is not None:
        program += f" / firmware {metadata.device_firmware}"
    cards.append(_format_card("PROGRAM", program))
    cards.append("END".ljust(_CARD_SIZE))

    header_bytes = _pad("".join(cards).encode("ascii"), b" ")

    if bzero:
        shifted = pixels.astype(np.int32) - int(bzero)
        out_dtype = ">i2" if bitpix == 16 else f">i{bitpix // 8}"
        data = shifted.astype(out_dtype)
    else:
        out_dtype = {8: ">u1", 16: ">i2", 32: ">i4", -32: ">f4", -64: ">f8"}[bitpix]
        data = pixels.astype(out_dtype)
    data_bytes = _pad(data.tobytes(order="C"), b"\x00")

    return header_bytes + data_bytes


def write_fits_file(
    path: Path,
    pixels: NDArray[np.generic],
    metadata: FrameMetadata,
    *,
    allow_cube: bool = False,
) -> None:
    """Write ``pixels``/``metadata`` to ``path`` durably: build the whole
    file in memory, write it to a sibling temp file, fsync it, then
    ``os.replace`` it into place. ``os.replace`` is atomic on the same
    filesystem, so a crash between the write and the rename leaves an orphan
    ``.part`` file next to an unaffected (or absent) final path — never a
    truncated file where a complete one is expected.
    """
    payload = write_fits_bytes(pixels, metadata, allow_cube=allow_cube)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            written = handle.write(payload)
            if written != len(payload):
                raise FitsWriteError(f"short FITS write: stored {written!r} of {len(payload)} bytes")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


__all__ = [
    "ROW_ORDER",
    "FitsWriteError",
    "bayer_pattern",
    "debayer",
    "debayer_green",
    "write_fits_bytes",
    "write_fits_file",
]
