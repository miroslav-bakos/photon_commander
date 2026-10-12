# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Tests for :mod:`ssc.astro.frames` — the J2000 <-> mount-frame conversion.

The checks that matter, decided before the module was written:

1. **Round trip** to a stated tolerance, with the residual reported.
2. **Magnitude**, because a conversion that quietly did nothing would pass every
   round-trip test ever written. The 2026 offset must be ~20 arcmin.
3. **Direction**, because inverting the conversion *doubles* the error instead
   of cancelling it and both directions look plausible in isolation.
4. **An independent source** — JPL Horizons' own apparent places, the same
   fixture ``test_bodies.py`` validates against, used here for the pair of
   columns it already carries: quantity 1 (astrometric ICRF) in, quantity 2
   (apparent, true equator and equinox of date) out. Nothing in that file was
   computed by SSC.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ssc.astro.events import Ephemeris, Site
from ssc.astro.frames import (
    ASCOM_EQUATORIAL_SYSTEM,
    ASSUMED_ELEVATION_M,
    EquatorialCoord,
    MountFrame,
    UnsupportedMountFrameError,
    frame_offset_arcsec,
    j2000_coord,
    mount_frame_from_ascom,
    mount_site,
    to_j2000,
    to_mount_frame,
)
from ssc.astro.units import Deg, Metres

HORIZONS_PATH = Path(__file__).parent / "horizons_bodies_reference.json"

#: A dark evening at the owner's latitude. Any instant would do; this one is the
#: same the Horizons fixture uses, one day on.
WHEN = datetime(2026, 9, 10, 21, 0, tzinfo=UTC)

SITE = Site(latitude_deg=Deg(48.15), longitude_deg=Deg(17.11), elevation_m=Metres(0.0))

#: Positions chosen to exercise the awkward geometry, not just the easy middle:
#: the pole (where an RA-space correction would diverge), both sides of the 0h
#: wrap, and a far-southern target.
SAMPLE_POSITIONS: tuple[tuple[str, float, float], ...] = (
    ("M31", 10.6847, 41.2691),
    ("Polaris", 37.9545, 89.2641),
    ("0.01 deg from the pole", 0.0, 89.99),
    ("M42", 83.8221, -5.3911),
    ("origin of the frame", 0.0, 0.0),
    ("just under 24h", 359.999, -20.0),
    ("deep south", 120.0, -80.0),
)


@pytest.fixture(scope="session")
def horizons() -> dict[str, Any]:
    """The checked-in JPL Horizons table, with its provenance in the file."""
    with HORIZONS_PATH.open("rb") as handle:
        data: dict[str, Any] = json.load(handle)
    return data


