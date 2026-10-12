# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Naming a target that may not hold still — ``docs/DESIGN.md`` 3.6.

:class:`~ssc.astro.catalog.CatalogObject` is the wrong shape for the Moon, and
not by a little: its whole contract is *fixed J2000 coordinates*. A solar-system
body has none. Measured over 6 h of one night (2026-09-10) the Moon moves 233.6'
in right ascension, Mercury 22.7', Mars 10.1', Jupiter 3.1', Uranus 0.005'.
Resolving "Moon" when a plan is written and storing the answer therefore images
roughly where the Moon *was*.

So a **target reference stores what the user chose, never where it was**:

* :class:`CatalogTarget` carries the catalogue record, whose coordinates are
  already time-independent, and
* :class:`BodyTarget` carries a :class:`~ssc.astro.bodies.BodyId` and nothing
  else. It has no coordinates to carry.

Both are handed to :func:`resolve_position` *with an instant* to get numbers.
That is the whole point of the type: a caller above can hold a choice without
having decided when to evaluate it, and cannot accidentally freeze a body,
because there is nothing frozen to hold.

**The Sun is not a target and there is no flag that makes it one.** It stays in
:mod:`ssc.astro.bodies` — twilight, solar elongation and "is it dark yet" all
need it, and the sky map draws it — but it is unreachable through every entry
point in this module, and :class:`BodyTarget` refuses to be constructed with it
at all. The reason is physical rather than editorial: the Seestar's ``Dark``
filter position is a mechanical shutter, not a solar filter, so an unfiltered
slew onto the Sun destroys the sensor in seconds and permanently. DESIGN 3.6 is
explicit that this is not a confirmation dialog — a dialog is a thing a tired
person clicks through at 2 a.m.

Angles here follow the rest of :mod:`ssc.astro`: degrees internally, right
ascension in hours only at the JSON boundary (``units.py``), times UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from ssc.astro.bodies import (
    BodyAltitudeSample,
    BodyId,
    BodyPosition,
    body_altitude_curve,
    body_events,
    body_position,
)
from ssc.astro.catalog import (
    CatalogObject,
    TargetCatalog,
    UnknownTargetError,
    normalize_query,
)
from ssc.astro.events import (
    TRUE_HORIZON_DEG,
    Ephemeris,
    EventTime,
    Site,
    _require_utc,
    target_events,
    target_position,
)
from ssc.astro.units import Deg, Hours, deg_to_hours, hours_to_deg

__all__ = [
    "BodyTarget",
    "CatalogTarget",
    "ResolvedTarget",
    "SunNotATargetError",
    "TargetKind",
    "TargetRef",
    "TrackingRate",
    "resolve_body",
    "resolve_position",
    "resolve_target",
    "target_ref_altitude_curve",
    "target_ref_events",
    "tracking_rate_for_body",
]


class TargetKind(StrEnum):
    """Which of the two references a caller is holding.

    A UI must show this, per DESIGN 3.6: a body result listed indistinguishably
    beside a catalogue entry invites exactly the "store the coordinates" mistake
    this module exists to prevent.
    """

    CATALOG = "catalog"
    BODY = "body"


class TrackingRate(StrEnum):
    """Which of the mount's drive rates a target needs (DESIGN 3.6 and 4.4b).

    Named for ASCOM's ``DriveRates``, which is what the Alpaca layer will set;
    :mod:`ssc.astro` neither knows nor cares how it is applied — this module
    only says which one the target *wants*. The coupling is the scheduler's to
    make, and DESIGN 3.6's point is that it is not a second decision the user
    should have to remember: at ~0.5 deg/h the Moon leaves a 2.24 x 3.99 deg
    telephoto frame within a few hours of sidereal tracking.
    """

    SIDEREAL = "sidereal"
    LUNAR = "lunar"
    SOLAR = "solar"


#: Every body that is not the Moon tracks at the sidereal rate. That is an
#: approximation for the planets and a deliberate one: the fastest of them,
#: Mercury, drifts about 4' per hour against the stars (see
#: :func:`ssc.astro.bodies.apparent_motion_arcmin_per_hour`), which is 3% of the
#: telephoto frame's 2.24 deg short side in an hour — visible in a long stack, not a
#: reason to want the *solar* rate, which is wrong for a planet in a different
#: direction. The mount offers three rates and none of them is planetary.
_BODY_TRACKING_RATE: Final[dict[BodyId, TrackingRate]] = {
    BodyId.MOON: TrackingRate.LUNAR,
    # The Sun is here for completeness of the mapping, never because it can be
    # selected — see :class:`SunNotATargetError`.
    BodyId.SUN: TrackingRate.SOLAR,
}


def tracking_rate_for_body(body_id: BodyId) -> TrackingRate:
    """The drive rate this body wants. Total over :class:`~ssc.astro.bodies.BodyId`."""
    return _BODY_TRACKING_RATE.get(body_id, TrackingRate.SIDEREAL)


