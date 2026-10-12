# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Where the Sun, the Moon and the seven other naked-eye-to-binocular planets
are at one instant — the sky map's solar-system layer.

Same contract as the rest of the astro core: pure, clock-injected, no I/O
beyond the already-loaded :class:`~ssc.astro.events.Ephemeris` handle. The
instant is an argument (CLAUDE.md invariant 3).

Conventions, stated once so no caller has to guess:

* **Topocentric.** Positions are computed from ``site``, not from the geocentre.
  This matters: the Moon's diurnal parallax reaches ~1 deg, *twice its own
  diameter*, so a geocentric Moon drawn at true size on a sky map is visibly
  in the wrong place. For the planets the same correction peaks near 33 arcsec
  (Venus at a 0.27 au closest approach) and for the Sun is under 9 arcsec, but
  it is the same code path, so it is applied to everything.
* **Apparent place, referred to the J2000 (ICRF) frame.** Light-time,
  relativistic deflection and annual aberration are applied; the frame is
  *not* precessed to the equinox of date. That is what
  ``Apparent.radec()`` returns with no ``epoch`` argument, and it is the frame
  everything else the sky map consumes already uses
  (:mod:`ssc.astro.catalog`, :mod:`ssc.astro.constellations`,
  :mod:`ssc.astro.stars`). Against a strictly *astrometric* J2000 catalogue
  these positions carry up to 20.5 arcsec of aberration — a third of a
  Jupiter, far below anything a sky map resolves, and the price of answering
  "where does it appear" rather than "where is its catalogue entry".
* **Barycentres for the planets.** ``de440s`` carries planetary system
  barycentres, not planet centres, for Mars outwards. The offset is at most
  0.06 arcsec at these distances (measured against JPL Horizons body 599 vs
  body 5 in ``tests/astro/horizons_bodies_reference.json``) — some three
  orders of magnitude below what a sky map can show, and below the disk of
  every planet in the table. Mercury and Venus have no moons, so their
  barycentre *is* their centre.
* **Apparent diameter** is derived from a fixed IAU radius table below and the
  light-time-corrected distance, not from the kernel — an ephemeris carries no
  body sizes. Equatorial radius, so the number is the equatorial angular
  width, matching Horizons' ``Ang-diam``. Saturn's rings are excluded.
* **Phase angle** is the true Sun-body-observer angle in ``[0, 180]`` deg:
  0 deg fully lit, 180 deg unlit. This is *not* the 0-360 deg lunation angle
  :func:`ssc.astro.events.moon_phase` returns — that one answers "which limb",
  this one answers "how lit". A client drawing the lunar terminator wants
  :class:`~ssc.astro.events.MoonPhase`; a client drawing a gibbous Venus
  marker wants this.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

# events.py owns the rise/set/transit machinery and the naive-datetime guard;
# both are imported rather than restated so that a body solved here and a
# catalogue object solved there give byte-identical event vocabulary and the
# identical invariant-5 error message. ``best_targets.py`` imports
# ``_require_utc`` for the same reason.
from ssc.astro.events import (
    MAX_VISIBILITY_SAMPLES,
    TRUE_HORIZON_DEG,
    Ephemeris,
    EventTime,
    Site,
    TargetEvent,
    _altaz_samples,
    _require_utc,
    _rise_set,
    _sorted,
    _transits,
)
from ssc.astro.units import (
    ArcMin,
    Deg,
    Hours,
    deg_to_arcmin,
    hours_to_deg,
    wrap_deg_360,
    wrap_hours,
)


class BodyKind(StrEnum):
    """How a client should draw the thing: a disk to scale, or a marker."""

    SUN = "sun"
    MOON = "moon"
    PLANET = "planet"


