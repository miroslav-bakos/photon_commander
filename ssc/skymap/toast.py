# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""TOAST tile addressing. Pure; no I/O.

WorldWide Telescope's own all-sky pyramids are TOAST (Tessellated
Octahedral Adaptive Subdivision Transform), not HEALPix: level ``L`` is a
``2^L x 2^L`` grid of tiles addressed ``(x, y)``. The STScI Pan-STARRS
pyramid that WWT draws today is one of these, laid out upstream as
``{level}/{y}/{y}_{x}.{ext}``.

The cache index keys every tile by ``(order, npix)`` whatever its scheme,
so a TOAST address is folded into one integer here: ``npix = y * 2^L + x``.
That is a bijection per level and nothing more — it carries no HEALPix
meaning, and no cone search exists for it (the projection is not equal-area
and the geometry is not worth transcribing for one survey).
"""

from __future__ import annotations

from typing import Final

#: WWT's own pyramids stop at level 12 (Pan-STARRS: ``TileLevels="12"``); the
#: packing arithmetic below is only exercised to 2*level bits.
MAX_LEVEL: Final = 20


def tiles_in_level(level: int) -> int:
    if not 0 <= level <= MAX_LEVEL:
        raise ValueError(f"level must be in 0..{MAX_LEVEL}, got {level}")
    return 1 << (2 * level)


def pack(level: int, x: int, y: int) -> int:
    """Fold ``(x, y)`` at ``level`` into the cache's ``npix`` integer."""
    size = 1 << level
    if not (0 <= x < size and 0 <= y < size):
        raise ValueError(f"({x}, {y}) is outside the {size}x{size} grid of level {level}")
    return y * size + x


def unpack(level: int, npix: int) -> tuple[int, int]:
    """Inverse of :func:`pack`: ``(x, y)``."""
    if not 0 <= npix < tiles_in_level(level):
        raise ValueError(f"npix {npix} is out of range for level {level}")
    size = 1 << level
    return npix % size, npix // size


def tile_path(level: int, x: int, y: int, ext: str) -> str:
    """``{level}/{y}/{y}_{x}.{ext}`` — the layout WWT's pyramids use upstream."""
    pack(level, x, y)  # range check
    if not ext or "/" in ext or "." in ext:
        raise ValueError(f"ext must be a bare extension, got {ext!r}")
    return f"{level}/{y}/{y}_{x}.{ext}"


__all__ = ["MAX_LEVEL", "pack", "tile_path", "tiles_in_level", "unpack"]