class SunNotATargetError(UnknownTargetError):
    """Raised for any query that names the Sun.

    A subclass of :class:`~ssc.astro.catalog.UnknownTargetError` so that every
    existing handler already fails closed on it, and a distinct type so that a
    caller which wants to explain *why* can catch it first. The distinction
    matters: "the Sun is excluded because pointing at it destroys the sensor"
    and "no such object" are different facts, and a user typing ``sun`` deserves
    the first one.
    """


_SUN_REFUSAL: Final = (
    "the Sun is deliberately not selectable as a target. The Seestar's Dark filter "
    "position is a mechanical shutter, not a solar filter, so an unfiltered slew onto "
    "the Sun would destroy the sensor permanently within seconds. It stays on the sky "
    "map and in the twilight calculations; there is no override."
)

#: Queries that mean the Sun and are therefore refused with :data:`_SUN_REFUSAL`
#: rather than a generic miss. ``SOL`` is included only to widen the *refusal*,
#: never resolution — it resolves to nothing either way, but the user gets the
#: reason instead of a shrug.
_SUN_QUERIES: Final[frozenset[str]] = frozenset({"SUN", "SOL", "THE SUN"})

#: Body name -> id, normalised exactly as the catalogue normalises a common
#: name, so "Jupiter", "jupiter" and "  JUPITER " all hit and nothing else does.
#: No nicknames: the catalogue resolver is tolerant about case and spacing and
#: about nothing else, and this is held to the same bar (DESIGN 3.6).
_BODY_BY_NAME: Final[dict[str, BodyId]] = {
    normalize_query(body_id.value): body_id for body_id in BodyId if body_id is not BodyId.SUN
}


@dataclass(frozen=True, slots=True)
class CatalogTarget:
    """A bundled catalogue object chosen as a target. Its coordinates are fixed,
    so holding the record itself is safe — this is the case the old ``resolve()``
    already handled, given a type so the moving case can sit beside it."""

    object: CatalogObject

    @property
    def kind(self) -> TargetKind:
        return TargetKind.CATALOG

    @property
    def name(self) -> str:
        return self.object.name

    @property
    def tracking_rate(self) -> TrackingRate:
        return TrackingRate.SIDEREAL

    @property
    def moves(self) -> bool:
        """Whether :func:`resolve_position` will give a different answer at a
        different instant. ``False`` here, and the reason the two kinds are one
        union rather than one type with an optional field."""
        return False


@dataclass(frozen=True, slots=True)
class BodyTarget:
    """A solar-system body chosen as a target — a *name*, never a position.

    Refuses the Sun at construction, so there is no route to a Sun target even
    for a caller that bypasses :func:`resolve_target` and builds the reference
    by hand.
    """

    body: BodyId

    def __post_init__(self) -> None:
        if self.body is BodyId.SUN:
            raise SunNotATargetError(_SUN_REFUSAL)

    @property
    def kind(self) -> TargetKind:
        return TargetKind.BODY

    @property
    def name(self) -> str:
        return self.body.value.capitalize()

    @property
    def tracking_rate(self) -> TrackingRate:
        return tracking_rate_for_body(self.body)

    @property
    def moves(self) -> bool:
        return True


#: What the user chose. Deliberately a union of two small records rather than
#: one record with nullable coordinate fields: a nullable position is a
#: position someone will eventually read without checking.
TargetRef = CatalogTarget | BodyTarget


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """A reference evaluated at one instant.

    ``when`` is always recorded, including for a catalogue target where it did
    not affect the answer, so that a consumer storing this can always tell what
    it is looking at and never has to ask the reference again to find out
    whether the numbers expire. ``moves`` says whether they do.
    """

    ref: TargetRef
    name: str
    #: The instant asked for, UTC. For a :class:`BodyTarget` these coordinates
    #: are valid at this instant and nowhere else.
    when: datetime
    ra_j2000_deg: Deg
    dec_j2000_deg: Deg
    tracking_rate: TrackingRate
    moves: bool
    #: The full ephemeris record — disk size, illuminated fraction, phase angle —
    #: for a body; ``None`` for a catalogue object, where the concepts do not
    #: apply. Never a placeholder.
    body_position: BodyPosition | None

    @property
    def ra_j2000_h(self) -> Hours:
        """Right ascension in hours, ``[0, 24)`` — the JSON-boundary form only
        (``units.py``); inside :mod:`ssc.astro` right ascension is degrees."""
        return deg_to_hours(self.ra_j2000_deg)


def resolve_body(query: str) -> BodyId:
    """``"Jupiter"``, ``"jupiter"``, ``"moon"`` -> a :class:`~ssc.astro.bodies.BodyId`.

    Raises :class:`SunNotATargetError` for the Sun and
    :class:`~ssc.astro.catalog.UnknownTargetError` for anything else that is not
    a body — never returns ``None``, matching
    :meth:`ssc.astro.catalog.TargetCatalog.resolve`.
    """
    key = normalize_query(query)
    if key in _SUN_QUERIES:
        raise SunNotATargetError(_SUN_REFUSAL)
    found = _BODY_BY_NAME.get(key)
    if found is None:
        raise UnknownTargetError(
            f"{query!r} is not a solar-system body SSC reports. The bodies are "
            + ", ".join(sorted(name.capitalize() for name in _BODY_BY_NAME))
            + "."
        )
    return found


