# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""HiPS tile addressing and HEALPix (nested) geometry. Pure; no I/O.

A HiPS (IVOA Hierarchical Progressive Survey) is a HEALPix pyramid of image
tiles served as files: ``Norder{order}/Dir{dir}/Npix{npix}.{ext}`` with
``dir = (npix // 10000) * 10000``, plus a ``properties`` file at the root
describing the pyramid (deepest order, tile format, tile width).

The HEALPix pieces here — nested ``ang2pix``, nested pixel centres, an
inclusive cone search — are a deliberately small transcription of the
reference ``healpix_base`` algorithms (Górski et al. 2005) in numpy.
``healpy`` is GPL and ``astropy-healpix`` is not a dependency, so this
module carries exactly what the tile cache needs and nothing more.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

import numpy as np
from numpy.typing import NDArray

#: A HiPS order this deep would be over 10 m pixels; nothing SSC serves goes
#: past order 12 (Pan-STARRS at ~0.25"/px) and the addressing arithmetic below
#: is only exercised to 2*order bits.
MAX_ORDER: Final = 29

_JRLL: Final = np.array([2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4], dtype=np.float64)
_JPLL: Final = np.array([1, 3, 5, 7, 0, 2, 4, 6, 1, 3, 5, 7], dtype=np.float64)

_HALFPI: Final = math.pi / 2

# IAU Galactic rotation in J2000 coordinates (same constants used by the
# vector frontend; rows take an equatorial unit vector to Galactic).
_EQ_TO_GAL: Final = np.array(
    [
        [-0.0548755604, -0.8734370902, -0.4838350155],
        [0.4941094279, -0.4448296300, 0.7469822445],
        [-0.8676661490, -0.1980763734, 0.4559837762],
    ],
    dtype=np.float64,
)


# -- addressing --------------------------------------------------------------- #


def tiles_in_order(order: int) -> int:
    """How many HEALPix pixels (and so HiPS tiles) exist at ``order``."""
    if not 0 <= order <= MAX_ORDER:
        raise ValueError(f"order must be in 0..{MAX_ORDER}, got {order}")
    return 12 * (1 << (2 * order))


def tile_dir(npix: int) -> int:
    """The ``Dir`` bucket a tile lives in: tiles are grouped 10 000 to a directory."""
    if npix < 0:
        raise ValueError(f"npix must not be negative, got {npix}")
    return (npix // 10_000) * 10_000


def tile_path(order: int, npix: int, ext: str) -> str:
    """``Norder{order}/Dir{dir}/Npix{npix}.{ext}`` — the path under a HiPS root."""
    if not 0 <= npix < tiles_in_order(order):
        raise ValueError(f"npix {npix} is out of range for order {order}")
    if not ext or "/" in ext or "." in ext:
        raise ValueError(f"ext must be a bare extension, got {ext!r}")
    return f"Norder{order}/Dir{tile_dir(npix)}/Npix{npix}.{ext}"


# -- HEALPix nested scheme ------------------------------------------------------ #


def _compress_bits(value: NDArray[np.uint64]) -> NDArray[np.uint64]:
    """Keep the even-position bits of each element and pack them together."""
    v = value & np.uint64(0x5555555555555555)
    v = (v | (v >> np.uint64(1))) & np.uint64(0x3333333333333333)
    v = (v | (v >> np.uint64(2))) & np.uint64(0x0F0F0F0F0F0F0F0F)
    v = (v | (v >> np.uint64(4))) & np.uint64(0x00FF00FF00FF00FF)
    v = (v | (v >> np.uint64(8))) & np.uint64(0x0000FFFF0000FFFF)
    v = (v | (v >> np.uint64(16))) & np.uint64(0x00000000FFFFFFFF)
    return v


def _spread_bits(value: int) -> int:
    """Inverse of :func:`_compress_bits` for one value: bit *i* moves to bit *2i*."""
    v = value & 0xFFFFFFFF
    v = (v | (v << 16)) & 0x0000FFFF0000FFFF
    v = (v | (v << 8)) & 0x00FF00FF00FF00FF
    v = (v | (v << 4)) & 0x0F0F0F0F0F0F0F0F
    v = (v | (v << 2)) & 0x3333333333333333
    v = (v | (v << 1)) & 0x5555555555555555
    return v


def pixel_centres(npix: NDArray[np.int64], order: int) -> NDArray[np.float64]:
    """Unit vectors (N, 3) of the centres of nested pixels ``npix`` at ``order``."""
    nside = 1 << order
    pix = npix.astype(np.uint64)
    face = (pix >> np.uint64(2 * order)).astype(np.int64)
    remainder = pix & np.uint64(nside * nside - 1)
    ix = _compress_bits(remainder).astype(np.float64)
    iy = _compress_bits(remainder >> np.uint64(1)).astype(np.float64)

    x = (ix + 0.5) / nside
    y = (iy + 0.5) / nside
    jr = _JRLL[face] - x - y
    north = jr < 1.0
    south = jr > 3.0
    nr = np.where(north, jr, np.where(south, 4.0 - jr, 1.0))
    z = np.where(
        north,
        1.0 - jr * jr / 3.0,
        np.where(south, (4.0 - jr) ** 2 / 3.0 - 1.0, (2.0 - jr) * 2.0 / 3.0),
    )
    tmp = _JPLL[face] * nr + x - y
    tmp = np.mod(tmp, 8.0)
    phi = (0.5 * _HALFPI * tmp) / nr
    sin_theta = np.sqrt(np.clip(1.0 - z * z, 0.0, 1.0))
    return np.stack([sin_theta * np.cos(phi), sin_theta * np.sin(phi), z], axis=1)


def ang2pix(ra_deg: float, dec_deg: float, order: int) -> int:
    """The nested HEALPix pixel at ``order`` containing (RA, Dec) in degrees."""
    if not 0 <= order <= MAX_ORDER:
        raise ValueError(f"order must be in 0..{MAX_ORDER}, got {order}")
    if not -90.0 <= dec_deg <= 90.0:
        raise ValueError(f"dec must be in -90..90, got {dec_deg}")
    nside = 1 << order
    z = math.sin(math.radians(dec_deg))
    za = abs(z)
    phi = math.radians(ra_deg % 360.0)
    tt = math.fmod(phi / _HALFPI, 4.0)

    if za <= 2.0 / 3.0:  # equatorial belt
        temp1 = nside * (0.5 + tt)
        temp2 = nside * z * 0.75
        jp = int(temp1 - temp2)
        jm = int(temp1 + temp2)
        ifp = jp >> order
        ifm = jm >> order
        face = (ifp | 4) if ifp == ifm else (ifp if ifp < ifm else ifm + 8)
        ix = jm & (nside - 1)
        iy = nside - (jp & (nside - 1)) - 1
    else:  # polar caps
        ntt = min(3, int(tt))
        tp = tt - ntt
        tmp = nside * math.sqrt(3.0 * (1.0 - za))
        jp = min(int(tp * tmp), nside - 1)
        jm = min(int((1.0 - tp) * tmp), nside - 1)
        if z >= 0:
            face, ix, iy = ntt, nside - jm - 1, nside - jp - 1
        else:
            face, ix, iy = ntt + 8, jp, jm
    return (face << (2 * order)) + _spread_bits(ix) + (_spread_bits(iy) << 1)


def max_pixel_radius_rad(order: int) -> float:
    """An upper bound on the centre-to-corner distance of any pixel at ``order``.

    The reference implementation's ``max_pixrad``: the worst case is a pixel
    on the equator/polar-cap boundary, and the bound is the distance between
    two specific points of it.
    """
    nside = 1 << order
    za = 2.0 / 3.0
    pa = math.pi / (4 * nside)
    t1 = (1.0 - 1.0 / nside) ** 2
    zb = 1.0 - t1 / 3.0
    va = (math.sqrt(1 - za * za) * math.cos(pa), math.sqrt(1 - za * za) * math.sin(pa), za)
    vb = (math.sqrt(max(0.0, 1 - zb * zb)), 0.0, zb)
    dot = va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2]
    return math.acos(max(-1.0, min(1.0, dot)))


def _unit_vector(ra_deg: float, dec_deg: float) -> NDArray[np.float64]:
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    return np.array([math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec)])


