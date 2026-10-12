"""``ssc.astro.bodies`` against JPL Horizons, plus the invariants of its shape.

The acceptance gate here is the same kind as ``test_almanac_validation.py``'s:
an *independent* source, checked in with its exact query, is the thing that
decides whether the module is right. Horizons runs DE441 with its own
light-time, deflection, aberration and precession chain, so agreement is
evidence about the whole pipeline rather than about the kernel alone.

Three separate comparisons are made, because they fail for different reasons:

* **astrometric ICRF** (Horizons quantity 1) against skyfield's
  ``observe(...).radec()`` — tests the ephemeris, light-time and the
  topocentric observer.
* **apparent, equinox of date** (Horizons quantity 2) against
  ``observe(...).apparent().radec(epoch=t)`` — adds deflection, aberration,
  precession and nutation.
* what the module **actually returns**, apparent place referred to J2000,
  which is the first plus aberration and must therefore sit *between* the two.
  ``test_returned_frame_is_apparent_not_astrometric`` pins that so the two
  cannot be silently swapped.

Run ``pytest -s tests/astro/test_bodies.py`` to print the deviation table.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ssc.astro.bodies import (
    BODY_ORDER,
    BodyId,
    BodyKind,
    bodies_credit,
    body_position,
    body_positions,
    separation_deg,
)
from ssc.astro.events import Ephemeris, Site
from ssc.astro.units import Deg, Metres

REFERENCE_PATH = Path(__file__).parent / "horizons_bodies_reference.json"

with REFERENCE_PATH.open("rb") as _handle:
    REFERENCE: dict[str, Any] = json.load(_handle)

#: Filled by the parametrised position tests, reported by the last test.
DEVIATIONS: list[tuple[str, float, float]] = []

# Tolerances, and why each is what it is. Each is roughly 5x the worst
# deviation actually measured, which is tight enough to fail on a real
# regression and loose enough to survive a skyfield point release.
#
# 50 mas on the astrometric position. DE440s against Horizons' DE441 agrees to
# well under a milliarcsecond for the planets; the slack is all the Moon, at
# 10.5 mas, where the topocentric correction is ~1 deg and this agreement says
# the observer geometry (WGS84 site, UT1, polar motion) matches to 1 part in
# 350 000 of it.
ASTROMETRIC_TOLERANCE_ARCSEC = 0.05

# 250 mas once precession, nutation and aberration are added. The residual is
# ~50 mas and near-identical for every body, i.e. a frame-rotation difference
# between skyfield's and Horizons' Earth-orientation models rather than
# anything per-body. Looser than the astrometric bound for that reason.
APPARENT_OF_DATE_TOLERANCE_ARCSEC = 0.25

# 0.02 deg on the phase angle: Horizons' S-T-O carries stellar aberration on
# the down-leg and skyfield's phase_angle() does not, which is ~20 arcsec for
# the Moon by Horizons' own documentation.
PHASE_ANGLE_TOLERANCE_DEG = 0.02

# 1e-4 on illuminated fraction: the same aberration difference, projected onto
# a quantity that is at most 1.
ILLUMINATED_TOLERANCE = 1.0e-4

# 1e-5 relative on apparent diameter — worst measured is 3.6e-7, which is the
# rounding of Horizons' own printed digits. Horizons turns out to use the same
# IAU radii this module does, so the comparison tests the geometry and the
# distance rather than the radius table, and can afford to be strict: a
# polar-instead-of-equatorial radius (0.3% for Saturn, 6.5% for Jupiter) or an
# arcmin/arcsec slip fails here immediately.
DIAMETER_TOLERANCE_FRACTION = 1.0e-5


@pytest.fixture(scope="module")
def horizons_site() -> Site:
    site = REFERENCE["site"]
    return Site(
        latitude_deg=Deg(site["latitude_deg"]),
        longitude_deg=Deg(site["longitude_deg"]),
        elevation_m=Metres(site["elevation_m"]),
    )


@pytest.fixture(scope="module")
def horizons_instant() -> datetime:
    return datetime.fromisoformat(REFERENCE["instant_utc"].replace("Z", "+00:00"))


def _sep_arcsec(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    """Great-circle separation in arcsec, from two RA/Dec pairs in degrees.

    Vincenty (``atan2``), **not** the cosine rule. The whole point of this
    file is to resolve milliarcsecond disagreements, and ``acos`` cannot: near
    zero separation ``cos(theta) ~ 1 - theta^2/2``, so double precision floors
    the answer at ~3 mas and returns 0 or pure noise below that. Written with
    ``acos`` first, this file reported nine bodies agreeing to *exactly* zero
    and Uranus to 3.07 mas; the real figures are 2 mas and 0.002 mas.
    """
    ra1, dec1, ra2, dec2 = map(math.radians, (ra1_deg, dec1_deg, ra2_deg, dec2_deg))
    delta_ra = ra2 - ra1
    numerator = math.hypot(
        math.cos(dec2) * math.sin(delta_ra),
        math.cos(dec1) * math.sin(dec2) - math.sin(dec1) * math.cos(dec2) * math.cos(delta_ra),
    )
    denominator = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(
        delta_ra
    )
    return math.degrees(math.atan2(numerator, denominator)) * 3600.0


def _skyfield_position(ephemeris: Ephemeris, site: Site, when: datetime, body_id: BodyId) -> Any:
    from ssc.astro.bodies import _SPK_TARGET

    instant = ephemeris.timescale.from_datetime(when)
    return ephemeris.observer(site).at(instant).observe(ephemeris.kernel[_SPK_TARGET[body_id]])


# -- the acceptance gate ------------------------------------------------------ #


@pytest.mark.parametrize("body_id", BODY_ORDER, ids=lambda b: b.value)
def test_astrometric_icrf_matches_horizons(
    ephemeris: Ephemeris, horizons_site: Site, horizons_instant: datetime, body_id: BodyId
) -> None:
    """Ephemeris + light-time + topocentric observer, before aberration."""
    reference = REFERENCE["bodies"][body_id.value]["barycentre"]
    ra, dec, _ = _skyfield_position(ephemeris, horizons_site, horizons_instant, body_id).radec()
    deviation = _sep_arcsec(
        float(ra.hours) * 15.0,
        float(dec.degrees),
        reference["ra_icrf_deg"],
        reference["dec_icrf_deg"],
    )
    DEVIATIONS.append(
        (f"{body_id.value} astrometric ICRF", deviation, ASTROMETRIC_TOLERANCE_ARCSEC)
    )
    assert deviation <= ASTROMETRIC_TOLERANCE_ARCSEC, (
        f"{body_id.value}: astrometric ICRF is {deviation:.4f} arcsec from Horizons"
    )


@pytest.mark.parametrize("body_id", BODY_ORDER, ids=lambda b: b.value)
def test_apparent_of_date_matches_horizons(
    ephemeris: Ephemeris, horizons_site: Site, horizons_instant: datetime, body_id: BodyId
) -> None:
    """The same chain plus deflection, aberration, precession and nutation.

    The module does not *return* equinox-of-date coordinates, but it is built
    on the ``apparent()`` position that produces them; checking them against
    Horizons quantity 2 is the only way to test the aberration and deflection
    terms against an outside source at all.
    """
    reference = REFERENCE["bodies"][body_id.value]["barycentre"]
    instant = ephemeris.timescale.from_datetime(horizons_instant)
    apparent = _skyfield_position(ephemeris, horizons_site, horizons_instant, body_id).apparent()
    ra, dec, _ = apparent.radec(epoch=instant)
    deviation = _sep_arcsec(
        float(ra.hours) * 15.0,
        float(dec.degrees),
        reference["ra_apparent_of_date_deg"],
        reference["dec_apparent_of_date_deg"],
    )
    DEVIATIONS.append(
        (f"{body_id.value} apparent of date", deviation, APPARENT_OF_DATE_TOLERANCE_ARCSEC)
    )
    assert deviation <= APPARENT_OF_DATE_TOLERANCE_ARCSEC, (
        f"{body_id.value}: apparent-of-date is {deviation:.4f} arcsec from Horizons"
    )


@pytest.mark.parametrize("body_id", BODY_ORDER, ids=lambda b: b.value)
def test_returned_frame_is_apparent_not_astrometric(
    ephemeris: Ephemeris, horizons_site: Site, horizons_instant: datetime, body_id: BodyId
) -> None:
    """What the module returns must be apparent place in the J2000 frame.

    Its offset from Horizons' astrometric ICRF is annual aberration alone,
    which is bounded by the aberration constant 20.49 arcsec (plus a few
    hundredths for deflection near the Sun and for the Moon's own diurnal
    aberration). Both bounds are asserted: a lower one, so that returning the
    astrometric position by mistake fails here rather than passing quietly.
    """
    reference = REFERENCE["bodies"][body_id.value]["barycentre"]
    position = body_position(ephemeris, horizons_site, horizons_instant, body_id)
    offset = _sep_arcsec(
        position.ra_j2000_h * 15.0,
        position.dec_j2000_deg,
        reference["ra_icrf_deg"],
        reference["dec_icrf_deg"],
    )
    assert 1.0 <= offset <= 21.0, (
        f"{body_id.value}: returned position is {offset:.3f} arcsec from the astrometric "
        "ICRF place; expected annual aberration only (1-21 arcsec). Below 1 arcsec means "
        "the astrometric position is being returned instead of the apparent one."
    )


@pytest.mark.parametrize("body_id", BODY_ORDER, ids=lambda b: b.value)
def test_apparent_diameter_matches_horizons(
    ephemeris: Ephemeris, horizons_site: Site, horizons_instant: datetime, body_id: BodyId
) -> None:
    reference = REFERENCE["bodies"][body_id.value]["centre"]
    expected_arcsec = reference["angular_diameter_arcsec"]
    assert expected_arcsec is not None, f"no Horizons Ang-diam for {body_id.value}"
    got_arcsec = (
        body_position(ephemeris, horizons_site, horizons_instant, body_id).apparent_diameter_arcmin
        * 60.0
    )
    relative = abs(got_arcsec - expected_arcsec) / expected_arcsec
    DEVIATIONS.append(
        (f"{body_id.value} diameter (relative)", relative, DIAMETER_TOLERANCE_FRACTION)
    )
    assert relative <= DIAMETER_TOLERANCE_FRACTION, (
        f"{body_id.value}: apparent diameter {got_arcsec:.4f} arcsec vs Horizons "
        f"{expected_arcsec:.4f} arcsec ({relative:.2%})"
    )


@pytest.mark.parametrize(
    "body_id", [b for b in BODY_ORDER if b is not BodyId.SUN], ids=lambda b: b.value
)
def test_illumination_and_phase_match_horizons(
    ephemeris: Ephemeris, horizons_site: Site, horizons_instant: datetime, body_id: BodyId
) -> None:
    reference = REFERENCE["bodies"][body_id.value]["centre"]
    position = body_position(ephemeris, horizons_site, horizons_instant, body_id)
    assert position.illuminated_fraction is not None
    assert position.phase_angle_deg is not None

    fraction_deviation = abs(
        position.illuminated_fraction - reference["illuminated_percent"] / 100.0
    )
    phase_deviation = abs(position.phase_angle_deg - reference["sun_target_observer_deg"])
    DEVIATIONS.append(
        (f"{body_id.value} illuminated fraction", fraction_deviation, ILLUMINATED_TOLERANCE)
    )
    DEVIATIONS.append(
        (f"{body_id.value} phase angle (deg)", phase_deviation, PHASE_ANGLE_TOLERANCE_DEG)
    )
    assert fraction_deviation <= ILLUMINATED_TOLERANCE
    assert phase_deviation <= PHASE_ANGLE_TOLERANCE_DEG


def test_barycentre_choice_is_below_sky_map_resolution() -> None:
    """The module docstring claims the barycentre-versus-centre shortcut costs
    at most a few hundredths of an arcsecond. Horizons is asked both ways in
    the fixture, so that claim is checked rather than asserted in prose."""
    worst = 0.0
    for body_id in BODY_ORDER:
        entry = REFERENCE["bodies"][body_id.value]
        offset = _sep_arcsec(
            entry["barycentre"]["ra_icrf_deg"],
            entry["barycentre"]["dec_icrf_deg"],
            entry["centre"]["ra_icrf_deg"],
            entry["centre"]["dec_icrf_deg"],
        )
        worst = max(worst, offset)
    assert worst <= 0.1, f"barycentre offset reaches {worst:.4f} arcsec"
    DEVIATIONS.append(("barycentre vs centre (arcsec)", worst, 0.1))


def test_worst_case_deviations(capsys: pytest.CaptureFixture[str]) -> None:
    """Prints the table the report quotes. Must run last in the file."""
    if not DEVIATIONS:
        pytest.skip("no deviations recorded (the ephemeris fixture was skipped)")
    with capsys.disabled():
        print(f"\n  {'quantity':<34} {'deviation':>12} {'tolerance':>12}")
        for label, value, tolerance in DEVIATIONS:
            print(f"  {label:<34} {value:>12.6f} {tolerance:>12.6f}")
    assert all(value <= tolerance for _, value, tolerance in DEVIATIONS)


# -- a pinned configuration that needs no ephemeris service ------------------- #


def test_total_lunar_eclipse_2026_03_03_moon_is_full(
    ephemeris: Ephemeris, horizons_site: Site
) -> None:
    """The total lunar eclipse of 3 March 2026, greatest eclipse 11:34 UTC
    (NASA GSFC five-millennium canon of lunar eclipses).

    Independently known without any ephemeris software: at greatest eclipse
    the Moon is by definition full and inside the Earth's umbra, so the
    illuminated fraction must round to 1.000, the phase angle must be about a
    degree, and the Moon must be very nearly opposite the Sun. Three different
    outputs of this module, pinned by one published fact.

    "About a degree" rather than "about zero" because these are **topocentric**
    numbers: seen from a point on the surface rather than from the geocentre,
    the Sun-Moon-observer angle picks up the Moon's diurnal parallax, which
    reaches ~1 deg. From Bratislava the eclipsed Moon is below the horizon at
    11:34 UTC and the parallax is close to its maximum, which is why the
    bound is 1.5 deg. A geocentric implementation would show ~0.03 deg here —
    the difference is the test noticing that the observer is real.
    """
    when = datetime(2026, 3, 3, 11, 34, 0, tzinfo=UTC)
    moon = body_position(ephemeris, horizons_site, when, BodyId.MOON)
    sun = body_position(ephemeris, horizons_site, when, BodyId.SUN)

    assert moon.illuminated_fraction is not None
    assert moon.phase_angle_deg is not None
    assert moon.illuminated_fraction > 0.9995, moon.illuminated_fraction
    assert moon.phase_angle_deg < 1.5, moon.phase_angle_deg
    assert separation_deg(sun, moon) > 178.5, separation_deg(sun, moon)


def test_moon_apparent_diameter_stays_in_the_known_range(
    ephemeris: Ephemeris, horizons_site: Site
) -> None:
    """A cheap guard against a units or radius slip that a single-epoch
    comparison would not catch.

    Bounds are derived, not remembered. Geocentric perigee and apogee are
    356 500 km and 406 720 km; a topocentric observer is at most one Earth
    radius (6 378 km) nearer the Moon (Moon overhead) or further from it
    (Moon at the nadir, which daily sampling does hit), so the topocentric
    distance runs 350 122 km to 413 098 km. With an equatorial radius of
    1 737.4 km, ``2 * asin(r / d)`` gives 34.12' down to 28.92'. That is
    wider than the 29.4'-34.1' figure quoted for *visible* full moons, and
    correctly so.
    """
    diameters = [
        body_position(
            ephemeris, horizons_site, datetime(2026, 1, day, 0, 0, tzinfo=UTC), BodyId.MOON
        ).apparent_diameter_arcmin
        for day in range(1, 29)
    ]
    assert 28.9 <= min(diameters) <= 34.2
    assert 28.9 <= max(diameters) <= 34.2
    # A full anomalistic month is 27.55 days, so 28 daily samples must span
    # most of the perigee-apogee range; anything narrower means the distance
    # is not varying and a constant has been baked in.
    assert max(diameters) - min(diameters) > 2.0


# -- shape, ordering and the house rules -------------------------------------- #


def test_body_positions_returns_the_documented_set_in_order(
    ephemeris: Ephemeris, horizons_site: Site, horizons_instant: datetime
) -> None:
    positions = body_positions(ephemeris, horizons_site, horizons_instant)
    assert tuple(p.id for p in positions) == BODY_ORDER
    assert [p.name for p in positions][:3] == ["Sun", "Moon", "Mercury"]
    kinds = {p.id: p.kind for p in positions}
    assert kinds[BodyId.SUN] is BodyKind.SUN
    assert kinds[BodyId.MOON] is BodyKind.MOON
    assert all(kinds[b] is BodyKind.PLANET for b in BODY_ORDER if b.value not in {"sun", "moon"})


def test_body_positions_honours_a_subset(
    ephemeris: Ephemeris, horizons_site: Site, horizons_instant: datetime
) -> None:
    positions = body_positions(
        ephemeris, horizons_site, horizons_instant, (BodyId.MARS, BodyId.SUN)
    )
    assert tuple(p.id for p in positions) == (BodyId.SUN, BodyId.MARS)


def test_sun_phase_fields_are_none_never_zero(
    ephemeris: Ephemeris, horizons_site: Site, horizons_instant: datetime
) -> None:
    """``None`` means "does not apply"; 0.0 would read as "unlit", which for
    the Sun is the opposite of the truth (CLAUDE.md failure handling)."""
    sun = body_position(ephemeris, horizons_site, horizons_instant, BodyId.SUN)
    assert sun.illuminated_fraction is None
    assert sun.phase_angle_deg is None


def test_ra_is_wrapped_into_the_json_boundary_interval(
    ephemeris: Ephemeris, horizons_site: Site
) -> None:
    """Sampled across a year so the 0h/24h wrap is actually crossed by every
    body, not merely assumed to be."""
    for month in range(1, 13):
        when = datetime(2026, month, 15, 3, 0, tzinfo=UTC)
        for position in body_positions(ephemeris, horizons_site, when):
            assert 0.0 <= position.ra_j2000_h < 24.0, position
            assert -90.0 <= position.dec_j2000_deg <= 90.0, position
            assert position.apparent_diameter_arcmin > 0.0, position
            if position.illuminated_fraction is not None:
                assert 0.0 <= position.illuminated_fraction <= 1.0, position
            if position.phase_angle_deg is not None:
                assert 0.0 <= position.phase_angle_deg <= 180.0, position


def test_naive_datetime_is_refused(ephemeris: Ephemeris, horizons_site: Site) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        body_position(
            ephemeris,
            horizons_site,
            # A deliberately naive datetime — the point of this test — so the
            # rule that forbids them is suppressed on the call itself. The
            # suppression was on the next line until `ruff format` reflowed
            # this call and orphaned it onto the wrong argument.
            datetime(2026, 9, 9, 21, 0),  # noqa: DTZ001
            BodyId.MARS,
        )


def test_moon_parallax_makes_the_site_matter(
    ephemeris: Ephemeris, horizons_instant: datetime
) -> None:
    """Topocentric, not geocentric: two sites 90 deg of latitude apart must
    see the Moon in measurably different places. If this ever collapses to
    zero the observer has been dropped somewhere in the chain."""
    north = Site(latitude_deg=Deg(60.0), longitude_deg=Deg(0.0), elevation_m=Metres(0.0))
    equator = Site(latitude_deg=Deg(-30.0), longitude_deg=Deg(0.0), elevation_m=Metres(0.0))
    a = body_position(ephemeris, north, horizons_instant, BodyId.MOON)
    b = body_position(ephemeris, equator, horizons_instant, BodyId.MOON)
    assert separation_deg(a, b) > 0.3

    # The Sun, 400 times further away, moves by well under an arcminute.
    sun_shift = separation_deg(
        body_position(ephemeris, north, horizons_instant, BodyId.SUN),
        body_position(ephemeris, equator, horizons_instant, BodyId.SUN),
    )
    assert 0.0 < sun_shift < 0.01


def test_credit_names_both_sources() -> None:
    credit = bodies_credit()
    assert "DE440s" in credit
    assert "IAU" in credit
