"""Hypothesis properties for the astro core.

Three families, per the M3 gate: angle wrapping, RA wrap at 0h/24h, and
``keep_region`` subset of ``frame`` subset of ``swept_envelope``.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from ssc.astro.framing import (
    DistortionModel,
    FieldGeometry,
    Provenance,
    Session,
    altaz_session_envelope,
    rectangle_radius_deg,
)
from ssc.astro.units import (
    ArcSec,
    Deg,
    Hours,
    deg_to_hours,
    hours_to_deg,
    signed_delta_deg,
    wrap_deg_180,
    wrap_deg_360,
    wrap_hours,
)

ANGLES = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False, width=64)
HOURS = st.floats(min_value=-1e5, max_value=1e5, allow_nan=False, allow_infinity=False, width=64)


def _congruent(left: float, right: float, period: float, tolerance: float) -> bool:
    difference = (left - right) % period
    return min(difference, period - difference) <= tolerance


# --- angle wrapping --------------------------------------------------------


@given(ANGLES)
def test_wrap_deg_360_stays_in_its_half_open_interval(value: float) -> None:
    wrapped = float(wrap_deg_360(value))
    assert 0.0 <= wrapped < 360.0


@given(ANGLES)
def test_wrap_deg_180_is_closed_at_plus_180(value: float) -> None:
    """The stated convention is (-180, +180]: -180 must never be returned."""
    wrapped = float(wrap_deg_180(value))
    assert -180.0 < wrapped <= 180.0


@given(ANGLES)
def test_wrapping_preserves_the_angle(value: float) -> None:
    assert _congruent(float(wrap_deg_360(value)), value, 360.0, 1e-6)
    assert _congruent(float(wrap_deg_180(value)), value, 360.0, 1e-6)


@given(ANGLES)
def test_wrapping_is_idempotent(value: float) -> None:
    once = wrap_deg_360(value)
    assert float(wrap_deg_360(float(once))) == float(once)
    once_180 = wrap_deg_180(value)
    assert float(wrap_deg_180(float(once_180))) == float(once_180)


@given(st.floats(min_value=-179.9, max_value=180.0, allow_nan=False))
def test_wrap_deg_180_is_the_identity_inside_its_interval(value: float) -> None:
    assert float(wrap_deg_180(value)) == value


@given(ANGLES, ANGLES)
def test_signed_delta_is_the_shortest_rotation(first: float, second: float) -> None:
    delta = float(signed_delta_deg(first, second))
    assert -180.0 < delta <= 180.0
    assert _congruent(first + delta, second, 360.0, 1e-6)


@given(ANGLES)
def test_signed_delta_to_self_is_zero(value: float) -> None:
    assert float(signed_delta_deg(value, value)) == 0.0


# --- right ascension wrap at 0h / 24h --------------------------------------


@given(HOURS)
def test_wrap_hours_stays_in_its_half_open_interval(value: float) -> None:
    assert 0.0 <= float(wrap_hours(value)) < 24.0


@given(HOURS)
def test_wrap_hours_preserves_the_angle(value: float) -> None:
    assert _congruent(float(wrap_hours(value)), value, 24.0, 1e-9)


def test_24h_and_0h_are_the_same_right_ascension() -> None:
    assert float(wrap_hours(24.0)) == 0.0
    assert float(wrap_hours(-0.0)) == 0.0
    assert float(wrap_hours(48.0)) == 0.0
    assert float(wrap_hours(-1e-15)) < 24.0


@given(HOURS)
def test_hours_and_degrees_wrap_to_the_same_place(value: float) -> None:
    """RA is hours at the JSON boundary and degrees inside; crossing 0h/24h must not
    move the target."""
    through_degrees = float(deg_to_hours(wrap_deg_360(float(hours_to_deg(Hours(value))))))
    assert _congruent(through_degrees, float(wrap_hours(value)), 24.0, 1e-9)


@given(st.floats(min_value=0.0, max_value=24.0, exclude_max=True, allow_nan=False))
def test_hours_to_deg_round_trips(value: float) -> None:
    assert float(deg_to_hours(hours_to_deg(Hours(value)))) == pytest.approx(value)


# --- keep_region subset frame subset swept_envelope ------------------------


def _geometry(width_deg: float, height_deg: float) -> FieldGeometry:
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


@settings(deadline=None, max_examples=40)
@given(
    width=st.floats(min_value=0.2, max_value=8.0),
    height=st.floats(min_value=0.2, max_value=8.0),
    latitude=st.floats(min_value=-70.0, max_value=70.0),
    declination=st.floats(min_value=-85.0, max_value=85.0),
    hour_angle=st.floats(min_value=-90.0, max_value=90.0),
    duration=st.floats(min_value=0.0, max_value=6.0),
    pa_offset=st.floats(min_value=-180.0, max_value=180.0),
)
def test_keep_region_inside_frame_inside_swept_envelope(
    width: float,
    height: float,
    latitude: float,
    declination: float,
    hour_angle: float,
    duration: float,
    pa_offset: float,
) -> None:
    """The invariant the whole planning tab rests on, checked radially: at every
    boundary direction the intersection is no larger than any single frame, and the
    union is no smaller."""
    geometry = _geometry(width, height)
    session = Session(
        latitude_deg=Deg(latitude),
        declination_deg=Deg(declination),
        hour_angle_start_deg=Deg(hour_angle),
        duration_hours=duration,
        pa_offset_deg=Deg(pa_offset),
    )
    envelope = altaz_session_envelope(geometry, session, boundary_samples=72)

    for sample in (envelope.samples[0], envelope.samples[-1]):
        position_angle = float(sample.position_angle_deg)
        for angle, keep, swept in zip(
            envelope.boundary_angles_deg,
            envelope.keep_radii_deg,
            envelope.swept_radii_deg,
            strict=True,
        ):
            frame = float(rectangle_radius_deg(geometry, float(angle) - position_angle))
            assert float(keep) <= frame + 1e-9
            assert frame <= float(swept) + 1e-9


@settings(deadline=None, max_examples=30)
@given(
    width=st.floats(min_value=0.2, max_value=8.0),
    height=st.floats(min_value=0.2, max_value=8.0),
    latitude=st.floats(min_value=-70.0, max_value=70.0),
    declination=st.floats(min_value=-85.0, max_value=85.0),
    hour_angle=st.floats(min_value=-90.0, max_value=90.0),
    duration=st.floats(min_value=0.0, max_value=8.0),
)
def test_keep_region_always_contains_the_inscribed_circle(
    width: float,
    height: float,
    latitude: float,
    declination: float,
    hour_angle: float,
    duration: float,
) -> None:
    """Rotation about the frame centre can never eat into the inscribed circle, so the
    lower bound SSC quotes is always honest."""
    assume(abs(latitude) < 89.9)
    geometry = _geometry(width, height)
    envelope = altaz_session_envelope(
        geometry,
        Session(
            latitude_deg=Deg(latitude),
            declination_deg=Deg(declination),
            hour_angle_start_deg=Deg(hour_angle),
            duration_hours=duration,
        ),
        boundary_samples=72,
    )
    floor = min(width, height) / 2.0
    assert min(float(radius) for radius in envelope.keep_radii_deg) >= floor - 1e-9
    assert (
        max(float(radius) for radius in envelope.swept_radii_deg)
        <= (math.hypot(width, height) / 2.0) + 1e-9
    )
