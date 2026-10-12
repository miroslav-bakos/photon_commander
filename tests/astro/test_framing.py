"""Unit tests for ssc.astro.framing: plate scale, provenance labelling, parallactic
angle and the four alt-az shapes."""

from __future__ import annotations

import copy
import math
import tomllib
from typing import Any

import pytest

from ssc.astro.framing import (
    HIGH_ALTITUDE_TRANSIT_DEG,
    DistortionModel,
    MountMode,
    Profile,
    Provenance,
    Session,
    altaz_session_envelope,
    altitude_deg,
    eq_session_envelope,
    field_geometry,
    frame_shape,
    is_zenith_singular,
    parallactic_angle_deg,
    parse_profile,
    pixel_scale_arcsec,
    position_angle_deg,
    rectangle_radius_deg,
    rotation_rate_deg_per_hour,
    sample_session,
    session_envelope,
)
from ssc.astro.units import Deg
from tests.astro.conftest import PROFILE_PATH


@pytest.fixture(scope="module")
def raw_profile() -> dict[str, Any]:
    with PROFILE_PATH.open("rb") as handle:
        data: dict[str, Any] = tomllib.load(handle)
    return data


# --- plate scale and field of view -----------------------------------------


def test_pixel_scale_formula() -> None:
    """206.265 * pixel_um / focal_length_mm (DESIGN 3.2)."""
    assert float(pixel_scale_arcsec(2.9, 160.0)) == pytest.approx(206.265 * 2.9 / 160.0)


@pytest.mark.parametrize("focal", [0.0, -160.0])
def test_pixel_scale_rejects_impossible_optics(focal: float) -> None:
    with pytest.raises(ValueError, match="focal_length_mm"):
        pixel_scale_arcsec(2.9, focal)


def test_tele_matches_the_grounding_facts(profile: Profile) -> None:
    """DESIGN 0.1: 3.74 arcsec/px, 4.58 deg diagonal against a published 4.6 deg.

    THE FRAME IS PORTRAIT: 2.24 wide by 3.99 tall. Measured from the device, not
    assumed — Alpaca reports cameraxsize=2160 / cameraysize=3840 and every FITS
    the scope writes carries NAXIS1=2160 / NAXIS2=3840. The IMX585 is a 16:9
    part mounted rotated. The profile previously had it landscape, so every FOV
    overlay was drawn rotated 90 degrees; this assertion is what stops that
    returning.

    DESIGN 0.1 and the profile comment both say "within 0.5%". The derivation
    actually lands at 0.53% (4.5754 deg vs 4.6), so the bound asserted here is
    0.6% and the doc wording is one decimal optimistic. The conclusion — the
    datasheet figures are a trustworthy starting point — is unaffected.
    """
    geometry = field_geometry(profile, "tele")
    assert float(geometry.pixel_scale_arcsec) == pytest.approx(3.74, abs=0.005)
    assert float(geometry.width_deg) == pytest.approx(2.24, abs=0.005)
    assert float(geometry.height_deg) == pytest.approx(3.99, abs=0.005)
    assert float(geometry.height_deg) > float(geometry.width_deg), "the frame is portrait"
    assert float(geometry.diagonal_deg) == pytest.approx(4.58, abs=0.01)
    assert abs(float(geometry.diagonal_deg) - 4.6) / 4.6 < 0.006


def test_inscribed_circle_is_the_short_side(profile: Profile) -> None:
    """DESIGN 3.2 names the number the planner must surface: 2.24 deg."""
    geometry = field_geometry(profile, "tele")
    assert float(geometry.inscribed_circle_deg) == pytest.approx(2.24, abs=0.005)
    assert geometry.inscribed_circle_deg == min(geometry.width_deg, geometry.height_deg)


def test_wide_geometry_is_rectilinear_and_exceeds_the_published_diagonal(
    profile: Profile,
) -> None:
    """The wide field does NOT match its published 63 deg diagonal, and should not.

    The device reports 1.6 um pixels on camera/1 (Alpaca `pixelsizex`), which is
    right for an IMX586: a 0.8 um quad-bayer part binned 4-in-1. The profile
    previously guessed 1.5 um, which happened to reproduce the published 63 deg
    exactly — a coincidence that was hiding the real pixel pitch.

    With the measured 1.6 um and the published 6.0 mm focal length the
    rectilinear diagonal is 67.3 deg, about 7% above the published figure. That
    is consistent with the lens' real barrel distortion, which the profile
    already flags as ``distortion = "approximate"``: a rectilinear projection
    overestimates the true field, so the published 63 deg is plausibly the
    honest usable field and 67.3 is the geometric idealisation.

    So this asserts the geometry we actually compute, and records why it differs
    from the spec, rather than fitting a parameter until the number matches.
    DESIGN section 7's plate-solve calibration is what settles it for real.
    """
    geometry = field_geometry(profile, "wide")
    assert float(geometry.diagonal_deg) == pytest.approx(67.3, abs=0.2)
    assert float(geometry.diagonal_deg) > 63.0
    assert float(geometry.height_deg) > float(geometry.width_deg), "the frame is portrait"


