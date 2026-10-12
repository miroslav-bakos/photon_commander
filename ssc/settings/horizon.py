# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Turning the stored skyline mask into the geometry type the astro core uses.

Mirrors :mod:`ssc.settings.location`: the settings store holds plain,
JSON-serialisable data (a list of azimuth/altitude pairs, or ``None`` when
nothing has been recorded), and this is the one place that turns it into
:class:`ssc.astro.horizon.HorizonProfile` — so the API layer and the astro
service agree on what "the profile" means without duplicating the conversion.
"""

from __future__ import annotations

from ssc.astro.horizon import HorizonPoint, HorizonProfile
from ssc.astro.units import Deg
from ssc.settings.models import HorizonSettings


def resolve(settings: HorizonSettings) -> HorizonProfile | None:
    """The active horizon profile, or ``None`` when the site has none recorded."""
    if settings.points is None:
        return None
    return HorizonProfile(
        points=tuple(
            HorizonPoint(azimuth_deg=Deg(point.azimuth_deg), altitude_deg=Deg(point.altitude_deg))
            for point in settings.points
        )
    )


__all__ = ["resolve"]
