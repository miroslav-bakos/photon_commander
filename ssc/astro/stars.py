# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Named bright stars: proper names for the sky map to draw and label.

Offline, bundled, same shape as :mod:`ssc.astro.catalog` and
:mod:`ssc.astro.constellations` — one file under ``data/`` is the source of
truth (``data/stars/bright_stars.csv``; see that file's own header for
provenance), read once by :func:`load_bright_stars`, no network, no other
I/O.

**Why this is not just more rows in ``ssc.astro.catalog``.** Bundled deep-sky
objects there have size, orientation and an object-type glyph; a star is a
point, always drawn the same way, and the source data is unrelated — the IAU
Working Group on Star Names' catalogue, CC BY 4.0, not the Messier/OpenNGC/
Sharpless lineage ``catalog.py``'s module docstring documents. Keeping stars
in their own module keeps ``catalog.py``'s merge-by-precedence machinery
(built for objects that can genuinely disagree on a field) from being pressed
into service for a dataset that never merges with anything.

**Licence.** ``data/stars/bright_stars.csv`` is CC BY 4.0 (THIRD_PARTY_NOTICES.md,
"Bundled named-bright-star table") — attribution required, no share-alike.
Its credit line is read from the file's own header, never assembled in
Python, via :func:`bright_stars_credit`, exactly as
:func:`ssc.astro.constellations.boundaries_credit` reads its file's.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from ssc.astro.catalog import MAX_CONE_LIMIT, MAX_CONE_RADIUS_DEG, cos_angular_separation
from ssc.astro.units import Deg

__all__ = [
    "MAX_CONE_LIMIT",
    "MAX_CONE_RADIUS_DEG",
    "BrightStar",
    "bright_stars_credit",
    "default_bright_stars_path",
    "load_bright_stars",
    "stars_within_cone",
]


@dataclass(frozen=True, slots=True)
class BrightStar:
    """One named bright star (``data/stars/bright_stars.csv``). Every field
    is always present — the bundled table only ever keeps rows the source
    gave a name, position, magnitude and designation for (CLAUDE.md: "None
    means unknown; it never means zero" — applied here by dropping the row
    at build time rather than shipping a partial one)."""

    name: str
    #: The source catalogue's own cross-reference, e.g. ``"HR 2491"``.
    designation: str
    ra_j2000_deg: Deg
    dec_j2000_deg: Deg
    magnitude: float


def default_bright_stars_path() -> Path:
    """``<repo>/data/stars/bright_stars.csv``. Bundled and committed, the
    same way :func:`ssc.astro.catalog.default_catalog_path` is — nothing to
    fetch at runtime."""
    return Path(__file__).resolve().parents[2] / "data" / "stars" / "bright_stars.csv"


_COLUMNS: Final = ("name", "designation", "ra_j2000_deg", "dec_j2000_deg", "magnitude")


def _data_lines(handle: Iterable[str]) -> list[str]:
    """Every non-comment, non-blank line, in order — ``'#'`` lines are the
    licence/provenance header, skipped the same way
    :func:`ssc.astro.constellations._data_lines` skips them."""
    lines: list[str] = []
    for line in handle:
        if line.startswith("#") or not line.strip():
            continue
        lines.append(line)
    return lines


def _parse_rows(path: Path) -> list[BrightStar]:
    with path.open(encoding="utf-8", newline="") as handle:
        lines = _data_lines(handle)
    if not lines:
        raise ValueError(f"{path}: no data rows")
    reader = csv.reader(lines)
    header = tuple(next(reader))
    if header != _COLUMNS:
        raise ValueError(f"{path}: unexpected columns {header!r}, want {_COLUMNS!r}")
    stars: list[BrightStar] = []
    seen_names: set[str] = set()
    for line_number, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(_COLUMNS):
            raise ValueError(f"{path}:{line_number}: expected {len(_COLUMNS)} columns, got {row!r}")
        name, designation, ra_text, dec_text, mag_text = row
        if name in seen_names:
            raise ValueError(f"{path}:{line_number}: duplicate star name {name!r}")
        seen_names.add(name)
        stars.append(
            BrightStar(
                name=name,
                designation=designation,
                ra_j2000_deg=Deg(float(ra_text)),
                dec_j2000_deg=Deg(float(dec_text)),
                magnitude=float(mag_text),
            )
        )
    return stars


@lru_cache(maxsize=4)
def load_bright_stars(path: Path | None = None) -> tuple[BrightStar, ...]:
    """Every bundled named bright star, read once and cached (the file never
    changes at runtime — CLAUDE.md invariant 3). ``path`` defaults to
    :func:`default_bright_stars_path`; overridable for tests, exactly like
    :func:`ssc.astro.catalog.load_catalog`'s ``paths`` argument."""
    return tuple(_parse_rows(path or default_bright_stars_path()))


_CREDIT_PREFIX: Final = "# credit:"


def _read_credit(path: Path) -> str:
    """The one-line ``# credit:`` header field, verbatim — the same rule
    :func:`ssc.astro.constellations._read_credit` applies to its own file."""
    with path.open(encoding="utf-8", newline="") as handle:
        for line in handle:
            if line.startswith(_CREDIT_PREFIX):
                return line.removeprefix(_CREDIT_PREFIX).strip()
            if not line.startswith("#"):
                break
    raise ValueError(f"{path}: no {_CREDIT_PREFIX!r} header line")


@lru_cache(maxsize=4)
def bright_stars_credit(path: Path | None = None) -> str:
    """The bundled star table's own credit line (e.g. ``"Star names: IAU
    Working Group on Star Names (WGSN), CC BY 4.0"``), read once from the
    file's header — never a literal string elsewhere, the same discipline
    :func:`ssc.astro.constellations.boundaries_credit` applies to its file."""
    return _read_credit(path or default_bright_stars_path())


def stars_within_cone(
    stars: Iterable[BrightStar], ra_deg: float, dec_deg: float, radius_deg: float, limit: int
) -> tuple[BrightStar, ...]:
    """Every star within ``radius_deg`` of ``(ra_deg, dec_deg)``, brightest
    (lowest magnitude) first, at most ``limit`` of them — the same cone-query
    convention as :meth:`ssc.astro.catalog.TargetCatalog.within_cone`
    (identical ``radius_deg``/``limit`` ceilings, reusing
    :func:`ssc.astro.catalog.cos_angular_separation` rather than
    reimplementing the separation test), except sorted by brightness, since
    a star has no size for "largest first" to mean anything: when the cap
    bites, the stars a viewer would actually recognize are the ones a map
    still draws.
    """
    if not 0.0 < radius_deg <= MAX_CONE_RADIUS_DEG:
        raise ValueError(f"radius_deg must be in (0, {MAX_CONE_RADIUS_DEG}], got {radius_deg}")
    if not 1 <= limit <= MAX_CONE_LIMIT:
        raise ValueError(f"limit must be in [1, {MAX_CONE_LIMIT}], got {limit}")
    cos_max_val = math.cos(math.radians(radius_deg))
    hits = [
        star
        for star in stars
        if cos_angular_separation(
            ra_deg, dec_deg, float(star.ra_j2000_deg), float(star.dec_j2000_deg)
        )
        >= cos_max_val
    ]
    hits.sort(key=lambda s: s.magnitude)
    return tuple(hits[:limit])