def test_unknown_optic_names_the_ones_that_exist(profile: Profile) -> None:
    with pytest.raises(KeyError, match="tele"):
        field_geometry(profile, "hyperspectral")


# --- provenance at the boundary --------------------------------------------


def test_uncalibrated_profile_is_labelled_datasheet(profile: Profile) -> None:
    """Invariant 7: [calibration] measured = false must reach the API boundary as a
    value, not as a comment in a TOML file."""
    geometry = field_geometry(profile, "tele")
    assert profile.calibration.measured is False
    assert geometry.provenance is Provenance.DATASHEET
    assert any("plate solve" in caveat for caveat in geometry.caveats)


def test_wide_lens_is_labelled_approximate(profile: Profile) -> None:
    """DESIGN 0.1: the wide overlay is indicative only, and says so at the boundary."""
    geometry = field_geometry(profile, "wide")
    assert geometry.distortion is DistortionModel.APPROXIMATE
    assert any("approximate" in caveat.lower() for caveat in geometry.caveats)


def test_tele_is_not_labelled_approximate(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")
    assert geometry.distortion is DistortionModel.RECTILINEAR
    assert not any("barrel distortion" in caveat for caveat in geometry.caveats)


def test_measured_calibration_wins_and_is_labelled(raw_profile: dict[str, Any]) -> None:
    raw = copy.deepcopy(raw_profile)
    raw["calibration"] = {"measured": True, "pixel_scale_arcsec": 3.81, "pa_offset_deg": 1.5}
    geometry = field_geometry(parse_profile(raw), "tele")
    assert geometry.provenance is Provenance.MEASURED
    assert float(geometry.pixel_scale_arcsec) == pytest.approx(3.81)
    assert not any("plate solve" in caveat for caveat in geometry.caveats)


def test_calibration_claiming_a_measurement_without_one_is_rejected(
    raw_profile: dict[str, Any],
) -> None:
    """No silent degradation: measured = true with no measured value is a broken profile."""
    raw = copy.deepcopy(raw_profile)
    raw["calibration"] = {"measured": True}
    with pytest.raises(ValueError, match="measured = true"):
        parse_profile(raw)


# --- parallactic angle -----------------------------------------------------


def test_transit_south_of_the_zenith_has_zero_parallactic_angle() -> None:
    assert float(parallactic_angle_deg(Deg(0.0), Deg(20.0), Deg(45.0))) == pytest.approx(0.0)


def test_transit_north_of_the_zenith_flips_to_180() -> None:
    """A target that culminates between the zenith and the pole is upside down at
    transit — and (-180, +180] is closed at +180 so it reads +180, not -180."""
    assert float(parallactic_angle_deg(Deg(0.0), Deg(60.0), Deg(45.0))) == pytest.approx(180.0)


def test_parallactic_angle_is_antisymmetric_in_hour_angle() -> None:
    east = float(parallactic_angle_deg(Deg(-37.0), Deg(12.0), Deg(45.0)))
    west = float(parallactic_angle_deg(Deg(37.0), Deg(12.0), Deg(45.0)))
    assert east == pytest.approx(-west)


def test_parallactic_angle_matches_meeus_141_by_hand() -> None:
    """Recompute Meeus 14.1 straight from the formula and compare — a guard against a
    refactor quietly changing the argument order."""
    hour_angle, declination, latitude = 42.0, -13.0, 51.4779
    expected = math.degrees(
        math.atan2(
            math.sin(math.radians(hour_angle)),
            math.tan(math.radians(latitude)) * math.cos(math.radians(declination))
            - math.sin(math.radians(declination)) * math.cos(math.radians(hour_angle)),
        )
    )
    got = float(parallactic_angle_deg(Deg(hour_angle), Deg(declination), Deg(latitude)))
    assert got == pytest.approx(expected)


def test_position_angle_adds_the_offset() -> None:
    base = float(parallactic_angle_deg(Deg(30.0), Deg(10.0), Deg(45.0)))
    assert float(position_angle_deg(Deg(30.0), Deg(10.0), Deg(45.0), Deg(12.5))) == pytest.approx(
        base + 12.5
    )


def test_geographic_poles_are_rejected() -> None:
    with pytest.raises(ValueError, match="undefined at latitude"):
        parallactic_angle_deg(Deg(0.0), Deg(10.0), Deg(90.0))


def test_zenith_is_flagged_not_crashed() -> None:
    """DESIGN 3.2: the zenith is a genuine singularity. It must be reported, and it
    must not raise, clamp or return a nan."""
    latitude = Deg(44.4268)
    value = float(parallactic_angle_deg(Deg(0.0), latitude, latitude))
    assert not math.isnan(value)
    assert is_zenith_singular(Deg(0.0), latitude, latitude)
    assert not is_zenith_singular(Deg(0.0), Deg(20.0), latitude)


def test_rotation_rate_diverges_toward_the_zenith() -> None:
    latitude = Deg(45.0)
    near = abs(rotation_rate_deg_per_hour(Deg(0.0), Deg(44.9), latitude))
    far = abs(rotation_rate_deg_per_hour(Deg(0.0), Deg(20.0), latitude))
    assert near > 100.0 * far


# --- shapes ----------------------------------------------------------------


def test_frame_corners_lie_on_the_half_diagonal(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")
    half_diagonal = float(geometry.diagonal_deg) / 2.0
    for position_angle in (0.0, 33.0, -117.0, 180.0):
        for corner in frame_shape(geometry, Deg(position_angle)).points:
            assert math.hypot(float(corner.x_deg), float(corner.y_deg)) == pytest.approx(
                half_diagonal
            )


def test_unrotated_frame_points_north(profile: Profile) -> None:
    """At PA = 0 the sensor's short axis is north-south and its long axis east-west."""
    geometry = field_geometry(profile, "tele")
    corners = frame_shape(geometry, Deg(0.0)).points
    assert float(corners[0].x_deg) == pytest.approx(geometry.half_width_deg)
    assert float(corners[0].y_deg) == pytest.approx(geometry.half_height_deg)


def test_rectangle_radius_hits_the_sides_and_the_corner(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")
    assert float(rectangle_radius_deg(geometry, 0.0)) == pytest.approx(geometry.half_height_deg)
    assert float(rectangle_radius_deg(geometry, 90.0)) == pytest.approx(geometry.half_width_deg)
    corner = math.degrees(math.atan2(geometry.half_width_deg, geometry.half_height_deg))
    assert float(rectangle_radius_deg(geometry, corner)) == pytest.approx(
        float(geometry.diagonal_deg) / 2.0
    )


# --- session envelopes -----------------------------------------------------


def test_eq_mode_does_not_rotate(profile: Profile) -> None:
    """DESIGN 3.2: in EQ mode the sensor is fixed with respect to the sky. One
    rectangle, the whole frame usable, no swept envelope to speak of."""
    geometry = field_geometry(profile, "tele")
    envelope = eq_session_envelope(geometry, Deg(20.0))
    assert envelope.mount is MountMode.EQ
    assert float(envelope.total_rotation_deg) == 0.0
    assert envelope.rotation_severity == "none"
    assert envelope.frame_start == envelope.frame_end
    assert envelope.swept_radii_deg == envelope.keep_radii_deg
    assert float(envelope.keep_region_min_diameter_deg) == pytest.approx(
        float(geometry.inscribed_circle_deg)
    )


def test_eq_mount_mode_never_takes_the_altaz_path(profile: Profile) -> None:
    """A long session that would rotate 140 deg in alt-az must not rotate at all in EQ."""
    geometry = field_geometry(profile, "tele")
    session = Session(
        latitude_deg=Deg(44.4268),
        declination_deg=Deg(41.0),
        hour_angle_start_deg=Deg(-30.0),
        duration_hours=4.0,
        pa_offset_deg=Deg(0.0),
    )
    equatorial = session_envelope(geometry, MountMode.EQ, session)
    altaz = session_envelope(geometry, MountMode.ALTAZ, session)
    assert float(equatorial.total_rotation_deg) == 0.0
    assert len(equatorial.samples) == 1
    assert abs(float(altaz.total_rotation_deg)) > 100.0


def test_keep_region_never_shrinks_below_the_inscribed_circle(profile: Profile) -> None:
    """However violent the rotation, the intersection still contains the inscribed
    circle — the lower bound DESIGN 3.2 asks to be surfaced."""
    geometry = field_geometry(profile, "tele")
    session = Session(
        latitude_deg=Deg(44.4268),
        declination_deg=Deg(44.0),
        hour_angle_start_deg=Deg(-60.0),
        duration_hours=8.0,
    )
    envelope = altaz_session_envelope(geometry, session)
    floor = float(geometry.inscribed_circle_deg) / 2.0
    assert min(float(radius) for radius in envelope.keep_radii_deg) >= floor - 1e-9
    assert float(envelope.keep_region_min_diameter_deg) == pytest.approx(
        float(geometry.inscribed_circle_deg), abs=1e-6
    )


def test_short_session_keeps_almost_the_whole_frame(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")
    session = Session(
        latitude_deg=Deg(44.4268),
        declination_deg=Deg(-10.0),
        hour_angle_start_deg=Deg(-40.0),
        duration_hours=0.25,
    )
    envelope = altaz_session_envelope(geometry, session)
    assert abs(float(envelope.total_rotation_deg)) < 3.0
    assert envelope.rotation_severity == "none"
    kept = sum(float(radius) for radius in envelope.keep_radii_deg)
    swept = sum(float(radius) for radius in envelope.swept_radii_deg)
    assert kept / swept > 0.97, "a 15-minute session should barely erode the frame"


@pytest.mark.parametrize(
    ("declination", "duration", "severity"),
    [(-20.0, 0.5, "none"), (10.0, 2.0, "amber"), (40.0, 4.0, "red")],
)
def test_rotation_severity_thresholds(
    profile: Profile, declination: float, duration: float, severity: str
) -> None:
    """DESIGN 3.2: amber past ~15 deg, red past ~40 deg."""
    geometry = field_geometry(profile, "tele")
    session = Session(
        latitude_deg=Deg(44.4268),
        declination_deg=Deg(declination),
        hour_angle_start_deg=Deg(-15.0),
        duration_hours=duration,
    )
    assert altaz_session_envelope(geometry, session).rotation_severity == severity


def test_high_transit_is_flagged(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")
    session = Session(
        latitude_deg=Deg(44.4268),
        declination_deg=Deg(41.0),
        hour_angle_start_deg=Deg(-30.0),
        duration_hours=4.0,
    )
    envelope = altaz_session_envelope(geometry, session)
    assert float(envelope.max_altitude_deg) > float(HIGH_ALTITUDE_TRANSIT_DEG)
    assert envelope.high_altitude_transit
    assert any("zenith" in warning for warning in envelope.warnings)


def test_zenith_transit_reports_the_singularity_without_crashing(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")
    latitude = Deg(44.4268)
    session = Session(
        latitude_deg=latitude,
        declination_deg=latitude,
        hour_angle_start_deg=Deg(-1.0),
        duration_hours=2.0 / 15.0,
    )
    envelope = altaz_session_envelope(geometry, session)
    assert envelope.zenith_singularity
    assert any("undefined" in warning for warning in envelope.warnings)
    assert all(not math.isnan(float(s.position_angle_deg)) for s in envelope.samples)
    assert float(envelope.max_altitude_deg) > 89.9


def test_sampling_concentrates_where_rotation_is_fastest(profile: Profile) -> None:
    """Adaptive, not uniform: the samples must bunch near the meridian, where |dq/dt|
    is largest, instead of being spread evenly over the session."""
    session = Session(
        latitude_deg=Deg(45.0),
        declination_deg=Deg(44.0),
        hour_angle_start_deg=Deg(-60.0),
        duration_hours=120.0 / 15.04106864,
    )
    samples = sample_session(session)
    near = [s for s in samples if abs(float(s.hour_angle_deg)) <= 5.0]
    far = [s for s in samples if 50.0 <= abs(float(s.hour_angle_deg)) <= 60.0]
    near_density = len(near) / 10.0
    far_density = len(far) / 20.0
    assert near_density > 20.0 * far_density, (
        f"{len(near)} samples in 10 deg of hour angle around the meridian vs "
        f"{len(far)} in the 20 deg at the ends: that is not adaptive sampling"
    )


def test_sampled_position_angles_bracket_the_endpoints(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")
    session = Session(
        latitude_deg=Deg(44.4268),
        declination_deg=Deg(10.0),
        hour_angle_start_deg=Deg(-45.0),
        duration_hours=3.0,
        pa_offset_deg=Deg(7.0),
    )
    envelope = altaz_session_envelope(geometry, session)
    assert envelope.samples[0].elapsed_hours == 0.0
    assert envelope.samples[-1].elapsed_hours == pytest.approx(3.0)
    assert envelope.position_angle_start_deg == envelope.samples[0].position_angle_deg
    assert float(envelope.samples[0].altitude_deg) == pytest.approx(
        float(altitude_deg(Deg(-45.0), Deg(10.0), Deg(44.4268)))
    )


def test_zero_length_session_is_a_single_frame(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")
    session = Session(
        latitude_deg=Deg(44.4268),
        declination_deg=Deg(10.0),
        hour_angle_start_deg=Deg(-45.0),
        duration_hours=0.0,
    )
    envelope = altaz_session_envelope(geometry, session)
    assert len(envelope.samples) == 1
    assert envelope.frame_start == envelope.frame_end
    assert envelope.swept_radii_deg == envelope.keep_radii_deg


def test_negative_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="duration_hours"):
        Session(
            latitude_deg=Deg(45.0),
            declination_deg=Deg(10.0),
            hour_angle_start_deg=Deg(0.0),
            duration_hours=-1.0,
        )