class BodyId(StrEnum):
    """The closed set of solar-system bodies SSC reports. Earth is absent for
    the obvious reason; Pluto is absent because it is 14th magnitude and the
    sky map has nothing useful to say about it."""

    SUN = "sun"
    MOON = "moon"
    MERCURY = "mercury"
    VENUS = "venus"
    MARS = "mars"
    JUPITER = "jupiter"
    SATURN = "saturn"
    URANUS = "uranus"
    NEPTUNE = "neptune"


#: Display order: the two that get drawn to scale first, then outwards from
#: the Sun. Also the order :func:`body_positions` returns.
BODY_ORDER: Final[tuple[BodyId, ...]] = (
    BodyId.SUN,
    BodyId.MOON,
    BodyId.MERCURY,
    BodyId.VENUS,
    BodyId.MARS,
    BodyId.JUPITER,
    BodyId.SATURN,
    BodyId.URANUS,
    BodyId.NEPTUNE,
)

_KIND: Final[dict[BodyId, BodyKind]] = {
    BodyId.SUN: BodyKind.SUN,
    BodyId.MOON: BodyKind.MOON,
}

_DISPLAY_NAME: Final[dict[BodyId, str]] = {
    body_id: body_id.value.capitalize() for body_id in BodyId
}

#: SPK target name per body. ``"<planet> barycenter"`` is spelled out rather
#: than left to skyfield's ``kernel["mars"]`` shorthand so that the
#: barycentre-versus-centre choice documented in the module docstring is
#: visible at the point it is made.
_SPK_TARGET: Final[dict[BodyId, str]] = {
    BodyId.SUN: "sun",
    BodyId.MOON: "moon",
    BodyId.MERCURY: "mercury barycenter",
    BodyId.VENUS: "venus barycenter",
    BodyId.MARS: "mars barycenter",
    BodyId.JUPITER: "jupiter barycenter",
    BodyId.SATURN: "saturn barycenter",
    BodyId.URANUS: "uranus barycenter",
    BodyId.NEPTUNE: "neptune barycenter",
}

#: Equatorial radii in km. Sun: IAU 2015 Resolution B3 nominal solar radius.
#: Everything else: Archinal et al. 2018, "Report of the IAU Working Group on
#: Cartographic Coordinates and Rotational Elements: 2015", Table 5 — the 1 bar
#: level for the four giants, which is what "the disk of Jupiter" means.
#: Not in ``units.py``: these are body facts, not units.
_EQUATORIAL_RADIUS_KM: Final[dict[BodyId, float]] = {
    BodyId.SUN: 695_700.0,
    BodyId.MOON: 1_737.4,
    BodyId.MERCURY: 2_440.53,
    BodyId.VENUS: 6_051.8,
    BodyId.MARS: 3_396.19,
    BodyId.JUPITER: 71_492.0,
    BodyId.SATURN: 60_268.0,
    BodyId.URANUS: 25_559.0,
    BodyId.NEPTUNE: 24_764.0,
}

_KM_PER_AU: Final = 149_597_870.7

_CREDIT: Final = (
    "Solar-system positions computed from the JPL DE440s planetary ephemeris "
    "(NASA/JPL-Caltech); body radii from IAU 2015 Resolution B3 (Sun) and "
    "Archinal et al. 2018, IAU WGCCRE 2015 report (planets and Moon)."
)


def bodies_credit() -> str:
    """The attribution line for this layer, for the sky map's credit strip.

    A function rather than an exported constant to match
    :func:`ssc.astro.stars.bright_stars_credit` and
    :func:`ssc.astro.constellations.boundaries_credit`, so the API layer never
    has to know whether a credit is read from a file or fixed in code.
    """
    return _CREDIT


