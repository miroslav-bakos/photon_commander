# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The coordinate frame the mount speaks, and how to get in and out of it.

An ASCOM mount declares its equatorial frame through ``EquatorialSystem``.
**The Seestar S30 Pro reports** ``equTopocentric`` **(1)** — coordinates of
*date* — while every RA/Dec elsewhere in SSC is J2000: the bundled catalogue
(:mod:`ssc.astro.catalog`), plate solves, the sky map, and the JSON boundary
in ``docs/DESIGN.md`` 3.4. In 2026 the gap is **~20 arcminutes**
(``docs/M0-FINDINGS.md``, "Mount capability set, read connected on 9.31"), and
it points the same way for every target, so it does not look like noise:

* Drawn unconverted, the mount's reported position lands ~20' from the truth
  and reads as *pointing error*, which is exactly the quantity the red FOV
  indicator of ``docs/DESIGN.md`` 3.5.1 exists to reveal.
* Slewed unconverted, that error goes to the mount. A slew lands 20' off and
  is recoverable. **A sync writes 20' into the mount's pointing model**, where
  it persists and degrades every later slew — state corruption, not a display
  bug.

So both directions are needed, and this module is the only place either is
done.

**It is not just precession.** Precession is ~20' and dominates, but stopping
there leaves nutation (~17") and annual aberration (~20") on the table. That
is tolerable inside a 2.24 deg frame and *not* tolerable in a pointing model or
in a polar-alignment residual measured in arcminutes. Both directions here go
through skyfield's full apparent-place chain — frame bias, precession,
nutation, annual *and* diurnal aberration, relativistic deflection — never a
hand-composed rotation. Measured against JPL Horizons' own apparent places
(``tests/astro/horizons_bodies_reference.json``, quantity 1 in, quantity 2
out): **worst deviation 0.079 arcsec** over nine bodies.

**Two device facts shape this, both measured on firmware 9.31:**

* ``DoesRefraction`` is **false**, so the mount applies no refraction and
  neither may SSC. Nothing here touches an ``altaz()`` call or a refraction
  model; adding one would introduce a disagreement of up to a degree near the
  horizon.
* ``SiteElevation`` is **not implemented** and errors on read, so the site's
  height is unavailable. It does not matter: a catalogue target is at infinite
  distance, so elevation enters only through the observer's rotational
  velocity, i.e. diurnal aberration. Measured, 0 m against 4000 m:
  **0.13 milliarcsec**. :func:`mount_site` therefore substitutes
  :data:`ASSUMED_ELEVATION_M` for an unknown elevation, in one named place,
  rather than letting each caller quietly pass zero.

Latitude and longitude are *not* optional the same way — topocentric means
observer-centred, and dropping to a geocentric observer costs 0.21 arcsec of
diurnal aberration (measured). Both are readable from the mount, and the
mount's own values are the ones to use: they define the frame it is speaking,
which is not necessarily the site SSC is configured for.

**Frames are data, not a boolean.** :class:`MountFrame` carries all five ASCOM
``EquatorialCoordinateType`` values so that a driver announcing ``B1950`` or
``Other`` can be *represented and refused* (:class:`UnsupportedMountFrameError`)
rather than guessed at. A driver announcing ``J2000`` needs no conversion and
gets none.

