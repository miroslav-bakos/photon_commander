"""Solar-system bodies as targets — ``docs/DESIGN.md`` 3.6.

Four things are being proved here, in order of how badly a silent failure would
hurt:

1. The Sun cannot be resolved as a target, by any route, and is refused with its
   own reason rather than a generic miss.
2. A body reference is genuinely time-dependent — resolving it twice 6 h apart
   gives the numbers DESIGN 3.6 quotes.
3. Rise/set/transit for a body are solved *with its motion*, validated against
   the same independent USNO/IMCCE table ``test_almanac_validation.py`` uses,
   and the size of the error the fixed-position approximation would have left is
   stated as a number rather than merely asserted non-zero.
4. The Moon carries the lunar drive rate with it.

Run with ``-s`` to see the two measured tables printed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ssc.astro.bodies import (
    BodyId,
    apparent_motion_arcmin_per_hour,
    body_events,
)
from ssc.astro.catalog import TargetCatalog, UnknownTargetError, load_catalog
from ssc.astro.events import (
    Ephemeris,
    Outcome,
    Site,
    TargetEvent,
    target_events,
    target_position,
)
from ssc.astro.targets import (
    BodyTarget,
    CatalogTarget,
    SunNotATargetError,
    TargetKind,
    TrackingRate,
    resolve_body,
    resolve_position,
    resolve_target,
    target_ref_altitude_curve,
    target_ref_events,
    tracking_rate_for_body,
)
from ssc.astro.units import Deg, Hours, Metres
from tests.astro.conftest import REFERENCE_PATH, site_of

#: The site and window DESIGN 3.6's drift table was measured from.
BRATISLAVA = Site(latitude_deg=Deg(48.15), longitude_deg=Deg(17.11), elevation_m=Metres(150.0))
NIGHT_START = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
NIGHT_END = NIGHT_START + timedelta(hours=6)

#: DESIGN 3.6, verbatim: arcmin of right ascension travelled over those 6 h.
#: The tolerance is 2% (floored at 0.02') — this is a check that the module
#: reproduces the documented behaviour, not a re-derivation of the ephemeris.
DESIGN_RA_DRIFT_ARCMIN: dict[BodyId, float] = {
    BodyId.MOON: 233.6,
    BodyId.MERCURY: 22.7,
    BodyId.MARS: 10.1,
    BodyId.JUPITER: 3.1,
    BodyId.URANUS: 0.005,
}

GREENWICH = Site(latitude_deg=Deg(51.4779), longitude_deg=Deg(-0.0015), elevation_m=Metres(0.0))

#: USNO's own stated convention for moonrise/moonset, quoted in
#: ``almanac_reference.json``: the geometric centre at -50', standing in for the
#: upper limb plus 34' of refraction. ``body_events`` takes a geometric
#: threshold, so this is how its rise/set is compared with USNO's.
USNO_MOON_HORIZON = Deg(-50.0 / 60.0)

#: Looser than the 30 s the fixture sets for ``lunar_events``, and deliberately:
#: ``lunar_events`` takes the Moon's semidiameter from the ephemeris at each
#: instant, while a fixed -50' threshold ignores the 14.7'-16.8' monthly swing.
#: Measured cost of that simplification below; the tolerance is set just above
#: it so the test still bites.
USNO_MOON_RISE_SET_TOLERANCE_S = 45.0
USNO_MOON_TRANSIT_TOLERANCE_S = 30.0

with REFERENCE_PATH.open("rb") as _handle:
    _ALMANAC: dict[str, Any] = json.load(_handle)


@pytest.fixture(scope="module")
def catalog() -> TargetCatalog:
    return load_catalog()


# --- 1. the Sun is not a target ---------------------------------------------


@pytest.mark.parametrize("query", ["sun", "Sun", "SUN", " sun ", "sol", "The Sun"])
def test_sun_is_refused_with_its_own_reason(catalog: TargetCatalog, query: str) -> None:
    with pytest.raises(SunNotATargetError) as excinfo:
        resolve_target(catalog, query)
    message = str(excinfo.value)
    assert "shutter" in message and "solar filter" in message, message
    assert "no override" in message, message


def test_sun_refusal_is_distinguishable_from_a_generic_miss(catalog: TargetCatalog) -> None:
    """The two failures must not be the same fact. A generic miss says "no such
    object"; the Sun's says why there will never be one."""
    with pytest.raises(SunNotATargetError) as sun:
        resolve_target(catalog, "sun")
    with pytest.raises(UnknownTargetError) as miss:
        resolve_target(catalog, "Zorblax 9")

    assert not isinstance(miss.value, SunNotATargetError)
    # ...and still fails closed for a caller that only knows the old exception.
    assert isinstance(sun.value, UnknownTargetError)
    assert str(sun.value) != str(miss.value)
    assert "sensor" in str(sun.value)
    assert "sensor" not in str(miss.value)


