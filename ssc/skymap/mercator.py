# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Web Mercator tile addressing. Pure; no I/O.

Legacy Surveys' own tile server (`legacysurvey.org/viewer/<id>/<ver>/<z>/<x>/<y>.jpg`)
is standard spherical Web Mercator, not HiPS/HEALPix and not WWT's TOAST: level (zoom)
``z`` is a ``2^z x 2^z`` grid of 256px tiles addressed ``(x, y)`` with ``y`` increasing
*south* (top-left origin) but **``x`` increasing *west*, mirrored relative to a standard
Google/OSM/Bing "XYZ" slippy map**. Confirmed from the server's own published source,
``https://github.com/legacysurvey/decals-web`` `map/utils.py`'s ``get_tile_wcs``: each
tile's world-pixel reference is built as ``rx = zoomscale/2 - x`` (note the *minus*: the
world-pixel column falls as the tile index ``x`` rises) before being handed to
`astrometry.util.util.anwcs_create_mercator_2(180., 0., rx*W+0.5, ...)` (standard
spherical Mercator, centred RA=180 Dec=0) — read 2026-09-07.

**This module got that mirror backwards until 2026-09-09** — the same bug independently
caught in the frontend's `imagerySpike.ts` (commit f1bda71): a plausible ``x = ra/360``
formula ("x increasing east", the standard-XYZ assumption) that was never checked
against the real upstream. Verified directly against the live tile server: at zoom 8,
M31 (RA 10.6847, Dec +41.269) is tile ``(x=248, y=95)`` —
``unwise-neo7/1/8/248/95.jpg`` downloads and shows the galaxy filling the frame — while
the naive ``ra/360`` formula's ``(x=7, y=95)`` is an unrelated star field one tile-row
over. A second, independent point confirms the direction rather than a coincidence at
one spot: M42 (RA 83.8208, Dec -5.3833) is ``(x=196, y=131)``, the Orion Nebula in
frame; ``(x=59, y=131)`` (the naive formula's answer) is not.

WWT's own ``MercatorTile`` (``@wwtelescope/engine``, `esm/mercator_tile.js`,
``computeBoundingSphere``) uses the identical convention: tile row ``tileY`` spans
declination from ``absoluteMetersToLatAtZoom(tileY*256, level)`` down to
``absoluteMetersToLatAtZoom((tileY+1)*256, level)``, and that formula's ``metersY =
20037508 - Y*metersPerPixel`` decreases (moves south) as the pixel row ``Y`` increases —
worked out by hand from the engine's own arithmetic (`web/node_modules/@wwtelescope/engine
/src/index.js`, 0.9.x as vendored here) 2026-09-07: level-1 tile ``(x, y=0)`` spans
Dec 0..+85.05, tile ``(x, y=1)`` spans Dec -85.05..0. That is: **no y-flip is needed
between the engine's tile row and Legacy Surveys' own** — this module, the proxy and the
engine all agree on the same row, so serving is a direct pass-through, never a resampler.

The cache index keys every tile by ``(order, npix)`` whatever its scheme (see
``toast.py`` for the sibling TOAST module this one mirrors), so a Mercator address is
folded into one integer the same way TOAST's is: ``npix = y * 2^z + x``. That bijection
carries no cartographic meaning beyond "this survey's own ``(z, x, y)``" — Mercator's
grid is the same shape as TOAST's per level, but the *tile path* upstream is different
(TOAST: ``{level}/{y}/{y}_{x}.ext``; Mercator/XYZ: ``{z}/{x}/{y}.ext``, Legacy Surveys'
own layout), so the two are kept as separate modules rather than one generic one.
"""

from __future__ import annotations

import math
from typing import Final

#: Legacy Surveys' own viewer declares `maxZoom = 16` (`legacysurvey.org/viewer/`,
#: inline script, read 2026-09-07); a live fetch of `ls-dr11` at z=16 returned a tile.
#: `minZoom = 1` is a UI limit only — z=0 (the single all-sky tile) was verified live to
#: return 200 too, so this module (like `toast.py`) starts its range at 0.
MAX_ZOOM: Final = 16

#: The standard Web Mercator clipping latitude (`2*atan(exp(pi)) - pi/2` in
#: degrees) — the projection is undefined at the poles, so every slippy-map
#: implementation (OSM, Google, WWT's own `MercatorTile`) clips there. A
#: declination request beyond this is clamped rather than refused: a cone
#: whose radius reaches past it still wants every real tile below it.
MAX_LAT_DEG: Final = 85.05112878


def tiles_in_zoom(zoom: int) -> int:
    if not 0 <= zoom <= MAX_ZOOM:
        raise ValueError(f"zoom must be in 0..{MAX_ZOOM}, got {zoom}")
    return 1 << (2 * zoom)


def pack(zoom: int, x: int, y: int) -> int:
    """Fold ``(x, y)`` at ``zoom`` into the cache's ``npix`` integer."""
    size = 1 << zoom
    if not (0 <= x < size and 0 <= y < size):
        raise ValueError(f"({x}, {y}) is outside the {size}x{size} grid of zoom {zoom}")
    return y * size + x


def unpack(zoom: int, npix: int) -> tuple[int, int]:
    """Inverse of :func:`pack`: ``(x, y)``."""
    if not 0 <= npix < tiles_in_zoom(zoom):
        raise ValueError(f"npix {npix} is out of range for zoom {zoom}")
    size = 1 << zoom
    return npix % size, npix // size


def tile_path(zoom: int, x: int, y: int, ext: str) -> str:
    """``{z}/{x}/{y}.{ext}`` — Legacy Surveys' own tile layout upstream."""
    pack(zoom, x, y)  # range check
    if not ext or "/" in ext or "." in ext:
        raise ValueError(f"ext must be a bare extension, got {ext!r}")
    return f"{zoom}/{x}/{y}.{ext}"


# -- cone addressing (target-field prefetch, ssc/skymap/service.py) --------------- #


def _x_frac(ra_deg: float) -> float:
    """RA, as a fraction of the map's width — ``x = frac * 2^zoom``.

    **Mirrored**, not ``ra / 360``: see the module docstring — Legacy Surveys'
    own tile builder puts increasing tile-index ``x`` at *decreasing*
    world-pixel column (``rx = zoomscale/2 - x``). RA=180 is still the map's
    centre (``frac = 0.5``), but ``frac`` *decreases* as RA increases, rather
    than the other way round — ``x`` increases to the west, not the east.
    ``% 1.0`` at the end folds the ``ra_deg % 360.0 == 0`` case (frac would
    otherwise land on the excluded upper bound ``1.0`` rather than the
    included ``0.0``, which is the same seam point on a cyclic map)."""
    return (1.0 - (ra_deg % 360.0) / 360.0) % 1.0


def _y_frac(dec_deg: float) -> float:
    """Declination, as a fraction of the map's height — ``y = frac * 2^zoom``,
    increasing south (see the module docstring). Clipped to
    :data:`MAX_LAT_DEG`, same as every other Web Mercator implementation."""
    lat = max(-MAX_LAT_DEG, min(MAX_LAT_DEG, dec_deg))
    return (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0


def cone_tiles_by_zoom(
    ra_deg: float, dec_deg: float, radius_deg: float, max_zoom: int
) -> dict[int, list[int]]:
    """Every tile at each zoom ``0..max_zoom`` that may intersect the cone.

    Inclusive and monotone in ``radius_deg`` like ``hips.cone_tiles_by_order``
    (a larger cone never loses a tile), computed directly from the cone's
    RA/Dec bounding band rather than by hierarchical descent — Mercator's
    grid is flat (no parent/child pixel tree to walk), so the bounding box in
    (x, y) fraction space at one zoom is independent of every other zoom.

    A one-tile margin is added on every side to absorb Mercator's own
    non-uniform scale within the box (RA spacing tightens away from the
    equator; a straight linear interpolation across the box's own Dec span
    can undercount by a fraction of a tile at the edge), and RA wraps at
    0h/24h by reducing every candidate ``x`` modulo the zoom's tile row width
    rather than special-casing the seam.
    """
    if not 0 <= max_zoom <= MAX_ZOOM:
        raise ValueError(f"max_zoom must be in 0..{MAX_ZOOM}, got {max_zoom}")
    if radius_deg < 0.0:
        raise ValueError(f"radius_deg must not be negative, got {radius_deg}")
    if not -90.0 <= dec_deg <= 90.0:
        raise ValueError(f"dec must be in -90..90, got {dec_deg}")

    dec_min = max(dec_deg - radius_deg, -90.0)
    dec_max = min(dec_deg + radius_deg, 90.0)
    #: Full RA sweep needed once the cone reaches a pole (every meridian
    #: passes through it) or is simply not a small circle any more.
    spans_every_ra = dec_max >= 90.0 - 1e-9 or dec_min <= -90.0 + 1e-9 or radius_deg >= 180.0

    result: dict[int, list[int]] = {}
    for zoom in range(max_zoom + 1):
        size = 1 << zoom
        y_top = max(0, math.floor(_y_frac(dec_max) * size) - 1)
        y_bottom = min(size - 1, math.floor(_y_frac(dec_min) * size) + 1)

        if spans_every_ra:
            x_candidates: range = range(size)
        else:
            #: The RA half-width of the cone at the declination band's most
            #: poleward edge — Mercator's RA spacing there is the tightest in
            #: the box, so using it (rather than the cone's own centre
            #: declination) keeps the box a superset at every row.
            limiting_dec = min(89.9, max(abs(dec_min), abs(dec_max)))
            cos_limit = max(math.cos(math.radians(limiting_dec)), 1e-6)
            sin_ratio = min(1.0, math.sin(math.radians(radius_deg)) / cos_limit)
            half_width_deg = math.degrees(math.asin(sin_ratio))
            x_center = _x_frac(ra_deg) * size
            half_width_tiles = half_width_deg / 360.0 * size + 1.0
            x_candidates = range(
                math.floor(x_center - half_width_tiles), math.ceil(x_center + half_width_tiles) + 1
            )

        tiles = {pack(zoom, x % size, y) for x in x_candidates for y in range(y_top, y_bottom + 1)}
        result[zoom] = sorted(tiles)
    return result


__all__ = [
    "MAX_LAT_DEG",
    "MAX_ZOOM",
    "cone_tiles_by_zoom",
    "pack",
    "tile_path",
    "tiles_in_zoom",
    "unpack",
]
