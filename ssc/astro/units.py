# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Angle and length units for the astro core, and the conventions that go with them.

Stated once here so no other module has to restate them:

* **Degrees are the internal unit.** Radians exist only inside a formula that
  needs them and never appear in a public signature.
* **Right ascension is hours only at the JSON boundary** (``ra_j2000_h``, see
  ``docs/DESIGN.md`` 3.4). Inside ``ssc.astro`` right ascension is degrees.
* **Longitude is positive east**, latitude positive north, altitude/elevation in
  metres above the ellipsoid.
* There is no project-wide default wrapping interval. Every helper below names
  its own interval, and every caller picks the one it means.

The ``NewType`` aliases are deliberate friction: ``math.sin`` takes a ``float``,
so a ``Rad`` can be handed to it, but a ``Rad`` can never be passed where a
``Deg`` is declared. That is the whole point.
"""

from __future__ import annotations

import math
from typing import Final, NewType

# Named angle types. Distinct under mypy, plain floats at runtime.
Deg = NewType("Deg", float)
Rad = NewType("Rad", float)
Hours = NewType("Hours", float)
ArcMin = NewType("ArcMin", float)
ArcSec = NewType("ArcSec", float)
Metres = NewType("Metres", float)

DEG_PER_HOUR: Final = 15.0
ARCMIN_PER_DEG: Final = 60.0
ARCSEC_PER_DEG: Final = 3600.0

#: Degrees of hour angle swept per hour of UTC by a target of fixed RA — the
#: mean sidereal rate, 360 deg / 23h56m04.0905s.
SIDEREAL_DEG_PER_HOUR: Final = 15.04106864

#: 206.265 arcsec is one radian expressed in arcsec / 1000; the small-angle
#: constant in the plate-scale formula of DESIGN 3.2.
ARCSEC_PER_RADIAN_PER_MM: Final = 206.265


# --- conversions -----------------------------------------------------------


def deg_to_rad(value: Deg) -> Rad:
    """Degrees to radians. No wrapping."""
    return Rad(math.radians(value))


def rad_to_deg(value: Rad) -> Deg:
    """Radians to degrees. No wrapping."""
    return Deg(math.degrees(value))


def hours_to_deg(value: Hours) -> Deg:
    """Hours of right ascension or hour angle to degrees. No wrapping."""
    return Deg(value * DEG_PER_HOUR)


def deg_to_hours(value: Deg) -> Hours:
    """Degrees to hours of right ascension or hour angle. No wrapping."""
    return Hours(value / DEG_PER_HOUR)


def arcsec_to_deg(value: ArcSec) -> Deg:
    """Arcseconds to degrees."""
    return Deg(value / ARCSEC_PER_DEG)


def deg_to_arcsec(value: Deg) -> ArcSec:
    """Degrees to arcseconds."""
    return ArcSec(value * ARCSEC_PER_DEG)


def arcmin_to_deg(value: ArcMin) -> Deg:
    """Arcminutes to degrees."""
    return Deg(value / ARCMIN_PER_DEG)


def deg_to_arcmin(value: Deg) -> ArcMin:
    """Degrees to arcminutes."""
    return ArcMin(value * ARCMIN_PER_DEG)


# --- wrapping --------------------------------------------------------------
#
# Python's float ``%`` can return the modulus itself for a tiny negative
# operand ((-1e-18) % 360.0 == 360.0), which would break a half-open interval.
# Each helper below folds that case back to the low end explicitly.


def wrap_deg_360(value: float) -> Deg:
    """Wrap an angle into **[0, 360)** degrees.

    Use for azimuth, right ascension in degrees, and anything else measured
    round the full circle from a zero point.
    """
    wrapped = value % 360.0
    if wrapped >= 360.0:  # only reachable via float rounding of a tiny negative
        return Deg(0.0)
    return Deg(wrapped)


def wrap_deg_180(value: float) -> Deg:
    """Wrap an angle into **(-180, +180]** degrees.

    Use for position angle, parallactic angle and hour angle: the interval is
    closed at +180 so that a due-south hour angle of exactly 180 deg stays
    positive rather than flipping sign on a rounding error.
    """
    if -180.0 < value <= 180.0:
        # Exact identity inside the interval. Without this, ``180 - ((180 - x) %
        # 360)`` perturbs the last bit or two, and a value that is wrapped on
        # every sample of a session would drift.
        return Deg(value)
    return Deg(180.0 - ((180.0 - value) % 360.0))


def wrap_hours(value: float) -> Hours:
    """Wrap an angle into **[0, 24)** hours — the RA convention at the JSON boundary."""
    wrapped = value % 24.0
    if wrapped >= 24.0:  # see wrap_deg_360
        return Hours(0.0)
    return Hours(wrapped)


def signed_delta_deg(from_deg: float, to_deg: float) -> Deg:
    """Shortest signed rotation from ``from_deg`` to ``to_deg``, in **(-180, +180]**.

    This is the primitive used to unwrap a sampled position-angle series: the
    session's total rotation is the sum of consecutive signed deltas, which is
    correct even when the series crosses the wrap point several times.
    """
    return wrap_deg_180(to_deg - from_deg)