@dataclass(frozen=True, slots=True)
class BodyPosition:
    """One solar-system body at one instant. See the module docstring for the
    frame, the origin and the phase-angle convention — every field here obeys
    them."""

    id: BodyId
    name: str
    kind: BodyKind
    #: Right ascension in **hours, [0, 24)** — the JSON-boundary form of
    #: DESIGN.md 3.4, and the form this dataclass exists to hand to the API.
    ra_j2000_h: Hours
    dec_j2000_deg: Deg
    #: Equatorial angular width of the full disk.
    apparent_diameter_arcmin: ArcMin
    #: ``None`` for the Sun only, where it is meaningless rather than unknown —
    #: the Sun is its own light source. Never 0 for "no data".
    illuminated_fraction: float | None
    #: Sun-body-observer angle, ``[0, 180]`` deg. ``None`` for the Sun, as above.
    phase_angle_deg: Deg | None


def _apparent_diameter(body_id: BodyId, distance_au: float) -> ArcMin:
    """Angular width of the disk at ``distance_au``.

    ``2 * asin(r / d)`` rather than ``2 * atan(r / d)``: the observer sees the
    tangent points, not the poles of the disk. The two differ by 0.01 arcsec
    for the Sun and less for everything else, but asin is the correct one and
    costs nothing.
    """
    radius_au = _EQUATORIAL_RADIUS_KM[body_id] / _KM_PER_AU
    # Unreachable for a real observer on Earth (the closest body, the Moon, is
    # ~220 of its own radii away); present so a nonsense distance raises
    # instead of handing back a NaN diameter.
    if distance_au <= radius_au:
        raise ValueError(
            f"{body_id.value} distance {distance_au} au is inside its own radius "
            f"{radius_au} au; the observer is not on Earth"
        )
    return deg_to_arcmin(Deg(math.degrees(2.0 * math.asin(radius_au / distance_au))))


def body_position(
    ephemeris: Ephemeris, site: Site, when: datetime, body_id: BodyId
) -> BodyPosition:
    """One body's apparent J2000 position, disk size and phase at ``when``.

    ``when`` must be timezone-aware; it is normalised to UTC and passed to the
    timescale, never defaulted to the wall clock.
    """
    instant = ephemeris.timescale.from_datetime(_require_utc("when", when))
    observer = ephemeris.observer(site).at(instant)
    position = observer.observe(ephemeris.kernel[_SPK_TARGET[body_id]]).apparent()
    ra, dec, distance = position.radec()

    if body_id is BodyId.SUN:
        illuminated: float | None = None
        phase_deg: Deg | None = None
    else:
        illuminated = float(position.fraction_illuminated(ephemeris.sun))
        phase_deg = Deg(float(position.phase_angle(ephemeris.sun).degrees))

    return BodyPosition(
        id=body_id,
        name=_DISPLAY_NAME[body_id],
        kind=_KIND.get(body_id, BodyKind.PLANET),
        ra_j2000_h=wrap_hours(float(ra.hours)),
        dec_j2000_deg=Deg(float(dec.degrees)),
        apparent_diameter_arcmin=_apparent_diameter(body_id, float(distance.au)),
        illuminated_fraction=illuminated,
        phase_angle_deg=phase_deg,
    )


def body_positions(
    ephemeris: Ephemeris,
    site: Site,
    when: datetime,
    body_ids: tuple[BodyId, ...] = BODY_ORDER,
) -> tuple[BodyPosition, ...]:
    """Every requested body at one instant, in :data:`BODY_ORDER`.

    The whole set is nine kernel lookups and costs about a millisecond, so
    there is no partial-computation path and no caching here: the caller asks
    for an instant and gets the instant.
    """
    wanted = set(body_ids)
    return tuple(
        body_position(ephemeris, site, when, body_id) for body_id in BODY_ORDER if body_id in wanted
    )


