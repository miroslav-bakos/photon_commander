"""Unit tests for ssc.astro.constellations: which of the 88 IAU regions a
J2000 point falls in, against the bundled Delporte (1930) boundary data
(data/constellations/boundaries.csv — see that file's header for
provenance; THIRD_PARTY_NOTICES.md "Constellations — gate reading of
2026-09-07").
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from skyfield.precessionlib import compute_precession

from ssc.astro.constellations import (
    _JD_B1875,
    BoundaryVertexJ2000,
    ConstellationBoundary,
    ConstellationBoundaryJ2000,
    UnknownConstellationError,
    _from_b1875,
    _ra_in_span,
    _short_delta_deg,
    boundaries_credit,
    boundaries_j2000,
    constellation_at,
    default_boundaries_path,
    load_boundaries,
)
from ssc.astro.units import Deg, Hours

# The 88 IAU 3-letter constellation abbreviations (Russell 1922 / Delporte
# 1930), independent of anything read from the bundled file — used below to
# check the loaded data against a fixed, known-correct set rather than just
# checking "88 of something".
_EXPECTED_ABBREVIATIONS = {
    "AND", "ANT", "APS", "AQL", "AQR", "ARA", "ARI", "AUR", "BOO", "CAE",
    "CAM", "CAP", "CAR", "CAS", "CEN", "CEP", "CET", "CHA", "CIR", "CMA",
    "CMI", "CNC", "COL", "COM", "CRA", "CRB", "CRT", "CRU", "CRV", "CVN",
    "CYG", "DEL", "DOR", "DRA", "EQU", "ERI", "FOR", "GEM", "GRU", "HER",
    "HOR", "HYA", "HYI", "IND", "LAC", "LEO", "LEP", "LIB", "LMI", "LUP",
    "LYN", "LYR", "MEN", "MIC", "MON", "MUS", "NOR", "OCT", "OPH", "ORI",
    "PAV", "PEG", "PER", "PHE", "PIC", "PSA", "PSC", "PUP", "PYX", "RET",
    "SCL", "SCO", "SCT", "SER", "SEX", "SGE", "SGR", "TAU", "TEL", "TRA",
    "TRI", "TUC", "UMA", "UMI", "VEL", "VIR", "VOL", "VUL",
}  # fmt: skip


@pytest.fixture(scope="module")
def boundaries() -> tuple[ConstellationBoundary, ...]:
    return load_boundaries()


# --- loading ------------------------------------------------------------


def test_default_boundaries_path_is_bundled_and_committed() -> None:
    path = default_boundaries_path()
    assert path.name == "boundaries.csv"
    assert path.is_file(), "the boundary table ships in the repo; there is nothing to fetch"


def test_every_iau_abbreviation_is_present_exactly_once(
    boundaries: tuple[ConstellationBoundary, ...],
) -> None:
    abbreviations = [b.abbreviation for b in boundaries]
    assert set(abbreviations) == _EXPECTED_ABBREVIATIONS
    assert len(abbreviations) == len(_EXPECTED_ABBREVIATIONS), "no abbreviation repeats"


def test_serpens_is_the_only_multi_ring_region(
    boundaries: tuple[ConstellationBoundary, ...],
) -> None:
    """Delporte's own tables split Serpens into Caput and Cauda either side
    of Ophiuchus; every other constellation is one closed piece."""
    by_abbrev = {b.abbreviation: b for b in boundaries}
    assert len(by_abbrev["SER"].rings) == 2
    for abbreviation, boundary in by_abbrev.items():
        if abbreviation != "SER":
            assert len(boundary.rings) == 1, abbreviation


# --- geometry sanity: closed, non-self-intersecting, tiling the sphere ---- #


def _unwrapped_ring_points(ring: tuple[object, ...]) -> list[tuple[float, float]]:
    """The ring's vertices plus one synthetic closing point, all in a
    continuous (non-modular) RA coordinate so that a straightforward planar
    check can be used for self-intersection — see the module's own
    ``_short_delta_deg`` for why "the short way" is the only sane way to
    unwrap a real boundary edge."""
    ra0 = float(ring[0].ra_b1875_deg)  # type: ignore[attr-defined]
    unwrapped = [ra0]
    for i in range(1, len(ring)):
        delta = _short_delta_deg(ring[i - 1].ra_b1875_deg, ring[i].ra_b1875_deg)  # type: ignore[attr-defined]
        unwrapped.append(unwrapped[-1] + delta)
    closing_delta = _short_delta_deg(ring[-1].ra_b1875_deg, ring[0].ra_b1875_deg)  # type: ignore[attr-defined]
    unwrapped.append(unwrapped[-1] + closing_delta)
    points = [(unwrapped[i], float(ring[i].dec_b1875_deg)) for i in range(len(ring))]  # type: ignore[attr-defined]
    points.append((unwrapped[-1], float(ring[0].dec_b1875_deg)))  # type: ignore[attr-defined]
    return points


def _segments_touch_only(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    """Whether two axis-aligned segments meet only at a single shared
    endpoint (fine — that is how a simple polygon's edges normally meet)
    rather than overlapping or crossing (a self-intersection bug)."""
    eps = 1e-9

    def bbox(p: tuple[float, float], q: tuple[float, float]) -> tuple[float, float, float, float]:
        return (min(p[0], q[0]), max(p[0], q[0]), min(p[1], q[1]), max(p[1], q[1]))

    ax0, ax1, ay0, ay1 = bbox(a1, a2)
    bx0, bx1, by0, by1 = bbox(b1, b2)
    if ax1 < bx0 - eps or bx1 < ax0 - eps or ay1 < by0 - eps or by1 < ay0 - eps:
        return True  # bounding boxes don't overlap at all
    a_vertical = a1[0] == a2[0]
    b_vertical = b1[0] == b2[0]
    if a_vertical == b_vertical:
        # parallel (both vertical or both horizontal): touching only if the
        # shared-axis ranges overlap in at most a point
        overlap = min(ax1, bx1) - max(ax0, bx0) if a_vertical else min(ay1, by1) - max(ay0, by0)
        return overlap <= eps
    vert = (a1, a2) if a_vertical else (b1, b2)
    other = (b1, b2) if a_vertical else (a1, a2)
    vx = vert[0][0]
    vy0, vy1 = min(vert[0][1], vert[1][1]), max(vert[0][1], vert[1][1])
    hy = other[0][1]
    hx0, hx1 = min(other[0][0], other[1][0]), max(other[0][0], other[1][0])
    on_vertical = vy0 - eps <= hy <= vy1 + eps
    on_horizontal = hx0 - eps <= vx <= hx1 + eps
    if not (on_vertical and on_horizontal):
        return True
    # they cross/touch at (vx, hy); fine only if that point is an endpoint
    # of *both* segments (a shared vertex), not an interior crossing
    endpoint_of_vert = any(abs(hy - y) <= eps for y in (vert[0][1], vert[1][1]))
    endpoint_of_horiz = any(abs(vx - x) <= eps for x in (other[0][0], other[1][0]))
    return endpoint_of_vert and endpoint_of_horiz


def test_boundary_rings_are_closed_and_non_self_intersecting(
    boundaries: tuple[ConstellationBoundary, ...],
) -> None:
    """Every ring is a valid simple closed polygon: consecutive vertices
    differ in exactly one axis (checked at load time already —
    ``load_boundaries`` would have raised otherwise), and no two
    non-adjacent edges cross or overlap."""
    for boundary in boundaries:
        for ring in boundary.rings:
            assert len(ring) >= 3, boundary.abbreviation
            points = _unwrapped_ring_points(ring)  # len == len(ring) + 1
            n = len(ring)
            edges = [(points[i], points[i + 1]) for i in range(n)]
            for i in range(n):
                for j in range(i + 1, n):
                    if j == i + 1 or (i == 0 and j == n - 1):
                        continue  # adjacent edges legitimately share one vertex
                    a1, a2 = edges[i]
                    b1, b2 = edges[j]
                    assert _segments_touch_only(a1, a2, b1, b2), (
                        f"{boundary.abbreviation} ring edges {i} and {j} cross or overlap"
                    )


def test_the_88_regions_tile_the_sphere_without_gaps_or_overlap(
    boundaries: tuple[ConstellationBoundary, ...],
) -> None:
    """A uniformly-sampled point on the sphere (Marsaglia's method: uniform
    RA, Dec via arcsin of a uniform sine) must land in exactly one region —
    zero would be a gap, two or more an overlap. Exercised directly in the
    B1875 frame these rings are actually defined in (bypassing precession,
    which :func:`constellation_at`'s own reference-point tests already
    check end to end), with a fixed seed so a failure is reproducible."""
    import random

    from ssc.astro.constellations import _point_in_ring

    rng = random.Random(20260907)
    for _ in range(2000):
        ra = rng.uniform(0.0, 360.0)
        dec = math.degrees(math.asin(rng.uniform(-1.0, 1.0)))
        matches = [
            b.abbreviation
            for b in boundaries
            if any(_point_in_ring(ra, dec, ring) for ring in b.rings)
        ]
        assert len(matches) == 1, (ra, dec, matches)


# --- known positions ------------------------------------------------------- #
# Each RA/Dec below is a catalogued J2000 position, not eyeballed, cited at
# its own test.


def test_polaris_is_in_ursa_minor() -> None:
    """Polaris (alpha UMi), HIP 11767: RA 02h31m49.09s, Dec +89d15m50.8s
    (J2000), Hipparcos Catalogue (ESA 1997), as tabulated by SIMBAD."""
    ra_h = 2 + 31 / 60 + 49.09 / 3600
    dec_deg = 89 + 15 / 60 + 50.8 / 3600
    assert constellation_at(Hours(ra_h), Deg(dec_deg)) == "UMI"


def test_sirius_is_in_canis_major() -> None:
    """Sirius (alpha CMa), HIP 32349: RA 06h45m08.917s, Dec -16d42m58.02s
    (J2000), Hipparcos Catalogue (ESA 1997), as tabulated by SIMBAD."""
    ra_h = 6 + 45 / 60 + 8.917 / 3600
    dec_deg = -(16 + 42 / 60 + 58.02 / 3600)
    assert constellation_at(Hours(ra_h), Deg(dec_deg)) == "CMA"


def test_galactic_centre_is_in_sagittarius() -> None:
    """Sgr A* (the Galactic Centre radio source): RA 17h45m40.04s,
    Dec -29d00m28.1s (J2000), Reid & Brunthaler 2004, ApJ 616, 872
    (bibcode 2004ApJ...616..872R), the VLBA astrometric position."""
    ra_h = 17 + 45 / 60 + 40.04 / 3600
    dec_deg = -(29 + 0 / 60 + 28.1 / 3600)
    assert constellation_at(Hours(ra_h), Deg(dec_deg)) == "SGR"


def test_betelgeuse_is_in_orion() -> None:
    """Betelgeuse (alpha Ori), HIP 27989: RA 05h55m10.3s, Dec +07d24m25s
    (J2000), Hipparcos Catalogue (ESA 1997), as tabulated by SIMBAD."""
    ra_h = 5 + 55 / 60 + 10.3 / 3600
    dec_deg = 7 + 24 / 60 + 25.0 / 3600
    assert constellation_at(Hours(ra_h), Deg(dec_deg)) == "ORI"


def test_vega_is_in_lyra() -> None:
    """Vega (alpha Lyr), HIP 91262: RA 18h36m56.3364s, Dec +38d47m01.28s
    (J2000), Hipparcos Catalogue (ESA 1997), as tabulated by SIMBAD."""
    ra_h = 18 + 36 / 60 + 56.3364 / 3600
    dec_deg = 38 + 47 / 60 + 1.28 / 3600
    assert constellation_at(Hours(ra_h), Deg(dec_deg)) == "LYR"


def test_unresolved_dec_out_of_range_is_a_type_error_free_zone() -> None:
    """Sanity check that the type stays a plain 3-letter code, not e.g. a
    tuple, for a very ordinary point (RA 0h, Dec 0d — Pisces/Cetus/Aquarius
    corner region historically, but this point itself is well inside one
    of them)."""
    result = constellation_at(Hours(0.0), Deg(0.0))
    assert isinstance(result, str)
    assert len(result) == 3


# --- deterministic tie-break on a shared edge ------------------------------ #


def _b1875_to_j2000(ra_b1875_deg: float, dec_b1875_deg: float) -> tuple[float, float]:
    """The exact inverse of ``ssc.astro.constellations._to_b1875``, i.e. the
    module's own ``_from_b1875`` (also what :func:`boundaries_j2000` uses) —
    called here, not reimplemented a second time, so this test exercises
    the real inversion rather than a parallel copy of it that could drift.
    Recovers J2000 coordinates that :func:`constellation_at` will precess
    back to *exactly* ``(ra_b1875_deg, dec_b1875_deg)`` (up to float
    roundoff), letting the tie-break test below exercise the public
    function with a real, exact-on-the-edge input, not just the private
    ring-containment helper."""
    matrix = compute_precession(_JD_B1875)
    ra_j2000, dec_j2000 = _from_b1875(ra_b1875_deg, dec_b1875_deg, matrix)
    return ra_j2000 / 15.0, dec_j2000


def test_a_point_exactly_on_a_shared_edge_resolves_deterministically() -> None:
    """Ursa Minor and Cepheus share the parallel (constant-Dec) edge from
    (RA 23h, Dec +88d) to (RA 8h, Dec +88d) in B1875 — both rings list it
    (``data/constellations/boundaries.csv``). Its midpoint, RA 3h30m
    Dec +88d, sits exactly on that shared edge with no other edge nearby.

    **Stated tie-break rule** (``ssc.astro.constellations._point_in_ring``):
    a fixed-RA ray is cast away from whichever pole a ring encloses (if
    any), and an edge counts as crossed only when it lies *strictly* on the
    ray's forward side of the query declination, with the RA span of each
    edge treated half-open (including its start, excluding its end). A
    point sitting exactly on a shared edge therefore never counts that edge
    for the ring on the "ray's own side", and counts it (or not) consistently
    for the other ring — the result is single-valued, not "whichever wins a
    race", and identical on every call.
    """
    ra_j2000_h, dec_j2000_deg = _b1875_to_j2000(52.5, 88.0)  # RA 52.5 deg == 3h30m
    first = constellation_at(Hours(ra_j2000_h), Deg(dec_j2000_deg))
    second = constellation_at(Hours(ra_j2000_h), Deg(dec_j2000_deg))
    assert first == second, "constellation_at must be deterministic on a shared edge"
    assert first in {"UMI", "CEP"}, first


def test_ra_in_span_is_half_open() -> None:
    """Direct unit check of the stated tie-break primitive: the span
    [ra1, ra2) includes its start, excludes its end."""
    assert _ra_in_span(10.0, Deg(10.0), Deg(20.0)) is True
    assert _ra_in_span(20.0, Deg(10.0), Deg(20.0)) is False
    assert _ra_in_span(15.0, Deg(10.0), Deg(20.0)) is True
    assert _ra_in_span(9.999, Deg(10.0), Deg(20.0)) is False


# --- hypothesis properties -------------------------------------------------- #


_RA_HOURS = st.floats(min_value=0.0, max_value=23.999_999, allow_nan=False, allow_infinity=False)
_DEC_DEG = st.floats(min_value=-89.999, max_value=89.999, allow_nan=False, allow_infinity=False)


@given(ra_h=_RA_HOURS, dec_deg=_DEC_DEG)
@settings(max_examples=200)
def test_constellation_at_always_returns_a_known_abbreviation(
    ra_h: float, dec_deg: float, boundaries: tuple[ConstellationBoundary, ...]
) -> None:
    del boundaries
    try:
        result = constellation_at(Hours(ra_h), Deg(dec_deg))
    except UnknownConstellationError:
        pytest.fail(f"({ra_h}, {dec_deg}) resolved to no single region")
    assert result in _EXPECTED_ABBREVIATIONS


@given(ra_h=_RA_HOURS, dec_deg=_DEC_DEG)
@settings(max_examples=200)
def test_constellation_at_is_invariant_under_a_24_hour_ra_wrap(ra_h: float, dec_deg: float) -> None:
    """RA wraps at 0h/24h: a point and the "same" point named 24h further
    round the sky must resolve to the same constellation."""
    wrapped = ra_h + 24.0 if ra_h < 12.0 else ra_h - 24.0
    assert constellation_at(Hours(ra_h), Deg(dec_deg)) == constellation_at(
        Hours(wrapped), Deg(dec_deg)
    )


# --- boundaries_j2000 / boundaries_credit (skymap-viz's precessed endpoint) ---- #


def test_boundaries_credit_is_the_data_files_own_header_line() -> None:
    """Never a literal string in code — read from ``# credit:`` in
    ``data/constellations/boundaries.csv`` (that file's own header)."""
    assert (
        boundaries_credit()
        == "Constellation boundaries: E. Delporte 1930, via Pierre Barbier, CC BY-SA 4.0"
    )


def test_boundaries_j2000_has_the_same_shape_as_the_source_data() -> None:
    """Same 88 regions, same ring structure (Serpens split in two), same
    total vertex count (1562) as :func:`load_boundaries` — precessing only
    moves points, it never adds, drops or reshapes a ring."""
    source = load_boundaries()
    precessed = boundaries_j2000()
    assert [b.abbreviation for b in precessed] == [b.abbreviation for b in source]
    assert [b.name for b in precessed] == [b.name for b in source]
    for src_boundary, j2000_boundary in zip(source, precessed, strict=True):
        assert isinstance(j2000_boundary, ConstellationBoundaryJ2000)
        assert len(j2000_boundary.rings) == len(src_boundary.rings)
        for src_ring, j2000_ring in zip(src_boundary.rings, j2000_boundary.rings, strict=True):
            assert len(j2000_ring) == len(src_ring)
            for vertex in j2000_ring:
                assert isinstance(vertex, BoundaryVertexJ2000)
                assert 0.0 <= float(vertex.ra_j2000_deg) < 360.0
                assert -90.0 <= float(vertex.dec_j2000_deg) <= 90.0
    total_vertices = sum(len(ring) for b in precessed for ring in b.rings)
    assert total_vertices == 1562


def test_boundaries_j2000_is_cached() -> None:
    assert boundaries_j2000() is boundaries_j2000()


@pytest.mark.parametrize("abbreviation", ["ORI", "CRU", "LYR", "AQL"])
def test_boundaries_j2000_vertices_land_where_constellation_at_agrees(abbreviation: str) -> None:
    """Spot-check a handful of well-known, non-pole-enclosing, non-RA-wrapping
    regions: nudge one of `boundaries_j2000`'s own precessed corners a short
    way toward its ring's centroid (still deep inside the region, away from
    any edge) and confirm `constellation_at` — which precesses independently,
    in the *other* direction, straight from the bundled B1875 data — places
    that J2000 point back in the same region. This is the cross-check
    `skymap-viz.md` asks for: two independent precession paths agreeing,
    not one function's output validated against itself."""
    by_abbrev = {b.abbreviation: b for b in boundaries_j2000()}
    region = by_abbrev[abbreviation]
    ring = region.rings[0]
    ra_centroid = sum(float(v.ra_j2000_deg) for v in ring) / len(ring)
    dec_centroid = sum(float(v.dec_j2000_deg) for v in ring) / len(ring)
    corner = ring[0]
    ra = float(corner.ra_j2000_deg) + 0.05 * (ra_centroid - float(corner.ra_j2000_deg))
    dec = float(corner.dec_j2000_deg) + 0.05 * (dec_centroid - float(corner.dec_j2000_deg))
    assert constellation_at(Hours(ra / 15.0), Deg(dec)) == abbreviation