def equatorial_to_galactic(ra_deg: float, dec_deg: float) -> tuple[float, float]:
    """J2000 equatorial coordinates to Galactic longitude/latitude, degrees."""
    vector = _EQ_TO_GAL @ _unit_vector(ra_deg, dec_deg)
    lon = math.degrees(math.atan2(float(vector[1]), float(vector[0]))) % 360.0
    lat = math.degrees(math.asin(max(-1.0, min(1.0, float(vector[2])))))
    return lon, lat


def cone_tiles_by_order(
    ra_deg: float, dec_deg: float, radius_deg: float, max_order: int
) -> dict[int, list[int]]:
    """Every tile at each order ``0..max_order`` that may intersect the cone.

    Inclusive: the result is a superset of the exact intersection (a pixel is
    kept when its centre lies within ``radius + max_pixel_radius`` of the cone
    axis), and monotone in ``radius`` — a larger cone never loses a tile. The
    descent is hierarchical, so the cost tracks the cone's area, not the
    sphere's.
    """
    if not 0 <= max_order <= MAX_ORDER:
        raise ValueError(f"max_order must be in 0..{MAX_ORDER}, got {max_order}")
    if radius_deg < 0.0:
        raise ValueError(f"radius_deg must not be negative, got {radius_deg}")
    if not -90.0 <= dec_deg <= 90.0:
        raise ValueError(f"dec must be in -90..90, got {dec_deg}")
    axis = _unit_vector(ra_deg, dec_deg)
    radius = math.radians(min(radius_deg, 180.0))

    result: dict[int, list[int]] = {}
    candidates: NDArray[np.int64] = np.arange(12, dtype=np.int64)
    for order in range(max_order + 1):
        centres = pixel_centres(candidates, order)
        distance = np.arccos(np.clip(centres @ axis, -1.0, 1.0))
        keep = distance <= radius + max_pixel_radius_rad(order)
        candidates = candidates[keep]
        result[order] = candidates.tolist()
        if order < max_order:
            candidates = (candidates[:, None] * 4 + np.arange(4, dtype=np.int64)).ravel()
    return result