def separation_deg(first: BodyPosition, second: BodyPosition) -> Deg:
    """Great-circle separation between two reported positions, in ``[0, 180]``.

    Exists so that a caller (or a test pinning an eclipse) never has to
    re-derive spherical trigonometry from the JSON-boundary hours field.
    """
    ra1 = math.radians(hours_to_deg(first.ra_j2000_h))
    ra2 = math.radians(hours_to_deg(second.ra_j2000_h))
    dec1 = math.radians(first.dec_j2000_deg)
    dec2 = math.radians(second.dec_j2000_deg)
    # Vincenty's formula: stable at both small and large separations, unlike
    # the plain cosine rule (rounding at small angles) or plain haversine
    # (fine here, but this form needs no special case near 180 deg either).
    delta_ra = ra2 - ra1
    numerator = math.hypot(
        math.cos(dec2) * math.sin(delta_ra),
        math.cos(dec1) * math.sin(dec2) - math.sin(dec1) * math.cos(dec2) * math.cos(delta_ra),
    )
    denominator = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(
        delta_ra
    )
    return Deg(math.degrees(math.atan2(numerator, denominator)))


# --- a body's own motion: events, altitude curve, drift rate -----------------
#
# ``ssc.astro.events`` solves rise/set/transit for a *fixed* RA/Dec. A solar-
# system body has none, and the difference is not academic. Solved for the Moon
# at Greenwich over 2026-09-10, the moving-body answer against the same solver
# handed the Moon's position frozen at the window start:
#
#     moonrise   04:30:15Z moving   vs 04:13:08Z frozen   —  17 min 07 s out
#     transit    11:23:46Z moving   vs 11:00:24Z frozen   —  23 min 22 s out
#     moonset    17:58:09Z moving   vs 17:47:41Z frozen   —  10 min 28 s out
#
# and for Jupiter the same day, 9 s / 21 s / 21 s. So the fixed approximation is
# arguably tolerable for the outer planets and flatly wrong for the Moon. Rather
# than encode that as a per-body policy — a table that would have to be re-judged
# every time the frame size or the tolerance changes — every body here is solved
# from the ephemeris at every instant the solver evaluates, which costs one
# vectorised kernel pass and leaves no approximation to justify. The residual is
# then the solver's own, validated against USNO/IMCCE in
# ``tests/astro/test_body_targets.py``.


def body_events(
    ephemeris: Ephemeris,
    site: Site,
    body_id: BodyId,
    start: datetime,
    end: datetime,
    min_altitude_deg: Deg = TRUE_HORIZON_DEG,
) -> tuple[EventTime, ...]:
    """``rises_above`` / ``transits`` / ``sets_below`` for a body, with its own motion.

    The same contract, the same event names and the same explained absences as
    :func:`ssc.astro.events.target_events` — deliberately, so a caller holding a
    target reference (:mod:`ssc.astro.targets`) gets one vocabulary whichever
    kind it holds. The threshold is a **geometric** altitude, the user's
    minimum-altitude limit: no refraction, no horizon dip, no allowance for the
    body's disk.

    That last point is what separates this from :func:`ssc.astro.events.lunar_events`,
    which answers the *almanac's* question — upper limb at the refracted horizon —
    and is the function to use for a "moonrise" a user compares against a
    printed table. This one answers the planner's question, "when is it above my
    tree line".

    The Sun is accepted here. Refusing it belongs at target *resolution*
    (:mod:`ssc.astro.targets`), not in the ephemeris layer: "when does the Sun
    clear 10 deg" is a legitimate question and the answer points nothing at it.

    ``start`` and ``end`` must be timezone-aware; they are the only clock.
    """
    start = _require_utc("start", start)
    end = _require_utc("end", end)
    if end <= start:
        raise ValueError("end must be after start")
    if not -90.0 <= min_altitude_deg <= 90.0:
        raise ValueError(f"min_altitude_deg out of range: {min_altitude_deg}")

    body = ephemeris.kernel[_SPK_TARGET[body_id]]
    return _sorted(
        [
            *_rise_set(
                ephemeris, site, body, TargetEvent.RISES_ABOVE, True, min_altitude_deg, start, end
            ),
            *_rise_set(
                ephemeris, site, body, TargetEvent.SETS_BELOW, False, min_altitude_deg, start, end
            ),
            *_transits(ephemeris, site, body, TargetEvent.TRANSITS, start, end),
        ]
    )