def test_unknown_name_still_fails_as_before(catalog: TargetCatalog) -> None:
    """An unresolvable name keeps the catalogue's own message — this module
    changed nothing about that path."""
    with pytest.raises(UnknownTargetError) as direct:
        catalog.resolve("Zorblax 9")
    with pytest.raises(UnknownTargetError) as through_targets:
        resolve_target(catalog, "Zorblax 9")
    assert str(direct.value) == str(through_targets.value)


def test_body_target_refuses_the_sun_at_construction() -> None:
    """Not only the resolver: there is no hand-built route to a Sun target."""
    with pytest.raises(SunNotATargetError):
        BodyTarget(BodyId.SUN)


def test_resolve_body_refuses_the_sun() -> None:
    with pytest.raises(SunNotATargetError):
        resolve_body("sun")


def test_the_sun_is_still_a_body(ephemeris: Ephemeris) -> None:
    """The exclusion is about *targets*. The Sun stays computable — twilight and
    the sky map depend on it (DESIGN 3.6)."""
    events = body_events(ephemeris, GREENWICH, BodyId.SUN, NIGHT_START, NIGHT_END, Deg(0.0))
    assert events


# --- 2. resolution ----------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Jupiter", BodyId.JUPITER),
        ("jupiter", BodyId.JUPITER),
        ("  JUPITER  ", BodyId.JUPITER),
        ("moon", BodyId.MOON),
        ("Mars", BodyId.MARS),
        ("neptune", BodyId.NEPTUNE),
    ],
)
def test_body_names_resolve(catalog: TargetCatalog, query: str, expected: BodyId) -> None:
    ref = resolve_target(catalog, query)
    assert isinstance(ref, BodyTarget)
    assert ref.body is expected
    assert ref.kind is TargetKind.BODY
    assert ref.moves is True


def test_catalogue_names_still_resolve_to_a_catalogue_target(catalog: TargetCatalog) -> None:
    ref = resolve_target(catalog, "M 31")
    assert isinstance(ref, CatalogTarget)
    assert ref.kind is TargetKind.CATALOG
    assert ref.moves is False
    assert ref.object.name == "M31"


@pytest.mark.parametrize("query", ["moonrise", "jupiterish", "mars bar", "mo on"])
def test_resolution_is_no_more_forgiving_than_the_catalogue(
    catalog: TargetCatalog, query: str
) -> None:
    """Case and spacing, and nothing else — the bar the catalogue resolver sets."""
    with pytest.raises(UnknownTargetError):
        resolve_target(catalog, query)


# --- 3. a body reference is time-dependent ----------------------------------


