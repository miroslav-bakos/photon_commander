# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Which location wins, and why — written down rather than implied.

DESIGN.md §5 lists four sources and says the manual override always wins. The
other three are ordered by *how good the answer actually is*, not by how the doc
happens to enumerate them:

1. ``MANUAL``  — the user typed it. Always wins; nothing overrides a decision.
2. ``BROWSER`` — Geolocation API, metre accuracy, needs a user gesture.
3. ``DEVICE``  — the scope's own ``pi_station_state`` / ``pi_get_time``. Real,
   implemented, and better than a city centroid (``docs/M0-FINDINGS.md``).
4. ``IP``      — city-level at best, and wrong behind a VPN.

``preferred_source`` overrides the whole order, for the case where the user
knows the browser fix is the stale one.
"""

from __future__ import annotations

from typing import Final

from ssc.settings.models import LocationSettings
from ssc.state.models import LocationSource, LocationState

#: Best answer first. Changing this changes twilight times; change it deliberately.
PRECEDENCE: Final[tuple[LocationSource, ...]] = (
    LocationSource.MANUAL,
    LocationSource.BROWSER,
    LocationSource.DEVICE,
    LocationSource.IP,
)


def resolve(settings: LocationSettings) -> LocationState | None:
    """The active location, or ``None`` when no source has ever answered."""
    order: tuple[LocationSource, ...] = PRECEDENCE
    preferred = settings.preferred_source
    if preferred is not None:
        order = (preferred, *(source for source in PRECEDENCE if source != preferred))

    for source in order:
        fix = settings.fixes.get(source)
        if fix is None:
            continue
        return LocationState(
            latitude_deg=fix.latitude_deg,
            longitude_deg=fix.longitude_deg,
            elevation_m=fix.elevation_m,
            timezone=fix.timezone,
            source=source,
            accuracy_m=fix.accuracy_m,
            acquired_at=fix.acquired_at,
        )
    return None


__all__ = ["PRECEDENCE", "resolve"]
