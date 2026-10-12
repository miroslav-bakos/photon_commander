"""The per-azimuth horizon mask and the visibility solver that reads it.

Three groups: the pure geometry of :mod:`ssc.astro.horizon` (including the
hypothesis properties for the 0/360 seam, which is where the interesting bug
lives), the degenerate flat profile that must behave exactly like the scalar
threshold it replaces, and the horizon-aware solver in
:func:`ssc.astro.events.horizon_visibility` against the real ephemeris.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ssc.astro.events import (
    MAX_VISIBILITY_SAMPLES,
    Ephemeris,
    Outcome,
    Site,
    VisibilityInterval,
    horizon_visibility,
    target_events,
    target_position,
)
from ssc.astro.horizon import HorizonPoint, HorizonProfile, flat_horizon, is_above_horizon
from ssc.astro.units import Deg, Hours

# --- strategies ------------------------------------------------------------

#: Azimuths on a 0.01 deg lattice: unique by construction and never closer than
#: 0.01 deg, so a random profile has a finite slope and the continuity bound
#: below means something.
LATTICE_AZIMUTHS = st.integers(min_value=0, max_value=35_999).map(
    lambda hundredths: hundredths / 100.0
)
ALTITUDES = st.floats(min_value=-10.0, max_value=85.0, allow_nan=False, allow_infinity=False)
ANY_AZIMUTH = st.floats(min_value=-1080.0, max_value=1440.0, allow_nan=False, allow_infinity=False)


@st.composite
def horizon_profiles(draw: st.DrawFn) -> HorizonProfile:
    azimuths = draw(st.lists(LATTICE_AZIMUTHS, min_size=2, max_size=10, unique=True))
    altitudes = draw(st.lists(ALTITUDES, min_size=len(azimuths), max_size=len(azimuths)))
    return HorizonProfile(
        points=tuple(
            HorizonPoint(azimuth_deg=Deg(azimuth), altitude_deg=Deg(altitude))
            for azimuth, altitude in zip(azimuths, altitudes, strict=True)
        )
    )


def _max_slope_deg_per_deg(profile: HorizonProfile) -> float:
    """Steepest segment of the mask, treating it as the cyclic polyline it is."""
    points = profile.points
    worst = 0.0
    for index, start in enumerate(points):
        end = points[(index + 1) % len(points)]
        span = (float(end.azimuth_deg) - float(start.azimuth_deg)) % 360.0
        rise = abs(float(end.altitude_deg) - float(start.altitude_deg))
        worst = max(worst, rise / span)
    return worst


def _profile(*pairs: tuple[float, float]) -> HorizonProfile:
    return HorizonProfile(
        points=tuple(
            HorizonPoint(azimuth_deg=Deg(azimuth), altitude_deg=Deg(altitude))
            for azimuth, altitude in pairs
        )
    )


# --- construction ----------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1])
def test_a_profile_needs_at_least_two_points(count: int) -> None:
    points = tuple(
        HorizonPoint(azimuth_deg=Deg(90.0 * i), altitude_deg=Deg(10.0)) for i in range(count)
    )
    with pytest.raises(ValueError, match="at least 2 points"):
        HorizonProfile(points=points)


def test_duplicate_azimuth_is_a_construction_error() -> None:
    with pytest.raises(ValueError, match="duplicate azimuth"):
        _profile((10.0, 5.0), (200.0, 8.0), (10.0, 9.0))


def test_duplicate_azimuth_is_detected_after_wrapping() -> None:
    """370 and 10 are the same bearing, so they are the same vertex."""
    with pytest.raises(ValueError, match="duplicate azimuth"):
        _profile((370.0, 5.0), (200.0, 8.0), (10.0, 9.0))


def test_points_are_stored_sorted_and_wrapped() -> None:
    profile = _profile((200.0, 8.0), (-10.0, 5.0), (95.0, 6.0))
    assert profile.azimuths_deg == (Deg(95.0), Deg(200.0), Deg(350.0))


def test_altitude_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="altitude out of range"):
        HorizonPoint(azimuth_deg=Deg(0.0), altitude_deg=Deg(91.0))


def test_non_finite_azimuth_is_rejected() -> None:
    with pytest.raises(ValueError, match="azimuth must be finite"):
        HorizonPoint(azimuth_deg=Deg(float("nan")), altitude_deg=Deg(0.0))


# --- interpolation, and the seam -------------------------------------------


def test_vertices_return_their_own_altitude() -> None:
    profile = _profile((0.0, 3.0), (90.0, 25.0), (180.0, 7.0), (270.0, 11.0))
    for point in profile.points:
        assert profile.altitude_at(point.azimuth_deg) == pytest.approx(float(point.altitude_deg))


def test_interpolation_between_vertices_is_linear() -> None:
    profile = _profile((0.0, 0.0), (100.0, 10.0), (200.0, 0.0))
    assert profile.altitude_at(Deg(25.0)) == pytest.approx(2.5)
    assert profile.altitude_at(Deg(50.0)) == pytest.approx(5.0)


def test_the_seam_segment_runs_through_north_not_through_south() -> None:
    """350 deg to 10 deg is 20 deg through north, not 340 deg the other way.

    With the wrong-way interpolation the value at due north would be dragged
    towards the far side of the circle instead of sitting halfway between the
    two points that actually bracket it.
    """
    profile = _profile((350.0, 10.0), (10.0, 20.0), (180.0, 40.0))
    assert profile.altitude_at(Deg(0.0)) == pytest.approx(15.0)
    assert profile.altitude_at(Deg(355.0)) == pytest.approx(12.5)
    assert profile.altitude_at(Deg(5.0)) == pytest.approx(17.5)
    # The southern point is on the other segments and must not leak into the seam.
    assert profile.altitude_at(Deg(95.0)) == pytest.approx(30.0)


def test_a_near_vertical_segment_is_a_chimney_not_an_error() -> None:
    profile = _profile((100.0, 5.0), (100.1, 45.0), (100.2, 5.0), (280.0, 5.0))
    assert profile.altitude_at(Deg(100.1)) == pytest.approx(45.0)
    assert profile.altitude_at(Deg(100.05)) == pytest.approx(25.0)


@given(horizon_profiles(), st.floats(min_value=1e-9, max_value=1e-4))
def test_altitude_at_is_continuous_across_north(profile: HorizonProfile, epsilon: float) -> None:
    """The classic wrap bug shows up as a step discontinuity at 0 deg."""
    west = float(profile.altitude_at(Deg(360.0 - epsilon)))
    east = float(profile.altitude_at(Deg(epsilon)))
    bound = _max_slope_deg_per_deg(profile) * 2.0 * epsilon + 1e-9
    assert abs(east - west) <= bound


@given(horizon_profiles(), ANY_AZIMUTH, st.floats(min_value=-1e-4, max_value=1e-4))
def test_altitude_at_is_continuous_everywhere(
    profile: HorizonProfile, azimuth: float, delta: float
) -> None:
    here = float(profile.altitude_at(Deg(azimuth)))
    there = float(profile.altitude_at(Deg(azimuth + delta)))
    bound = _max_slope_deg_per_deg(profile) * abs(delta) + 1e-9
    assert abs(there - here) <= bound


@given(
    st.floats(min_value=0.5, max_value=89.5),
    ALTITUDES,
    ALTITUDES,
    ALTITUDES,
)
def test_a_profile_straddling_zero_interpolates_through_north(
    offset: float, west: float, east: float, south: float
) -> None:
    """Due north is exactly halfway along the seam segment, whatever is due south."""
    profile = _profile((360.0 - offset, west), (offset, east), (180.0, south))
    assert float(profile.altitude_at(Deg(0.0))) == pytest.approx((west + east) / 2.0, abs=1e-9)


@given(horizon_profiles(), ANY_AZIMUTH)
def test_interpolation_never_overshoots_its_vertices(
    profile: HorizonProfile, azimuth: float
) -> None:
    altitudes = [float(point.altitude_deg) for point in profile.points]
    value = float(profile.altitude_at(Deg(azimuth)))
    assert min(altitudes) - 1e-9 <= value <= max(altitudes) + 1e-9


@given(horizon_profiles(), ANY_AZIMUTH, st.integers(min_value=-3, max_value=3))
def test_altitude_at_is_invariant_under_full_turns(
    profile: HorizonProfile, azimuth: float, turns: int
) -> None:
    here = float(profile.altitude_at(Deg(azimuth)))
    turned = float(profile.altitude_at(Deg(azimuth + 360.0 * turns)))
    assert turned == pytest.approx(here, abs=1e-9)


# --- the flat profile is the scalar threshold ------------------------------


@given(st.floats(min_value=-30.0, max_value=85.0), ANY_AZIMUTH)
def test_flat_horizon_is_constant_at_every_bearing(limit: float, azimuth: float) -> None:
    assert float(flat_horizon(Deg(limit)).altitude_at(Deg(azimuth))) == limit


@given(
    st.floats(min_value=-30.0, max_value=85.0),
    st.floats(min_value=-90.0, max_value=90.0),
    ANY_AZIMUTH,
)
def test_is_above_horizon_on_a_flat_profile_is_the_scalar_comparison(
    limit: float, altitude: float, azimuth: float
) -> None:
    profile = flat_horizon(Deg(limit))
    assert is_above_horizon(profile, altitude_deg=Deg(altitude), azimuth_deg=Deg(azimuth)) == (
        altitude > limit
    )


def test_a_body_exactly_on_the_skyline_is_not_above_it() -> None:
    profile = flat_horizon(Deg(20.0))
    assert not is_above_horizon(profile, altitude_deg=Deg(20.0), azimuth_deg=Deg(123.0))


# --- the horizon-aware solver ----------------------------------------------
#
# One site, one target, one night, chosen so the target rises, transits and sets
# well inside the window: Greenwich, M42 (RA 5.5881 h, dec -5.3911 deg), the
# night of 2026-01-15. Above 20 deg it sweeps azimuth 128 -> 232 deg through a
# 33.1 deg transit due south, which leaves plenty of skyline to put things on.

GREENWICH = Site(latitude_deg=Deg(51.4779), longitude_deg=Deg(-0.0015))
M42_RA_H = Hours(5.5881)
M42_DEC_DEG = Deg(-5.3911)
NIGHT_START = datetime(2026, 1, 15, 16, 0, tzinfo=UTC)
NIGHT_END = datetime(2026, 1, 16, 8, 0, tzinfo=UTC)


def _visibility(
    ephemeris: Ephemeris,
    profile: HorizonProfile,
    *,
    start: datetime = NIGHT_START,
    end: datetime = NIGHT_END,
    step_minutes: float = 1.0,
) -> tuple[VisibilityInterval, ...]:
    return horizon_visibility(
        ephemeris,
        GREENWICH,
        M42_RA_H,
        M42_DEC_DEG,
        start,
        end,
        profile,
        step_minutes=step_minutes,
    )


def test_a_flat_profile_reproduces_the_scalar_rise_and_set(ephemeris: Ephemeris) -> None:
    """The degenerate case: the same answer as ``target_events`` with the same limit."""
    intervals = horizon_visibility(
        ephemeris,
        GREENWICH,
        M42_RA_H,
        M42_DEC_DEG,
        NIGHT_START,
        NIGHT_END,
        flat_horizon(Deg(20.0)),
    )
    assert len(intervals) == 1
    scalar = {
        event.event: event.when
        for event in target_events(
            ephemeris, GREENWICH, M42_RA_H, M42_DEC_DEG, NIGHT_START, NIGHT_END, Deg(20.0)
        )
        if event.outcome is Outcome.OCCURS
    }
    assert abs((intervals[0].start - scalar["rises_above"]).total_seconds()) < 2.0
    assert abs((intervals[0].end - scalar["sets_below"]).total_seconds()) < 2.0
    assert not intervals[0].start_is_window_edge
    assert not intervals[0].end_is_window_edge


def test_a_chimney_splits_one_window_into_two(ephemeris: Ephemeris) -> None:
    """A 45 deg obstruction across the meridian, where the target only reaches 33 deg."""
    open_sky = _visibility(ephemeris, flat_horizon(Deg(20.0)))
    chimney = _profile(
        (0.0, 20.0), (174.9, 20.0), (175.0, 45.0), (185.0, 45.0), (185.1, 20.0), (300.0, 20.0)
    )
    blocked = _visibility(ephemeris, chimney)

    assert len(open_sky) == 1
    assert len(blocked) == 2
    first, second = blocked
    # Same outer edges, a real gap in the middle, and time genuinely lost.
    assert abs((first.start - open_sky[0].start).total_seconds()) < 2.0
    assert abs((second.end - open_sky[0].end).total_seconds()) < 2.0
    assert first.end < second.start
    assert not first.end_is_window_edge
    assert not second.start_is_window_edge
    lost_hours = open_sky[0].duration_hours - first.duration_hours - second.duration_hours
    assert 0.4 < lost_hours < 0.7

    # The gap really is the chimney: the target is behind it at the midpoint.
    midpoint = first.end + (second.start - first.end) / 2
    position = target_position(ephemeris, GREENWICH, M42_RA_H, M42_DEC_DEG, midpoint)
    assert not is_above_horizon(
        chimney, altitude_deg=position.altitude_deg, azimuth_deg=position.azimuth_deg
    )


#: A wall of trees from az 200 to 220 with a 0.4 deg notch in it at az 210. The
#: target crosses the notch in about 96 s -- far less than the 5-minute uniform
#: step used below, so only the vertex knots can find it.
NOTCHED_TREELINE = _profile(
    (0.0, 20.0),
    (199.9, 20.0),
    (200.0, 45.0),
    (209.75, 45.0),
    (209.8, 20.0),
    (210.0, 20.0),
    (210.2, 20.0),
    (210.25, 45.0),
    (220.0, 45.0),
    (220.1, 20.0),
    (300.0, 20.0),
)


def test_a_narrow_gap_between_obstructions_is_not_stepped_over(ephemeris: Ephemeris) -> None:
    coarse = _visibility(ephemeris, NOTCHED_TREELINE, step_minutes=5.0)
    assert len(coarse) == 3
    gap = coarse[1]
    assert 60.0 < (gap.end - gap.start).total_seconds() < 5.0 * 60.0

    # Ground truth: the same notch found by brute force at a 3-second step.
    fine = horizon_visibility(
        ephemeris,
        GREENWICH,
        M42_RA_H,
        M42_DEC_DEG,
        gap.start.replace(minute=0, second=0, microsecond=0),
        gap.end.replace(minute=59, second=0, microsecond=0),
        NOTCHED_TREELINE,
        step_minutes=0.05,
    )
    reference = [
        interval
        for interval in fine
        if not interval.start_is_window_edge and not interval.end_is_window_edge
    ]
    assert len(reference) == 1
    assert abs((gap.start - reference[0].start).total_seconds()) < 2.0
    assert abs((gap.end - reference[0].end).total_seconds()) < 2.0


def test_a_target_behind_the_skyline_all_night_yields_no_intervals(ephemeris: Ephemeris) -> None:
    assert _visibility(ephemeris, flat_horizon(Deg(85.0))) == ()


def test_a_window_that_starts_mid_visibility_is_flagged_at_its_edges(
    ephemeris: Ephemeris,
) -> None:
    intervals = _visibility(
        ephemeris,
        flat_horizon(Deg(20.0)),
        start=datetime(2026, 1, 15, 20, 0, tzinfo=UTC),
        end=datetime(2026, 1, 15, 23, 0, tzinfo=UTC),
    )
    assert len(intervals) == 1
    assert intervals[0].start_is_window_edge
    assert intervals[0].end_is_window_edge
    assert intervals[0].duration_hours == pytest.approx(3.0)


def test_the_solver_refuses_a_reversed_window(ephemeris: Ephemeris) -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        _visibility(ephemeris, flat_horizon(Deg(20.0)), start=NIGHT_END, end=NIGHT_START)


def test_the_solver_refuses_a_naive_datetime(ephemeris: Ephemeris) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        horizon_visibility(
            ephemeris,
            GREENWICH,
            M42_RA_H,
            M42_DEC_DEG,
            NIGHT_START.replace(tzinfo=None),
            NIGHT_END,
            flat_horizon(Deg(20.0)),
        )


def test_an_absurd_grid_is_refused_rather_than_allocated(ephemeris: Ephemeris) -> None:
    with pytest.raises(ValueError, match=f"over the {MAX_VISIBILITY_SAMPLES} ceiling"):
        _visibility(
            ephemeris,
            flat_horizon(Deg(20.0)),
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2027, 1, 1, tzinfo=UTC),
            step_minutes=1.0,
        )


@pytest.mark.parametrize("step_minutes", [0.0, -1.0])
def test_the_solver_refuses_a_non_positive_step(ephemeris: Ephemeris, step_minutes: float) -> None:
    with pytest.raises(ValueError, match="step_minutes must be positive"):
        _visibility(ephemeris, flat_horizon(Deg(20.0)), step_minutes=step_minutes)