def test_resolving_a_body_twice_gives_different_coordinates(ephemeris: Ephemeris) -> None:
    """DESIGN 3.6's own drift table, recomputed. This is the measurement the
    whole module exists because of."""
    rows = []
    for body_id, design_arcmin in DESIGN_RA_DRIFT_ARCMIN.items():
        ref = BodyTarget(body_id)
        first = resolve_position(ephemeris, BRATISLAVA, ref, NIGHT_START)
        second = resolve_position(ephemeris, BRATISLAVA, ref, NIGHT_END)
        drift_arcmin = abs(second.ra_j2000_deg - first.ra_j2000_deg) * 60.0
        rows.append((body_id.value, design_arcmin, drift_arcmin))
        tolerance = max(0.02 * design_arcmin, 0.02)
        assert abs(drift_arcmin - design_arcmin) <= tolerance, (
            f"{body_id.value}: {drift_arcmin:.3f}' of RA over 6 h, DESIGN 3.6 says "
            f"{design_arcmin}' (tolerance {tolerance:.3f}')"
        )
        assert second.when != first.when
        assert second.moves is True

    print("\nRA travelled over 6 h from 2026-09-10 18:00Z, Bratislava:")
    for name, design, measured in rows:
        print(f"  {name:<8} DESIGN {design:>7}'   measured {measured:>8.3f}'")


def test_a_catalogue_target_is_not_time_dependent(ephemeris: Ephemeris) -> None:
    ref = CatalogTarget(load_catalog().resolve("M31"))
    first = resolve_position(ephemeris, BRATISLAVA, ref, NIGHT_START)
    second = resolve_position(ephemeris, BRATISLAVA, ref, NIGHT_END)
    assert first.ra_j2000_deg == second.ra_j2000_deg
    assert first.dec_j2000_deg == second.dec_j2000_deg
    assert first.moves is False
    assert first.body_position is None
    # ``when`` is still recorded, so a consumer can always tell what it holds.
    assert first.when == NIGHT_START


def test_resolve_position_refuses_a_naive_datetime(ephemeris: Ephemeris) -> None:
    """Invariant 5: no naive datetime, and no clock inside the solver."""
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_position(
            ephemeris, BRATISLAVA, BodyTarget(BodyId.MOON), NIGHT_START.replace(tzinfo=None)
        )


def test_ra_hours_is_the_json_boundary_form(ephemeris: Ephemeris) -> None:
    resolved = resolve_position(ephemeris, BRATISLAVA, BodyTarget(BodyId.MARS), NIGHT_START)
    assert 0.0 <= resolved.ra_j2000_h < 24.0
    assert resolved.ra_j2000_h * 15.0 == pytest.approx(resolved.ra_j2000_deg, abs=1e-9)


def test_apparent_motion_ranks_the_bodies_the_way_the_design_does(ephemeris: Ephemeris) -> None:
    """The number a scheduler will weigh against its frame size, so its ordering
    had better match the drift table above."""
    rates = {
        body_id: float(apparent_motion_arcmin_per_hour(ephemeris, BRATISLAVA, NIGHT_START, body_id))
        for body_id in DESIGN_RA_DRIFT_ARCMIN
    }
    assert rates[BodyId.MOON] > 30.0
    assert rates[BodyId.MOON] > rates[BodyId.MERCURY] > rates[BodyId.MARS]
    assert rates[BodyId.MARS] > rates[BodyId.JUPITER] > rates[BodyId.URANUS]
    assert rates[BodyId.URANUS] < 0.01


# --- 4. events solved with the body's own motion ----------------------------


def _altitude_of(
    ephemeris: Ephemeris, site: Site, ra_h: Hours, dec_deg: Deg, when: datetime
) -> float:
    """A fixed-RA/Dec altitude, for comparing against a curve that moves."""
    return float(target_position(ephemeris, site, ra_h, dec_deg, when).altitude_deg)


def _moon_event(events: tuple[Any, ...], name: str) -> datetime:
    found = [e for e in events if e.event == name and e.outcome is Outcome.OCCURS]
    assert len(found) == 1, f"{name}: expected exactly one, got {found}"
    when = found[0].when
    assert when is not None
    return when


