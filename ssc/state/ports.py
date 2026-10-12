# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The seam between application state and whatever is actually driving a scope.

Nothing above the Alpaca seam may import ``ssc.seestar`` (CLAUDE.md invariant 1),
so the device layer is reached through these two protocols and the composition
root in :mod:`ssc.main` supplies the implementation.

* :class:`DeviceGateway` is the outbound half — discover, probe, connect.
* :class:`DeviceSink` is the inbound half. It exists because
  ``SeestarSession.subscribe_state`` / ``subscribe_events`` take plain callables:
  the adapter converts the device's own types into
  :mod:`ssc.state.models` types and calls these methods, which is how telemetry
  reaches the snapshot without the protocol reaching this package.

Both are ``Protocol`` classes, so a test substitutes a fake gateway without
touching a socket and without importing the device layer either.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ssc.state.models import (
    AlpacaDevicesState,
    AutofocusState,
    CameraFieldOfView,
    DeviceDescriptor,
    DeviceEventRecord,
    LinkState,
    MountAxis,
    MountState,
    MountTrackingRate,
)

#: ``(completed, total)`` — reported after each host during a scan.
ProgressCallback = Callable[[int, int], None]


class DeviceGatewayError(Exception):
    """The gateway was asked to do something it must not or cannot do.

    An unreachable host is *not* this: that is a result, carried in
    :attr:`~ssc.state.models.DeviceDescriptor.note` with ``reachable=False``.
    """


class MountRefusedError(DeviceGatewayError):
    """The device (or the lack of a live link) refused the command outright —
    e.g. ``Park`` below the horizon (``[1279]``, DESIGN.md §4.4b). Carries the
    device's own message verbatim, and its Alpaca ``ErrorNumber`` when the
    refusal came from the device rather than from "not connected"
    (CLAUDE.md invariant 7 — the reason reaches the caller in the device's own
    words, never swallowed or retried on its behalf).
    """

    def __init__(self, message: str, *, device_code: int | None = None) -> None:
        super().__init__(message)
        self.device_code = device_code


class MountNotAvailableError(DeviceGatewayError):
    """The member is not available on this device or this driver surface —
    ASCOM ``NotImplemented``/``ActionNotImplemented``, or a mapping the
    concurrent ``alpaca-driver`` work has not reached yet. Distinct from
    :class:`MountRefusedError`: this is "SSC cannot ask for this at all", not
    "the device was asked and said no".
    """


class FocuserRefusedError(DeviceGatewayError):
    """The focuser (or the lack of a live link to it) refused the command
    outright — e.g. ``Move`` while not connected. Carries the device's own
    message verbatim, and its Alpaca ``ErrorNumber`` when the refusal came
    from the device rather than from "not connected" (CLAUDE.md invariant 7 —
    the reason reaches the caller in the device's own words, never swallowed
    or retried on its behalf). Mirrors :class:`MountRefusedError`.
    """

    def __init__(self, message: str, *, device_code: int | None = None) -> None:
        super().__init__(message)
        self.device_code = device_code


class FocuserNotAvailableError(DeviceGatewayError):
    """The member is not available on this device or this driver surface —
    ASCOM ``NotImplemented``/``ActionNotImplemented``, or a mapping the
    concurrent ``alpaca-driver`` work has not reached yet. Distinct from
    :class:`FocuserRefusedError`: this is "SSC cannot ask for this at all",
    not "the device was asked and said no". Mirrors :class:`MountNotAvailableError`.
    """


class FilterWheelRefusedError(DeviceGatewayError):
    """The filter wheel refused a position change or is not connected."""

    def __init__(self, message: str, *, device_code: int | None = None) -> None:
        super().__init__(message)
        self.device_code = device_code


class FilterWheelNotAvailableError(DeviceGatewayError):
    """The filter-wheel member is not exposed by this device or driver."""


class HeaterRefusedError(DeviceGatewayError):
    """The dew-heater switch refused a state change or is not connected."""

    def __init__(self, message: str, *, device_code: int | None = None) -> None:
        super().__init__(message)
        self.device_code = device_code


class HeaterNotAvailableError(DeviceGatewayError):
    """The standard Alpaca dew-heater switch is not exposed by this device."""


@runtime_checkable
class DeviceSink(Protocol):
    """Where the device layer pushes what it learns."""

    def on_link(self, link: LinkState) -> None: ...

    def on_device(self, device: DeviceDescriptor | None) -> None: ...

    def on_device_event(self, event: DeviceEventRecord) -> None: ...

    def on_mount(self, mount: MountState | None) -> None: ...

    def on_devices(self, devices: AlpacaDevicesState | None) -> None: ...

    def on_camera_fov(self, fov: CameraFieldOfView | None) -> None: ...