**Do not feed this an apparent place.** :func:`to_mount_frame` expects
*astrometric* J2000 — a catalogue entry or a plate solve. The positions
:mod:`ssc.astro.bodies` reports already carry aberration and deflection (in the
J2000 frame, by that module's stated contract), so passing one here applies
both a second time; the same trap cost 5.5 arcsec in the body-target work
(``tests/astro/test_body_targets.py``). A body's mount-frame coordinates come
from computing its apparent place of date directly, not from converting its
reported J2000 position.

**Purity.** No I/O beyond the already-open :class:`~ssc.astro.events.Ephemeris`
handle, no ``datetime.now()`` — the instant is an argument (CLAUDE.md
invariant 3), and it must be timezone-aware UTC (invariant 5).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from skyfield.api import Star

from ssc.astro.events import Ephemeris, Site, _require_utc
from ssc.astro.units import (
    ArcSec,
    Deg,
    Hours,
    Metres,
    deg_to_hours,
    hours_to_deg,
    wrap_deg_360,
)

__all__ = [
    "ASCOM_EQUATORIAL_SYSTEM",
    "ASSUMED_ELEVATION_M",
    "SUPPORTED_MOUNT_FRAMES",
    "EquatorialCoord",
    "FrameInversionError",
    "MountFrame",
    "UnsupportedMountFrameError",
    "frame_offset_arcsec",
    "j2000_coord",
    "mount_frame_from_ascom",
    "mount_site",
    "to_j2000",
    "to_mount_frame",
]


class MountFrame(StrEnum):
    """The equatorial frame a driver declares through ASCOM ``EquatorialSystem``.

    All five ``EquatorialCoordinateType`` values are members on purpose: an
    unsupported frame must be *representable* so it can be refused with its own
    name (:class:`UnsupportedMountFrameError`), not collapsed into "not J2000"
    and then guessed at.
    """

    #: ``equOther`` (0). Custom or unspecified — unconvertible by definition.
    OTHER = "other"
    #: ``equTopocentric`` (1). Apparent place of date, observer-centred. What
    #: the Seestar reports, and the only frame here that needs real work.
    TOPOCENTRIC = "topocentric"
    #: ``equJ2000`` (2). Already SSC's frame; conversion is the identity.
    J2000 = "j2000"
    #: ``equJ2050`` (3).
    J2050 = "j2050"
    #: ``equB1950`` (4).
    B1950 = "b1950"


#: ASCOM ``EquatorialCoordinateType`` wire values. Kept here rather than in
#: ``ssc.alpaca`` so that every consumer of a mount's declared frame reads the
#: same table; it is pure data and imports nothing device-shaped.
ASCOM_EQUATORIAL_SYSTEM: Final[Mapping[int, MountFrame]] = {
    0: MountFrame.OTHER,
    1: MountFrame.TOPOCENTRIC,
    2: MountFrame.J2000,
    3: MountFrame.J2050,
    4: MountFrame.B1950,
}

#: The frames this module can convert. Anything else is refused; see the module
#: docstring on why guessing is worse than failing.
SUPPORTED_MOUNT_FRAMES: Final[frozenset[MountFrame]] = frozenset(
    {MountFrame.TOPOCENTRIC, MountFrame.J2000}
)

#: Elevation assumed when the mount cannot report its own (the Seestar's
#: ``SiteElevation`` is unimplemented on 9.31). Costs 0.13 mas against a site
#: at 4000 m — see the module docstring and
#: ``tests/astro/test_frames.py::test_unknown_elevation_costs_under_a_milliarcsecond``.
ASSUMED_ELEVATION_M: Final = Metres(0.0)

#: Convergence bound for the inverse solve, in degrees: 1e-10 deg = 0.36
#: microarcsec, some ten orders of magnitude below the ~20' the conversion moves
#: and five below anything the hardware can express. Tight enough that the
#: inverse is an inverse rather than an approximation, loose enough to stay
#: above skyfield's own double-precision noise floor.
_INVERSE_TOLERANCE_DEG: Final = 1e-10

#: The inverse converges in 3-5 passes for every case tested, pole included; a
#: ceiling of 10 leaves headroom and still fails loudly rather than looping.
_INVERSE_MAX_ITERATIONS: Final = 10


class UnsupportedMountFrameError(ValueError):
    """The mount declared a frame SSC will not convert.

    Raised rather than defaulting to "treat it as J2000" or "treat it as
    topocentric": both readings are plausible, both are silently wrong by up to
    a degree for B1950, and a wrong sync is durable (CLAUDE.md invariant 7).
    """


class FrameInversionError(RuntimeError):
    """The mount-frame-to-J2000 solve did not converge.

    Unreachable in practice — the map is a near-rigid rotation and the fixed
    point is reached in a handful of passes — so reaching this means an
    assumption broke, and a loud failure is the only safe answer.
    """


@dataclass(frozen=True, slots=True)
class EquatorialCoord:
    """An RA/Dec **and the frame it is in**, which is the whole point.

    Degrees, per ``ssc.astro.units``: right ascension wrapped to **[0, 360)**
    on construction (it is circular, so wrapping is the convention, not a
    repair), declination validated in **[-90, +90]** because a value outside it
    is a real error and never a wrap. ``ra_h`` converts to hours for the JSON
    boundary only.

    Frames never mix by accident: a coordinate that has not been through
    :func:`to_j2000` carries ``frame=MountFrame.TOPOCENTRIC`` all the way to
    whoever draws or stores it.
    """

    ra_deg: Deg
    dec_deg: Deg
    frame: MountFrame

    def __post_init__(self) -> None:
        if not math.isfinite(self.ra_deg) or not math.isfinite(self.dec_deg):
            raise ValueError(f"non-finite coordinate: ra={self.ra_deg}, dec={self.dec_deg}")
        if not -90.0 <= self.dec_deg <= 90.0:
            raise ValueError(f"declination out of range: {self.dec_deg}")
        object.__setattr__(self, "ra_deg", wrap_deg_360(self.ra_deg))

    @property
    def ra_h(self) -> Hours:
        """Right ascension in hours, ``[0, 24)`` — the JSON boundary form only."""
        return deg_to_hours(self.ra_deg)


def j2000_coord(ra_deg: Deg, dec_deg: Deg) -> EquatorialCoord:
    """A J2000 coordinate. Sugar for the common case, so callers never hand-write
    the frame and never get it wrong."""
    return EquatorialCoord(ra_deg=ra_deg, dec_deg=dec_deg, frame=MountFrame.J2000)


def mount_frame_from_ascom(value: int) -> MountFrame:
    """Map an ASCOM ``EquatorialSystem`` wire value to a :class:`MountFrame`.

    An unknown integer raises: a driver reporting 7 is a driver SSC does not
    understand, and mapping it to a default would be the exact silent
    degradation invariant 7 forbids.
    """
    try:
        return ASCOM_EQUATORIAL_SYSTEM[value]
    except KeyError:
        raise ValueError(
            f"unknown ASCOM EquatorialCoordinateType {value}; expected one of "
            f"{sorted(ASCOM_EQUATORIAL_SYSTEM)}"
        ) from None


def mount_site(latitude_deg: Deg, longitude_deg: Deg, elevation_m: Metres | None = None) -> Site:
    """The observer the mount's frame is referred to, from the mount's own
    ``SiteLatitude``/``SiteLongitude``/``SiteElevation``.

    ``elevation_m=None`` means **unknown** — which is what the Seestar reports,
    since ``SiteElevation`` errors on read — and is answered with
    :data:`ASSUMED_ELEVATION_M`. That substitution is deliberate and bounded
    (0.13 mas at 4000 m, tested), and it lives here so it happens once, named,
    instead of every caller passing a bare ``0.0`` that reads like a
    measurement.
    """
    return Site(
        latitude_deg=latitude_deg,
        longitude_deg=longitude_deg,
        elevation_m=ASSUMED_ELEVATION_M if elevation_m is None else elevation_m,
    )


def to_mount_frame(
    ephemeris: Ephemeris,
    site: Site,
    coord: EquatorialCoord,
    when: datetime,
    mount_frame: MountFrame,
) -> EquatorialCoord:
    """Convert an **astrometric J2000** coordinate into the frame the mount speaks.

    This is the outbound direction: the one used for slew and sync targets, and
    the one where getting it wrong is durable. ``coord`` must already be J2000
    (a catalogue entry or a plate solve, *not* a
    :mod:`ssc.astro.bodies` position — see the module docstring).

    ``mount_frame`` is the mount's declared ``EquatorialSystem``, taken as data:
    :data:`MountFrame.J2000` returns the input unchanged, :data:`MountFrame.TOPOCENTRIC`
    applies the full apparent-place chain for ``when``, and anything else raises.

    ``when`` must be timezone-aware; it is the instant the coordinates are *for*,
    never "now".
    """
    if coord.frame is not MountFrame.J2000:
        raise ValueError(
            f"to_mount_frame takes a J2000 coordinate, got {coord.frame.value}; call to_j2000 first"
        )
    _check_supported(mount_frame)
    if mount_frame is MountFrame.J2000:
        return coord

    instant = _require_utc("when", when)
    ra_deg, dec_deg = _apparent_of_date(ephemeris, site, instant)(coord.ra_deg, coord.dec_deg)
    return EquatorialCoord(ra_deg=ra_deg, dec_deg=dec_deg, frame=mount_frame)


def to_j2000(
    ephemeris: Ephemeris, site: Site, coord: EquatorialCoord, when: datetime
) -> EquatorialCoord:
    """Convert a coordinate the mount reported into **astrometric J2000**.

    This is the inbound direction: what the red FOV indicator of
    ``docs/DESIGN.md`` 3.5.1 needs before it can be drawn on a J2000 sky map.
    The frame is read from ``coord`` itself, so a mount that declares J2000
    round-trips untouched and one that declares B1950 is refused.

    There is no closed-form inverse of an apparent place, so this solves for the
    J2000 coordinate whose forward image is ``coord``, iterating in unit-vector
    space (pole-safe, unlike correcting RA and Dec separately) to
    :data:`_INVERSE_TOLERANCE_DEG`. Measured round-trip residual over seven
    positions including a target 0.01 deg from the pole: **under 0.2
    microarcsec**.
    """
    _check_supported(coord.frame)
    if coord.frame is MountFrame.J2000:
        return coord

    instant = _require_utc("when", when)
    forward = _apparent_of_date(ephemeris, site, instant)
    ra_deg, dec_deg = _invert(forward, coord.ra_deg, coord.dec_deg)
    return EquatorialCoord(ra_deg=ra_deg, dec_deg=dec_deg, frame=MountFrame.J2000)


def frame_offset_arcsec(first: EquatorialCoord, second: EquatorialCoord) -> ArcSec:
    """Great-circle separation between two coordinates, in arcsec.

    Exists so a caller can *show* the size of the frame correction — or of a
    believed-versus-solved disagreement (``docs/DESIGN.md`` 3.5.1) — without
    re-deriving spherical trigonometry. It deliberately does **not** check that
    the two share a frame: comparing across frames is precisely the measurement
    this answers.
    """
    return ArcSec(
        _separation_deg(first.ra_deg, first.dec_deg, second.ra_deg, second.dec_deg) * 3600.0
    )


# --- internals -------------------------------------------------------------


def _check_supported(frame: MountFrame) -> None:
    if frame not in SUPPORTED_MOUNT_FRAMES:
        raise UnsupportedMountFrameError(
            f"mount declares EquatorialSystem {frame.value!r}, which SSC does not convert "
            f"(supported: {sorted(f.value for f in SUPPORTED_MOUNT_FRAMES)}). Refusing rather "
            "than guessing: a wrong frame is ~20 arcmin of silent error, and a sync makes it "
            "durable."
        )


def _apparent_of_date(
    ephemeris: Ephemeris, site: Site, when: datetime
) -> Callable[[float, float], tuple[Deg, Deg]]:
    """Build the J2000 -> topocentric-apparent-of-date map for one instant.

    Returned as a closure because the inverse evaluates it several times and the
    observer and the ``Time`` (which carries the precession/nutation matrices)
    are the expensive parts; rebuilding them per iteration would quadruple the
    cost of every mount poll for nothing.

    ``radec(epoch=t)`` rotates by skyfield's ``Time.M``, i.e. frame bias then
    precession then nutation — the *true* equator and equinox of date, which is
    what ASCOM ``equTopocentric`` means. No refraction is applied anywhere in
    this chain, matching the device's ``DoesRefraction: false``.
    """
    observer: Any = ephemeris.observer(site)
    time: Any = ephemeris.timescale.from_datetime(when)
    at_time: Any = observer.at(time)

    def forward(ra_deg: float, dec_deg: float) -> tuple[Deg, Deg]:
        star = Star(ra_hours=ra_deg / 15.0, dec_degrees=dec_deg)
        apparent: Any = at_time.observe(star).apparent()
        ra, dec, _ = apparent.radec(epoch=time)
        return Deg(hours_to_deg(Hours(float(ra.hours)))), Deg(float(dec.degrees))

    return forward


def _invert(
    forward: Callable[[float, float], tuple[Deg, Deg]], ra_deg: Deg, dec_deg: Deg
) -> tuple[Deg, Deg]:
    """Solve ``forward(x) == (ra_deg, dec_deg)`` for ``x`` by fixed-point iteration.

    The forward map is a rotation of ~20 arcmin plus a distortion of order
    1e-4 of that, so the naive correction ``x <- x + (target - forward(x))``
    contracts hard and reaches the tolerance in 3-5 passes. The correction is
    applied to unit vectors rather than to RA and Dec because an RA correction
    in degrees diverges as ``1/cos(dec)`` and would misbehave exactly where the
    Seestar spends its time when polar-aligned: near the pole.
    """
    target = _unit_vector(ra_deg, dec_deg)
    guess = target
    for _ in range(_INVERSE_MAX_ITERATIONS):
        guess_ra, guess_dec = _from_unit_vector(guess)
        image = _unit_vector(*forward(guess_ra, guess_dec))
        if _separation_deg_vec(image, target) <= _INVERSE_TOLERANCE_DEG:
            return guess_ra, guess_dec
        guess = _normalise(tuple(g + t - i for g, t, i in zip(guess, target, image, strict=True)))
    raise FrameInversionError(
        f"mount-frame to J2000 solve did not reach {_INVERSE_TOLERANCE_DEG} deg in "
        f"{_INVERSE_MAX_ITERATIONS} passes for ra={ra_deg}, dec={dec_deg}"
    )


_Vector = tuple[float, float, float]


def _unit_vector(ra_deg: float, dec_deg: float) -> _Vector:
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    return (math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec))