@dataclass(frozen=True, slots=True)
class BodyAltitudeSample:
    """One instant of a body's altitude curve. Apparent altaz, as everywhere else
    in the astro core: light-time and aberration applied, atmospheric refraction
    not."""

    when: datetime
    altitude_deg: Deg
    azimuth_deg: Deg


def body_altitude_curve(
    ephemeris: Ephemeris,
    site: Site,
    body_id: BodyId,
    start: datetime,
    end: datetime,
    samples: int,
) -> tuple[BodyAltitudeSample, ...]:
    """``samples`` uniformly-spaced altitudes across ``[start, end]``, inclusive
    of both ends, with the body re-observed at every one of them.

    A fixed-RA/Dec curve cannot stand in for this: over one night the Moon
    walks ~7 deg along its own orbit, which shows up as a visibly wrong curve
    shape, not merely a shifted one.

    ``samples`` is refused below 2 (a curve needs two ends) and above
    :data:`ssc.astro.events.MAX_VISIBILITY_SAMPLES` rather than clamped — see
    that constant for why the ceiling is loud.
    """
    start = _require_utc("start", start)
    end = _require_utc("end", end)
    if end <= start:
        raise ValueError("end must be after start")
    if not 2 <= samples <= MAX_VISIBILITY_SAMPLES:
        raise ValueError(f"samples must be in [2, {MAX_VISIBILITY_SAMPLES}], got {samples}")

    step = (end - start) / (samples - 1)
    times = [start + step * index for index in range(samples)]
    body = ephemeris.kernel[_SPK_TARGET[body_id]]
    return tuple(
        BodyAltitudeSample(when=when, altitude_deg=Deg(altitude), azimuth_deg=wrap_deg_360(azimuth))
        for when, altitude, azimuth in _altaz_samples(
            ephemeris.observer(site), body, ephemeris.timescale, times
        )
    )


#: Half-width of the symmetric finite difference :func:`apparent_motion_arcmin_per_hour`
#: takes. 30 min is short enough that the Moon's path is straight over the
#: interval to well under an arcminute, and long enough that the two endpoint
#: positions differ by far more than the ephemeris's own noise.
_MOTION_HALF_STEP_HOURS: Final = 0.5


def apparent_motion_arcmin_per_hour(
    ephemeris: Ephemeris, site: Site, when: datetime, body_id: BodyId
) -> ArcMin:
    """How fast the body is moving across the sky at ``when``, arcmin per hour.

    Great-circle rate, topocentric, including the parallactic component — this
    is the number that says how stale a *stored* position becomes, so it is the
    apparent motion an observer sees, not the geocentric orbital one.

    This exists so that "the Moon must be re-resolved at run time, Uranus need
    not" is a measurement a caller makes against its own frame size and its own
    lead time, rather than a constant someone wrote down once. Measured at
    Bratislava, 2026-09-10 18:00 UTC: Moon 38.6, Mercury 4.2, Mars 1.6,
    Jupiter 0.5, Uranus 0.002 arcmin/h.
    """
    when = _require_utc("when", when)
    delta = timedelta(hours=_MOTION_HALF_STEP_HOURS)
    before = body_position(ephemeris, site, when - delta, body_id)
    after = body_position(ephemeris, site, when + delta, body_id)
    return ArcMin(deg_to_arcmin(separation_deg(before, after)) / (2.0 * _MOTION_HALF_STEP_HOURS))


__all__ = [
    "BODY_ORDER",
    "BodyAltitudeSample",
    "BodyId",
    "BodyKind",
    "BodyPosition",
    "apparent_motion_arcmin_per_hour",
    "bodies_credit",
    "body_altitude_curve",
    "body_events",
    "body_position",
    "body_positions",
    "separation_deg",
]