@runtime_checkable
class DeviceGateway(Protocol):
    """Everything ``ssc-core`` asks of the layer below it."""

    @property
    def default_control_port(self) -> int:
        """The port to try when the caller did not name one.

        The value belongs to the protocol layer, so it is asked for rather than
        duplicated up here where it would drift.
        """
        ...

    async def probe(
        self,
        host: str,
        *,
        control_port: int,
        timeout: float,  # noqa: ASYNC109 - the caller's budget, passed to the transport
    ) -> DeviceDescriptor:
        """Test one address and report what is really there — never a green tick."""
        ...

    async def discover(
        self,
        *,
        timeout: float,  # noqa: ASYNC109 - the scan's overall deadline, not a wrapper
        on_progress: ProgressCallback | None = None,
    ) -> list[DeviceDescriptor]:
        """Scan this host's own RFC 1918 subnets. Bounded, and cancellable by the caller.

        Raises:
            DeviceGatewayError: the scan range is outside RFC 1918 or too large.
        """
        ...

    async def connect(self, host: str, *, control_port: int) -> None:
        """Open the device session. Progress is reported through the sink."""
        ...

    async def disconnect(self) -> None:
        """Close the device session. Idempotent."""
        ...

    async def mount_alignment(self) -> bool | None:
        """Whether the connected mount currently reports EQ alignment.

        ``True`` is EQ, ``False`` is alt-az, ``None`` when not connected or the
        device did not answer. Read live from the standard ASCOM
        ``Telescope.AlignmentMode`` member, on demand, rather than cached on
        :class:`~ssc.state.models.DeviceDescriptor` — mount mode can change
        without a reconnect, and the only caller (the Planning tab's framing
        endpoint, ``ssc/api/routes/astro.py``) needs a fresh answer to warn
        when a plan and the device disagree, never a guess (CLAUDE.md
        invariant 7: unknown stays ``None``).
        """
        ...

    async def mount_alignment_mode(self) -> int | None:
        """Live ``Telescope.AlignmentMode`` — ASCOM's raw ``AlignmentModes``
        code (0 ``algAltAz``, 1 ``algPolar``, 2 ``algGermanPolar``), read on
        demand and never cached, since a polar-alignment run needs to know
        this *before* committing several minutes of unattended slewing to
        it — a stale cached value from a mount that changed mode between
        connects would be exactly the silent degradation CLAUDE.md invariant
        7 forbids. Distinct from :meth:`mount_alignment`, which collapses
        the Seestar's own ``mount.equ_mode`` into a bool for the Planning
        tab's boolean view of the same standard ASCOM member (invariant 2):
        this method exposes its raw value, the one ``ssc.astro.polar``'s polar-alignment
        refusal is defined against. ``None`` when not connected or the member could not be read
        (invariant 7: unknown, never guessed).
        """
        ...

    def attach(self, sink: DeviceSink) -> Callable[[], None]:
        """Register ``sink``; returns the unsubscribe callable."""
        ...

    async def set_site(
        self, *, latitude_deg: float, longitude_deg: float, elevation_m: float | None
    ) -> None:
        """Push the observing site onto the connected device's own
        ``SiteLatitude``/``SiteLongitude``/``SiteElevation``.

        This device has no independent location source (``get_user_location``
        returns 103), so the site it reports has to come from somewhere —
        SSC's own configured observing location — before it can be read back
        and used to build the observer a mount-frame conversion is referred
        to. ``ssc.astro.frames``' module docstring is explicit that the
        *mount's own* reported site, not SSC's configured one, is what that
        conversion must use; this is how the mount comes to have one to
        report at all. ``elevation_m=None`` leaves ``SiteElevation`` unset,
        which is fine — ``ssc.astro.frames.mount_site`` substitutes a named,
        bounded assumption for an unknown elevation.

        Best-effort: a failure is logged, never raised, so it cannot break a
        connect attempt otherwise going fine.
        """
        ...

    # -- mount control (DESIGN.md §4.4b) ------------------------------------- #
    #
    # Every one of these raises MountRefusedError (409) or MountNotAvailableError
    # (501) rather than returning a success/failure flag — architecture policy invariant 7:
    # a control that silently no-ops is worse than one that raises loudly.

    async def mount_slew(self, *, ra_hours: float, dec_deg: float) -> None:
        """``SlewToCoordinatesAsync``. Returns once accepted, not once arrived —
        poll ``MountState.slewing`` (or ``GET /api/state``) for completion."""
        ...

    async def mount_sync(self, *, ra_hours: float, dec_deg: float) -> None:
        """``SyncToCoordinates``."""
        ...

    async def mount_move_axis(self, *, axis: MountAxis, rate_deg_s: float) -> None:
        """``MoveAxis``. ``rate_deg_s=0`` stops that one axis — the jog-button
        release, not a special case."""
        ...

    async def mount_stop(self) -> None:
        """The panic control: ``AbortSlew`` and ``MoveAxis(rate=0)`` on both
        axes. Attempts every one of them even if an earlier one raises, since
        this must do everything it can rather than stop at the first failure."""
        ...

    async def mount_set_tracking(
        self, *, enabled: bool, rate: MountTrackingRate | None
    ) -> MountState:
        """Sets ``Tracking`` and, if given, ``TrackingRate``; returns a fresh
        read-back rather than the caller's own guess of the result."""
        ...

    async def mount_park(self) -> None:
        """``Park``. Refuses below the horizon (``[1279]``) rather than being
        retried or silently swallowed — recovering from that refusal (e.g. via
        ``FindHome``) is a policy decision for the caller, not this method."""
        ...

    async def mount_unpark(self) -> None:
        """``Unpark``. Permits motion; moves nothing by itself (DESIGN.md §4.4b)."""
        ...

    async def mount_find_home(self) -> None:
        """``FindHome``."""
        ...

    # -- focuser control (bead ssc-bkv.7) ------------------------------------- #
    #
    # Same shape as mount control above: a read returns ``None`` when unknown
    # rather than guessing (invariant 7), while a commanded action raises
    # FocuserRefusedError/FocuserNotAvailableError rather than a silent no-op.

    async def focuser_position(self) -> tuple[int | None, str | None]:
        """Live ``Focuser.Position`` and, when the position could not be
        read, the reason why. ``None`` when not connected or the device did
        not answer — never guessed (CLAUDE.md invariant 7), and read fresh on
        demand rather than cached, like :meth:`mount_alignment`. Mirrors
        :meth:`~ssc.main.AlpacaDeviceGateway._read_device_connected`'s own
        ``(value, note)`` shape, for the same reason: a client rendering
        "unknown" needs to be able to say *why*, not offer one fixed guess."""
        ...

    async def focuser_is_moving(self) -> tuple[bool | None, str | None]:
        """Live ``Focuser.IsMoving`` and, when unknown, the reason why.

        This is the device-backed activity signal used by the global footer;
        ``None`` remains distinct from idle rather than being guessed false.
        """
        ...

    async def focuser_step_size(self) -> tuple[int | None, str | None]:
        """Live ``Focuser.StepSize`` (IFocuserV3) and, when unknown, the
        reason why — same ``(value, note)`` shape as :meth:`focuser_position`,
        read independently of it rather than assumed to succeed or fail
        together. ``StepSize`` is the ASCOM-defined physical size of one
        position unit; a device that does not report it must answer
        ``(None, note)`` here, never a guessed distance (invariant 7). A
        caller wanting *some* increment for a UI +/- control when this is
        ``None`` should fall back to 1 — the smallest possible change to an
        integer ``Position`` — which is a fact of the ASCOM type, not a
        guess about physical step size."""
        ...

    async def focuser_move(self, *, position: int) -> None:
        """``Focuser.Move`` — IFocuserV3. A real move needs firmware 9.16+
        (DESIGN.md §4.4b lineage); this method does not detect or check the
        firmware version, it only issues the standard ASCOM member and lets
        the device itself refuse or accept it."""
        ...

    async def focuser_halt(self) -> None:
        """``Focuser.Halt``."""
        ...

    async def focuser_start_autofocus(
        self, *, step: int, fine_step: int, exposure_s: float, settle_s: float
    ) -> None:
        """Measure and apply SSC's focus curve over standard Camera/Focuser
        members. ``step`` and ``fine_step`` are coarse and fine focuser deltas;
        ``exposure_s`` applies to every sampled frame; ``settle_s`` is the
        optical delay and must be at least two seconds."""
        ...

    # -- filter-wheel control (bead ssc-bkv.6) ------------------------------- #

    async def filterwheel_get_names(self) -> tuple[list[str] | None, str | None]:
        """Live ``FilterWheel.Names`` plus the reason when unavailable."""
        ...

    async def filterwheel_get_position(self) -> tuple[int | None, str | None]:
        """Live ``FilterWheel.Position`` plus the reason when unavailable.

        ASCOM uses ``-1`` while the wheel is moving; callers must preserve it
        rather than treating it as an invalid slot or guessing the target.
        """
        ...

    async def focuser_autofocus_state(self) -> AutofocusState:
        """Live in-memory progress for SSC's current or latest autofocus run."""
        ...

    async def filterwheel_set_position(self, *, position: int) -> None:
        """Set ``FilterWheel.Position`` to a zero-based slot index."""
        ...

    async def heater_get_state(self) -> tuple[bool | None, str | None]:
        """Read the standard Alpaca ``Switch.GetSwitch(Id=0)`` dew heater."""
        ...

    async def heater_set_state(self, *, enabled: bool) -> None:
        """Set the standard Alpaca ``Switch.SetSwitch(Id=0)`` dew heater."""
        ...


__all__ = [
    "DeviceGateway",
    "DeviceGatewayError",
    "DeviceSink",
    "FilterWheelNotAvailableError",
    "FilterWheelRefusedError",
    "FocuserNotAvailableError",
    "FocuserRefusedError",
    "HeaterNotAvailableError",
    "HeaterRefusedError",
    "MountNotAvailableError",
    "MountRefusedError",
    "ProgressCallback",
]