def _from_unit_vector(vector: _Vector) -> tuple[Deg, Deg]:
    x, y, z = vector
    return (
        wrap_deg_360(math.degrees(math.atan2(y, x))),
        Deg(math.degrees(math.atan2(z, math.hypot(x, y)))),
    )


def _normalise(vector: tuple[float, ...]) -> _Vector:
    x, y, z = vector
    norm = math.sqrt(x * x + y * y + z * z)
    if norm == 0.0:
        raise FrameInversionError("iteration collapsed to the origin")
    return (x / norm, y / norm, z / norm)


def _separation_deg_vec(first: _Vector, second: _Vector) -> float:
    """Angle between two unit vectors, via ``atan2(|a x b|, a . b)`` — accurate at
    the microarcsecond separations the inverse's convergence test compares, where
    a plain ``acos`` of the dot product has no significant digits left."""
    dot = sum(a * b for a, b in zip(first, second, strict=True))
    cross = (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )
    return math.degrees(math.atan2(math.sqrt(sum(c * c for c in cross)), dot))


def _separation_deg(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    """Great-circle separation in degrees, from RA/Dec in degrees.

    A third separation helper in ``ssc.astro`` and deliberately so:
    :func:`ssc.astro.catalog.cos_angular_separation` returns a cosine for cone
    tests (no ``acos``, useless at arcsec scale) and
    :func:`ssc.astro.bodies.separation_deg` takes two ``BodyPosition`` objects.
    This one takes raw degrees and stays accurate at the sub-arcsecond
    separations this module reports.
    """
    return _separation_deg_vec(_unit_vector(ra1_deg, dec1_deg), _unit_vector(ra2_deg, dec2_deg))
