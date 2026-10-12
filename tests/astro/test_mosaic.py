"""Unit and property tests for ssc.astro.mosaic: RA-convergent panel tiling and
the per-panel alt-az rotation envelope.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ssc.astro.framing import ArcSec, DistortionModel, FieldGeometry, MountMode, Provenance, Session
from ssc.astro.mosaic import (
    MosaicSpec,
    PanelCenter,
    mosaic_panel_centers,
    mosaic_plan,
    sky_tile_centers_deg,
)
from ssc.astro.units import Deg, signed_delta_deg, wrap_deg_180, wrap_deg_360


def _geometry(width_deg: float = 3.99, height_deg: float = 2.24) -> FieldGeometry:
    return FieldGeometry(
        optic_id="synthetic",
        label="synthetic",
        pixel_scale_arcsec=ArcSec(1.0),
        width_deg=Deg(width_deg),
        height_deg=Deg(height_deg),
        diagonal_deg=Deg(math.hypot(width_deg, height_deg)),
        inscribed_circle_deg=Deg(min(width_deg, height_deg)),
        provenance=Provenance.DATASHEET,
        distortion=DistortionModel.RECTILINEAR,
        caveats=(),
    )


# --- MosaicSpec validation ---------------------------------------------------


def test_spec_rejects_non_positive_counts() -> None:
    with pytest.raises(ValueError, match="ra_num"):
        MosaicSpec(ra_num=0, dec_num=1, overlap_pct=20.0)
    with pytest.raises(ValueError, match="dec_num"):
        MosaicSpec(ra_num=1, dec_num=0, overlap_pct=20.0)


def test_spec_rejects_overlap_out_of_range() -> None:
    with pytest.raises(ValueError, match="overlap_pct"):
        MosaicSpec(ra_num=1, dec_num=1, overlap_pct=100.0)
    with pytest.raises(ValueError, match="overlap_pct"):
        MosaicSpec(ra_num=1, dec_num=1, overlap_pct=-1.0)


def test_spec_panel_count() -> None:
    assert MosaicSpec(ra_num=3, dec_num=2, overlap_pct=20.0).panel_count == 6


# --- single panel is just the centre ----------------------------------------


def test_single_panel_mosaic_is_the_target_itself() -> None:
    geometry = _geometry()
    centers = mosaic_panel_centers(Deg(10.0), Deg(20.0), geometry, MosaicSpec(1, 1, 20.0))
    assert len(centers) == 1
    assert float(centers[0].ra_j2000_deg) == pytest.approx(10.0)
    assert float(centers[0].dec_j2000_deg) == pytest.approx(20.0)


# --- degenerate pole case ----------------------------------------------------


def test_row_at_the_pole_raises_rather_than_dividing_by_zero() -> None:
    geometry = _geometry()
    with pytest.raises(ValueError, match="pole"):
        mosaic_panel_centers(Deg(0.0), Deg(90.0), geometry, MosaicSpec(2, 1, 20.0))


def test_grid_too_tall_for_its_declination_centre_raises() -> None:
    geometry = _geometry(height_deg=60.0)  # absurdly tall synthetic frame
    with pytest.raises(ValueError, match="declination"):
        mosaic_panel_centers(Deg(0.0), Deg(85.0), geometry, MosaicSpec(1, 3, 0.0))


# --- RA convergence: property tests -----------------------------------------


def _row_dec(dec_center: float, dec_step: float, dec_num: int, row: int) -> float:
    midpoint = (dec_num - 1) / 2.0
    return dec_center + (row - midpoint) * dec_step


@settings(deadline=None, max_examples=100)
@given(
    width=st.floats(min_value=0.05, max_value=4.0),
    height=st.floats(min_value=0.05, max_value=4.0),
    ra_center=st.floats(min_value=0.0, max_value=360.0, exclude_max=True),
    dec_center=st.floats(min_value=-75.0, max_value=75.0),
    ra_num=st.integers(min_value=1, max_value=4),
    dec_num=st.integers(min_value=1, max_value=4),
    overlap_pct=st.floats(min_value=0.0, max_value=90.0),
)
def test_mosaic_tiles_with_no_gaps_and_at_least_the_requested_overlap(
    width: float,
    height: float,
    ra_center: float,
    dec_center: float,
    ra_num: int,
    dec_num: int,
    overlap_pct: float,
) -> None:
    """Adjacent panels overlap by at least ``overlap_pct`` and never gap, in both
    axes, including a grid that straddles the RA 0h/24h wrap and one whose rows
    approach the pole (declination up to 75 deg, well short of the degenerate
    row this module rejects outright)."""
    geometry = _geometry(width, height)
    spec = MosaicSpec(ra_num, dec_num, overlap_pct)
    panels = mosaic_panel_centers(Deg(ra_center), Deg(dec_center), geometry, spec)

    assert len(panels) == spec.panel_count

    dec_step = height * (1.0 - overlap_pct / 100.0)
    ra_step = width * (1.0 - overlap_pct / 100.0)
    tolerance = 1e-6

    by_row: dict[int, list[PanelCenter]] = {}
    for panel in panels:
        by_row.setdefault(panel.row, []).append(panel)

    # RA axis: within a row, consecutive columns are separated (great-circle
    # distance along that row's declination) by exactly ra_step -- no more
    # (that would gap), no less (that would under-overlap).
    for row, row_panels in by_row.items():
        row_panels_sorted = sorted(row_panels, key=lambda p: p.col)
        panel_dec = _row_dec(dec_center, dec_step, dec_num, row)
        cos_dec = math.cos(math.radians(panel_dec))
        for a, b in pairwise(row_panels_sorted):
            circular_diff = abs(float(wrap_deg_180(float(b.ra_j2000_deg) - float(a.ra_j2000_deg))))
            physical_separation = circular_diff * abs(cos_dec)
            assert physical_separation <= width + tolerance, "RA-adjacent panels must not gap"
            assert physical_separation == pytest.approx(ra_step, abs=1e-6, rel=1e-6)
            overlap_fraction = 1.0 - physical_separation / width
            assert overlap_fraction >= overlap_pct / 100.0 - 1e-6

    # Dec axis: consecutive rows (any shared column) are separated by exactly
    # dec_step, regardless of RA convergence -- declination does not converge.
    rows_sorted = sorted(by_row)
    for row_a, row_b in pairwise(rows_sorted):
        dec_a = _row_dec(dec_center, dec_step, dec_num, row_a)
        dec_b = _row_dec(dec_center, dec_step, dec_num, row_b)
        separation = abs(dec_b - dec_a)
        assert separation <= height + tolerance, "dec-adjacent panels must not gap"
        assert separation == pytest.approx(dec_step, abs=1e-6, rel=1e-6)
        overlap_fraction = 1.0 - separation / height
        assert overlap_fraction >= overlap_pct / 100.0 - 1e-6


def test_ra_wrap_is_contiguous_across_0h_24h() -> None:
    """A grid centred a fraction of a degree east of 0h RA still tiles as one
    contiguous strip once panel centres wrap into [0, 360)."""
    geometry = _geometry(width_deg=2.0, height_deg=2.0)
    spec = MosaicSpec(ra_num=3, dec_num=1, overlap_pct=20.0)
    panels = mosaic_panel_centers(Deg(0.2), Deg(0.0), geometry, spec)
    ras = sorted(float(p.ra_j2000_deg) for p in panels)
    # One of the three panel centres must have wrapped around past 360 -> just
    # above 0, i.e. the raw (unwrapped) spread is small even though the wrapped
    # values are not monotonic across the seam.
    unwrapped = [float(wrap_deg_180(ra - 0.2)) for ra in ras]
    assert max(unwrapped) - min(unwrapped) == pytest.approx(3.2, abs=1e-6)  # (ra_num - 1) * ra_step


# --- alt-az compounding: per-panel keep-region ------------------------------


def test_no_session_leaves_envelopes_none_and_warns() -> None:
    geometry = _geometry()
    plan = mosaic_plan(
        geometry, MountMode.ALTAZ, MosaicSpec(2, 2, 20.0), Deg(10.0), Deg(30.0), center_session=None
    )
    assert all(panel.envelope is None for panel in plan.panels)
    assert any("No rotation session" in w for w in plan.warnings)
    assert plan.worst_rotation_severity == "none"


def test_session_produces_a_keep_region_per_panel() -> None:
    geometry = _geometry()
    session = Session(
        latitude_deg=Deg(45.0),
        declination_deg=Deg(30.0),
        hour_angle_start_deg=Deg(-20.0),
        duration_hours=2.0,
    )
    plan = mosaic_plan(
        geometry,
        MountMode.ALTAZ,
        MosaicSpec(2, 2, 20.0),
        Deg(10.0),
        Deg(30.0),
        center_session=session,
    )
    assert all(isinstance(panel.envelope, type(plan.panels[0].envelope)) for panel in plan.panels)
    for panel in plan.panels:
        assert panel.envelope is not None
        assert panel.envelope.keep_region is not None
    assert any("panels each rotate independently" in w for w in plan.warnings)


def test_eq_mode_mosaic_does_not_warn_about_compounding_rotation() -> None:
    """EQ mode has no rotation to compound (framing.py: PA(t) is constant)."""
    geometry = _geometry()
    session = Session(
        latitude_deg=Deg(45.0),
        declination_deg=Deg(30.0),
        hour_angle_start_deg=Deg(-20.0),
        duration_hours=2.0,
    )
    plan = mosaic_plan(
        geometry,
        MountMode.EQ,
        MosaicSpec(2, 2, 20.0),
        Deg(10.0),
        Deg(30.0),
        center_session=session,
    )
    assert not any("rotate independently" in w for w in plan.warnings)
    assert all(panel.envelope is not None for panel in plan.panels)


def test_session_time_estimate_multiplies_panel_count() -> None:
    geometry = _geometry()
    plan = mosaic_plan(geometry, MountMode.EQ, MosaicSpec(3, 2, 20.0), Deg(10.0), Deg(30.0))
    assert plan.panel_count == 6
    assert plan.session_time_estimate_seconds(120.0) == pytest.approx(720.0)


def test_session_time_estimate_rejects_negative_time() -> None:
    geometry = _geometry()
    plan = mosaic_plan(geometry, MountMode.EQ, MosaicSpec(1, 1, 20.0), Deg(10.0), Deg(30.0))
    with pytest.raises(ValueError, match="negative"):
        plan.session_time_estimate_seconds(-1.0)


# --- sky_tile_centers_deg ----------------------------------------------------


def test_sky_tile_centers_single_tile_is_the_centre_itself() -> None:
    centers = sky_tile_centers_deg(10.0, 20.0, 1, 3.0)
    assert centers == ((Deg(10.0), Deg(20.0)),)


def test_sky_tile_centers_dec_step_is_always_exact_tile_size() -> None:
    # Dec offsets need no convergence correction -- always exactly
    # tile_size_deg apart, at any declination.
    for dec_center in (0.0, 45.0, -60.0, 80.0):
        centers = sky_tile_centers_deg(10.0, dec_center, 2, 3.0)
        decs = sorted({round(dec, 9) for _ra, dec in centers})
        assert decs == [pytest.approx(dec_center - 1.5), pytest.approx(dec_center + 1.5)]


def test_sky_tile_centers_ra_step_is_exact_tile_size_at_the_equator() -> None:
    # 3 rows so the middle one sits exactly on dec=0, where cos(dec) == 1 and
    # a degree of RA is a plain degree -- centres one tile width (2 deg)
    # apart, edge to edge, no gap or overlap.
    centers = sky_tile_centers_deg(10.0, 0.0, 3, 2.0)
    equator_ras = sorted(round(ra, 9) for ra, dec in centers if dec == 0.0)
    assert equator_ras == [8.0, 10.0, 12.0]


def test_sky_tile_centers_ra_convergence_uses_each_rows_own_declination() -> None:
    # High declination: the RA half-step must be wider than at the equator by
    # 1/cos(dec) of that row's own (offset) declination, not the centre's.
    centers = sky_tile_centers_deg(10.0, 80.0, 2, 3.0)
    ras_by_row: dict[float, list[float]] = {}
    for ra, dec in centers:
        ras_by_row.setdefault(dec, []).append(ra)
    assert len(ras_by_row) == 2
    for dec, ras in ras_by_row.items():
        cos_dec = math.cos(math.radians(dec))
        expected_half_step = 1.5 / cos_dec
        expected = {
            round(float(wrap_deg_360(10.0 - expected_half_step)), 6),
            round(float(wrap_deg_360(10.0 + expected_half_step)), 6),
        }
        assert {round(ra, 6) for ra in ras} == expected


def test_sky_tile_centers_wraps_ra_at_0_24h() -> None:
    centers = sky_tile_centers_deg(0.5, 0.0, 2, 3.0)
    ras = sorted(ra for ra, _dec in centers)
    # One tile's centre lands west of 0 deg RA -- must wrap into [0, 360),
    # not go negative.
    assert all(0.0 <= ra < 360.0 for ra in ras)
    assert any(ra > 300.0 for ra in ras)


def test_sky_tile_centers_clamps_near_the_pole_instead_of_raising() -> None:
    # mosaic_panel_centers raises this close to a pole; sky_tile_centers_deg
    # must not -- it clamps the cos(dec) input and each tile's own
    # declination instead, since a cutout tile grid cannot refuse an
    # arbitrary target's declination the way a deliberate mosaic framing can.
    centers = sky_tile_centers_deg(10.0, 89.5, 2, 3.0)
    assert len(centers) == 4
    for ra, dec in centers:
        assert -90.0 <= dec <= 90.0
        assert 0.0 <= ra < 360.0
        assert math.isfinite(ra)


def test_ngc7000_panels_are_numbered_left_to_right_in_north_up_sky_map() -> None:
    panels = mosaic_panel_centers(Deg(314.0), Deg(44.0), _geometry(), MosaicSpec(2, 1, 20.0))
    # The sky projection puts east (increasing RA) on the left.
    assert panels[0].col == 0
    assert signed_delta_deg(Deg(314.0), panels[0].ra_j2000_deg) > 0
    assert signed_delta_deg(Deg(314.0), panels[1].ra_j2000_deg) < 0
