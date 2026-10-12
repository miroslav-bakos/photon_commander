# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""A J2000 slew/sync target -> the mount's own declared frame, in one place.

Two callers need exactly this conversion and neither may skip it: the Mount
tab's own control path (:class:`~ssc.api.services.DeviceService`) and the
scheduler's device port (:class:`~ssc.api.device_commands.AlpacaDeviceCommands`,
the executor's own route to the driver — a separate client of the same
``DeviceService``-established pattern, per its own module docstring). Both go
through ``ssc.astro.frames`` for the actual math; this module is only the
orchestration around it — reading the mount's declared frame, its own
reported site, and the loaded ephemeris, and refusing rather than guessing
when any of the three is not yet known — so that orchestration exists once,
not twice with the two copies free to drift apart.

See ``ssc/astro/frames.py``'s module docstring for why the outbound direction
matters most: a slew unconverted lands ~20' off and is recoverable, a sync
unconverted writes that error into the mount's pointing model, where it is
durable.
"""

from __future__ import annotations

from datetime import datetime

from ssc.astro.events import Ephemeris
from ssc.astro.frames import (
    MountFrame,
    UnsupportedMountFrameError,
    j2000_coord,
    mount_site,
    to_mount_frame,
)
from ssc.astro.units import Deg, Hours, hours_to_deg
from ssc.state.models import MountState

__all__ = ["MountFrameUnavailableError", "resolve_mount_target"]


class MountFrameUnavailableError(Exception):
    """The mount's frame, its own site, or the ephemeris kernel is not yet
    known, or the mount declares a frame ``ssc.astro.frames`` does not
    convert (B1950/J2050/other).

    Each caller translates this into its own vocabulary: the interactive API
    raises :class:`~ssc.state.ports.MountRefusedError` (a 409), the
    scheduler's device port raises
    :class:`~ssc.scheduler.executor.DeviceCommandError` (the item fails with
    this reason). Neither guesses — CLAUDE.md invariant 7, and a wrong sync
    is durable.
    """


def resolve_mount_target(
    *,
    mount: MountState | None,
    ephemeris: Ephemeris | None,
    ephemeris_error: str | None,
    ra_hours: float,
    dec_deg: float,
    when: datetime,
) -> tuple[float, float]:
    """A J2000 ``(ra_hours, dec_deg)`` target -> the frame the connected mount
    declares through its ``EquatorialSystem``.

    A mount already declaring J2000 is passed through unconverted. ``when``
    is the instant the coordinates are *for* — the caller's own "now", since
    neither the Mount tab nor the scheduler's device port is a pure solver
    (CLAUDE.md invariant 3 binds ``ssc/astro``, ``ssc/plan``, ``ssc/scheduler``,
    not this adapter layer).
    """
    if mount is None or mount.equatorial_system is None:
        raise MountFrameUnavailableError(
            "the mount's equatorial frame (EquatorialSystem) is not known yet; "
            "refusing to guess whether this target needs converting"
        )
    if mount.equatorial_system is MountFrame.J2000:
        return ra_hours, dec_deg
    if ephemeris is None:
        raise MountFrameUnavailableError(
            f"cannot convert to the mount's own {mount.equatorial_system.value!r} frame: "
            f"{ephemeris_error or 'the ephemeris kernel is not loaded'}"
        )
    if mount.site_latitude_deg is None or mount.site_longitude_deg is None:
        raise MountFrameUnavailableError(
            "the mount has not yet reported its own SiteLatitude/SiteLongitude; "
            "refusing to guess the observer its frame is referred to"
        )
    site = mount_site(Deg(mount.site_latitude_deg), Deg(mount.site_longitude_deg))
    target = j2000_coord(hours_to_deg(Hours(ra_hours)), Deg(dec_deg))
    try:
        converted = to_mount_frame(ephemeris, site, target, when, mount.equatorial_system)
    except UnsupportedMountFrameError as exc:
        raise MountFrameUnavailableError(str(exc)) from exc
    return float(converted.ra_h), float(converted.dec_deg)