def test_moon_events_differ_from_the_fixed_position_approximation(ephemeris: Ephemeris) -> None:
    """The error the *rejected* method would have left, in seconds.

    "Fixed-position approximation" means what a plan that resolves at planning
    time actually does: freeze the Moon's coordinates at the start of the window
    and solve as if it were a star.
    """
    start = datetime(2026, 9, 10, tzinfo=UTC)
    end = start + timedelta(days=1)

    moving = body_events(ephemeris, GREENWICH, BodyId.MOON, start, end, Deg(0.0))
    frozen_at = resolve_position(ephemeris, GREENWICH, BodyTarget(BodyId.MOON), start)
    frozen = target_events(
        ephemeris,
        GREENWICH,
        frozen_at.ra_j2000_h,
        frozen_at.dec_j2000_deg,
        start,
        end,
        Deg(0.0),
    )

    errors = {}
    for name in (TargetEvent.RISES_ABOVE, TargetEvent.TRANSITS, TargetEvent.SETS_BELOW):
        errors[name.value] = (_moon_event(frozen, name) - _moon_event(moving, name)).total_seconds()

    print("\nMoon, Greenwich 2026-09-10 — error left by freezing the position at 00:00Z:")
    for name, seconds in errors.items():
        print(f"  {name:<12} {seconds:+9.1f} s  ({seconds / 60.0:+6.2f} min)")

    # Every one of them is far outside the 30 s the almanac gate allows, which
    # is the point: this approximation is not a tuning choice for the Moon.
    assert abs(errors["rises_above"]) > 900.0
    assert abs(errors["transits"]) > 1300.0
    assert abs(errors["sets_below"]) > 600.0


def test_planet_events_are_barely_affected_by_freezing(ephemeris: Ephemeris) -> None:
    """The same measurement for Jupiter, stated so the Moon's number has a scale.

    Under a minute over a whole day — which is why the choice made in
    ``bodies.py`` was "always use the ephemeris" rather than a per-body policy
    table: there is no body for which the ephemeris path is *worse*.
    """
    start = datetime(2026, 9, 10, tzinfo=UTC)
    end = start + timedelta(days=1)
    moving = body_events(ephemeris, GREENWICH, BodyId.JUPITER, start, end, Deg(0.0))
    frozen_at = resolve_position(ephemeris, GREENWICH, BodyTarget(BodyId.JUPITER), start)
    frozen = target_events(
        ephemeris, GREENWICH, frozen_at.ra_j2000_h, frozen_at.dec_j2000_deg, start, end, Deg(0.0)
    )
    worst = max(
        abs((_moon_event(frozen, name) - _moon_event(moving, name)).total_seconds())
        for name in (TargetEvent.RISES_ABOVE, TargetEvent.TRANSITS, TargetEvent.SETS_BELOW)
    )
    print(f"\nJupiter, same day: freezing costs at most {worst:.1f} s")
    assert worst < 60.0


def test_body_events_use_the_same_vocabulary_as_a_catalogue_target(ephemeris: Ephemeris) -> None:
    """A caller holding a ``TargetRef`` must never branch on its kind."""
    start = datetime(2026, 9, 10, 12, tzinfo=UTC)
    end = start + timedelta(days=1)
    body = target_ref_events(ephemeris, GREENWICH, BodyTarget(BodyId.SATURN), start, end, Deg(20.0))
    catalogue = target_ref_events(
        ephemeris, GREENWICH, CatalogTarget(load_catalog().resolve("M31")), start, end, Deg(20.0)
    )
    assert {str(e.event) for e in body} == {str(e.event) for e in catalogue}
    assert {str(e.event) for e in body} == {
        TargetEvent.RISES_ABOVE.value,
        TargetEvent.TRANSITS.value,
        TargetEvent.SETS_BELOW.value,
    }


def test_body_events_explain_an_absence_rather_than_returning_nothing(
    ephemeris: Ephemeris, almanac: dict[str, Any]
) -> None:
    """Polar case: the Moon that never clears 20 deg from Tromso. ``None`` must
    come with a reason (invariant 7)."""
    tromso = site_of(almanac, "tromso")
    start = datetime(2026, 12, 21, tzinfo=UTC)
    events = body_events(
        ephemeris, tromso, BodyId.MOON, start, start + timedelta(days=1), Deg(70.0)
    )
    absent = [e for e in events if e.when is None]
    assert absent, events
    assert all(e.outcome is Outcome.ALWAYS_BELOW for e in absent), absent