def cone_tiles(ra_deg: float, dec_deg: float, radius_deg: float, order: int) -> list[int]:
    """The tiles at exactly ``order`` that may intersect the cone (see above)."""
    return cone_tiles_by_order(ra_deg, dec_deg, radius_deg, order)[order]


# -- the ``properties`` file ------------------------------------------------------ #


@dataclass(frozen=True, slots=True)
class HipsProperties:
    """What a HiPS root's ``properties`` file says about the pyramid.

    ``None`` means the file did not say (CLAUDE.md invariant 7) — a survey
    whose deepest order is unknown is served as such, not defaulted.
    """

    hips_order: int | None
    hips_tile_format: tuple[str, ...]
    hips_tile_width: int | None
    hips_frame: str | None
    raw: dict[str, str] = field(default_factory=dict)


def parse_properties(text: str) -> HipsProperties:
    """Parse the ``key = value`` lines of a ``properties`` file. Comments start with ``#``."""
    raw: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        raw[key.strip()] = value.strip()

    def as_int(key: str) -> int | None:
        value = raw.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"properties: {key}={value!r} is not an integer") from exc

    formats = tuple(raw.get("hips_tile_format", "").split())
    return HipsProperties(
        hips_order=as_int("hips_order"),
        hips_tile_format=formats,
        hips_tile_width=as_int("hips_tile_width"),
        hips_frame=raw.get("hips_frame"),
        raw=raw,
    )


__all__ = [
    "MAX_ORDER",
    "HipsProperties",
    "ang2pix",
    "cone_tiles",
    "cone_tiles_by_order",
    "equatorial_to_galactic",
    "max_pixel_radius_rad",
    "parse_properties",
    "pixel_centres",
    "tile_dir",
    "tile_path",
    "tiles_in_order",
]
