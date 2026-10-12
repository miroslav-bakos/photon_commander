# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Artificial sky for rehearsing PA without calling a camera or mount.

The generator uses a physical RA rotation in the local horizontal frame and
Skyfield's reverse coordinate path. The production PA fit then receives J2000
plate centres and timestamps exactly as it does for real solves. All inputs are
explicit; this module has no device I/O or wall-clock read.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from ssc.astro.events import Ephemeris, Site
from ssc.astro.frames import EquatorialCoord, MountFrame, to_j2000
from ssc.astro.polar import PolarAlignmentModel, PolarPoint, solve_polar_alignment
from ssc.astro.units import Deg


def _unit(altitude_deg: float, azimuth_deg: float) -> tuple[float, float, float]:
    alt, az = math.radians(altitude_deg), math.radians(azimuth_deg)
    return math.cos(alt) * math.cos(az), math.cos(alt) * math.sin(az), math.sin(alt)


def _rotate(axis: tuple[float, float, float], degrees: float, vector: tuple[float, float, float]) -> tuple[float, float, float]:
    angle = math.radians(degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    dot = sum(a * b for a, b in zip(axis, vector, strict=True))
    cross = (
        axis[1] * vector[2] - axis[2] * vector[1],
        axis[2] * vector[0] - axis[0] * vector[2],
        axis[0] * vector[1] - axis[1] * vector[0],
    )
    return tuple(vector[i] * cosine + cross[i] * sine + axis[i] * dot * (1.0 - cosine) for i in range(3))


def simulate_polar_alignment(
    ephemeris: Ephemeris,
    site: Site,
    when: datetime,
    *,
    altitude_adjustment_deg: float,
    azimuth_adjustment_deg: float,
    point_count: int,
) -> tuple[tuple[PolarPoint, ...], PolarAlignmentModel]:
    """Expose and solve a deterministic 60-degree RA arc from a moved wedge.

    Initial error is +1 degree in altitude, -1 degree in azimuth. A positive
    altitude control raises the axis; a positive azimuth control turns it east.
    This is an isolated rehearsal, so no real telescope or site reading is used.
    """
    if not 3 <= point_count <= 12:
        raise ValueError("point_count must be between 3 and 12")
    if not -15.0 <= altitude_adjustment_deg <= 15.0:
        raise ValueError("altitude adjustment must be within 15 degrees")
    if not -15.0 <= azimuth_adjustment_deg <= 15.0:
        raise ValueError("azimuth adjustment must be within 15 degrees")
    axis = _unit(float(site.latitude_deg) + 1.0 + altitude_adjustment_deg, -1.0 + azimuth_adjustment_deg)
    start = _unit(65.0, 180.0)
    observer: Any = ephemeris.observer(site)
    points: list[PolarPoint] = []
    for index in range(point_count):
        angle = -30.0 + index * 60.0 / (point_count - 1)
        direction = _rotate(axis, angle, start)
        altitude = math.degrees(math.atan2(direction[2], math.hypot(direction[0], direction[1])))
        azimuth = math.degrees(math.atan2(direction[1], direction[0])) % 360.0
        # Repeatable sub-arcsecond plate-centre scatter makes the five-point
        # residual informative while leaving the axis fit easy to inspect.
        noise_arcsec = (-0.6, 0.2, 0.5, -0.3, 0.1)[index % 5]
        altitude += noise_arcsec / 3600.0
        azimuth += noise_arcsec / (3600.0 * math.cos(math.radians(altitude)))
        instant = when + timedelta(seconds=15 * index)
        time: Any = ephemeris.timescale.from_datetime(instant)
        apparent: Any = observer.at(time).from_altaz(alt_degrees=altitude, az_degrees=azimuth)
        ra, dec, _ = apparent.radec(epoch=time)
        j2000 = to_j2000(
            ephemeris,
            site,
            EquatorialCoord(
                ra_deg=Deg(float(ra.hours) * 15.0),
                dec_deg=Deg(float(dec.degrees)),
                frame=MountFrame.TOPOCENTRIC,
            ),
            instant,
        )
        points.append(PolarPoint(coord=j2000, when=instant))
    model = solve_polar_alignment(ephemeris, site, points, apply_refraction=False)
    return tuple(points), model