# --- 4b. the same, against the independent almanac --------------------------


def _almanac_moon_cases() -> list[tuple[dict[str, Any], str]]:
    return [
        (row, phenomenon)
        for row in _ALMANAC["rows"]
        for phenomenon in ("moonrise", "moon_transit", "moonset")
    ]


#: Filled by the parametrised test below, reported by the last test here.
_MOON_DEVIATIONS: list[tuple[str, float, str, str, str]] = []

_EVENT_FOR_PHENOMENON = {
    "moonrise": TargetEvent.RISES_ABOVE.value,
    "moonset": TargetEvent.SETS_BELOW.value,
    "moon_transit": TargetEvent.TRANSITS.value,
}


@pytest.mark.parametrize(
    "case",
    _almanac_moon_cases(),
    ids=lambda case: f"{case[0]['site']}-{case[0]['date_utc']}-{case[1]}",
)
def test_body_events_for_the_moon_match_the_independent_almanac(
    ephemeris: Ephemeris, almanac: dict[str, Any], case: tuple[dict[str, Any], str]
) -> None:
    """``body_events`` for the Moon, against USNO — the same table
    ``test_almanac_validation.py`` holds ``lunar_events`` to, reached through
    the new moving-body path instead.

    Nothing in ``almanac_reference.json`` was computed by SSC, and USNO's stated
    convention (centre at -50') is expressed here as the geometric threshold
    ``body_events`` takes, so the comparison is like for like apart from the
    Moon's varying semidiameter — see ``USNO_MOON_RISE_SET_TOLERANCE_S``.
    """
    row, phenomenon = case
    site = site_of(almanac, row["site"])
    start = datetime.fromisoformat(row["date_utc"]).replace(tzinfo=UTC)
    end = start + timedelta(days=1)
    expected = row["expected"][phenomenon]
    label = f"{row['site']} {row['date_utc']} {phenomenon}"

    threshold = Deg(0.0) if phenomenon == "moon_transit" else USNO_MOON_HORIZON
    events = body_events(ephemeris, site, BodyId.MOON, start, end, threshold)
    found = [e for e in events if str(e.event) == _EVENT_FOR_PHENOMENON[phenomenon]]
    assert found, f"{label}: no result at all"

    if expected["utc"] is None:
        assert found[0].when is None, (
            f"{label}: the almanac says this does not occur ({expected['outcome']}), "
            f"SSC produced {found[0].when}"
        )
        assert found[0].outcome.value == expected["outcome"], (
            f"{label}: expected outcome {expected['outcome']}, got {found[0].outcome.value}"
        )
        return

    occurring = [e for e in found if e.outcome is Outcome.OCCURS and e.when is not None]
    assert occurring, f"{label}: expected {expected['utc']}, got {found[0].outcome.value}"
    hours, minutes = (int(part) for part in expected["utc"].split(":")[:2])
    reference = start + timedelta(hours=hours, minutes=minutes)
    deviation = min(abs((e.when - reference).total_seconds()) for e in occurring if e.when)
    tolerance = (
        USNO_MOON_TRANSIT_TOLERANCE_S
        if phenomenon == "moon_transit"
        else USNO_MOON_RISE_SET_TOLERANCE_S
    )
    _MOON_DEVIATIONS.append((phenomenon, deviation, row["site"], row["date_utc"], expected["utc"]))
    assert deviation <= tolerance, (
        f"{label}: {deviation:.1f} s from the USNO value {expected['utc']} "
        f"(tolerance {tolerance} s)"
    )


def test_worst_case_moon_deviation_is_reported() -> None:
    """Print the actual worst case per phenomenon, never just "passed"."""
    if not _MOON_DEVIATIONS:
        pytest.skip("no deviations collected — run the whole module")
    worst: dict[str, tuple[float, str, str, str]] = {}
    for phenomenon, deviation, site, date, reference in _MOON_DEVIATIONS:
        if deviation > worst.get(phenomenon, (-1.0, "", "", ""))[0]:
            worst[phenomenon] = (deviation, site, date, reference)
    print("\nbody_events(Moon) worst case vs USNO:")
    for phenomenon, (deviation, site, date, reference) in sorted(worst.items()):
        print(f"  {phenomenon:<13} {deviation:6.2f} s   {site} {date} (ref {reference})")
    assert worst


