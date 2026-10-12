# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Which of the 88 IAU constellation regions a point on the sky falls in.

Offline, bundled, exactly the same shape as :mod:`ssc.astro.catalog`: one
file under ``data/`` is the source of truth (``data/constellations/boundaries.csv``,
see that file's own header for provenance — Delporte 1930, via a from-scratch
transcription, never VizieR VI/49/VI/42; THIRD_PARTY_NOTICES.md "Constellations
— gate reading of 2026-09-07"), read once by :func:`load_boundaries`, with no
network and no other I/O.

**Epoch.** Delporte defined the 88 boundaries as segments of meridians and
parallels in the equinox of B1875.0 — not J2000. Rather than reshape ~1500
boundary vertices into curves (precessing a straight RA/Dec edge to another
epoch turns it into a curve, which a straight-line polygon can only
approximate), :func:`constellation_at` precesses the *query point* from
J2000 to B1875 instead, then tests it against the exact, still axis-aligned
edges Delporte drew. Because precession is an invertible, angle-preserving
map of the sphere to itself, "which region contains a J2000 point" and
"which region contains its B1875 image" are the same question — this is
exact, not an approximation, and it is the same technique standard
constellation-lookup implementations use.

The precession applied is pure precession only (Capitaine et al. 2003 / IAU
2006, via :func:`skyfield.precessionlib.compute_precession` — a plain
function of a Julian date, no ephemeris kernel, no timescale files, no I/O),
with neither nutation nor frame bias. That is deliberate, not an
approximation for convenience: Delporte's boundaries are defined against the
*mean* equinox of B1875.0, and nutation is exactly the (arcsecond-level)
correction from the mean equinox to the *true* equinox of a date — applying
it here would move the query point away from the frame the boundaries
actually use.

**Purity.** No ``datetime.now()`` — B1875.0 is a fixed constant, not "now".
The one file read happens once per process via :func:`functools.lru_cache`
on :func:`load_boundaries`.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from skyfield.precessionlib import compute_precession

from ssc.astro.units import Deg, Hours, hours_to_deg, wrap_deg_360

__all__ = [
    "BoundaryVertexJ2000",
    "ConstellationBoundary",
    "ConstellationBoundaryJ2000",
    "UnknownConstellationError",
    "boundaries_credit",
    "boundaries_j2000",
    "constellation_at",
    "default_boundaries_path",
    "load_boundaries",
]


class UnknownConstellationError(LookupError):
    """The 88 loaded regions did not resolve the point to exactly one of
    them. The 88 regions tile the whole sphere with no gaps or overlaps
    (validated by ``tests/astro/test_constellations.py``), so this should be
    unreachable for any valid ``(ra, dec)`` — raised rather than silently
    picking a region or returning ``None``, so a future data regression is
    loud, not a quietly wrong answer (CLAUDE.md invariant 7)."""


@dataclass(frozen=True, slots=True)
class BoundaryVertex:
    """One polygon vertex, in the source epoch (B1875.0 — see the module
    docstring for why this module never converts these to J2000)."""

    ra_b1875_deg: Deg
    dec_b1875_deg: Deg


@dataclass(frozen=True, slots=True)
class ConstellationBoundary:
    """One IAU constellation region. ``rings`` has one entry for every
    constellation except Serpens ("SER"), whose boundary Delporte's own
    tables split into two disjoint pieces (Caput and Cauda, either side of
    Ophiuchus) sharing one abbreviation; a point is in the region if it is
    in *any* of its rings. Each ring is a closed polygon — the edge from its
    last vertex back to its first is implied, never a duplicated row."""

    abbreviation: str
    name: str
    rings: tuple[tuple[BoundaryVertex, ...], ...]


@dataclass(frozen=True, slots=True)
class BoundaryVertexJ2000:
    """One vertex of a :func:`boundaries_j2000` ring, precessed out of the
    source B1875.0 epoch. Not exact in the way :class:`BoundaryVertex`'s
    B1875 edges are — precessing a straight constant-RA/constant-Dec B1875
    edge produces a curve, not another straight edge, in J2000 (module
    docstring). A frontend drawing these as straight polyline segments is
    an approximation of that curve, invisible at any zoom this map allows
    over the timescale precession moves anything (module docstring's own
    "effectively never" framing); it is not the exact tiling
    :func:`constellation_at` relies on, which is why that function keeps
    precessing the query point instead of ever calling this."""

    ra_j2000_deg: Deg
    dec_j2000_deg: Deg


@dataclass(frozen=True, slots=True)
class ConstellationBoundaryJ2000:
    """:class:`ConstellationBoundary`, precessed to J2000 — see
    :func:`boundaries_j2000`."""

    abbreviation: str
    name: str
    rings: tuple[tuple[BoundaryVertexJ2000, ...], ...]


def default_boundaries_path() -> Path:
    """``<repo>/data/constellations/boundaries.csv``. Bundled and committed,
    the same way :func:`ssc.astro.catalog.default_catalog_path` is —
    nothing to fetch at runtime."""
    return Path(__file__).resolve().parents[2] / "data" / "constellations" / "boundaries.csv"


_COLUMNS: Final = ("abbreviation", "name", "ring", "ra_b1875_deg", "dec_b1875_deg")


def _data_lines(handle: Iterable[str]) -> list[str]:
    """Every non-comment, non-blank line, in order. ``'#'`` lines are the
    licence/provenance header (see the data file itself); skipped here the
    same way :func:`ssc.astro.catalog._csv_lines` skips them."""
    lines: list[str] = []
    for line in handle:
        if line.startswith("#") or not line.strip():
            continue
        lines.append(line)
    return lines


def _parse_rows(path: Path) -> list[tuple[str, str, int, float, float]]:
    with path.open(encoding="utf-8", newline="") as handle:
        lines = _data_lines(handle)
    if not lines:
        raise ValueError(f"{path}: no data rows")
    reader = csv.reader(lines)
    header = tuple(next(reader))
    if header != _COLUMNS:
        raise ValueError(f"{path}: unexpected columns {header!r}, want {_COLUMNS!r}")
    rows: list[tuple[str, str, int, float, float]] = []
    for line_number, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(_COLUMNS):
            raise ValueError(f"{path}:{line_number}: expected {len(_COLUMNS)} columns, got {row!r}")
        abbreviation, name, ring_text, ra_text, dec_text = row
        rows.append((abbreviation, name, int(ring_text), float(ra_text), float(dec_text)))
    return rows


_CREDIT_PREFIX: Final = "# credit:"


def _read_credit(path: Path) -> str:
    """The one-line ``# credit:`` header field, verbatim — the same
    "read the licence text out of the data file, never assemble it in
    Python" rule :func:`ssc.astro.catalog._read_csv` applies to its own
    ``# source:`` line, applied here to the shorter display form the API
    hands the frontend (:func:`boundaries_credit`)."""
    with path.open(encoding="utf-8", newline="") as handle:
        for line in handle:
            if line.startswith(_CREDIT_PREFIX):
                return line.removeprefix(_CREDIT_PREFIX).strip()
            if not line.startswith("#"):
                break
    raise ValueError(f"{path}: no {_CREDIT_PREFIX!r} header line")


@lru_cache(maxsize=4)
def boundaries_credit(path: Path | None = None) -> str:
    """The constellation-boundary data's own credit line (e.g.
    ``"Constellation boundaries: E. Delporte 1930, via Pierre Barbier, CC
    BY-SA 4.0"``), read once from the bundled file's header — never a
    literal string elsewhere, the same discipline
    :attr:`ssc.astro.catalog.CatalogObject.source` applies to catalogue
    credits."""
    return _read_credit(path or default_boundaries_path())


def _short_delta_deg(ra1: Deg, ra2: Deg) -> float:
    """Signed shortest angular step from ``ra1`` to ``ra2``, degrees,
    ``(-180, 180]``. Every real boundary edge is far shorter than a
    half-circle, so "the short way" is always the way Delporte drew it —
    this is how a wraparound edge (e.g. one crossing RA 24h/0h) is told
    apart from an ordinary one, without needing the edge tagged specially
    in the data file."""
    delta = (float(ra2) - float(ra1)) % 360.0
    if delta > 180.0:
        delta -= 360.0
    return delta


def _ring_encloses_pole(ring: tuple[BoundaryVertex, ...]) -> Literal["north", "south", None]:
    """Exactly two of the 88 rings — Ursa Minor and Octans — enclose a
    celestial pole; every other ring is a simple bounded region that does
    not. This is a structural fact of the ring's own path, computed once
    here rather than hardcoded: walking the ring's edges and summing each
    one's :func:`_short_delta_deg` totals +-360 deg iff the ring winds all
    the way around the RA axis (i.e. encloses a pole), 0 deg otherwise —
    and which pole is decided by which hemisphere the ring's vertices sit
    in. :func:`_point_in_ring` needs this because a point in a pole-
    enclosing ring can be *between* the ring's highest-declination edge and
    the pole itself, where nothing in the vertex list "closes" the top."""
    total = 0.0
    n = len(ring)
    for i in range(n):
        total += _short_delta_deg(ring[i].ra_b1875_deg, ring[(i + 1) % n].ra_b1875_deg)
    if abs(total) < 300.0:
        return None
    mean_dec = sum(float(v.dec_b1875_deg) for v in ring) / n
    return "north" if mean_dec > 0.0 else "south"


def _ra_in_span(ra0: float, ra1: Deg, ra2: Deg) -> bool:
    """Whether ``ra0`` lies on the short-way arc from ``ra1`` to ``ra2``,
    **half-open**: includes ``ra1``, excludes ``ra2``. This half-open
    convention is the deterministic tie-break for a point exactly on a
    boundary — see :func:`_point_in_ring`."""
    delta = _short_delta_deg(ra1, ra2)
    start = ra1
    if delta < 0.0:
        start, delta = ra2, -delta
    offset = (ra0 - float(start)) % 360.0
    return offset < delta


def _point_in_ring(ra0: float, dec0: float, ring: tuple[BoundaryVertex, ...]) -> bool:
    """Even-odd (ray-casting) containment test for one ring, exact because
    every edge is a true constant-RA or constant-Dec segment in this frame
    — no polygon-chord approximation of a curve anywhere.

    The ray is cast in the declination direction, at fixed RA = ``ra0``,
    counting crossed constant-Dec ("parallel") edges only (a fixed-RA ray
    can only ever cross those). Its direction is *away from* whichever pole
    this ring encloses, if any (:func:`_ring_encloses_pole`): casting a ray
    toward an enclosed pole is invalid — nothing in the vertex list "closes"
    a pole-enclosing ring's top, so a ray sent that way would silently
    undercount. Casting away from it always terminates by crossing the
    ring's real boundary, for a ring that encloses a pole or not.

    **Tie-break rule** (a point exactly on a shared edge resolves to
    exactly one deterministic side, never both, never neither): an edge is
    counted as crossed only when it lies *strictly* on the ray's forward
    side of ``dec0`` (so a point sitting exactly on a parallel edge never
    counts that edge for either neighbour), and :func:`_ra_in_span` treats
    an edge's RA range as half-open (a point exactly on the RA boundary
    between two parallel edges belongs to the one that starts there, not
    the one that ends there). Together these make the function
    single-valued at every edge and vertex of the data actually shipped.
    """
    ray_toward_north = _ring_encloses_pole(ring) == "south"
    n = len(ring)
    crossings = 0
    for i in range(n):
        v1, v2 = ring[i], ring[(i + 1) % n]
        if v1.dec_b1875_deg != v2.dec_b1875_deg:
            continue
        edge_dec = float(v1.dec_b1875_deg)
        hit_dec = edge_dec > dec0 if ray_toward_north else edge_dec < dec0
        if hit_dec and _ra_in_span(ra0, v1.ra_b1875_deg, v2.ra_b1875_deg):
            crossings += 1
    return crossings % 2 == 1


def _validate_ring(abbreviation: str, ring_index: int, ring: tuple[BoundaryVertex, ...]) -> None:
    n = len(ring)
    if n < 3:
        raise ValueError(f"{abbreviation} ring {ring_index}: only {n} vertices")
    for i in range(n):
        v1, v2 = ring[i], ring[(i + 1) % n]
        same_ra = v1.ra_b1875_deg == v2.ra_b1875_deg
        same_dec = v1.dec_b1875_deg == v2.dec_b1875_deg
        if same_ra == same_dec:  # neither axis-aligned, or a zero-length edge
            raise ValueError(
                f"{abbreviation} ring {ring_index}: edge {v1} -> {v2} is not a single "
                "constant-RA or constant-Dec segment"
            )


def _load_boundaries_uncached(path: Path) -> tuple[ConstellationBoundary, ...]:
    rows = _parse_rows(path)
    order: list[str] = []
    by_abbrev: dict[str, tuple[str, dict[int, list[BoundaryVertex]]]] = {}
    for abbreviation, name, ring_index, ra_deg, dec_deg in rows:
        if not 0.0 <= ra_deg < 360.0:
            raise ValueError(f"{abbreviation}: ra_b1875_deg out of [0, 360): {ra_deg}")
        if not -90.0 <= dec_deg <= 90.0:
            raise ValueError(f"{abbreviation}: dec_b1875_deg out of [-90, 90]: {dec_deg}")
        if abbreviation not in by_abbrev:
            by_abbrev[abbreviation] = (name, {})
            order.append(abbreviation)
        existing_name, rings_by_index = by_abbrev[abbreviation]
        if existing_name != name:
            raise ValueError(f"{abbreviation}: name changes from {existing_name!r} to {name!r}")
        rings_by_index.setdefault(ring_index, []).append(
            BoundaryVertex(ra_b1875_deg=Deg(ra_deg), dec_b1875_deg=Deg(dec_deg))
        )

    boundaries: list[ConstellationBoundary] = []
    for abbreviation in order:
        name, rings_by_index = by_abbrev[abbreviation]
        expected_indices = range(len(rings_by_index))
        if sorted(rings_by_index) != list(expected_indices):
            raise ValueError(
                f"{abbreviation}: ring numbers {sorted(rings_by_index)} are not 0..N-1"
            )
        rings = tuple(tuple(rings_by_index[i]) for i in expected_indices)
        for ring_index, ring in enumerate(rings):
            _validate_ring(abbreviation, ring_index, ring)
        boundaries.append(ConstellationBoundary(abbreviation=abbreviation, name=name, rings=rings))
    return tuple(boundaries)


@lru_cache(maxsize=4)
def load_boundaries(path: Path | None = None) -> tuple[ConstellationBoundary, ...]:
    """Every bundled constellation region, read once and cached (the file
    never changes at runtime — CLAUDE.md invariant 3). ``path`` defaults to
    :func:`default_boundaries_path`; overridable for tests, exactly like
    :func:`ssc.astro.catalog.load_catalog`'s ``paths`` argument."""
    return _load_boundaries_uncached(path or default_boundaries_path())


#: Julian date (TDB) of the Besselian epoch B1875.0, from the standard
#: Besselian-epoch-to-JD relation (Lieske 1979; Explanatory Supplement to
#: the Astronomical Almanac 3rd ed. eq. 3.221-2): JD = 2415020.31352 +
#: (B - 1900.0) * 365.242198781, evaluated at B = 1875.0. This is the epoch
#: Delporte's tables use throughout (module docstring).
_JD_B1875: Final = 2415020.31352 + (1875.0 - 1900.0) * 365.242198781


def _to_b1875(ra_j2000_deg: Deg, dec_j2000_deg: Deg) -> tuple[float, float]:
    """Precess a J2000 (ICRF) direction to mean B1875.0 — see the module
    docstring for why this is precession only, no nutation, no frame bias,
    and why the query point moves rather than the boundary vertices."""
    ra_rad = math.radians(float(ra_j2000_deg))
    dec_rad = math.radians(float(dec_j2000_deg))
    x = math.cos(dec_rad) * math.cos(ra_rad)
    y = math.cos(dec_rad) * math.sin(ra_rad)
    z = math.sin(dec_rad)
    matrix = compute_precession(_JD_B1875)
    xp = matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z
    yp = matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z
    zp = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z
    ra_b1875 = math.degrees(math.atan2(yp, xp)) % 360.0
    dec_b1875 = math.degrees(math.asin(max(-1.0, min(1.0, zp))))
    return ra_b1875, dec_b1875


def constellation_at(ra_j2000_h: Hours, dec_j2000_deg: Deg) -> str:
    """The IAU 3-letter abbreviation of the constellation containing this
    J2000 point (e.g. ``"UMI"``). Pure and deterministic: same input,
    same output, always — see :func:`_point_in_ring` for the exact,
    stated tie-break on a shared boundary.

    Raises :class:`UnknownConstellationError` if the loaded boundary data
    does not resolve the point to exactly one region — see that class's
    docstring for why this should be unreachable rather than a case this
    function silently papers over.
    """
    ra_j2000_deg = wrap_deg_360(float(hours_to_deg(Hours(ra_j2000_h))))
    ra_b1875, dec_b1875 = _to_b1875(ra_j2000_deg, Deg(dec_j2000_deg))
    matches = [
        boundary.abbreviation
        for boundary in load_boundaries()
        if any(_point_in_ring(ra_b1875, dec_b1875, ring) for ring in boundary.rings)
    ]
    if len(matches) != 1:
        raise UnknownConstellationError(
            f"(ra_j2000_h={ra_j2000_h}, dec_j2000_deg={dec_j2000_deg}) precesses to "
            f"B1875 ({ra_b1875}, {dec_b1875}), which matched {matches!r} regions, not 1"
        )
    return matches[0]


def _from_b1875(ra_b1875_deg: float, dec_b1875_deg: float, matrix: Any) -> tuple[float, float]:
    """The inverse of :func:`_to_b1875`: precess a mean-B1875.0 direction
    back to J2000. Reuses the exact rotation matrix :func:`_to_b1875` builds
    from :func:`compute_precession` — no second precession routine — applied
    transposed. ``matrix`` (from :func:`compute_precession`) rotates a
    J2000 unit vector to its B1875 image; because it is a pure rotation
    (orthogonal, determinant +1, the standard property of any product of
    Euler-angle rotations, which is exactly how ``compute_precession``
    builds it), its inverse is its transpose, so this is the same rotation
    run backwards, not an independently-derived transform that could drift
    from :func:`_to_b1875` over time."""
    ra_rad = math.radians(ra_b1875_deg)
    dec_rad = math.radians(dec_b1875_deg)
    x = math.cos(dec_rad) * math.cos(ra_rad)
    y = math.cos(dec_rad) * math.sin(ra_rad)
    z = math.sin(dec_rad)
    xp = matrix[0][0] * x + matrix[1][0] * y + matrix[2][0] * z
    yp = matrix[0][1] * x + matrix[1][1] * y + matrix[2][1] * z
    zp = matrix[0][2] * x + matrix[1][2] * y + matrix[2][2] * z
    ra_j2000 = math.degrees(math.atan2(yp, xp)) % 360.0
    dec_j2000 = math.degrees(math.asin(max(-1.0, min(1.0, zp))))
    return ra_j2000, dec_j2000


@lru_cache(maxsize=4)
def boundaries_j2000(path: Path | None = None) -> tuple[ConstellationBoundaryJ2000, ...]:
    """Every bundled constellation region's vertex rings, precessed from the
    source B1875.0 epoch to J2000 — for a frontend that must never do its
    own astrometry (CLAUDE.md, "The engine boundary is architecture").

    B1875-to-J2000 is a fixed rotation of the sphere (module docstring:
    precession only, no nutation, no frame bias), not a function of "now",
    so there is no ``epoch``/``now`` parameter here to get stale: the
    result is exactly as static as :func:`load_boundaries` itself, and is
    cached the same way, once per process (``lru_cache``, matching the
    long client-side ``staleTime`` the frontend gives this — precession
    drift over the vertices' own definition is invisible at any zoom this
    map allows, "effectively never" in the same sense the module docstring
    already uses for the query-point direction).
    """
    matrix = compute_precession(_JD_B1875)
    boundaries: list[ConstellationBoundaryJ2000] = []
    for boundary in load_boundaries(path):
        rings = tuple(
            tuple(
                BoundaryVertexJ2000(
                    ra_j2000_deg=Deg(ra_j2000),
                    dec_j2000_deg=Deg(dec_j2000),
                )
                for ra_j2000, dec_j2000 in (
                    _from_b1875(float(vertex.ra_b1875_deg), float(vertex.dec_b1875_deg), matrix)
                    for vertex in ring
                )
            )
            for ring in boundary.rings
        )
        boundaries.append(
            ConstellationBoundaryJ2000(
                abbreviation=boundary.abbreviation, name=boundary.name, rings=rings
            )
        )
    return tuple(boundaries)
