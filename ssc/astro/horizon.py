# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The per-azimuth altitude mask: trees, roofs and the neighbour's floodlight.

``docs/DESIGN.md`` 5 calls the horizon profile "under-appreciated and very high
value for a backyard scope", and 3.2 draws it on the sky map. It is a different
thing from :func:`ssc.astro.events.horizon_dip_deg`, which is one scalar about
the *geometric* horizon at a given elevation. This module is about the horizon
the observer actually has.

Conventions, stated once:

* **Azimuth is 0 at due north and increases eastward**, wrapped to ``[0, 360)``
  — the same convention :func:`ssc.astro.events.target_position` reports.
* **Altitude is geometric**, degrees above the astronomical horizon, in
  ``[-90, 90]``. No refraction is applied: a treeline is an object in the way,
  not a horizon definition.
* **Between two points the mask is linearly interpolated in altitude against
  azimuth.** That is what a user dragging a skyline expects to get, and it is
  stated here rather than left implicit because everything downstream — the
  visibility solver, the sky-map overlay — has to agree on it.
* **The profile is cyclic.** The segment from the last point to the first runs
  *eastward through north*, so a profile whose points straddle 0 interpolates
  the short way across the seam and not backwards through south.

This module is pure geometry: no ephemeris, no I/O, no clock. The horizon-aware
visibility solver that pairs a profile with a target lives in
:func:`ssc.astro.events.horizon_visibility`, which is where the ephemeris is.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from itertools import pairwise

from ssc.astro.units import Deg, wrap_deg_360


@dataclass(frozen=True, slots=True)
class HorizonPoint:
    """One vertex of the skyline: an altitude at an azimuth.

    ``azimuth_deg`` is normalised into ``[0, 360)`` on construction, so 370 and
    10 are the same vertex and are caught as duplicates by
    :class:`HorizonProfile`. ``altitude_deg`` is left exactly as given.
    """

    azimuth_deg: Deg
    altitude_deg: Deg

    def __post_init__(self) -> None:
        if not math.isfinite(self.azimuth_deg):
            raise ValueError(f"azimuth must be finite, got {self.azimuth_deg}")
        if not math.isfinite(self.altitude_deg):
            raise ValueError(f"altitude must be finite, got {self.altitude_deg}")
        if not -90.0 <= self.altitude_deg <= 90.0:
            raise ValueError(f"altitude out of range: {self.altitude_deg}")
        object.__setattr__(self, "azimuth_deg", wrap_deg_360(float(self.azimuth_deg)))


@dataclass(frozen=True, slots=True)
class HorizonProfile:
    """A closed, cyclic, piecewise-linear altitude mask.

    ``points`` is stored sorted by azimuth ascending, which is the canonical
    form: the order a caller supplies carries no information, because the
    profile is a set of samples round a circle.

    Two things are construction errors rather than something to paper over
    (invariant 7):

    * **fewer than two points** — one point does not define a mask, and zero
      points is a missing profile, which is not the same as a flat one. Use
      :func:`flat_horizon` if a constant altitude is what is meant.
    * **duplicate azimuths** — two altitudes at the same bearing is a
      contradiction the module refuses to resolve by picking one.

    Two points at *nearly* the same azimuth are fine and deliberate: that is how
    a chimney or a mast is drawn, and the resulting near-vertical segment is
    interpolated exactly as steeply as it was drawn.
    """

    points: tuple[HorizonPoint, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError(
                f"a horizon profile needs at least 2 points, got {len(self.points)}; "
                "use flat_horizon() for a constant altitude"
            )
        ordered = tuple(sorted(self.points, key=lambda point: float(point.azimuth_deg)))
        for previous, current in pairwise(ordered):
            if float(previous.azimuth_deg) == float(current.azimuth_deg):
                raise ValueError(
                    f"duplicate azimuth {current.azimuth_deg} deg in horizon profile: "
                    f"altitudes {previous.altitude_deg} and {current.altitude_deg}"
                )
        object.__setattr__(self, "points", ordered)

    @property
    def azimuths_deg(self) -> tuple[Deg, ...]:
        """The vertex azimuths, ascending. These are the mask's only kinks, and so the
        only places its altitude can have a local minimum — which is what makes the
        visibility solver's sampling guarantee possible."""
        return tuple(point.azimuth_deg for point in self.points)

    def altitude_at(self, azimuth_deg: Deg) -> Deg:
        """Mask altitude at one bearing, by linear interpolation between neighbours.

        The azimuth is wrapped to ``[0, 360)`` first, so any real number is a
        valid argument. Interpolation always runs eastward from the point at or
        before the bearing to the next one round the circle, which means the
        segment spanning the seam (from, say, 350 deg to 10 deg) is traversed
        through north over 20 deg, never backwards through south over 340.

        Continuous everywhere, including at 0 and at every vertex.
        """
        azimuth = float(wrap_deg_360(float(azimuth_deg)))
        points = self.points
        index = bisect_right(points, azimuth, key=lambda point: float(point.azimuth_deg))
        if 1 <= index <= len(points) - 1:
            start, end = points[index - 1], points[index]
        else:
            # Before the first vertex or at/after the last: the seam segment.
            start, end = points[-1], points[0]

        span = (float(end.azimuth_deg) - float(start.azimuth_deg)) % 360.0
        if span == 0.0:  # pragma: no cover - duplicates are rejected in __post_init__
            return start.altitude_deg
        offset = (azimuth - float(start.azimuth_deg)) % 360.0
        fraction = offset / span
        return Deg(
            float(start.altitude_deg)
            + fraction * (float(end.altitude_deg) - float(start.altitude_deg))
        )


def flat_horizon(altitude_deg: Deg) -> HorizonProfile:
    """A profile that is the same altitude at every bearing.

    This exists so that the ordinary "just a minimum altitude" case and a full
    skyline mask are the *same* code path downstream — the altitude-limit ring
    the sky map draws (DESIGN 3.2) is a flat profile, not a special case. Two
    vertices are enough to pin a constant round the circle.
    """
    return HorizonProfile(
        points=(
            HorizonPoint(azimuth_deg=Deg(0.0), altitude_deg=altitude_deg),
            HorizonPoint(azimuth_deg=Deg(180.0), altitude_deg=altitude_deg),
        )
    )


def is_above_horizon(profile: HorizonProfile, *, altitude_deg: Deg, azimuth_deg: Deg) -> bool:
    """Is a body at this altitude/azimuth clear of the mask?

    Strict: a body exactly on the skyline is *not* above it. The roofline is
    opaque, and the tie only ever happens at the instant of a crossing.
    """
    return float(altitude_deg) > float(profile.altitude_at(azimuth_deg))


__all__ = [
    "HorizonPoint",
    "HorizonProfile",
    "flat_horizon",
    "is_above_horizon",
]