# --- 5. the altitude curve --------------------------------------------------


def test_body_altitude_curve_follows_the_body_not_a_frozen_point(ephemeris: Ephemeris) -> None:
    start = datetime(2026, 9, 10, 18, tzinfo=UTC)
    end = start + timedelta(hours=8)
    moon = target_ref_altitude_curve(ephemeris, GREENWICH, BodyTarget(BodyId.MOON), start, end, 17)
    frozen_at = resolve_position(ephemeris, GREENWICH, BodyTarget(BodyId.MOON), start)

    assert len(moon) == 17
    assert moon[0].when == start
    assert moon[-1].when == end
    assert all(0.0 <= sample.azimuth_deg < 360.0 for sample in moon)

    # At the first sample the curve and the frozen position describe the same
    # Moon, so they must agree — but not exactly. Round-tripping a body through
    # apparent J2000 RA/Dec and back through a fixed-star observation applies
    # light-time and aberration a second time; measured here at 5.5 arcsec,
    # which is ~2 px at the telephoto's plate scale and far below anything the
    # framing geometry acts on. Asserted at 30 arcsec so the round-trip error
    # cannot grow silently.
    round_trip_error = abs(
        moon[0].altitude_deg
        - _altitude_of(ephemeris, GREENWICH, frozen_at.ra_j2000_h, frozen_at.dec_j2000_deg, start)
    )
    print(f"\nRA/Dec round-trip error at t0: {round_trip_error * 3600.0:.2f} arcsec")
    assert round_trip_error < 30.0 / 3600.0
    # ...and the last must not, because by then the Moon has moved on.
    drift = abs(
        moon[-1].altitude_deg
        - _altitude_of(ephemeris, GREENWICH, frozen_at.ra_j2000_h, frozen_at.dec_j2000_deg, end)
    )
    print(f"Moon altitude after 8 h: {drift:.3f} deg from the frozen-position curve")
    assert drift > 0.5


@pytest.mark.parametrize("samples", [1, 0, -3])
def test_altitude_curve_refuses_a_degenerate_sample_count(
    ephemeris: Ephemeris, samples: int
) -> None:
    with pytest.raises(ValueError, match="samples"):
        target_ref_altitude_curve(
            ephemeris, GREENWICH, BodyTarget(BodyId.MOON), NIGHT_START, NIGHT_END, samples
        )


# --- 6. tracking rate is part of the target's identity ----------------------


def test_the_moon_wants_the_lunar_rate() -> None:
    assert tracking_rate_for_body(BodyId.MOON) is TrackingRate.LUNAR
    assert BodyTarget(BodyId.MOON).tracking_rate is TrackingRate.LUNAR


@pytest.mark.parametrize(
    "body_id",
    [BodyId.MERCURY, BodyId.VENUS, BodyId.MARS, BodyId.JUPITER, BodyId.SATURN, BodyId.NEPTUNE],
)
def test_planets_track_sidereally(body_id: BodyId) -> None:
    assert BodyTarget(body_id).tracking_rate is TrackingRate.SIDEREAL


def test_a_catalogue_target_tracks_sidereally(catalog: TargetCatalog) -> None:
    assert CatalogTarget(catalog.resolve("M31")).tracking_rate is TrackingRate.SIDEREAL


def test_the_rate_travels_with_the_resolved_position(ephemeris: Ephemeris) -> None:
    """DESIGN 3.6: "A body target and its tracking rate are one decision, not two."""
    resolved = resolve_position(ephemeris, BRATISLAVA, BodyTarget(BodyId.MOON), NIGHT_START)
    assert resolved.tracking_rate is TrackingRate.LUNAR