def resolve_target(catalog: TargetCatalog, query: str) -> TargetRef:
    """Resolve a typed name to whichever kind of target it names.

    Bodies are tried first and the Sun is refused before either lookup runs, so
    the exclusion cannot be defeated by a catalogue file that happens to contain
    an object called "Sun". A body name wins over a catalogue common name for
    the same reason a user typing "Mars" means the planet; no such collision
    exists in the bundled data today.

    Raises :class:`SunNotATargetError` for the Sun and
    :class:`~ssc.astro.catalog.UnknownTargetError` for an unresolvable name,
    with the catalogue's own message — so an unknown name still fails exactly as
    it did before this module existed.
    """
    key = normalize_query(query)
    if key in _SUN_QUERIES:
        raise SunNotATargetError(_SUN_REFUSAL)
    body = _BODY_BY_NAME.get(key)
    if body is not None:
        return BodyTarget(body)
    return CatalogTarget(catalog.resolve(query))


def resolve_position(
    ephemeris: Ephemeris, site: Site, ref: TargetRef, when: datetime
) -> ResolvedTarget:
    """Where the reference is at ``when``.

    For a :class:`BodyTarget` this is a topocentric ephemeris evaluation and the
    answer is good for that instant only. For a :class:`CatalogTarget` the
    catalogue's own coordinates are returned unchanged and ``when`` is merely
    recorded.

    ``when`` must be timezone-aware. There is no default and no clock in this
    module (CLAUDE.md invariant 3).
    """
    match ref:
        case BodyTarget():
            position = body_position(ephemeris, site, when, ref.body)
            return ResolvedTarget(
                ref=ref,
                name=position.name,
                when=_require_utc("when", when),
                ra_j2000_deg=hours_to_deg(position.ra_j2000_h),
                dec_j2000_deg=position.dec_j2000_deg,
                tracking_rate=ref.tracking_rate,
                moves=True,
                body_position=position,
            )
        case CatalogTarget():
            return ResolvedTarget(
                ref=ref,
                name=ref.object.name,
                when=_require_utc("when", when),
                ra_j2000_deg=ref.object.ra_j2000_deg,
                dec_j2000_deg=ref.object.dec_j2000_deg,
                tracking_rate=TrackingRate.SIDEREAL,
                moves=False,
                body_position=None,
            )


def target_ref_events(
    ephemeris: Ephemeris,
    site: Site,
    ref: TargetRef,
    start: datetime,
    end: datetime,
    min_altitude_deg: Deg = TRUE_HORIZON_DEG,
) -> tuple[EventTime, ...]:
    """``rises_above`` / ``transits`` / ``sets_below`` for either kind of target.

    A body is solved from the ephemeris at every instant the solver evaluates
    (:func:`ssc.astro.bodies.body_events`); a catalogue object goes to
    :func:`ssc.astro.events.target_events`. Both return the same
    :class:`~ssc.astro.events.TargetEvent` names and the same explained
    absences, so a caller never branches on the kind.
    """
    match ref:
        case BodyTarget():
            return body_events(ephemeris, site, ref.body, start, end, min_altitude_deg)
        case CatalogTarget():
            return target_events(
                ephemeris,
                site,
                deg_to_hours(ref.object.ra_j2000_deg),
                ref.object.dec_j2000_deg,
                start,
                end,
                min_altitude_deg,
            )


def target_ref_altitude_curve(
    ephemeris: Ephemeris,
    site: Site,
    ref: TargetRef,
    start: datetime,
    end: datetime,
    samples: int,
) -> tuple[BodyAltitudeSample, ...]:
    """``samples`` altitudes across ``[start, end]``, inclusive of both ends.

    A body is re-observed at every sample rather than propagated from one
    position — over a night the Moon walks ~7 deg along its own orbit, which
    changes the shape of the curve and not just its offset.
    """
    match ref:
        case BodyTarget():
            return body_altitude_curve(ephemeris, site, ref.body, start, end, samples)
        case CatalogTarget():
            if samples < 2:
                raise ValueError(f"samples must be at least 2, got {samples}")
            if end <= start:
                raise ValueError("end must be after start")
            step = (end - start) / (samples - 1)
            curve = []
            for index in range(samples):
                position = target_position(
                    ephemeris,
                    site,
                    deg_to_hours(ref.object.ra_j2000_deg),
                    ref.object.dec_j2000_deg,
                    start + step * index,
                )
                curve.append(
                    BodyAltitudeSample(
                        when=position.when,
                        altitude_deg=position.altitude_deg,
                        azimuth_deg=position.azimuth_deg,
                    )
                )
            return tuple(curve)
