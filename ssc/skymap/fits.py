# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""A minimal FITS reader: header cards and one image HDU. Pure; no I/O.

Lifted and generalised from ``scripts/build-wham-hips.py`` (which read one
fixed ``BITPIX=-32`` plate-carree map at build time) because target-field
cutouts arrive over the network as arbitrary single- or multi-plane FITS
images — IRSA's AllWISE Atlas cutouts, SDSS SAS frames, Legacy Surveys'
``grz`` cubes — and now have to be parsed and their WCS extracted at request
time. No PIL/astropy dependency is added for it (CLAUDE.md: check the deps
before reaching for one); this reads exactly the subset of the standard SSC
ever writes to disk or hands to a client.

Supported: ``NAXIS`` 2 (single-plane) or 3 (a stack of planes, e.g. Legacy
Surveys' g/r/z cube), ``BITPIX`` in ``{8, 16, 32, -32, -64}``, ``BZERO``/
``BSCALE`` applied on read. Not supported, and refused rather than guessed
at: multi-HDU files (only the primary HDU is read), compressed-image HDUs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

#: FITS header/data blocks are always a multiple of this many bytes.
_BLOCK_SIZE = 2880
_CARD_SIZE = 80

#: ``BITPIX`` -> numpy dtype, big-endian as FITS mandates.
_DTYPES: dict[int, str] = {
    8: ">u1",
    16: ">i2",
    32: ">i4",
    -32: ">f4",
    -64: ">f8",
}


class FitsError(ValueError):
    """The bytes are not a FITS image this reader understands."""


def _parse_card_value(raw: str) -> object:
    value = raw.split(" /", 1)[0].strip() if not raw.lstrip().startswith("'") else raw
    if value.startswith("'"):
        end = value.find("'", 1)
        return value[1:end].strip()
    if value in {"T", "F"}:
        return value == "T"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_header(raw: bytes) -> tuple[dict[str, object], int]:
    """The header cards, and the byte offset the data starts at.

    ``raw`` is the whole file (or enough of it to hold the header); reading
    only the header is not worth a second code path for cutout files, which
    are at most a few megabytes.
    """
    header: dict[str, object] = {}
    offset = 0
    while True:
        if offset + _CARD_SIZE > len(raw):
            raise FitsError("no END card before the end of the buffer")
        card = raw[offset : offset + _CARD_SIZE].decode("ascii", errors="replace")
        offset += _CARD_SIZE
        key = card[:8].strip()
        if key == "END":
            break
        if key in {"COMMENT", "HISTORY", ""} or card[8:10] != "= ":
            continue
        header[key] = _parse_card_value(card[10:])
    data_start = math.ceil(offset / _BLOCK_SIZE) * _BLOCK_SIZE
    return header, data_start


@dataclass(frozen=True, slots=True)
class FitsImage:
    """One primary-HDU image: ``data`` is ``(height, width)`` for ``NAXIS=2``
    or ``(planes, height, width)`` for ``NAXIS=3``, values already scaled by
    ``BZERO``/``BSCALE``."""

    data: NDArray[np.float64]
    header: dict[str, object] = field(default_factory=dict)

    @property
    def naxis(self) -> int:
        return int(self.header.get("NAXIS", self.data.ndim))  # type: ignore[call-overload,no-any-return]

    @property
    def width(self) -> int:
        return int(self.data.shape[-1])

    @property
    def height(self) -> int:
        return int(self.data.shape[-2])

    @property
    def planes(self) -> int:
        return self.data.shape[0] if self.data.ndim == 3 else 1


def read_fits_image(raw: bytes) -> FitsImage:
    """Parse the primary HDU. Refuses anything not a plain 2-D or 3-D image."""
    header, data_start = parse_header(raw)
    bitpix = header.get("BITPIX")
    naxis = header.get("NAXIS")
    if not isinstance(bitpix, int) or bitpix not in _DTYPES:
        raise FitsError(f"unsupported BITPIX {bitpix!r}")
    if naxis not in (2, 3):
        raise FitsError(f"unsupported NAXIS {naxis!r}; only 2 or 3 is read")
    width = int(header["NAXIS1"])  # type: ignore[call-overload]
    height = int(header["NAXIS2"])  # type: ignore[call-overload]
    planes = int(header["NAXIS3"]) if naxis == 3 else 1  # type: ignore[call-overload]
    count = width * height * planes
    dtype = _DTYPES[bitpix]
    available = (len(raw) - data_start) // np.dtype(dtype).itemsize
    if available < count:
        raise FitsError(f"truncated FITS data: need {count} pixels of {dtype}, have {available}")
    flat = np.frombuffer(raw, dtype=dtype, count=count, offset=data_start).astype(np.float64)
    shape = (planes, height, width) if naxis == 3 else (height, width)
    data = flat.reshape(shape)
    bzero = float(header.get("BZERO", 0.0))  # type: ignore[arg-type]
    bscale = float(header.get("BSCALE", 1.0))  # type: ignore[arg-type]
    if bzero or bscale != 1.0:
        data = data * bscale + bzero
    if naxis == 2:
        return FitsImage(data, header)
    return FitsImage(data, header)


@dataclass(frozen=True, slots=True)
class ImageWCS:
    """The linear (TAN/SIN-tangent-plane) WCS a cutout piece carries, as the
    plain numbers the API hands the browser — CRPIX/CRVAL/CD, never an
    opaque astropy object, because the client (not this process) draws it."""

    crpix1: float
    crpix2: float
    crval1: float
    crval2: float
    cd1_1: float
    cd1_2: float
    cd2_1: float
    cd2_2: float
    ctype1: str
    ctype2: str


def extract_wcs(header: dict[str, object]) -> ImageWCS:
    """CRPIX/CRVAL plus a CD matrix, built from ``CDn_n`` when present or
    from ``CDELTn``/``CROTA2`` otherwise (the older convention SDSS's SAS
    frames and IRSA's IBE cutouts both still carry on some products).

    Raises :class:`FitsError` if the header has neither — a piece with no
    usable WCS is not served with a fabricated one (CLAUDE.md invariant 7).
    """
    required = ("CRPIX1", "CRPIX2", "CRVAL1", "CRVAL2")
    missing = [key for key in required if key not in header]
    if missing:
        raise FitsError(f"header has no usable WCS: missing {missing}")
    crpix1 = float(header["CRPIX1"])  # type: ignore[arg-type]
    crpix2 = float(header["CRPIX2"])  # type: ignore[arg-type]
    crval1 = float(header["CRVAL1"])  # type: ignore[arg-type]
    crval2 = float(header["CRVAL2"])  # type: ignore[arg-type]
    ctype1 = str(header.get("CTYPE1", "RA---TAN"))
    ctype2 = str(header.get("CTYPE2", "DEC--TAN"))
    if "CD1_1" in header:
        cd1_1 = float(header["CD1_1"])  # type: ignore[arg-type]
        cd1_2 = float(header.get("CD1_2", 0.0))  # type: ignore[arg-type]
        cd2_1 = float(header.get("CD2_1", 0.0))  # type: ignore[arg-type]
        cd2_2 = float(header["CD2_2"])  # type: ignore[arg-type]
    elif "CDELT1" in header and "CDELT2" in header:
        cdelt1 = float(header["CDELT1"])  # type: ignore[arg-type]
        cdelt2 = float(header["CDELT2"])  # type: ignore[arg-type]
        crota2 = math.radians(float(header.get("CROTA2", 0.0)))  # type: ignore[arg-type]
        cos_r, sin_r = math.cos(crota2), math.sin(crota2)
        cd1_1 = cdelt1 * cos_r
        cd1_2 = -cdelt2 * sin_r
        cd2_1 = cdelt1 * sin_r
        cd2_2 = cdelt2 * cos_r
    else:
        raise FitsError("header has no usable WCS: neither CDn_n nor CDELTn present")
    return ImageWCS(
        crpix1=crpix1,
        crpix2=crpix2,
        crval1=crval1,
        crval2=crval2,
        cd1_1=cd1_1,
        cd1_2=cd1_2,
        cd2_1=cd2_1,
        cd2_2=cd2_2,
        ctype1=ctype1,
        ctype2=ctype2,
    )


__all__ = [
    "FitsError",
    "FitsImage",
    "ImageWCS",
    "extract_wcs",
    "parse_header",
    "read_fits_image",
]