def separation_arcsec(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    """Great-circle separation, computed here from scratch so the tests do not
    check the module against its own helper."""
    ra1, dec1, ra2, dec2 = (math.radians(v) for v in (ra1_deg, dec1_deg, ra2_deg, dec2_deg))
    numerator = math.hypot(
        math.cos(dec2) * math.sin(ra2 - ra1),
        math.cos(dec1) * math.sin(dec2) - math.sin(dec1) * math.cos(dec2) * math.cos(ra2 - ra1),
    )
    denominator = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(
        ra2 - ra1
    )
    return math.degrees(math.atan2(numerator, denominator)) * 3600.0


def offset_arcsec(first: EquatorialCoord, second: EquatorialCoord) -> float:
    return separation_arcsec(first.ra_deg, first.dec_deg, second.ra_deg, second.dec_deg)


# --- 1. round trip ---------------------------------------------------------


def test_round_trip_returns_the_original(ephemeris: Ephemeris) -> None:
    """J2000 -> topocentric -> J2000 to within 1 microarcsec.

    That bound is arbitrary only in being far tighter than anything physical:
    the mount reports RA to about 1e-5 h (0.05 arcsec) and the conversion moves
    the coordinate by ~20 arcmin, so a residual seven orders of magnitude below
    the correction proves the inverse is a true inverse and not an
    approximation that happens to be close.
    """
    worst = 0.0
    worst_case = ""
    for name, ra, dec in SAMPLE_POSITIONS:
        start = j2000_coord(Deg(ra), Deg(dec))
        mount = to_mount_frame(ephemeris, SITE, start, WHEN, MountFrame.TOPOCENTRIC)
        back = to_j2000(ephemeris, SITE, mount, WHEN)
        residual = offset_arcsec(start, back)
        if residual > worst:
            worst, worst_case = residual, name
    print(f"\nworst round-trip residual: {worst * 1e6:.4f} microarcsec ({worst_case})")
    assert worst < 1e-6, f"{worst} arcsec at {worst_case}"


@settings(max_examples=25, deadline=None)
@given(
    ra_deg=st.floats(min_value=0.0, max_value=360.0, allow_nan=False),
    dec_deg=st.floats(min_value=-90.0, max_value=90.0, allow_nan=False),
)
def test_round_trip_is_an_identity_anywhere_on_the_sphere(
    ephemeris: Ephemeris, ra_deg: float, dec_deg: float
) -> None:
    """The property behind the fixed sample above: the two directions compose to
    the identity for any point, poles and the 0h wrap included."""
    start = j2000_coord(Deg(ra_deg), Deg(dec_deg))
    mount = to_mount_frame(ephemeris, SITE, start, WHEN, MountFrame.TOPOCENTRIC)
    back = to_j2000(ephemeris, SITE, mount, WHEN)
    assert offset_arcsec(start, back) < 1e-6


# --- 2. magnitude ----------------------------------------------------------


def test_the_2026_offset_is_about_twenty_arcminutes(ephemeris: Ephemeris) -> None:
    """The frame gap in 2026 is ~20 arcmin (docs/M0-FINDINGS.md).

    This is the test that a do-nothing conversion fails. The bracket is
    deliberately wide — the offset is target-dependent, from ~6 arcmin near the
    south pole to ~23 arcmin near the equinox — and still nowhere near zero.
    """
    reported: list[str] = []
    for name, ra, dec in SAMPLE_POSITIONS:
        start = j2000_coord(Deg(ra), Deg(dec))
        mount = to_mount_frame(ephemeris, SITE, start, WHEN, MountFrame.TOPOCENTRIC)
        arcmin = offset_arcsec(start, mount) / 60.0
        reported.append(f"{name}: {arcmin:.3f}'")
        assert 5.0 < arcmin < 30.0, f"{name} moved {arcmin} arcmin"
    print("\nJ2000 -> topocentric offset, 2026-09-10: " + ", ".join(reported))

    m31 = j2000_coord(Deg(10.6847), Deg(41.2691))
    moved = to_mount_frame(ephemeris, SITE, m31, WHEN, MountFrame.TOPOCENTRIC)
    assert 18.0 < offset_arcsec(m31, moved) / 60.0 < 20.0


def test_the_offset_grows_with_time_like_precession(ephemeris: Ephemeris) -> None:
    """Twenty years on, the correction is roughly 20/26 times larger — the
    signature of precession dominating. A conversion applying only nutation and
    aberration would stay flat instead."""
    m31 = j2000_coord(Deg(10.6847), Deg(41.2691))
    now = offset_arcsec(m31, to_mount_frame(ephemeris, SITE, m31, WHEN, MountFrame.TOPOCENTRIC))
    later_when = WHEN + timedelta(days=365.25 * 20)
    later = offset_arcsec(
        m31, to_mount_frame(ephemeris, SITE, m31, later_when, MountFrame.TOPOCENTRIC)
    )
    print(f"\nM31 offset 2026: {now / 60:.3f}' ; 2046: {later / 60:.3f}'")
    assert later > now * 1.5


# --- 3. direction ----------------------------------------------------------


def test_applying_the_conversion_backwards_doubles_the_error(
    ephemeris: Ephemeris, horizons: dict[str, Any]
) -> None:
    """The test that fails if the two directions are swapped.

    Horizons supplies both ends independently, so "which way is which" is not
    SSC's own opinion. Feeding the astrometric position through the *inverse*
    lands ~2x the frame offset away from the apparent place it should have
    reached — never near it — so a swapped implementation cannot pass this by
    looking plausible.
    """
    site, when, entry = _horizons_case(horizons, "jupiter")
    astrometric = j2000_coord(Deg(entry["ra_icrf_deg"]), Deg(entry["dec_icrf_deg"]))
    apparent_ra = entry["ra_apparent_of_date_deg"]
    apparent_dec = entry["dec_apparent_of_date_deg"]

    gap = separation_arcsec(astrometric.ra_deg, astrometric.dec_deg, apparent_ra, apparent_dec)
    right_way = to_mount_frame(ephemeris, site, astrometric, when, MountFrame.TOPOCENTRIC)
    wrong_way = to_j2000(
        ephemeris,
        site,
        EquatorialCoord(
            ra_deg=astrometric.ra_deg,
            dec_deg=astrometric.dec_deg,
            frame=MountFrame.TOPOCENTRIC,
        ),
        when,
    )
    right = separation_arcsec(right_way.ra_deg, right_way.dec_deg, apparent_ra, apparent_dec)
    wrong = separation_arcsec(wrong_way.ra_deg, wrong_way.dec_deg, apparent_ra, apparent_dec)
    print(
        f"\nJupiter: frame gap {gap / 60:.3f}' ; correct direction lands {right:.3f}\" from "
        f"Horizons, inverted direction lands {wrong / 60:.3f}'"
    )
    assert right < 1.0
    assert wrong / gap == pytest.approx(2.0, abs=0.01)


def test_precession_moves_this_target_the_way_it_should(ephemeris: Ephemeris) -> None:
    """A sign check independent of any magnitude.

    For a target near RA 0h and northern declination, going J2000 -> of-date
    increases both RA and Dec (the equinox regresses along the ecliptic). Both
    coordinates flip sign under the inverse, so this pins orientation without
    reference to how big the correction is.
    """
    target = j2000_coord(Deg(10.6847), Deg(41.2691))
    forward = to_mount_frame(ephemeris, SITE, target, WHEN, MountFrame.TOPOCENTRIC)
    backward = to_j2000(
        ephemeris,
        SITE,
        EquatorialCoord(ra_deg=target.ra_deg, dec_deg=target.dec_deg, frame=MountFrame.TOPOCENTRIC),
        WHEN,
    )
    assert forward.ra_deg > target.ra_deg
    assert forward.dec_deg > target.dec_deg
    assert backward.ra_deg < target.ra_deg
    assert backward.dec_deg < target.dec_deg


# --- 4. against an independent source --------------------------------------


def _horizons_case(horizons: dict[str, Any], body: str) -> tuple[Site, datetime, dict[str, Any]]:
    site_row = horizons["site"]
    site = Site(
        latitude_deg=Deg(site_row["latitude_deg"]),
        longitude_deg=Deg(site_row["longitude_deg"]),
        elevation_m=Metres(site_row["elevation_m"]),
    )
    when = datetime.fromisoformat(horizons["instant_utc"])
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return site, when.astimezone(UTC), horizons["bodies"][body]["centre"]


@pytest.mark.parametrize(
    "body", ["moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune"]
)
def test_forward_matches_horizons_apparent_place(
    ephemeris: Ephemeris, horizons: dict[str, Any], body: str
) -> None:
    """Astrometric ICRF in, apparent-of-date out, both columns from Horizons.

    Tolerance 0.5 arcsec: an order of magnitude below the ~20 arcsec each of
    nutation and aberration contribute, so a conversion that dropped either term
    could not pass, and comfortably above the residual left by SSC running
    de440s against Horizons' DE441.

    The Sun is excluded: gravitational light deflection is undefined for the
    deflector itself, and the Sun is never a target anyway (DESIGN 3.6).
    """
    site, when, entry = _horizons_case(horizons, body)
    astrometric = j2000_coord(Deg(entry["ra_icrf_deg"]), Deg(entry["dec_icrf_deg"]))
    got = to_mount_frame(ephemeris, site, astrometric, when, MountFrame.TOPOCENTRIC)
    deviation = separation_arcsec(
        got.ra_deg,
        got.dec_deg,
        entry["ra_apparent_of_date_deg"],
        entry["dec_apparent_of_date_deg"],
    )
    print(f'\n{body}: {deviation:.4f}" from Horizons quantity 2')
    assert deviation < 0.5, f"{body} deviates {deviation} arcsec"


@pytest.mark.parametrize("body", ["moon", "jupiter", "neptune"])
def test_inverse_matches_horizons_astrometric_place(
    ephemeris: Ephemeris, horizons: dict[str, Any], body: str
) -> None:
    """The other direction against the same table: apparent-of-date in,
    astrometric ICRF out. Checking only the forward direction would leave the
    inverse validated against nothing but itself."""
    site, when, entry = _horizons_case(horizons, body)
    apparent = EquatorialCoord(
        ra_deg=Deg(entry["ra_apparent_of_date_deg"]),
        dec_deg=Deg(entry["dec_apparent_of_date_deg"]),
        frame=MountFrame.TOPOCENTRIC,
    )
    got = to_j2000(ephemeris, site, apparent, when)
    deviation = separation_arcsec(
        got.ra_deg, got.dec_deg, entry["ra_icrf_deg"], entry["dec_icrf_deg"]
    )
    print(f'\n{body}: inverse {deviation:.4f}" from Horizons quantity 1')
    assert deviation < 0.5, f"{body} deviates {deviation} arcsec"


# --- frames are data -------------------------------------------------------


def test_a_j2000_mount_needs_no_conversion() -> None:
    """A driver declaring J2000 gets the identity in both directions — and gets
    it without the ephemeris being touched, which is why ``None`` can stand in
    for the handle here."""
    no_kernel = cast(Ephemeris, None)
    coord = j2000_coord(Deg(83.8221), Deg(-5.3911))
    assert to_mount_frame(no_kernel, SITE, coord, WHEN, MountFrame.J2000) == coord
    assert to_j2000(no_kernel, SITE, coord, WHEN) == coord


@pytest.mark.parametrize("frame", [MountFrame.OTHER, MountFrame.B1950, MountFrame.J2050])
def test_an_unconvertible_frame_is_refused_not_guessed(frame: MountFrame) -> None:
    """Refusal beats a default. B1950 differs from J2000 by ~0.7 deg, and
    treating ``Other`` as either is a coin flip that a sync makes permanent."""
    no_kernel = cast(Ephemeris, None)
    with pytest.raises(UnsupportedMountFrameError, match=frame.value):
        to_mount_frame(no_kernel, SITE, j2000_coord(Deg(0.0), Deg(0.0)), WHEN, frame)
    with pytest.raises(UnsupportedMountFrameError, match=frame.value):
        to_j2000(no_kernel, SITE, EquatorialCoord(Deg(0.0), Deg(0.0), frame), WHEN)


def test_the_ascom_wire_values_map_to_the_declared_frames() -> None:
    """The Seestar reports ``equatorialsystem: 1`` (docs/M0-FINDINGS.md)."""
    assert mount_frame_from_ascom(1) is MountFrame.TOPOCENTRIC
    assert mount_frame_from_ascom(2) is MountFrame.J2000
    assert ASCOM_EQUATORIAL_SYSTEM[4] is MountFrame.B1950
    with pytest.raises(ValueError, match="unknown ASCOM EquatorialCoordinateType 7"):
        mount_frame_from_ascom(7)


def test_a_mount_frame_coordinate_cannot_be_passed_as_j2000() -> None:
    """The type carries the frame, so the mix-up this module exists to prevent
    is caught at the call, not 20 arcmin later on the sky map."""
    no_kernel = cast(Ephemeris, None)
    of_date = EquatorialCoord(Deg(10.0), Deg(41.0), MountFrame.TOPOCENTRIC)
    with pytest.raises(ValueError, match="takes a J2000 coordinate"):
        to_mount_frame(no_kernel, SITE, of_date, WHEN, MountFrame.TOPOCENTRIC)


def test_the_result_carries_its_frame(ephemeris: Ephemeris) -> None:
    coord = j2000_coord(Deg(83.8221), Deg(-5.3911))
    mount = to_mount_frame(ephemeris, SITE, coord, WHEN, MountFrame.TOPOCENTRIC)
    assert mount.frame is MountFrame.TOPOCENTRIC
    assert to_j2000(ephemeris, SITE, mount, WHEN).frame is MountFrame.J2000


# --- coordinates, sites, clock ---------------------------------------------


def test_right_ascension_wraps_and_declination_does_not() -> None:
    """RA is circular so it wraps to [0, 360); a declination outside [-90, 90] is
    a genuine error and is raised, never folded."""
    assert j2000_coord(Deg(-1.0), Deg(0.0)).ra_deg == pytest.approx(359.0)
    assert j2000_coord(Deg(360.0), Deg(0.0)).ra_deg == pytest.approx(0.0)
    assert j2000_coord(Deg(370.0), Deg(0.0)).ra_h == pytest.approx(10.0 / 15.0)
    with pytest.raises(ValueError, match="declination out of range"):
        j2000_coord(Deg(0.0), Deg(90.1))
    with pytest.raises(ValueError, match="non-finite"):
        j2000_coord(Deg(float("nan")), Deg(0.0))


def test_unknown_elevation_becomes_the_named_assumption_not_a_bare_zero() -> None:
    assert mount_site(Deg(48.15), Deg(17.11), None).elevation_m == ASSUMED_ELEVATION_M
    assert mount_site(Deg(48.15), Deg(17.11), Metres(150.0)).elevation_m == 150.0


def test_unknown_elevation_costs_under_a_milliarcsecond(ephemeris: Ephemeris) -> None:
    """What assuming sea level actually costs, measured rather than asserted.

    ``SiteElevation`` errors on read on firmware 9.31, so this bound is the
    justification for :data:`ASSUMED_ELEVATION_M`. A catalogue target is at
    infinite distance, so elevation reaches the answer only through the
    observer's rotational velocity.
    """
    high = Site(latitude_deg=Deg(48.15), longitude_deg=Deg(17.11), elevation_m=Metres(4000.0))
    worst = 0.0
    for _, ra, dec in SAMPLE_POSITIONS:
        coord = j2000_coord(Deg(ra), Deg(dec))
        at_sea = to_mount_frame(ephemeris, SITE, coord, WHEN, MountFrame.TOPOCENTRIC)
        at_alt = to_mount_frame(ephemeris, high, coord, WHEN, MountFrame.TOPOCENTRIC)
        worst = max(worst, offset_arcsec(at_sea, at_alt))
    print(f"\nsea level vs 4000 m: worst {worst * 1000:.4f} mas")
    assert worst < 0.001


def test_the_observer_site_matters_at_the_sub_arcsecond_level(ephemeris: Ephemeris) -> None:
    """Latitude and longitude are required inputs, unlike elevation: diurnal
    aberration is small but it is 1000x the elevation term, and topocentric
    means observer-centred."""
    equator = Site(latitude_deg=Deg(0.0), longitude_deg=Deg(0.0), elevation_m=Metres(0.0))
    coord = j2000_coord(Deg(83.8221), Deg(-5.3911))
    here = to_mount_frame(ephemeris, SITE, coord, WHEN, MountFrame.TOPOCENTRIC)
    there = to_mount_frame(ephemeris, equator, coord, WHEN, MountFrame.TOPOCENTRIC)
    difference = offset_arcsec(here, there)
    print(f'\n48N/17E vs 0N/0E: {difference:.4f}"')
    assert 0.01 < difference < 1.0


def test_a_naive_datetime_is_refused(ephemeris: Ephemeris) -> None:
    """Invariant 5: no naive datetimes cross an interface."""
    with pytest.raises(ValueError, match="timezone-aware"):
        to_mount_frame(
            ephemeris,
            SITE,
            j2000_coord(Deg(0.0), Deg(0.0)),
            datetime(2026, 9, 10, 21, 0),  # noqa: DTZ001 — the point of the test
            MountFrame.TOPOCENTRIC,
        )


def test_the_conversion_depends_on_the_instant_it_is_given(ephemeris: Ephemeris) -> None:
    """No hidden clock: two different instants give two different answers, and
    the same instant twice gives the same answer bit for bit."""
    coord = j2000_coord(Deg(83.8221), Deg(-5.3911))
    first = to_mount_frame(ephemeris, SITE, coord, WHEN, MountFrame.TOPOCENTRIC)
    again = to_mount_frame(ephemeris, SITE, coord, WHEN, MountFrame.TOPOCENTRIC)
    other = to_mount_frame(
        ephemeris, SITE, coord, WHEN + timedelta(days=180), MountFrame.TOPOCENTRIC
    )
    assert first == again
    assert offset_arcsec(first, other) > 1.0


def test_frame_offset_reports_the_size_of_the_correction(ephemeris: Ephemeris) -> None:
    """The number the UI shows beside a believed-versus-solved disagreement."""
    coord = j2000_coord(Deg(10.6847), Deg(41.2691))
    mount = to_mount_frame(ephemeris, SITE, coord, WHEN, MountFrame.TOPOCENTRIC)
    assert frame_offset_arcsec(coord, mount) == pytest.approx(offset_arcsec(coord, mount), rel=1e-9)
    assert frame_offset_arcsec(coord, coord) == pytest.approx(0.0, abs=1e-9)
