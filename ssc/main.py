# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``ssc-core``: the FastAPI app, its lifespan, and the composition root.

This is the one module that knows how everything fits together. It never
touches ``ssc.seestar`` — the wire protocol is contained entirely inside
``ssc.alpaca`` (CLAUDE.md invariant 1) — and it does not take a shortcut past
the driver to reach the device either (invariant 2): ``ssc-core`` both *hosts*
its own ``ssc-alpaca`` driver, in-process on loopback (DESIGN.md §1, §2.1), and
talks to it as a *client* of that same HTTP surface a third-party Alpaca client
(a compatible Alpaca client) would use. :class:`AlpacaDeviceGateway` is the
:class:`~ssc.state.ports.DeviceGateway` behind that seam.

Startup order matters and is deliberate:

1. **Refuse a bad bind before anything else happens.** Binding telescope control
   to a public interface is not a warning, it is an exit (DESIGN.md §9).
2. Open the database, apply migrations, load settings — a settings section that
   will not parse stops the process rather than resetting the user's location.
3. Host ``ssc-alpaca`` and confirm it is actually listening — the client built
   on top of it must never be handed an address nothing answers on yet.
4. Build the state snapshot *from* what was loaded, so the first
   ``GET /api/state`` is already true.
5. Advertise ``_ssc._tcp`` last, once the service can actually answer.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import logging
import math
import re
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from ssc import net as _net
from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.server import DEFAULT_ALPACA_PORT
from ssc.alpaca.server import ServerConfig as AlpacaServerConfig
from ssc.alpaca.server import create_app as create_alpaca_app
from ssc.api import api_router
from ssc.api.astro_service import build_astro_service
from ssc.api.autofocus_service import (
    AutofocusAlreadyRunningError,
    AutofocusConditionsError,
    AutofocusError,
    AutofocusRestoreError,
    AutofocusService,
)
from ssc.api.browser_origin import BrowserOriginMiddleware, TransportProxyMiddleware
from ssc.api.capture_orchestrator import (
    CaptureDitherProbeCleanupError,
    CaptureService,
    initial_capture_state,
)
from ssc.api.deps import Services
from ssc.api.device_commands import AlpacaCapturePort, AlpacaDeviceCommands, AlpacaDeviceEvents
from ssc.api.dual_capture import dual_motion_guard
from ssc.api.heater_automation import HeaterAutomationService
from ssc.api.operation_service import OperationService
from ssc.api.plans_service import PlanTimezoneUnknownError, current_night_date, resolve_timezone
from ssc.api.polar_alignment_service import PolarAlignmentService, restored_night_state
from ssc.api.run_service import RunService
from ssc.api.scheduled_focus import adaptive_autofocus
from ssc.api.security import AuthService, RateLimiter
from ssc.api.services import DeviceService
from ssc.api.target_centering_service import TargetCenteringService
from ssc.api.tasks import TaskRegistry
from ssc.api.weather_service import build_weather_service, resolve_metno_contact
from ssc.api.ws import frame_schemas
from ssc.astro.frames import MountFrame, mount_frame_from_ascom
from ssc.astro.framing import Profile
from ssc.capture import reconcile_incomplete
from ssc.client.alpaca import DEFAULT_BASE_URL, AlpacaClient, AlpacaDeviceClient
from ssc.client.errors import AlpacaClientError, AlpacaErrorNumber, AlpacaRemoteError
from ssc.client.models import DiscoveredDevice, ProbeResult
from ssc.objectinfo import ObjectInfoService
from ssc.platesolve import PlateSolveResult
from ssc.scheduler.executor import (
    CapturePort,
    DeviceCommandError,
    DeviceCommandNotImplementedError,
    DeviceFocusConditionsError,
)
from ssc.settings.config import MDNS_SERVICE_TYPE, AppConfig
from ssc.settings.secrets import SecretStore, probe_backend
from ssc.settings.store import SettingsStore
from ssc.settings.tls import (
    CA_RENEWAL_WARNING_WINDOW,
    TlsConfigError,
    TlsMaterial,
    ensure_tls_material,
)
from ssc.skymap import (
    BulkCutoutService,
    CutoutService,
    ExportImportService,
    SkymapService,
    build_user_agent,
)
from ssc.skymap.cache import TileCache
from ssc.state.events import NoticeCleared, NoticeRaised
from ssc.state.models import (
    AlpacaDeviceItem,
    AlpacaDeviceKind,
    AlpacaDevicesState,
    AutoConnectState,
    AutofocusState,
    BatteryTelemetry,
    CameraFieldOfView,
    DeviceDescriptor,
    LinkState,
    LinkStatus,
    MountAxis,
    MountCapabilities,
    MountState,
    MountTrackingRate,
    Notice,
    NoticeLevel,
    PolarAlignmentState,
    SecretsCapability,
    StateSnapshot,
    devices_stale,
    mount_stale,
)
from ssc.state.ports import (
    DeviceGateway,
    DeviceGatewayError,
    DeviceSink,
    FilterWheelNotAvailableError,
    FilterWheelRefusedError,
    FocuserNotAvailableError,
    FocuserRefusedError,
    HeaterNotAvailableError,
    HeaterRefusedError,
    MountNotAvailableError,
    MountRefusedError,
    ProgressCallback,
)
from ssc.state.store import StateStore
from ssc.store.autofocus import AutofocusRepository
from ssc.store.autofocus import restored_state as restored_autofocus_state
from ssc.store.capture import CaptureRepository
from ssc.store.database import Database, StoreError, open_database
from ssc.store.migrations import migrate
from ssc.store.plans import PlanRepository
from ssc.store.polar_alignment import PolarAlignmentRepository
from ssc.store.runs import RunRepository, ensure_durable_commits

_log = logging.getLogger("ssc")

#: The three RFC 1918 blocks. Loopback is allowed separately; everything else is
#: refused except Tailscale's RFC 6598 shared address space below.
#: ``IPv4Network.is_private`` is a different, wider test.
RFC1918_BLOCKS: Final[tuple[ipaddress.IPv4Network, ...]] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)

#: RFC 6598 shared address space. Tailscale allocates node addresses from this
#: block; it is not publicly routed, and accepting it keeps the wildcard LAN
#: listener usable when the Pi joins a tailnet. Public IPv4 addresses remain
#: fatal. See ``remote_access.py`` for the separate Serve/HTTPS policy.
SHARED_ADDRESS_SPACE: Final = ipaddress.IPv4Network("100.64.0.0/10")

#: ``0.0.0.0`` means "every interface", so every interface has to pass the test.
WILDCARD_HOSTS: Final = frozenset({"0.0.0.0", "::"})


class BindRefusedError(Exception):
    """The requested bind address would expose telescope control. Fatal."""


# --------------------------------------------------------------------------- #
# LAN security: the bind guard
# --------------------------------------------------------------------------- #


def local_ipv4_addresses() -> list[str]:
    """Every IPv4 address actually bound to one of this host's interfaces.

    Delegates to :mod:`ssc.net`; see there for why a routing guess is not good
    enough for the wildcard-bind safety check.
    """
    return _net.local_ipv4_addresses()


def _is_safe(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv6Address):
        return address.is_link_local or address.is_private
    return address in SHARED_ADDRESS_SPACE or any(address in block for block in RFC1918_BLOCKS)


def _is_loopback_host(host: str) -> bool:
    """Strictly loopback — unlike :func:`_is_safe`, ``0.0.0.0``/``::`` and any
    RFC 1918 address are both *not* loopback here, because both mean "another
    device on the LAN can reach this", which is exactly the distinction the
    no-auth startup warning needs."""
    if host in WILDCARD_HOSTS:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind_address(host: str, *, interfaces: Sequence[str] | None = None) -> None:
    """Raise unless every bound address is loopback, RFC 1918, or RFC 6598.

    ``0.0.0.0`` is legitimate — it is how the phone reaches the service — but only
    on a machine whose own addresses are all private. A host with a public IP
    binding the wildcard would put mount control on the internet, so it is
    refused with an explanation rather than started with a warning nobody reads.
    """
    if host in WILDCARD_HOSTS:
        found = list(interfaces) if interfaces is not None else local_ipv4_addresses()
        public = [candidate for candidate in found if not _is_safe(ipaddress.ip_address(candidate))]
        if public:
            raise BindRefusedError(
                f"refusing to bind {host!r}: this machine has publicly routable address(es) "
                f"{', '.join(public)}, so the wildcard bind would expose telescope "
                "control beyond the LAN. Bind a specific private address instead "
                "(SSC_HOST=192.168.x.y), or 127.0.0.1 for local-only use."
            )
        return

    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise BindRefusedError(
            f"refusing to bind {host!r}: it is not an IP address. SSC binds an "
            "address, not a name, so that what it is exposing is unambiguous."
        ) from exc

    if not _is_safe(address):
        raise BindRefusedError(
            f"refusing to bind {host!r}: it is not a loopback, RFC 1918, or RFC 6598 address. "
            "SSC is a LAN tool (DESIGN.md §9); putting telescope control on a "
            "public interface needs a different design, not a different flag."
        )


# --------------------------------------------------------------------------- #
# Hosting ssc-alpaca: ssc-core is both server (:7777) and, of its own driver,
# client (:5555) — DESIGN.md §1/§2.1 draws them as separate boxes on the same
# machine; hosting the driver from this lifespan instead of a second process
# keeps deployment single-command while the seam stays real HTTP end to end.
# --------------------------------------------------------------------------- #

#: How long to wait for uvicorn to confirm ssc-alpaca is actually listening
#: before treating the failure as fatal (architecture policy invariant 7 — no silent
#: fallback to a shortcut past the driver).
_ALPACA_STARTUP_TIMEOUT_S: Final = 10.0

#: Comfortably above the largest caller-configurable discovery timeout
#: (``AppConfig.discovery_timeout_s``, capped at 120s) so a slow scan is never
#: cut off client-side while ssc-alpaca is still legitimately working.
_ALPACA_CLIENT_TIMEOUT_S: Final = 130.0


class AlpacaHostingError(Exception):
    """ssc-alpaca did not start. Fatal — there is no shortcut past it to fall back to."""


async def _stop_dither_probe_camera(camera: AlpacaDeviceClient) -> None:
    """Confirm interrupted diagnostic acquisition is idle before handoff."""
    async with asyncio.timeout(5):
        try:
            await camera.put("abortexposure")
        except AlpacaRemoteError as exc:
            if exc.number not in _MOUNT_NOT_IMPLEMENTED_NUMBERS:
                raise
            camera_state = await camera.get("camerastate")
            if isinstance(camera_state, int) and not isinstance(camera_state, bool) and camera_state == 0:
                return
            await camera.put("stopexposure")
        # Native Alpaca exposes polled CameraState, not an idle event.
        while True:
            camera_state = await camera.get("camerastate")
            if isinstance(camera_state, bool) or not isinstance(camera_state, int):
                raise RuntimeError(f"invalid CameraState during dither cleanup: {camera_state!r}")
            if camera_state == 0:
                return
            await asyncio.sleep(0.1)


async def _serve(server: uvicorn.Server) -> None:
    """Run ``server.serve()``, converting a startup failure into a plain exception.

    ``uvicorn.Server.startup()`` answers a bind failure (address in use, say)
    with ``sys.exit()``, i.e. ``SystemExit``. asyncio's task machinery
    re-raises ``SystemExit``/``KeyboardInterrupt`` out of the event loop rather
    than storing them for a later ``task.exception()``, which would surface as
    an unhandled, unretrieved exception instead of the loud, specific
    :class:`AlpacaHostingError` :meth:`AlpacaRuntime.start` is meant to raise.
    Catching it here, inside the task's own coroutine, keeps it a normal,
    retrievable exception. ``CancelledError`` is deliberately not caught: a
    genuine cancellation (the startup deadline, or :meth:`AlpacaRuntime.aclose`)
    must still leave the task looking cancelled.
    """
    try:
        await server.serve()
    except (SystemExit, KeyboardInterrupt) as exc:
        raise RuntimeError(f"uvicorn.Server.serve() did not start: {exc}") from exc


class AlpacaRuntime:
    """Hosts ``ssc-alpaca`` in-process on loopback, and owns the client of it.

    ``SeestarBackend()`` starts knowing neither host nor port (CLAUDE.md
    invariant 11): nothing here ever supplies a device address, only the fixed,
    non-secret loopback port ``ssc-alpaca`` itself always listens on.

    ``port`` defaults to the real driver port; tests pass ``0`` for an
    OS-assigned ephemeral one so the suite never fights a real ``ssc-alpaca``
    for :data:`~ssc.alpaca.server.DEFAULT_ALPACA_PORT`.
    """

    def __init__(self, *, port: int = DEFAULT_ALPACA_PORT) -> None:
        self.backend = SeestarBackend()
        self._port = port
        base_url = DEFAULT_BASE_URL if port == DEFAULT_ALPACA_PORT else f"http://127.0.0.1:{port}"
        self.client = AlpacaClient(base_url, request_timeout_s=_ALPACA_CLIENT_TIMEOUT_S)
        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None

    @property
    def port(self) -> int:
        """The port actually bound, once :meth:`start` has returned."""
        server = self._server
        if server is None or not server.servers:
            return self._port
        sockets = server.servers[0].sockets
        return self._port if not sockets else int(sockets[0].getsockname()[1])

    async def start(self) -> None:
        app = create_alpaca_app(self.backend, config=AlpacaServerConfig())
        # The outer server owns process logging. Reconfiguring it here closes
        # the application's active diagnostic writer during embedded startup.
        config = uvicorn.Config(app, host="127.0.0.1", port=self._port,
                                log_level="warning", log_config=None)
        server = uvicorn.Server(config)
        self._server = server
        task = asyncio.create_task(_serve(server), name="ssc-alpaca")
        self._serve_task = task
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ALPACA_STARTUP_TIMEOUT_S
        while not server.started:
            if task.done():
                if task.cancelled():
                    raise AlpacaHostingError("ssc-alpaca was cancelled before it started listening")
                raise AlpacaHostingError(
                    f"ssc-alpaca failed to start on 127.0.0.1:{self._port}"
                ) from task.exception()
            if loop.time() > deadline:
                task.cancel()
                raise AlpacaHostingError(
                    "ssc-alpaca did not report listening within "
                    f"{_ALPACA_STARTUP_TIMEOUT_S}s on 127.0.0.1:{self._port}"
                )
            await asyncio.sleep(0.01)
        if self._port == 0:
            # An ephemeral bind: the client was built against port 0, which
            # is not a real address to connect to — rebuild it now that the OS
            # has actually assigned one. Only the port==0 test path takes
            # this; production always has a real, known port from the start.
            self.client = AlpacaClient(
                f"http://127.0.0.1:{self.port}", request_timeout_s=_ALPACA_CLIENT_TIMEOUT_S
            )
        _log.info("ssc-alpaca listening on 127.0.0.1:%d", self.port)

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.backend.aclose()
        server, self._server = self._server, None
        if server is not None:
            server.should_exit = True
        task, self._serve_task = self._serve_task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task


# --------------------------------------------------------------------------- #
# The device gateway: SSC's own UI reaches the scope only through ssc-alpaca
# (architecture policy invariant 2), over the same HTTP surface a third-party client
# would use.
# --------------------------------------------------------------------------- #

#: Link states worth reading the mount in. Mirrors ``mount_alignment``'s own
#: ``READY``-only gate, loosened to include ``DEGRADED`` — the control channel
#: is up either way (see ``_NOT_DISCONNECTED`` above), and a mount member that
#: cannot actually be read still comes back ``None`` per-field rather than
#: failing the whole tick.
_MOUNT_POLLABLE: Final = frozenset({LinkStatus.READY, LinkStatus.DEGRADED})

#: ASCOM's own axis numbering (DESIGN.md §4.4b: axis 2 is unsupported and not offered).
_AXIS_NUMBERS: Final[Mapping[MountAxis, int]] = {"primary": 0, "secondary": 1}

#: ``Telescope.TrackingRate``/``TrackingRates`` — exactly sidereal/lunar/solar
#: are ever seen on this device (DESIGN.md §4.4b: no King rate); any other
#: code is left out rather than guessed at.
_TRACKING_RATE_NAMES: Final[Mapping[int, MountTrackingRate]] = {
    0: "sidereal",
    1: "lunar",
    2: "solar",
}
_TRACKING_RATE_CODES: Final[Mapping[MountTrackingRate, int]] = {
    name: code for code, name in _TRACKING_RATE_NAMES.items()
}

_MOUNT_NOT_IMPLEMENTED_NUMBERS: Final[frozenset[int]] = frozenset(
    {int(AlpacaErrorNumber.NOT_IMPLEMENTED), int(AlpacaErrorNumber.ACTION_NOT_IMPLEMENTED)}
)


def _tracking_rate_name(code: int | None) -> MountTrackingRate | None:
    return None if code is None else _TRACKING_RATE_NAMES.get(code)


def _pixel_span_to_fov_deg(n_px: int, pixel_size_um: float, focal_length_mm: float) -> float:
    """The angle one sensor axis subtends on sky, from its pixel count, pixel
    pitch (microns) and the optic's focal length (millimetres) — the standard
    small-angle field formula, applied per axis so a non-square sensor gets
    two different answers rather than one derived from its diagonal."""
    span_mm = n_px * pixel_size_um * 1e-3
    return math.degrees(2.0 * math.atan(span_mm / (2.0 * focal_length_mm)))


def _equatorial_system_name(code: int | None) -> MountFrame | None:
    """``Telescope.EquatorialSystem``'s ASCOM enum -> :class:`MountFrame`, via
    ``ssc.astro.frames``' own table (CLAUDE.md: consumed here rather than
    duplicated, so a client is never told a frame name that module's own
    conversion functions do not recognise). A code outside the ASCOM-defined
    0-4 is a driver bug, not a value to guess at — surfaced as ``None``
    (unknown), logged, exactly like any other unreadable member here."""
    if code is None:
        return None
    try:
        return mount_frame_from_ascom(code)
    except ValueError:
        _log.warning("mount reported EquatorialSystem %r, which ASCOM does not define", code)
        return None


# --------------------------------------------------------------------------- #
# The connections footer: every device ssc-alpaca serves, not only the mount
# (DESIGN.md — the desktop and the phone must never disagree about it).
# --------------------------------------------------------------------------- #

#: ``DeviceType`` as ``ssc-alpaca``'s own ``/management/v1/configureddevices``
#: spells it (``DeviceRegistry.configured``, ``ssc/alpaca/server.py``),
#: casefolded — matched against, never assumed present: a device type this
#: table does not recognise is logged and left out of the snapshot rather
#: than guessed at (architecture policy invariant 7).
_DEVICE_KIND_FROM_TYPE: Final[Mapping[str, AlpacaDeviceKind]] = {
    "telescope": "telescope",
    "camera": "camera",
    "focuser": "focuser",
    "filterwheel": "filterwheel",
    "switch": "switch",
}

#: Short, human labels for the footer — one interface, one word. ``camera``
#: and ``focuser`` get a role suffix when the device's own name says which
#: one it is (see :func:`_device_label`): this scope's driver names them
#: e.g. "Seestar S30 Pro_SYNTHETIC000001 Telephoto Camera" (docs/M0-FINDINGS.md,
#: 2026-09-10), far too long to render in a footer.
_DEVICE_KIND_LABELS: Final[Mapping[AlpacaDeviceKind, str]] = {
    "telescope": "Mount",
    "camera": "Camera",
    "focuser": "Focuser",
    "filterwheel": "Filter wheel",
    "switch": "Switch",
}


def _device_label(kind: AlpacaDeviceKind, number: int, device_name: str) -> str:
    """A short label for one configured device — shortened here, once, so no
    other caller invents a second naming scheme.

    Only ``camera`` and ``focuser`` are ever seen twice on one scope (this
    device: telephoto + wide of each — DESIGN.md). Which is which is read
    from the device's own ``DeviceName``, never assumed by number, because a
    different scope's ordering is not guaranteed to match this one's. When
    the name gives no hint (SSC's own driver still answers a short generic
    name for these — ``ssc.alpaca.devices``' own ``default_name``, outside
    this package's ownership) and there is more than one of that kind, the
    device number is appended instead of a fabricated role.
    """
    base = _DEVICE_KIND_LABELS[kind]
    if kind not in ("camera", "focuser"):
        return base
    lowered = device_name.casefold()
    if "wide" in lowered:
        return f"{base} (wide)"
    if "tele" in lowered:  # matches "telephoto"
        return f"{base} (tele)"
    return base if number == 0 else f"{base} {number}"


@dataclass
class _DeviceReconnect:
    """One non-connected device's own retry state — never shared across
    devices, so a jammed camera's backoff has no effect on a focuser that is
    connecting fine (:meth:`AlpacaDeviceGateway._maintain_reconnect`)."""

    #: When the next attempt is due. Set to "now" the moment a device is
    #: first seen disconnected, so the very next poll tick makes the first
    #: real attempt — this tick reports ``"never attempted"`` honestly
    #: instead of attempting silently mid-discovery.
    next_attempt_at: datetime
    attempts: int = 0
    #: ``None`` only until the first attempt completes; from then on this is
    #: what :meth:`AlpacaDeviceGateway._maintain_reconnect` reports while
    #: backed off, so a client watching between attempts sees the last real
    #: reason rather than a placeholder.
    last_error: str | None = None


class AlpacaDeviceGateway:
    """Adapts :class:`~ssc.client.alpaca.AlpacaClient` to :class:`~ssc.state.ports.DeviceGateway`.

    Alpaca has no push channel, so telemetry is polled from ``ssc.status``
    (``AlpacaDeviceClient.status``) every :data:`_POLL_INTERVAL_S` once
    connected — reading ssc-core's own in-process driver over loopback, never
    the mount itself, so this is not the "stuck retry loop spamming the mount"
    DESIGN.md §9 warns against. Polling is turned back into an edge-triggered
    stream by publishing to the sinks only when the decoded link or device
    actually changes; otherwise the append-only ``seq`` log would grow once a
    second forever (CLAUDE.md invariant 4).

    Mount telemetry (DESIGN.md §4.4b) is the one exception to edge-triggering:
    every successful tick republishes a fresh :class:`~ssc.state.models.MountState`
    even when every field reads the same, because the reading's own age —
    :attr:`~ssc.state.models.MountState.stale` — is part of what a client
    renders, and a value that stopped advancing would look live when it is
    not. A reconnecting client that missed some of these still lands on the
    truth: ``StateStore`` answers a gap with a forced resnapshot rather than a
    partial replay (CLAUDE.md invariant 4).

    The connections footer (:meth:`_poll_devices`) is the same idea, one
    level out: every device ``ssc-alpaca`` serves, not only the mount, each
    with its own tri-state ``connected`` read straight from that device's own
    latch — never inferred from the telescope's own link (see
    :meth:`_poll_devices`'s own docstring).

    **Every configured device is kept connected, not only the mount**
    (bug fix, 2026-09-11: only ``telescope/0`` was ever asked to connect, so
    every camera, focuser and filter wheel sat permanently ``connected:
    false`` whether or not anything was actually wrong). A device that reads
    a clean ``Connected: false`` is reconnected from right here, on the same
    poll tick — never a second timer — with per-device exponential backoff
    (:meth:`_reconnect_delay`) so a device that is genuinely absent, or held
    open by another Alpaca client (the owner's own ZWO Seestar app), is not
    hammered every :data:`_POLL_INTERVAL_S`. This does make the footer poll
    write, which it used not to; see :meth:`_poll_devices`'s own docstring
    for what is still true of it.
    """

    #: Restated — ``ssc.main`` may not import ``ssc.seestar`` (invariants 1/2).
    #: Matches ``ssc.seestar.transport.CONTROL_PORT``, the port ``ssc.probe``
    #: and ``ssc.set_device_address`` themselves fall back to when a caller
    #: does not name one. The duplication is the same trade ``ssc.client``
    #: already makes for its ASCOM error numbers (see its own module docs): a
    #: client of a standard protocol depends on the protocol, not on reaching
    #: past it into one particular server's internals.
    _DEFAULT_CONTROL_PORT: Final = 4701

    #: Fast enough that a lost link or a completed connect shows up within a
    #: second or two of polling; see the class docstring for why this is safe.
    _POLL_INTERVAL_S: Final = 1.0

    #: Bounds every individual device read the connections-footer poll makes
    #: (``_poll_devices``) — ``configureddevices`` itself and each device's
    #: own ``Connected``. A member that never answers must degrade that one
    #: read to ``connected: null``, never hang the whole tick behind it: a
    #: wedged member held the control channel — and this poll loop, before
    #: this bound existed — stuck for half an hour, 2026-09-10.
    _DEVICE_READ_TIMEOUT_S: Final = 3.0

    #: The reconnect backoff (:meth:`_reconnect_delay`) — doubles per
    #: consecutive failure from this floor up to :data:`_RECONNECT_MAX_DELAY_S`,
    #: so a device that is genuinely gone, or held open by something else on
    #: the LAN, settles into one attempt a minute rather than one a second
    #: (DESIGN.md §9: a stuck retry loop must not spam the mount).
    _RECONNECT_MIN_DELAY_S: Final[float] = 2.0
    _RECONNECT_MAX_DELAY_S: Final[float] = 60.0

    def __init__(
        self,
        client: AlpacaClient,
        *,
        tasks: TaskRegistry,
        state: StateStore | None = None,
        persist_autofocus: Callable[[AutofocusState], Awaitable[None]] | None = None,
        before_motion: Callable[[], contextlib.AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        self._before_motion = before_motion
        self._client = client
        self._telescope: AlpacaDeviceClient = client.device("telescope")
        self._camera: AlpacaDeviceClient = client.device("camera")
        self._focuser: AlpacaDeviceClient = client.device("focuser")
        self._filterwheel: AlpacaDeviceClient = client.device("filterwheel")
        self._switch: AlpacaDeviceClient = client.device("switch")
        self._tasks = tasks
        self._autofocus = AutofocusService(
            focuser=self._focuser,
            camera=self._camera,
            state_store=state,
            tasks=tasks,
            persist=persist_autofocus,
        )
        self._site_elevation_supported: bool | None = None
        self._sinks: list[DeviceSink] = []
        self._poll_task: asyncio.Task[None] | None = None
        self._last_link: LinkState | None = None
        self._last_device: DeviceDescriptor | None = None
        self._last_mount: MountState | None = None
        self._last_devices: AlpacaDevicesState | None = None
        #: Read once per connection, not on every tick — these do not change
        #: while connected. ``None`` invalidates it, which happens whenever the
        #: link leaves a pollable state (see ``_publish``), so a reconnect (to
        #: the same device or a different one) always re-reads it fresh.
        self._mount_capabilities: MountCapabilities | None = None
        #: The camera's field of view, read once per connection — same
        #: reasoning as ``_mount_capabilities`` above, and invalidated
        #: alongside it. ``_camera_fov_attempted`` is separate from
        #: ``_camera_fov`` itself so a camera that genuinely cannot answer
        #: (not connected yet, or missing a member) is tried exactly once per
        #: connection rather than every tick — see ``_poll_camera_fov``.
        self._camera_fov: CameraFieldOfView | None = None
        self._camera_fov_attempted: bool = False
        #: One entry per device id (``"kind/number"``) currently not cleanly
        #: connected — populated the first time :meth:`_maintain_reconnect`
        #: sees it, and dropped the moment it reads ``Connected: true``
        #: again, so a device that has never had trouble never appears here.
        self._reconnects: dict[str, _DeviceReconnect] = {}

    @property
    def default_control_port(self) -> int:
        return self._DEFAULT_CONTROL_PORT

    def attach(self, sink: DeviceSink) -> Callable[[], None]:
        self._sinks.append(sink)
        return lambda: self._sinks.remove(sink)

    async def probe(
        self,
        host: str,
        *,
        control_port: int,
        timeout: float,  # noqa: ASYNC109 - matches the DeviceGateway protocol
    ) -> DeviceDescriptor:
        # ssc.probe has no caller-configurable timeout over the seam (see
        # ssc.client.alpaca.AlpacaClient.probe); the parameter stays so the
        # Protocol — and every caller of it — does not change, and the probe
        # is bounded instead by AlpacaClient's own fixed request timeout.
        del timeout
        try:
            result = await self._client.probe(host, control_port)
        except AlpacaClientError as exc:
            return DeviceDescriptor(
                host=host, control_port=control_port, reachable=False, note=str(exc)
            )
        return _descriptor_from_probe(host, control_port, result)

    async def discover(
        self,
        *,
        timeout: float,  # noqa: ASYNC109 - the scan's overall deadline
        on_progress: ProgressCallback | None = None,
    ) -> list[DeviceDescriptor]:
        # ssc.discover answers once, with the final list — no incremental
        # progress crosses the HTTP boundary, so on_progress never fires here.
        del on_progress
        try:
            found = await self._client.discover(timeout_s=timeout)
        except AlpacaClientError as exc:
            raise DeviceGatewayError(str(exc)) from exc
        return [_descriptor_from_discovered(device) for device in found]

    async def connect(self, host: str, *, control_port: int) -> None:
        try:
            await self._client.set_device_address(host, control_port)
            await self._telescope.connect()
        except AlpacaClientError as exc:
            _log.warning("connect to %s:%d did not complete: %s", host, control_port, exc)
            raise DeviceGatewayError(str(exc)) from exc
        if not await self._poll_once():
            raise DeviceGatewayError("Alpaca reports that the telescope is not connected")
        self._ensure_polling()

    async def disconnect(self) -> None:
        self._cancel_polling()
        try:
            await self._telescope.disconnect()
        except AlpacaClientError as exc:
            _log.warning("disconnect did not complete cleanly: %s", exc)
        if self._last_link is not None and self._last_link.status is not LinkStatus.DISCONNECTED:
            # Something was genuinely connected at some point — see
            # SeestarDeviceGateway's old comment for why this guard exists:
            # publishing DISCONNECTED with nothing ever having connected would
            # be fabricated, and a client mid connect could read it as failure.
            self._publish(
                LinkState(status=LinkStatus.DISCONNECTED, changed_at=datetime.now(tz=UTC)), None
            )

    async def mount_alignment(self) -> bool | None:
        """Whether the live ASCOM alignment mode is equatorial.

        Read the standard ``Telescope.AlignmentMode`` member rather than the
        Seestar-only ``ssc.device_state`` action.  The latter is unavailable on
        a telescope's native Alpaca surface and used to produce one warning on
        every Planning framing refresh.  Both Polar and German Polar are EQ;
        unknown and invalid values remain unknown.
        """
        if self._last_link is None or self._last_link.status is not LinkStatus.READY:
            return None
        mode = await self._get_int("alignmentmode")
        if mode == 0:
            return False
        if mode in (1, 2):
            return True
        return None

    async def mount_alignment_mode(self) -> int | None:
        """Live ``Telescope.AlignmentMode`` — the standard ASCOM member,
        read straight off ``telescope/0`` like every other mount control in
        this class, never through ``ssc.device_state``'s cached Seestar
        state (:meth:`mount_alignment`'s own path). SSC has never read this
        member before; ``ssc.astro.polar``'s polar-alignment run is the first caller
        that needs to refuse before it ever slews."""
        return await self._get_int("alignmentmode")

    async def set_site(
        self, *, latitude_deg: float, longitude_deg: float, elevation_m: float | None
    ) -> None:
        try:
            await self._telescope.put("sitelatitude", SiteLatitude=latitude_deg)
            await self._telescope.put("sitelongitude", SiteLongitude=longitude_deg)
        except AlpacaClientError as exc:
            _log.warning("could not set the telescope's observing site: %s", exc)
            return
        if elevation_m is None or self._site_elevation_supported is False:
            return
        try:
            await self._telescope.put("siteelevation", SiteElevation=elevation_m)
            self._site_elevation_supported = True
        except AlpacaRemoteError as exc:
            if exc.number in {
                int(AlpacaErrorNumber.NOT_IMPLEMENTED),
                int(AlpacaErrorNumber.ACTION_NOT_IMPLEMENTED),
            }:
                self._site_elevation_supported = False
                _log.info("Telescope.SiteElevation is not supported; future site updates omit it")
                return
            _log.warning("could not set the telescope's site elevation: %s", exc)
        except AlpacaClientError as exc:
            _log.warning("could not set the telescope's site elevation: %s", exc)

    # -- mount control (DESIGN.md §4.4b) -------------------------------------- #
    #
    # Every standard ASCOM member goes to telescope/0 through the same client
    # generic get/put the rest of this gateway already uses — no vendor action,
    # per architecture policy invariant 2 (prefer the standard member; ssc.* only for
    # what ASCOM cannot express, which none of this needs).

    async def mount_slew(self, *, ra_hours: float, dec_deg: float) -> None:
        self._require_mount_ready()
        await self._mount_put(
            "slewtocoordinatesasync", RightAscension=ra_hours, Declination=dec_deg
        )

    async def mount_sync(self, *, ra_hours: float, dec_deg: float) -> None:
        self._require_mount_ready()
        await self._mount_put("synctocoordinates", RightAscension=ra_hours, Declination=dec_deg)

    async def mount_move_axis(self, *, axis: MountAxis, rate_deg_s: float) -> None:
        self._require_mount_ready()
        await self._mount_put("moveaxis", Axis=_AXIS_NUMBERS[axis], Rate=rate_deg_s)

    async def mount_stop(self) -> None:
        """``AbortSlew`` and ``MoveAxis(Rate=0)`` on both axes — the panic
        control. Every one of the three is attempted even if an earlier one
        fails, because "stop" must do everything it can rather than give up
        after the first refusal; it only raises if all three failed."""
        self._require_mount_ready()
        attempts: tuple[tuple[str, dict[str, object]], ...] = (
            ("abortslew", {}),
            ("moveaxis", {"Axis": 0, "Rate": 0.0}),
            ("moveaxis", {"Axis": 1, "Rate": 0.0}),
        )
        errors: list[MountRefusedError | MountNotAvailableError] = []
        for member, params in attempts:
            try:
                await self._mount_put(member, **params)
            except (MountRefusedError, MountNotAvailableError) as exc:
                errors.append(exc)
        if len(errors) == len(attempts):
            raise errors[0]

    async def mount_set_tracking(
        self, *, enabled: bool, rate: MountTrackingRate | None
    ) -> MountState:
        self._require_mount_ready()
        await self._mount_put("tracking", Tracking=enabled)
        if rate is not None:
            await self._mount_put("trackingrate", TrackingRate=_TRACKING_RATE_CODES[rate])
        state = await self._read_mount_telemetry(datetime.now(tz=UTC))
        self._publish_mount(state)
        return state

    async def mount_park(self) -> None:
        self._require_mount_ready()
        await self._mount_put("park")

    async def mount_unpark(self) -> None:
        self._require_mount_ready()
        await self._mount_put("unpark")

    async def mount_find_home(self) -> None:
        self._require_mount_ready()
        await self._mount_put("findhome")

    def _require_mount_ready(self) -> None:
        if self._last_link is None or self._last_link.status not in _MOUNT_POLLABLE:
            raise MountRefusedError("not connected to the scope")

    async def _mount_put(self, member: str, **params: object) -> None:
        # Emergency stop must remain available even when camera cleanup fails.
        stopping = member == "abortslew" or (member == "moveaxis" and params.get("Rate") == 0)
        if not stopping and self._before_motion is not None:
            try:
                async with self._before_motion():
                    await self._mount_put_unchecked(member, **params)
            except MountRefusedError:
                raise
            except Exception as exc:
                raise MountRefusedError(f"Camera cleanup required before mount command: {exc}") from exc
        else:
            await self._mount_put_unchecked(member, **params)

    async def _mount_put_unchecked(self, member: str, **params: object) -> None:
        try:
            await self._telescope.put(member, **params)
        except AlpacaRemoteError as exc:
            if exc.number in _MOUNT_NOT_IMPLEMENTED_NUMBERS:
                raise MountNotAvailableError(f"{exc.member}: {exc.message}") from exc
            raise MountRefusedError(exc.message, device_code=exc.number) from exc
        except AlpacaClientError as exc:
            raise MountRefusedError(str(exc)) from exc

    # -- focuser control (bead ssc-bkv.7) -------------------------------------- #
    #
    # Same seam as mount control above, on the focuser's own device object.
    # "Ready" here means *the focuser itself* reports Connected — unlike the
    # mount, which is read off this gateway's own cached telescope link
    # (``_require_mount_ready``), the focuser connects independently of the
    # telescope link (see this class's own docstring, the 2026-09-11 fix
    # where only telescope/0 was ever reconnected). ``_poll_devices`` does
    # read and publish this focuser's ``Connected`` into
    # ``snapshot.alpaca_devices`` every tick (it is what keeps the focuser
    # reconnected in the first place) — but ``_require_focuser_ready`` below
    # does not consult that cache, it reads ``Connected`` live, bounded by
    # the same ``_DEVICE_READ_TIMEOUT_S`` as that poll.

    async def focuser_position(self) -> tuple[int | None, str | None]:
        return await self._focuser_get_int("position")

    async def focuser_is_moving(self) -> tuple[bool | None, str | None]:
        value, note = await self._focuser_get_optional("ismoving")
        if not isinstance(value, bool):
            if value is not None and note is None:
                note = "ismoving returned a non-boolean value"
            return None, note
        return value, note

    async def focuser_step_size(self) -> tuple[int | None, str | None]:
        """Live ``Focuser.StepSize``. On this hardware ``stepsize`` is
        declared ``unimplemented`` (``ssc/alpaca/devices/focuser.py``), which
        surfaces here as ``AlpacaRemoteError``/``AlpacaClientError`` and is
        swallowed by ``_focuser_get_optional`` into ``(None, note)`` the same
        as any other unread member — never a guessed distance."""
        return await self._focuser_get_int("stepsize")

    async def focuser_move(self, *, position: int) -> None:
        await self._require_focuser_ready()
        await self._focuser_put("move", Position=position)

    async def focuser_halt(self) -> None:
        await self._require_focuser_ready()
        await self._focuser_put("halt")

    async def focuser_start_autofocus(
        self, *, step: int, fine_step: int, exposure_s: float, settle_s: float
    ) -> None:
        await self._require_focuser_ready()
        try:
            await self._autofocus.run(
                step=step, fine_step=fine_step, exposure_s=exposure_s, settle_s=settle_s
            )
        except AutofocusAlreadyRunningError as exc:
            raise FocuserRefusedError(str(exc)) from exc
        except AutofocusError as exc:
            raise FocuserRefusedError(str(exc)) from exc
        except AlpacaRemoteError as exc:
            if exc.number in _MOUNT_NOT_IMPLEMENTED_NUMBERS:
                raise FocuserNotAvailableError(f"{exc.member}: {exc.message}") from exc
            raise FocuserRefusedError(exc.message, device_code=exc.number) from exc
        except AlpacaClientError as exc:
            raise FocuserRefusedError(str(exc)) from exc

    async def focuser_autofocus_state(self) -> AutofocusState:
        return self._autofocus.state

    async def _require_focuser_ready(self) -> None:
        """Bounded live read of ``Focuser.Connected`` — bounded by the same
        ``_DEVICE_READ_TIMEOUT_S`` as ``_read_device_connected``, because
        this is what gates ``Halt``, the one control documented as needing
        to work immediately, and inheriting the client's full 15s default
        (``ssc.client.alpaca.DEFAULT_REQUEST_TIMEOUT_S``) is unacceptable there.

        A transient transport/timeout fault here is a refusal
        (:class:`FocuserRefusedError`, 409 — "try again"), never
        :class:`FocuserNotAvailableError` (501 — "no focuser on this
        firmware, ever"): the frontend latches a 501 permanently
        (``web/src/api/focuser.ts``), so only an actual ASCOM
        NotImplemented/ActionNotImplemented may raise it — the same
        distinction :meth:`_focuser_put` already draws one call later.
        """
        try:
            connected = await asyncio.wait_for(
                self._focuser.get("connected"), timeout=self._DEVICE_READ_TIMEOUT_S
            )
        except TimeoutError:
            raise FocuserRefusedError(
                f"connected did not answer within {self._DEVICE_READ_TIMEOUT_S:g}s"
            ) from None
        except AlpacaRemoteError as exc:
            if exc.number in _MOUNT_NOT_IMPLEMENTED_NUMBERS:
                raise FocuserNotAvailableError(f"{exc.member}: {exc.message}") from exc
            raise FocuserRefusedError(exc.message, device_code=exc.number) from exc
        except AlpacaClientError as exc:
            raise FocuserRefusedError(str(exc)) from exc
        if connected is not True:
            raise FocuserRefusedError("not connected to the focuser")

    async def _focuser_put(self, member: str, **params: object) -> None:
        try:
            await self._focuser.put(member, **params)
        except AlpacaRemoteError as exc:
            if exc.number in _MOUNT_NOT_IMPLEMENTED_NUMBERS:
                raise FocuserNotAvailableError(f"{exc.member}: {exc.message}") from exc
            raise FocuserRefusedError(exc.message, device_code=exc.number) from exc
        except AlpacaClientError as exc:
            raise FocuserRefusedError(str(exc)) from exc

    async def _focuser_get_optional(
        self, member: str, **params: object
    ) -> tuple[object | None, str | None]:
        """One ``GET``, swallowed to ``(None, note)`` on any failure —
        including ``NotConnected`` — mirroring ``_read_device_connected``'s
        ``(value, note)`` shape: a read has a natural "unknown", so this
        never raises, but the caller still gets to say *why* rather than
        collapsing every cause to one unexplained gap."""
        try:
            return await self._focuser.get(member, **params), None
        except AlpacaClientError as exc:
            _log.debug("focuser: %s unavailable: %s", member, exc)
            return None, str(exc)

    async def _focuser_get_int(
        self, member: str, **params: object
    ) -> tuple[int | None, str | None]:
        value, note = await self._focuser_get_optional(member, **params)
        if isinstance(value, bool) or not isinstance(value, int):
            if value is not None and note is None:
                note = f"{member} returned a non-numeric value"
            return None, note
        return value, note

    # -- filter-wheel control (bead ssc-bkv.6) ------------------------------- #

    async def filterwheel_get_names(self) -> tuple[list[str] | None, str | None]:
        value, note = await self._filterwheel_get_optional("names")
        if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
            if value is not None and note is None:
                note = "names returned a value other than a list of strings"
            return None, note
        return value, note

    async def filterwheel_get_position(self) -> tuple[int | None, str | None]:
        value, note = await self._filterwheel_get_optional("position")
        if isinstance(value, bool) or not isinstance(value, int):
            if value is not None and note is None:
                note = "position returned a non-integer value"
            return None, note
        return value, note

    async def filterwheel_set_position(self, *, position: int) -> None:
        await self._require_filterwheel_ready()
        try:
            await self._filterwheel.put("position", Position=position)
        except AlpacaRemoteError as exc:
            if exc.number in _MOUNT_NOT_IMPLEMENTED_NUMBERS:
                raise FilterWheelNotAvailableError(f"{exc.member}: {exc.message}") from exc
            raise FilterWheelRefusedError(exc.message, device_code=exc.number) from exc
        except AlpacaClientError as exc:
            raise FilterWheelRefusedError(str(exc)) from exc

    async def heater_get_state(self) -> tuple[bool | None, str | None]:
        try:
            value = await self._switch.get("getswitch", Id=0)
        except AlpacaClientError as exc:
            _log.debug("dew heater unavailable: %s", exc)
            return None, str(exc)
        if not isinstance(value, bool):
            return None, "Switch.GetSwitch returned a non-boolean value"
        return value, None

    async def heater_set_state(self, *, enabled: bool) -> None:
        try:
            connected = await asyncio.wait_for(
                self._switch.get("connected"), timeout=self._DEVICE_READ_TIMEOUT_S
            )
            if connected is not True:
                raise HeaterRefusedError("not connected to the dew-heater switch")
            await self._switch.put("setswitch", Id=0, State=enabled)
        except HeaterRefusedError:
            raise
        except AlpacaRemoteError as exc:
            if exc.number in _MOUNT_NOT_IMPLEMENTED_NUMBERS:
                raise HeaterNotAvailableError(f"{exc.member}: {exc.message}") from exc
            raise HeaterRefusedError(exc.message, device_code=exc.number) from exc
        except (AlpacaClientError, TimeoutError) as exc:
            raise HeaterRefusedError(str(exc)) from exc

    async def _require_filterwheel_ready(self) -> None:
        try:
            connected = await asyncio.wait_for(
                self._filterwheel.get("connected"), timeout=self._DEVICE_READ_TIMEOUT_S
            )
        except TimeoutError:
            raise FilterWheelRefusedError(
                f"connected did not answer within {self._DEVICE_READ_TIMEOUT_S:g}s"
            ) from None
        except AlpacaRemoteError as exc:
            if exc.number in _MOUNT_NOT_IMPLEMENTED_NUMBERS:
                raise FilterWheelNotAvailableError(f"{exc.member}: {exc.message}") from exc
            raise FilterWheelRefusedError(exc.message, device_code=exc.number) from exc
        except AlpacaClientError as exc:
            raise FilterWheelRefusedError(str(exc)) from exc
        if connected is not True:
            raise FilterWheelRefusedError("not connected to the filter wheel")

    async def _filterwheel_get_optional(self, member: str) -> tuple[object | None, str | None]:
        try:
            return await self._filterwheel.get(member), None
        except AlpacaClientError as exc:
            _log.debug("filter wheel: %s unavailable: %s", member, exc)
            return None, str(exc)

    # -- polling -------------------------------------------------------------- #

    def _ensure_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = self._tasks.spawn(self._poll_loop(), name="alpaca-status-poll")

    def _cancel_polling(self) -> None:
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()

    async def _poll_loop(self) -> None:
        # Freshness must advance even while an Alpaca request is blocked or
        # failing and the browser's state WebSocket is still healthy.
        freshness = asyncio.create_task(self._watch_staleness(), name="alpaca-freshness")
        try:
            with contextlib.suppress(asyncio.CancelledError):
                while True:
                    await asyncio.sleep(self._POLL_INTERVAL_S)
                    if not await self._poll_once():
                        return
        except Exception:
            # The socket may remain healthy after the poll task fails. Do not
            # leave retained values marked fresh when their watchdog exits too.
            if self._last_mount is not None and not self._last_mount.stale:
                self._publish_mount(self._last_mount.model_copy(update={"stale": True}))
            if self._last_devices is not None and not self._last_devices.stale:
                self._publish_devices(self._last_devices.model_copy(update={"stale": True}))
            raise
        finally:
            freshness.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await freshness

    async def _watch_staleness(self) -> None:
        while True:
            await asyncio.sleep(self._POLL_INTERVAL_S)
            self._publish_elapsed_staleness(datetime.now(tz=UTC))

    def _publish_elapsed_staleness(self, now: datetime) -> None:
        mount = self._last_mount
        if mount is not None and not mount.stale and mount_stale(mount.polled_at, now=now):
            self._publish_mount(mount.model_copy(update={"stale": True}))
        devices = self._last_devices
        if devices is not None and not devices.stale and devices_stale(devices.polled_at, now=now):
            self._publish_devices(devices.model_copy(update={"stale": True}))

    async def _poll_once(self) -> bool:
        """Run one status tick; ``False`` means the device is disconnected.

        The Alpaca server is local and remains reachable when the physical
        Seestar disappears, so its standard 0x407 response is a link-loss
        signal, not a transient HTTP polling failure. Returning ``False``
        ends this poll session and lets :class:`DeviceService` supervise the
        reconnect with its normal backoff.
        """
        try:
            payload = await self._telescope.status()
            link, device = _link_and_device_from_status(payload)
            if device is not None and isinstance(payload.get("battery"), dict):
                device = device.model_copy(update={"battery": BatteryTelemetry.model_validate(payload["battery"])})
        except AlpacaRemoteError as exc:
            if exc.is_not_connected:
                reason = f"device disconnected: {exc.message}"
                self._publish(
                    LinkState(
                        status=LinkStatus.DISCONNECTED,
                        endpoint=self._last_link.endpoint if self._last_link is not None else None,
                        reason=reason,
                        changed_at=datetime.now(tz=UTC),
                    ),
                    None,
                )
                self._publish_mount(None)
                self._publish_devices(None)
                self._publish_camera_fov(None)
                self._reconnects.clear()
                _log.info("ssc.status reports a disconnected telescope; stopping this poll session")
                return False
            _log.warning("ssc.status poll failed: %s", exc)
            return True
        except AlpacaClientError as exc:
            _log.warning("ssc.status poll failed: %s", exc)
            return True
        except (KeyError, ValueError, TypeError):
            # A malformed or unrecognised payload shape (protocol drift, say)
            # is a reported failure, not a reason for the poll loop itself —
            # and the link state it would have described — to die silently.
            _log.exception("ssc.status returned a payload this gateway could not decode")
            return True
        self._publish(link, device)
        await self._poll_mount()
        await self._poll_devices()
        return True

    async def _poll_mount(self) -> None:
        """The mount half of the same tick (DESIGN.md §4.4b) — not a second
        loop. A no-op while the link is not in a pollable state; the last
        published :class:`~ssc.state.models.MountState`, if any, is left
        exactly as it was, and its own :attr:`~ssc.state.models.MountState.stale`
        is what tells a client to stop trusting it (module docstring)."""
        if self._last_link is None or self._last_link.status not in _MOUNT_POLLABLE:
            return
        if self._mount_capabilities is None:
            self._mount_capabilities = await self._read_mount_capabilities()
        state = await self._read_mount_telemetry(datetime.now(tz=UTC))
        self._publish_mount(state)

    async def _read_mount_telemetry(self, polled_at: datetime) -> MountState:
        # Independent ASCOM property reads must not accumulate fourteen network
        # round trips before publishing a sample. Bound fan-out for small servers,
        # and retain the acquisition-start timestamp rather than freshening old data.
        semaphore = asyncio.Semaphore(3)
        async def read(getter: Any, member: str) -> Any:
            async with semaphore:
                return await getter(member)

        specs = {
            "ra_hours": (self._get_float, "rightascension"),
            "dec_deg": (self._get_float, "declination"),
            "alt_deg": (self._get_float, "altitude"),
            "az_deg": (self._get_float, "azimuth"),
            "sidereal_time_hours": (self._get_float, "siderealtime"),
            "tracking": (self._get_bool, "tracking"),
            "tracking_rate": (self._get_int, "trackingrate"),
            "at_park": (self._get_bool, "atpark"),
            "at_home": (self._get_bool, "athome"),
            "slewing": (self._get_bool, "slewing"),
            "equatorial_system": (self._get_int, "equatorialsystem"),
            "site_latitude_deg": (self._get_float, "sitelatitude"),
            "site_longitude_deg": (self._get_float, "sitelongitude"),
        }
        values = await asyncio.gather(*(read(getter, member) for getter, member in specs.values()))
        telemetry = dict(zip(specs, values, strict=True))
        # Native drivers can briefly omit RA/Dec just after an asynchronous
        # dither. Reacquire the entire position/motion group once before
        # publishing a partial sample that would reset the 30-second settle
        # proof. Never carry forward an earlier position or freshen its clock.
        required = ("ra_hours", "dec_deg", "tracking", "slewing", "equatorial_system")
        if any(telemetry[name] is None for name in required[:4]):
            retry = await asyncio.gather(*(read(*specs[name]) for name in required))
            telemetry.update(zip(required, retry, strict=True))
        telemetry["tracking_rate"] = _tracking_rate_name(telemetry["tracking_rate"])
        telemetry["equatorial_system"] = _equatorial_system_name(telemetry["equatorial_system"])
        return MountState(polled_at=polled_at, stale=False, capabilities=self._mount_capabilities, **telemetry)

    async def _read_mount_capabilities(self) -> MountCapabilities:
        return MountCapabilities(
            can_slew_async=await self._get_bool("canslewasync"),
            can_sync=await self._get_bool("cansync"),
            can_park=await self._get_bool("canpark"),
            can_unpark=await self._get_bool("canunpark"),
            can_find_home=await self._get_bool("canfindhome"),
            can_set_tracking=await self._get_bool("cansettracking"),
            can_move_primary_axis=await self._get_bool("canmoveaxis", Axis=0),
            can_move_secondary_axis=await self._get_bool("canmoveaxis", Axis=1),
            axis_rate_max_deg_s=await self._axis_rate_max_deg_s(),
            tracking_rates=await self._tracking_rates(),
        )

    async def _axis_rate_max_deg_s(self) -> float | None:
        """The lower of the two moving axes' own ``AxisRates`` ceilings —
        never the delivered rate, which is unmeasured (DESIGN.md §4.4b)."""
        maxima: list[float] = []
        for axis in (0, 1):
            ranges = await self._get_optional("axisrates", Axis=axis)
            if not isinstance(ranges, list):
                continue
            for entry in ranges:
                if not isinstance(entry, Mapping):
                    continue
                maximum = entry.get("Maximum")
                if isinstance(maximum, int | float) and not isinstance(maximum, bool):
                    maxima.append(float(maximum))
        return min(maxima) if maxima else None

    async def _tracking_rates(self) -> list[MountTrackingRate] | None:
        value = await self._get_optional("trackingrates")
        if not isinstance(value, list):
            return None
        names: list[MountTrackingRate] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                continue
            name = _TRACKING_RATE_NAMES.get(item)
            if name is not None:
                names.append(name)
        return names

    async def _get_optional(self, member: str, **params: object) -> object | None:
        """One ``GET``, swallowed to ``None`` on any failure — a poll tick
        never fails wholesale because one member is unmapped or unreadable
        (CLAUDE.md invariant 7: unknown is ``None``, never fabricated)."""
        try:
            return await self._telescope.get(member, **params)
        except AlpacaClientError as exc:
            _log.debug("mount poll: %s unavailable: %s", member, exc)
            return None

    async def _get_float(self, member: str, **params: object) -> float | None:
        value = await self._get_optional(member, **params)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    async def _get_bool(self, member: str, **params: object) -> bool | None:
        value = await self._get_optional(member, **params)
        return value if isinstance(value, bool) else None

    async def _get_int(self, member: str, **params: object) -> int | None:
        value = await self._get_optional(member, **params)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _publish_mount(self, mount: MountState | None) -> None:
        if mount is not None and not mount.stale and mount_stale(mount.polled_at, now=datetime.now(tz=UTC)):
            mount = mount.model_copy(update={"stale": True})
        self._last_mount = mount
        for sink in self._sinks:
            sink.on_mount(mount)

    # -- the connections footer (every device, not only the mount) ----------- #

    async def _poll_devices(self) -> None:
        """Every device ``ssc-alpaca`` serves, and each one's own ``Connected``
        — the connections-footer half of this tick, not a second loop (class
        docstring). Runs whatever the telescope link's own status is, unlike
        :meth:`_poll_mount`: these are ``ssc-alpaca``'s own per-device
        latches (``ssc/alpaca/devices/base.py``'s ``_get_connected``),
        answerable regardless of whether the scope-side session is up.

        This is no longer a pure read (bug fix, 2026-09-11): a device that
        reads a clean ``Connected: false`` is reconnected right here, once
        per tick per device and bounded by its own backoff — see
        :meth:`_maintain_reconnect`. A read that instead comes back
        ambiguous (a timeout, a non-boolean value, a transport failure) is
        left exactly as read, never turned into a connect attempt: that is a
        communication problem with the device, not evidence it is merely
        unconnected, and retrying a member that will not even answer
        ``Connected`` cannot help.

        Enumerated fresh every tick rather than cached, and republished
        unconditionally on every successful read (mirrors ``MountChanged`` —
        see ``AlpacaDevicesState``'s docstring): a live client's own idea of
        ``stale`` needs ``polled_at`` to keep advancing, not only the moments
        it resyncs a whole snapshot.
        """
        # Retain acquisition start: a slow batch must not make earlier
        # device readings appear current merely because its final read finished.
        polled_at = datetime.now(tz=UTC)
        try:
            configured = await asyncio.wait_for(
                self._client.configured_devices(), timeout=self._DEVICE_READ_TIMEOUT_S
            )
        except (AlpacaClientError, TimeoutError) as exc:
            _log.warning("devices poll: configureddevices failed: %s", exc)
            return
        items: list[AlpacaDeviceItem] = []
        for entry in configured:
            item = await self._read_device_item(entry)
            if item is not None:
                items.append(item)
        self._publish_devices(
            AlpacaDevicesState(polled_at=polled_at, stale=False, items=items)
        )
        await self._poll_camera_fov(items)

    async def _read_device_item(self, entry: Mapping[str, Any]) -> AlpacaDeviceItem | None:
        """One ``configureddevices`` entry -> :class:`AlpacaDeviceItem`, or
        ``None`` when its ``DeviceType``/``DeviceNumber`` cannot be placed —
        logged, never guessed at (CLAUDE.md invariant 7), so a future device
        type this table does not yet know is dropped rather than mislabelled."""
        device_type = entry.get("DeviceType")
        device_number = entry.get("DeviceNumber")
        device_name = entry.get("DeviceName")
        if (
            not isinstance(device_type, str)
            or not isinstance(device_number, int)
            or isinstance(device_number, bool)
        ):
            _log.warning(
                "devices poll: configureddevices entry missing DeviceType/DeviceNumber: %r", entry
            )
            return None
        kind = _DEVICE_KIND_FROM_TYPE.get(device_type.casefold())
        if kind is None:
            _log.warning(
                "devices poll: configureddevices reported an unrecognised DeviceType %r",
                device_type,
            )
            return None
        name = device_name if isinstance(device_name, str) else ""
        #: The canonical, lowercase ``kind`` is what addresses the device
        #: from here on — not the raw ``DeviceType`` text, which is only ever
        #: used to place it in :data:`_DEVICE_KIND_FROM_TYPE` above. Alpaca
        #: paths are case-insensitive either way, but a fixed, known-good
        #: spelling makes the id, the address and a timeout's own note agree.
        device_id = f"{kind}/{device_number}"
        temperature_c: float | None = None
        temperature_note: str | None = None
        if kind == "camera" and device_number == 0:
            (connected, note), (temperature_c, temperature_note) = await asyncio.gather(
                self._read_device_connected(kind, device_number),
                self._read_camera_temperature(device_number),
            )
        else:
            connected, note = await self._read_device_connected(kind, device_number)
        if connected is True:
            # Confirmed up: whatever backoff state was tracking its last
            # trouble no longer applies (architecture policy invariant 7 — stale
            # failure history left behind would be its own kind of fiction).
            self._reconnects.pop(device_id, None)
        elif connected is False and note is None:
            # A clean, well-formed "no" — the one case this gateway now acts
            # on. See _maintain_reconnect for why an ambiguous read (`note`
            # already set) is left untouched instead.
            connected, note = await self._maintain_reconnect(kind, device_number, device_id)
        return AlpacaDeviceItem(
            id=device_id,
            kind=kind,
            number=device_number,
            label=_device_label(kind, device_number, name),
            connected=connected,
            note=note,
            temperature_c=temperature_c,
            temperature_note=temperature_note,
        )

    async def _read_camera_temperature(self, device_number: int) -> tuple[float | None, str | None]:
        """Read the primary sensor without extending the device poll's wall time."""
        try:
            value = await asyncio.wait_for(
                self._client.device("camera", device_number).get("ccdtemperature"),
                timeout=self._DEVICE_READ_TIMEOUT_S,
            )
        except TimeoutError:
            return None, f"camera/{device_number} temperature did not answer"
        except AlpacaClientError as exc:
            return None, str(exc)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None, f"camera/{device_number} reported CCDTemperature={value!r}"
        return float(value), None

    async def _read_device_connected(
        self, kind: AlpacaDeviceKind, device_number: int
    ) -> tuple[bool | None, str | None]:
        """This one device's own ``Connected`` — bounded, so one member that
        never answers degrades to ``connected: null`` for this device alone
        rather than wedging the rest of this tick, or the poll loop behind it
        (measured live, 2026-09-10: an unbounded read here is what stuck the
        control channel for half an hour)."""
        device = self._client.device(kind, device_number)
        try:
            value = await asyncio.wait_for(
                device.get("connected"), timeout=self._DEVICE_READ_TIMEOUT_S
            )
        except TimeoutError:
            return None, (
                f"{kind}/{device_number} did not answer within {self._DEVICE_READ_TIMEOUT_S:g}s"
            )
        except AlpacaClientError as exc:
            return None, str(exc)
        if isinstance(value, bool):
            return value, None
        return (
            None,
            f"{kind}/{device_number} reported Connected={value!r}, not a boolean",
        )

    async def _maintain_reconnect(
        self, kind: AlpacaDeviceKind, device_number: int, device_id: str
    ) -> tuple[bool | None, str | None]:
        """Bring one cleanly-``Connected: false`` device back up, or say
        plainly why not — the three states the task exists to make
        distinguishable, never a bare ``None`` standing in for one of them
        (CLAUDE.md invariant 7):

        * **"never attempted"** — the first tick this device has ever been
          seen disconnected. An attempt is scheduled for right now, but is
          not made *this* tick, so a device caught mid-discovery is reported
          honestly rather than silently acted on first.
        * **"attempt failed: <reason>"** — the most recent attempt did not
          leave the device connected, whether it raised outright or the PUT
          returned cleanly and the device still would not confirm it
          (``ssc.seestar.native`` has already observed this firmware's own
          ``Connected`` latch not round-tripping reliably in the other
          direction — treated here as equally unreliable on the way up).
        * ``None`` (``connected: true``) — the reconnect succeeded; the
          caller has already cleared this device's state.

        Backed off per :meth:`_reconnect_delay` between attempts, so a
        device that is genuinely absent, or held open by another Alpaca
        client (the owner's own ZWO Seestar app), gets one attempt a minute
        once it has failed a few times in a row — never one every
        :data:`_POLL_INTERVAL_S` (DESIGN.md §9).
        """
        now = datetime.now(tz=UTC)
        state = self._reconnects.get(device_id)
        if state is None:
            self._reconnects[device_id] = _DeviceReconnect(next_attempt_at=now)
            return False, "never attempted"
        if now < state.next_attempt_at:
            return False, state.last_error or "never attempted"
        try:
            await self._client.device(kind, device_number).put("connected", Connected=True)
        except AlpacaClientError as exc:
            return False, self._record_reconnect_failure(state, now, str(exc))
        fresh_connected, fresh_note = await self._read_device_connected(kind, device_number)
        if fresh_connected is True:
            self._reconnects.pop(device_id, None)
            return True, None
        reason = fresh_note if fresh_note is not None else "Connected did not take"
        return fresh_connected, self._record_reconnect_failure(state, now, reason)

    def _record_reconnect_failure(self, state: _DeviceReconnect, now: datetime, reason: str) -> str:
        state.attempts += 1
        state.last_error = f"attempt failed: {reason}"
        state.next_attempt_at = now + timedelta(seconds=self._reconnect_delay(state.attempts))
        return state.last_error

    def _reconnect_delay(self, attempts: int) -> float:
        """Exponential backoff from :data:`_RECONNECT_MIN_DELAY_S`, doubling
        per consecutive failure and capped at :data:`_RECONNECT_MAX_DELAY_S`
        — attempt count grows with the *log* of how long a device has been
        down, not with how many poll ticks have gone by."""
        doubled: float = self._RECONNECT_MIN_DELAY_S * (2 ** max(attempts - 1, 0))
        return min(doubled, self._RECONNECT_MAX_DELAY_S)

    def _publish_devices(self, devices: AlpacaDevicesState | None) -> None:
        if devices is not None and not devices.stale and devices_stale(devices.polled_at, now=datetime.now(tz=UTC)):
            devices = devices.model_copy(update={"stale": True})
        self._last_devices = devices
        for sink in self._sinks:
            sink.on_devices(devices)

    # -- camera field of view (DESIGN.md §3.5.1's rotated FOV rectangle) ------ #

    async def _poll_camera_fov(self, items: Sequence[AlpacaDeviceItem]) -> None:
        """Attempt the camera's field of view exactly once per connection,
        and only once ``camera/0`` has actually been enumerated by the same
        tick's connections-footer poll (CLAUDE.md invariant 11: never assume
        a camera exists before ``configureddevices`` has said so).

        Gated on presence in ``items``, not on that device's own ``Connected``
        — :class:`~ssc.state.models.AlpacaDeviceItem`'s own docstring records
        that ``Connected`` is not a reliable health signal on this device, so
        it is not treated as one here either.
        """
        if self._camera_fov_attempted:
            return
        if not any(item.kind == "camera" and item.number == 0 for item in items):
            return
        self._camera_fov_attempted = True
        fov = await self._read_camera_fov()
        if fov is None:
            return
        self._camera_fov = fov
        self._publish_camera_fov(fov)

    async def _read_camera_fov(self) -> CameraFieldOfView | None:
        """See :class:`~ssc.state.models.CameraFieldOfView`'s docstring for
        the formula, the axis trap and why a partial read publishes nothing.
        """
        camera = self._client.device("camera", 0)
        x_size = await self._geometry_int(camera, "cameraxsize")
        y_size = await self._geometry_int(camera, "cameraysize")
        pixel_size_um = await self._geometry_float(camera, "pixelsizex")
        #: ASCOM ``Telescope.FocalLength`` is defined in **metres**, unlike
        #: every other length on this seam — see ``ssc.alpaca.devices.telescope
        #: ._get_focal_length``, which is this same value in millimetres
        #: divided by 1000 before it ever reaches the wire.
        focal_length_m = await self._geometry_float(self._telescope, "focallength")
        if (
            x_size is None
            or y_size is None
            or pixel_size_um is None
            or focal_length_m is None
            or x_size <= 0
            or y_size <= 0
            or pixel_size_um <= 0
            or focal_length_m <= 0
        ):
            return None
        focal_length_mm = focal_length_m * 1000.0
        return CameraFieldOfView(
            width_deg=_pixel_span_to_fov_deg(x_size, pixel_size_um, focal_length_mm),
            height_deg=_pixel_span_to_fov_deg(y_size, pixel_size_um, focal_length_mm),
        )

    async def _geometry_int(
        self, device: AlpacaDeviceClient, member: str, **params: object
    ) -> int | None:
        value = await self._get_geometry_member(device, member, **params)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    async def _geometry_float(
        self, device: AlpacaDeviceClient, member: str, **params: object
    ) -> float | None:
        value = await self._get_geometry_member(device, member, **params)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    async def _get_geometry_member(
        self, device: AlpacaDeviceClient, member: str, **params: object
    ) -> object | None:
        """One bounded ``GET``, for the one-time camera-geometry read —
        mirrors :meth:`_read_device_connected`'s own bound and for the same
        reason: a member that never answers must degrade this one geometry
        input to ``None``, never hang the tick behind it."""
        try:
            return await asyncio.wait_for(
                device.get(member, **params), timeout=self._DEVICE_READ_TIMEOUT_S
            )
        except TimeoutError:
            _log.debug(
                "camera fov: %s did not answer within %gs", member, self._DEVICE_READ_TIMEOUT_S
            )
            return None
        except AlpacaClientError as exc:
            _log.debug("camera fov: %s unavailable: %s", member, exc)
            return None

    def _publish_camera_fov(self, fov: CameraFieldOfView | None) -> None:
        for sink in self._sinks:
            sink.on_camera_fov(fov)

    def _publish(self, link: LinkState, device: DeviceDescriptor | None) -> None:
        if link != self._last_link:
            self._last_link = link
            if link.status not in _MOUNT_POLLABLE:
                # Invalidates the cached capabilities on any drop, not just a
                # clean disconnect, so a reconnect — to the same device or a
                # different one — always reads them fresh rather than reusing
                # a possibly stale or wrong set. The camera's field of view
                # is invalidated the same way and for the same reason; the
                # last *published* value stays in the snapshot until a fresh
                # read replaces it, exactly as ``_mount_capabilities`` itself
                # already does inside the last-published ``MountState``.
                self._mount_capabilities = None
                self._camera_fov = None
                self._camera_fov_attempted = False
            for sink in self._sinks:
                sink.on_link(link)
        if device != self._last_device:
            self._last_device = device
            for sink in self._sinks:
                sink.on_device(device)


def _descriptor_from_probe(host: str, control_port: int, result: ProbeResult) -> DeviceDescriptor:
    """``ssc.probe``'s :class:`~ssc.client.models.ProbeResult` → :class:`DeviceDescriptor`.

    Neither ``capabilities`` nor a live event count crosses this action — those
    are properties of a connected session, and probing never opens one.

    ``result.port`` wins over the port that was asked about: a device on the
    native Alpaca surface answers on its Alpaca port, and the address this
    descriptor carries is the one the app remembers and reconnects to.
    TCP acceptance alone does not identify a usable device: require the
    protocol handshake before advertising it as reachable to the app.
    """
    return DeviceDescriptor(
        host=host,
        control_port=result.port or control_port,
        reachable=result.reachable and result.handshake,
        firmware=result.firmware,
        firmware_verified=result.verified,
        model=result.model,
        serial=result.serial,
        note=result.reason,
    )


def _descriptor_from_discovered(device: DiscoveredDevice) -> DeviceDescriptor:
    """``ssc.discover``'s :class:`~ssc.client.models.DiscoveredDevice` → :class:`DeviceDescriptor`.

    Found implies reachable. ``ssc.discover`` does not report whether the
    firmware is a verified one — only ``ssc.probe`` does — so this leaves
    ``firmware_verified`` at its honest default of ``False`` rather than
    guessing; a caller wanting that badge probes the address it found.

    ``note`` carries what the scan observed — which surface answered, and what
    it did and did not report — so a device found with no firmware version says
    why instead of looking broken.
    """
    return DeviceDescriptor(
        host=device.host,
        control_port=device.port,
        reachable=True,
        firmware=device.firmware,
        model=device.model,
        serial=device.serial,
        note=device.note,
    )


def _link_and_device_from_status(
    payload: Mapping[str, Any],
) -> tuple[LinkState, DeviceDescriptor | None]:
    """``ssc.status``'s JSON → the pair the sinks expect.

    Mirrors the shape ``ssc.alpaca.actions._status`` publishes — see that
    module — decoded defensively since it crossed real HTTP.
    """
    changed_at_raw = payload.get("changed_at")
    link = LinkState(
        status=LinkStatus(str(payload.get("state", LinkStatus.DISCONNECTED))),
        endpoint=_optional_str(payload.get("endpoint")),
        reason=_optional_str(payload.get("reason")),
        control_connected=bool(payload.get("control_connected", False)),
        events_connected=bool(payload.get("events_connected", False)),
        events_dropped=int(payload.get("events_dropped", 0)),
        connect_attempts=int(payload.get("connect_attempts", 0)),
        changed_at=datetime.fromisoformat(str(changed_at_raw)) if changed_at_raw else None,
    )
    device_payload = payload.get("device")
    device = (
        None
        if not isinstance(device_payload, Mapping)
        else _descriptor_from_device_state(device_payload)
    )
    return link, device


def _descriptor_from_device_state(payload: Mapping[str, Any]) -> DeviceDescriptor:
    """The ``device`` field of ``ssc.status`` — a live session's own ``DeviceInfo`` — decoded."""
    capabilities = payload.get("capabilities")
    return DeviceDescriptor(
        host=str(payload["host"]),
        control_port=int(payload["control_port"]),
        reachable=bool(payload.get("reachable", False)),
        firmware=_optional_str(payload.get("firmware")),
        firmware_verified=bool(payload.get("is_verified") or False),
        model=_optional_str(payload.get("model")),
        serial=_optional_str(payload.get("serial")),
        capabilities=dict(capabilities) if isinstance(capabilities, Mapping) else {},
        note=_optional_str(payload.get("note")),
    )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------- #
# mDNS
# --------------------------------------------------------------------------- #


class MdnsAdvertiser:
    """``_ssc._tcp`` so the phone finds the desktop without anyone typing an IP."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._zeroconf: object | None = None
        self._info: object | None = None

    async def start(self) -> None:
        from zeroconf import IPVersion, ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf

        addresses = [socket.inet_aton(a) for a in _mdns_lan_addresses(local_ipv4_addresses())]
        if not addresses:
            _log.warning("no local IPv4 address found; skipping mDNS advertisement")
            return
        safe = self._config.instance_name.replace(".", "-")
        info = ServiceInfo(
            MDNS_SERVICE_TYPE,
            f"{safe}.{MDNS_SERVICE_TYPE}",
            addresses=addresses,
            port=self._config.port,
            properties={"path": "/", "api": "/api", "ws": "/ws", "scheme": self._config.scheme},
            # Do not claim Avahi's host record from a second Zeroconf stack.
            # Doing so made Avahi rename ``photon.local`` to ``photon-2.local``
            # and let this service publish the loopback address for the former.
            server=_mdns_server_name(self._config.instance_name, socket.gethostname()),
        )
        zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        await zeroconf.async_register_service(info, allow_name_change=True)
        self._zeroconf = zeroconf
        self._info = info
        _log.info("advertising %s on port %d", MDNS_SERVICE_TYPE, self._config.port)

    async def stop(self) -> None:
        from zeroconf.asyncio import AsyncZeroconf

        zeroconf, self._zeroconf = self._zeroconf, None
        info, self._info = self._info, None
        if isinstance(zeroconf, AsyncZeroconf):
            if info is not None:
                with contextlib.suppress(Exception):
                    await zeroconf.async_unregister_service(info)  # type: ignore[arg-type]
            await zeroconf.async_close()


def _mdns_lan_addresses(addresses: Sequence[str]) -> list[str]:
    """Addresses another LAN client can actually use to reach this service."""
    usable: list[str] = []
    for candidate in addresses:
        with contextlib.suppress(ValueError):
            address = ipaddress.IPv4Address(candidate)
            if not address.is_loopback and not address.is_unspecified:
                usable.append(candidate)
    return usable


def _mdns_server_name(instance_name: str, hostname: str) -> str:
    """A service host label distinct from the OS-owned ``hostname.local``."""
    label = re.sub(r"[^A-Za-z0-9-]+", "-", f"{instance_name}-{hostname}").strip("-")
    return f"{(label or 'photon-commander')[:63]}.local."


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


async def build_services(
    config: AppConfig,
    *,
    gateway: DeviceGateway | None = None,
    database: Database | None = None,
    alpaca_runtime: AlpacaRuntime | None = None,
    tls_material: TlsMaterial | None = None,
) -> Services:
    """Open the store, migrate, load settings, host ssc-alpaca, assemble the container.

    ``gateway`` bypasses hosting ssc-alpaca entirely — tests pass a fake one and
    never bind a socket for it (CLAUDE.md invariant 2 is about SSC's own UI at
    runtime, not about every test needing a live driver). With no gateway, the
    driver is hosted: ``alpaca_runtime`` lets a test supply one whose backend
    talks to the in-process emulator instead of a socket to a real scope.
    """
    handle = database if database is not None else await open_database(config.database_path)
    # Recorded so ``ExportImportService`` can put an informational schema
    # version in ``manifest.json`` — an import never refuses on this value,
    # the archive's row data is replayed through ``TileCache``/
    # ``CutoutService`` rather than restored as raw SQL.
    schema_version = await migrate(handle)
    capture_repo = CaptureRepository(handle)
    # Recover durable final files before constructing the first snapshot.
    # Unverified files remain untouched and prevent unsafe startup.
    try:
        await ensure_durable_commits(handle)
        discarded = await reconcile_incomplete(capture_repo)
    except Exception:
        if database is None:
            await handle.aclose()
        raise
    if discarded:
        _log.warning("removed %d missing-file capture placeholders", len(discarded))
    # Durable-before-acted-on (architecture policy invariant 6) has to hold for the very
    # first run this process ever starts, not just the second one. `SqliteJournal`
    # used to switch the connection to `synchronous = FULL` lazily, on its own
    # first `append` — but `RunRepository.create` writes the `runs` row (via a
    # plain `Database.execute`, never through the journal) before the executor's
    # first journal append, so that one INSERT could commit under WAL's default
    # `synchronous = NORMAL` on a freshly launched process. Establishing it here,
    # once, before anything touches the database, closes that window instead of
    # relying on ordering between two call sites to happen to be safe.

    settings = SettingsStore(handle, auth_required_override=config.auth_required)
    await settings.load()
    secrets_store = SecretStore()
    plans = PlanRepository(handle)

    # LAN security is part of the job, not a hardening pass (architecture policy §9):
    # loginless is the owner's own choice for their own LAN, but it must be
    # visible in the log the moment it is also reachable *beyond* this host,
    # not left for someone to notice later. Loopback-only plus no auth is
    # unremarkable — nothing but this process can ever reach it — so it does
    # not warn.
    if not settings.effective_auth_required and not _is_loopback_host(config.host):
        _log.warning(
            "auth_required is off and this instance is bound to %s: any device "
            "that can reach port %d can view and control the mount, with no "
            "pairing at all. Set SSC_AUTH_REQUIRED=1, or turn pairing back on "
            "from Settings, to require a session again.",
            config.host,
            config.port,
        )

    # architecture policy invariant 7: a missing OS keyring is degraded, not silent.
    # Probed once, here, before the first snapshot is built, so the very
    # first GET /api/state already tells the truth about it.
    backend_status = await probe_backend()
    secrets_capability = SecretsCapability(
        available=backend_status.available,
        backend=backend_status.backend,
        reason=backend_status.reason,
    )
    startup_notices: list[Notice] = []
    if not backend_status.available:
        _log.warning(
            "no usable OS keyring backend (%s): %s", backend_status.backend, backend_status.reason
        )
        startup_notices.append(
            Notice(
                id="secrets-backend-unavailable",
                level=NoticeLevel.WARNING,
                code="secrets_backend_unavailable",
                message=(
                    "weather providers requiring an API key are unavailable: "
                    f"{backend_status.reason}"
                ),
                raised_at=datetime.now(tz=UTC),
            )
        )

    # architecture policy invariant 7: the ephemeris kernel is gitignored and fetched once
    # at install time, so its absence on a fresh checkout is a real, expected
    # case, not a startup crash — the rest of ssc-core works fine without it.
    # Every Planning-tab endpoint that actually needs it names this notice's fix.
    # architecture policy invariant 7: rotating the local TLS CA is a real, disruptive
    # event — every device that trusted the old one has to trust the new one —
    # so it is surfaced with enough lead time to plan it, not discovered when
    # the leaf certificate it signs can no longer be renewed under it.
    if tls_material is not None and tls_material.ca_expires_at is not None:
        remaining = tls_material.ca_expires_at - datetime.now(tz=UTC)
        if remaining <= CA_RENEWAL_WARNING_WINDOW:
            startup_notices.append(
                Notice(
                    id="tls-ca-expiring",
                    level=NoticeLevel.WARNING,
                    code="tls_ca_expiring",
                    message=(
                        "the local HTTPS certificate authority expires on "
                        f"{tls_material.ca_expires_at.isoformat()}; every device that "
                        "trusts it will need to trust its replacement once it is rotated"
                    ),
                    raised_at=datetime.now(tz=UTC),
                )
            )

    astro_service = await build_astro_service(include_dev_profiles=config.enable_dev_profiles)
    if astro_service.ephemeris_error is not None:
        startup_notices.append(
            Notice(
                id="ephemeris-kernel-unavailable",
                level=NoticeLevel.WARNING,
                code="ephemeris_kernel_unavailable",
                message=f"the Planning tab is unavailable: {astro_service.ephemeris_error}",
                raised_at=datetime.now(tz=UTC),
            )
        )

    # architecture policy invariant 7: Met.no requires a real identifying contact that
    # has no default and no fallback — the stored setting first, then
    # SSC_WEATHER_CONTACT. An unconfigured host still starts, but the weather
    # forecast (and therefore the header widget's capture-conditions
    # synthesis) names the fix rather than going silently dark.
    weather_service, weather_notice = build_weather_service(
        config,
        secrets_store,
        backend_status,
        stored_contact=settings.weather.metno_contact,
        selected_provider=settings.weather.provider,
    )
    if weather_notice is not None:
        startup_notices.append(
            Notice(
                id="weather-contact-unconfigured",
                level=NoticeLevel.WARNING,
                code="weather_contact_unconfigured",
                message=f"the weather forecast is unavailable: {weather_notice}",
                raised_at=datetime.now(tz=UTC),
            )
        )

    # Sky-survey tiles (THIRD_PARTY_NOTICES.md requirement 5): proxied, cached and
    # prefetched here so the map works with no network. The identifying
    # User-Agent carries the same operator contact Met.no gets, read at each
    # request so a contact typed into Settings takes effect without a restart.
    def skymap_user_agent() -> str:
        contact = resolve_metno_contact(
            stored=settings.weather.metno_contact, environment=config.weather_contact
        )
        return build_user_agent(contact.value)

    # Target-field cutouts (WISE/SDSS/Legacy Surveys, THIRD_PARTY_NOTICES.md
    # "Endpoint gate, 2026-09-07"): a distinct fetch/cache layer from the
    # tile pyramid above, sharing the same ``skymap`` snapshot section and
    # the same one-publisher discipline (SkymapService.publish, below).
    cutouts = CutoutService(
        handle,
        config.cutouts_dir,
        max_bytes=config.cutouts_max_bytes,
        user_agent=skymap_user_agent,
        plans=plans,
    )
    # Marks any cutout fetch a previous process left running as interrupted
    # (architecture policy invariant 6), before the first GET /api/state can be asked.
    await cutouts.startup()

    # Bulk cutout prefetch (ssc/skymap/bulk_prefetch.py): a queue of the same
    # per-target cutout requests above, over a scoped catalogue subset
    # (never "the whole catalogue" — architecture policy invariant 11's sibling
    # concern about scale). Reuses `cutouts` for every fetch, so the
    # etiquette, caching and eviction above are never duplicated.
    bulk_cutouts = BulkCutoutService(handle, cutouts, astro_service.catalog)
    await bulk_cutouts.startup()

    # Click-through info (M11, THIRD_PARTY_NOTICES.md "Click-through info
    # sources"): off by default (ObjectInfoSettings), checked fresh on every
    # call so flipping the Settings toggle takes effect without a restart —
    # the same reasoning `skymap_user_agent` above already follows for the
    # Met.no contact. Shares the identifying User-Agent with the sky-survey
    # proxy above (same format, same operator contact).
    def object_info_enabled() -> bool:
        return settings.object_info.enabled

    object_info = ObjectInfoService(
        handle, enabled=object_info_enabled, user_agent=skymap_user_agent
    )

    # Kept as its own variable (rather than inlined into `SkymapService(...)`
    # below) because `ExportImportService` needs the same `TileCache`
    # instance too — one cache, two consumers, never two separate indexes
    # of the same files on disk.
    tile_cache = TileCache(handle, config.tiles_dir)
    skymap = SkymapService(
        tile_cache,
        max_bytes=config.tiles_max_bytes,
        user_agent=skymap_user_agent,
        bundled_root=config.bundled_hips_dir,
        cutouts_max_bytes=config.cutouts_max_bytes,
        cutouts_provider=cutouts.compose,
        bulk_cutouts_provider=bulk_cutouts.compose,
    )
    # Marks any download a previous process left running as interrupted, and
    # builds the section the very first GET /api/state already carries.
    skymap_state = await skymap.startup()

    # Export/import of this instance's own already-fetched tile and cutout
    # cache (portability/backup, never a way to ship a pre-built pack —
    # ``ssc.skymap.export_import`` module docstring). Reuses `skymap.publish`
    # so an import's new rows reach connected clients through the same
    # one-section, one-publisher discipline as a live fetch.
    export_import = ExportImportService(
        handle,
        tile_cache,
        cutouts,
        data_dir=config.data_dir,
        schema_version=schema_version,
        publish=skymap.publish,
    )

    # Built early (needs only ``handle``, not the hosted driver below) so the
    # very first snapshot already carries it — mirrors ``secrets_capability``
    # above, not ``run``'s "``None`` until something has happened" (see
    # ``CaptureState``'s own docstring). Reused, not rebuilt, once the driver
    # is resolved further down.
    last_frames = await capture_repo.list_recent(limit=1)
    capture_section = initial_capture_state(
        last=last_frames[0] if last_frames else None,
        folder_configured=settings.capture.folder is not None,
    )
    polar_alignment_repo = PolarAlignmentRepository(handle)
    autofocus_repo = AutofocusRepository(handle)
    autofocus_section = AutofocusState()
    try:
        saved_autofocus = await autofocus_repo.get()
        autofocus_section = (
            AutofocusState()
            if saved_autofocus is None
            else restored_autofocus_state(saved_autofocus)
        )
        if saved_autofocus is not None and autofocus_section != saved_autofocus:
            await autofocus_repo.save(autofocus_section)
    except Exception as exc:
        _log.error("autofocus retention could not be restored (%s)", type(exc).__name__)
        startup_notices.append(
            Notice(
                id="autofocus-retention",
                code="autofocus_retention_failed",
                level=NoticeLevel.WARNING,
                raised_at=datetime.now(UTC),
                message="Saved autofocus measurements could not be restored. Focus position is unverified.",
            )
        )

    def polar_night_key(moment: datetime) -> tuple[date, str] | None:
        location = settings.active_location()
        if location is None or location.timezone is None:
            return None
        try:
            tz = resolve_timezone(location)
        except PlanTimezoneUnknownError:
            return None
        return current_night_date(moment, tz), tz.key

    async def persist_polar_alignment(section: PolarAlignmentState) -> None:
        key = polar_night_key(section.started_at or datetime.now(UTC))
        if key is None:
            raise StoreError("Set the observing location timezone to retain polar alignment for its night")
        await polar_alignment_repo.save(*key, section)

    key = polar_night_key(datetime.now(UTC))
    saved_polar = None if key is None else await polar_alignment_repo.get(*key)
    polar_alignment_section = (PolarAlignmentState() if saved_polar is None
                               else restored_night_state(saved_polar, datetime.now(UTC)))

    snapshot = StateSnapshot(
        server_started_at=datetime.now(tz=UTC),
        location=settings.active_location(),
        settings=settings.summary(),
        secrets=secrets_capability,
        notices=startup_notices,
        # So the very first GET /api/state already reflects the persisted
        # master switch (DeviceService mirrors it identically at construction
        # — see its own docstring).
        auto_connect=AutoConnectState(enabled=settings.device.auto_connect),
        # So the first GET /api/state already lists whatever plans are stored,
        # rather than every client having to fetch GET /api/plans separately.
        plans=await plans.list_summaries(),
        skymap=skymap_state,
        capture=capture_section,
        polar_alignment=polar_alignment_section,
        autofocus=autofocus_section,
    )
    state = StateStore(snapshot, buffer_size=config.event_buffer_size)
    auth = AuthService(handle, settings, secrets_store, ttl=timedelta(days=config.session_ttl_days))
    tasks = TaskRegistry()
    operations = OperationService(state, tasks)
    skymap.attach(state, lambda coro, name: tasks.spawn(coro, name=name))
    cutouts.attach(skymap.publish, lambda coro, name: tasks.spawn(coro, name=name))
    bulk_cutouts.attach(skymap.publish, lambda coro, name: tasks.spawn(coro, name=name))

    if gateway is not None:
        device_gateway: DeviceGateway = gateway
        alpaca_runtime = None
        run_device_commands = None
    else:
        alpaca_runtime = alpaca_runtime or (
            AlpacaRuntime()
            if config.alpaca_port is None
            else AlpacaRuntime(port=config.alpaca_port)
        )
        await alpaca_runtime.start()
        def _recover_companion_before_motion() -> contextlib.AbstractAsyncContextManager[None]:
            assert alpaca_runtime is not None
            return dual_motion_guard(repo=capture_repo,
                tele=alpaca_runtime.client.device("camera"),
                wide=alpaca_runtime.client.device("camera", 1))

        run_device_commands = AlpacaDeviceCommands(
            alpaca_runtime.client, state=state, astro=astro_service,
            before_motion=_recover_companion_before_motion,
        )
        device_gateway = AlpacaDeviceGateway(
            alpaca_runtime.client,
            tasks=tasks,
            state=state,
            persist_autofocus=autofocus_repo.save,
            before_motion=_recover_companion_before_motion,
        )

    device = DeviceService(
        device_gateway,
        state,
        settings,
        tasks,
        astro_service,
        discovery_timeout_s=config.discovery_timeout_s,
        probe_timeout_s=config.probe_timeout_s,
        reconnect_initial_delay_s=config.reconnect_initial_delay_s,
        reconnect_max_delay_s=config.reconnect_max_delay_s,
    )
    # Non-blocking: the first connect attempt, if any, happens inside the
    # spawned task, never here — device-independent endpoints (planning,
    # astro, weather) must come up whether or not a scope answers.
    device.start_supervisor()
    if run_device_commands is not None:
        async def _run_scheduled_autofocus() -> None:
            async def run_at_exposure(exposure_s: float) -> None:
                try:
                    await device.focuser_start_autofocus(
                        step=10, fine_step=2, exposure_s=exposure_s, settle_s=2.0
                    )
                except FocuserNotAvailableError as exc:
                    raise DeviceCommandNotImplementedError(str(exc)) from exc
                except FocuserRefusedError as exc:
                    if isinstance(exc.__cause__, AutofocusConditionsError):
                        raise DeviceFocusConditionsError(str(exc)) from exc
                    raise DeviceCommandError(
                        str(exc), recoverable=not isinstance(exc.__cause__, AutofocusRestoreError)
                    ) from exc

            def report(message: str) -> None:
                state.apply(
                    NoticeRaised(
                        notice=Notice(
                            id="scheduled-focus-recovery",
                            level=NoticeLevel.WARNING,
                            code="scheduled_focus_recovery",
                            message=message,
                            raised_at=datetime.now(UTC),
                        )
                    )
                )

            await adaptive_autofocus(run_at_exposure, report)
            state.apply(NoticeCleared(notice_id="scheduled-focus-recovery"))

        run_device_commands.set_autofocus_runner(_run_scheduled_autofocus)
    heater_automation = HeaterAutomationService(
        settings=settings,
        weather=weather_service,
        device=device,
        tasks=tasks,
    )
    heater_automation.start()

    # Closed-loop centering is shared by interactive Slew + center, manual
    # Capture's center-after-drift, and scheduled capture. Construct it before
    # either capture adapter so no path falls back to an unverified one-shot
    # slew/solve pair.
    polar_alignment = PolarAlignmentService(
        camera=None if alpaca_runtime is None else alpaca_runtime.client.device("camera"),
        telescope=None if alpaca_runtime is None else alpaca_runtime.client.device("telescope"),
        filterwheel=(
            None if alpaca_runtime is None else alpaca_runtime.client.device("filterwheel")
        ),
        device=device,
        state=state,
        astro=astro_service,
        tasks=tasks,
        section=polar_alignment_section,
        settings=settings,
        persist=persist_polar_alignment,
        night_key=polar_night_key,
    )
    target_centering = TargetCenteringService(
        device=device,
        solver=polar_alignment,
        state=state,
    )
    if run_device_commands is not None:
        run_device_commands.set_polar_alignment_runner(polar_alignment.run_unattended)

    async def _recenter_and_solve(ra_h: float, dec_deg: float) -> None:
        await target_centering.center_default(ra_h, dec_deg)

    # The executor's device ports go through the same hosted driver, as a
    # client of it (architecture policy invariant 2) — ``run_device_commands`` itself is
    # built above, alongside the gateway, with a substituted gateway leaving it
    # ``None`` since there is no driver to execute against then (RunService.start
    # says so, 503).

    def _capture_port_factory(run_id: str) -> CapturePort:
        # Only ever handed to ``RunService`` when ``alpaca_runtime`` is not
        # ``None`` (the ternary below) — asserted rather than re-checked so a
        # future call site that got that wrong fails loudly instead of typing
        # around it.
        assert alpaca_runtime is not None
        assert run_device_commands is not None
        client = alpaca_runtime.client
        return AlpacaCapturePort(
            run_id=run_id,
            wide_camera=client.device("camera", 1),
            companion_enabled=settings.capture.wide_companion_enabled,
            device_commands=run_device_commands,
            camera=client.device("camera"),
            telescope=client.device("telescope"),
            filterwheel=client.device("filterwheel"),
            focuser=client.device("focuser"),
            repo=capture_repo,
            settings=settings,
            state=state,
            recenter=_recenter_and_solve,
            reset_live_stack=capture.reset_scheduled_live_stack,
            add_live_stack_frame=capture.queue_scheduled_live_stack_frame,
            stop_live_stack=capture.stop_scheduled_live_stack,
            restore_live_stack=capture.restore_scheduled_live_stack,
            dither=capture.dither_scheduled,
            recover_dither=capture.recover_scheduled_dither,
            recover_operation=lambda level, operation: capture.recover_scheduled_operation(
                level=level, operation=operation, ensure_link=device.ensure_ready,
            ),
            before_capture=capture.prepare_scheduled_capture,
            begin_capture=capture.begin_scheduled_exposure,
            update_capture_stage=capture.update_scheduled_exposure_stage,
            complete_capture=capture.complete_scheduled_exposure,
            fail_capture=capture.fail_scheduled_exposure,
            focus_quality=capture.scheduled_focus_quality,
            metadata_capabilities=capture.metadata_capabilities,
            set_dither_center=capture.set_dither_center,
            ephemeris=astro_service.ephemeris,
        )

    async def _connect_for_dawn_safety() -> None:
        # A fresh working connection needs no replacement. At the priority
        # cutoff, reconnection after an outage is explicitly owner-authorized.
        if state.snapshot.link.status is not LinkStatus.READY:
            await device.connect_preferred()

    runs = RunService(
        runs=RunRepository(handle),
        plans=plans,
        state=state,
        tasks=tasks,
        astro=astro_service,
        weather=weather_service,
        settings=settings,
        device=run_device_commands,
        events=(
            None if alpaca_runtime is None else AlpacaDeviceEvents(alpaca_runtime.client, state)
        ),
        capture_factory=None if alpaca_runtime is None else _capture_port_factory,
        operation_recovery_active=lambda: capture.scheduled_recovery_active,
        dawn_safety_enabled=run_device_commands is not None,
        safety_prepare=lambda: capture.prepare_dawn_safety(),
        safety_reconnect=_connect_for_dawn_safety,
    )
    # A run the previous process left unfinished is surfaced as interrupted,
    # with a resume action, before the first client can ask (DESIGN.md 4.2).

    dither_pixel_scale_arcsec: float | None = None
    if settings.device.hardware_profile_id is not None:
        active_profile = astro_service.profile(settings.device.hardware_profile_id)
        try:
            dither_pixel_scale_arcsec = float(
                astro_service.geometry(active_profile.id, "tele").pixel_scale_arcsec
            )
        except KeyError:
            _log.warning(
                "scope profile %s has no tele optic; capture dithering is unavailable",
                active_profile.id,
            )

    async def _solve_dither_probe() -> PlateSolveResult:
        # Dither owns the main camera here. Use the existing temporary solver
        # acquisition, never the science catalogue or live-stack ingress.
        assert alpaca_runtime is not None
        await capture.prepare_main_capture()
        camera = alpaca_runtime.client.device("camera")
        try:
            return await polar_alignment.solve_here(exposure_s=2.0)
        except (Exception, asyncio.CancelledError):
            # A durable Stop/window expiry can interrupt StartExposure or image
            # transfer. Do not release ownership until the camera is idle.
            try:
                await _stop_dither_probe_camera(camera)
            except Exception as cleanup_error:
                raise CaptureDitherProbeCleanupError(
                    f"diagnostic camera idle confirmation failed: {cleanup_error}"
                ) from cleanup_error
            raise

    async def _cleanup_dither_probe() -> None:
        assert alpaca_runtime is not None
        await _stop_dither_probe_camera(alpaca_runtime.client.device("camera"))

    capture = CaptureService(
        camera=None if alpaca_runtime is None else alpaca_runtime.client.device("camera"),
        wide_camera=(None if alpaca_runtime is None else alpaca_runtime.client.device("camera", 1)),
        telescope=None if alpaca_runtime is None else alpaca_runtime.client.device("telescope"),
        filterwheel=(
            None if alpaca_runtime is None else alpaca_runtime.client.device("filterwheel")
        ),
        focuser=None if alpaca_runtime is None else alpaca_runtime.client.device("focuser"),
        repo=capture_repo,
        settings=settings,
        state=state,
        tasks=tasks,
        section=capture_section,
        # Dithers are absolute J2000 positions around CaptureService's fixed
        # sequence center. The mount-frame conversion therefore follows the
        # same verified path as every other catalogue slew; the unsupported
        # native ``ssc.set_dither`` action is not involved.
        dither=None if run_device_commands is None else run_device_commands.slew_to,
        dither_abort=None if run_device_commands is None else device.mount_stop,
        dither_recovery_recorder=runs.record_dither_tracking_recovery,
        solve_dither_probe=None if alpaca_runtime is None else _solve_dither_probe,
        cleanup_dither_probe=None if alpaca_runtime is None else _cleanup_dither_probe,
        dither_pixel_scale_arcsec=dither_pixel_scale_arcsec,
        recenter=None if run_device_commands is None else _recenter_and_solve,
        scheduled_targets=runs.live_stack_targets,
        ephemeris=astro_service.ephemeris,
        auxiliary_camera_busy=lambda: (
            polar_alignment._section.status == "running"
            or polar_alignment._section.refinement.status == "running"
            or (isinstance(device_gateway, AlpacaDeviceGateway)
                and device_gateway._autofocus.state.status == "running")
        ),
    )
    await runs.startup()

    async def _prepare_pa_camera() -> None:
        if (isinstance(device_gateway, AlpacaDeviceGateway)
                and device_gateway._autofocus.state.status == "running"):
            raise RuntimeError("Autofocus owns the camera; polar alignment cannot start")
        await capture.prepare_auxiliary_capture()

    async def _prepare_af_camera() -> None:
        if (polar_alignment._section.status == "running"
                or polar_alignment._section.refinement.status == "running"):
            raise AutofocusError("Polar alignment owns the camera; autofocus cannot start")
        await capture.prepare_auxiliary_capture()

    polar_alignment.before_acquisition = _prepare_pa_camera
    if isinstance(device_gateway, AlpacaDeviceGateway):
        device_gateway._autofocus.before_acquisition = _prepare_af_camera
    runs.set_calibration_count_updater(capture.update_active_calibration_count)
    async def _cancel_scheduled_calibration() -> None:
        # The batch can finish while the durable Stop is being appended.
        if capture.section.calibration.status == "running":
            await capture.cancel_calibration()

    runs.set_calibration_canceller(_cancel_scheduled_calibration)

    async def _run_scheduled_calibration(**options: object) -> None:
        run_id = options.get("run_id")
        block_id = options.get("block_id")
        if not isinstance(run_id, str) or not isinstance(block_id, str):
            raise DeviceCommandError("scheduled calibration requires durable run and item identity")
        async def continue_guard() -> bool:
            return await runs.scheduled_calibration_permitted(run_id)
        frame_type = str(options.get("frame_type", ""))
        if frame_type not in {"bias", "dark", "flat"}:
            raise DeviceCommandError(f"unsupported scheduled calibration type {frame_type!r}")
        filter_positions: list[int] = []
        filter_name = options.get("filter_name")
        if frame_type == "flat":
            if alpaca_runtime is None or not isinstance(filter_name, str):
                raise DeviceCommandError("scheduled flats require a named filter")
            names = await alpaca_runtime.client.device("filterwheel").get("names")
            labels = [str(name) for name in names] if isinstance(names, list) else []
            folded = [label.casefold() for label in labels]
            if filter_name.casefold() not in folded:
                raise DeviceCommandError(
                    f"filter {filter_name!r} is not one of the wheel slots {labels}"
                )
            filter_positions = [folded.index(filter_name.casefold())]
        configured_count = options.get("count")
        if configured_count is not None and (
            isinstance(configured_count, bool) or not isinstance(configured_count, int)
        ):
            raise DeviceCommandError("scheduled calibration count must be an integer")
        configured_exposure = options.get("exposure_sec")
        if configured_exposure is not None and (
            isinstance(configured_exposure, bool)
            or not isinstance(configured_exposure, int | float)
        ):
            raise DeviceCommandError("scheduled calibration exposure must be numeric")
        configured_gain = options.get("gain")
        if configured_gain is not None and (
            isinstance(configured_gain, bool) or not isinstance(configured_gain, int)
        ):
            raise DeviceCommandError("scheduled calibration gain must be an integer")
        await capture.start_calibration(
            kind=frame_type,  # type: ignore[arg-type]
            count=50 if configured_count is None else int(configured_count),
            duration_s=(
                float(configured_exposure)
                if frame_type == "dark" and configured_exposure is not None
                else None
            ),
            gain=configured_gain,
            filter_positions=filter_positions,
            optic_id=None if options.get("optic_id") is None else str(options["optic_id"]),
            flat_source=(
                options.get("flat_source")  # type: ignore[arg-type]
                if frame_type == "flat"
                else None
            ),
            generate_master=bool(options.get("generate_master", True)),
            keep_only_master=False,
            run_id=run_id,
            block_id=block_id,
            continue_guard=continue_guard,
        )
        await capture.wait_for_calibration()
        if capture.section.calibration.status != "completed":
            raise DeviceCommandError(
                capture.section.calibration.error
                or capture.section.calibration.message
                or "scheduled calibration failed"
            )

    if run_device_commands is not None:
        run_device_commands.set_calibration_runner(_run_scheduled_calibration)

    def _apply_capture_profile(profile: Profile | None) -> None:
        if profile is None:
            capture.set_dither_pixel_scale_arcsec(None)
            return
        try:
            scale = float(astro_service.geometry(profile.id, "tele").pixel_scale_arcsec)
        except KeyError:
            scale = None
        capture.set_dither_pixel_scale_arcsec(scale)

    device.set_profile_listener(_apply_capture_profile)
    capture.start_stream_automation()
    return Services(
        config=config,
        database=handle,
        settings=settings,
        secrets=secrets_store,
        state=state,
        auth=auth,
        device=device,
        astro=astro_service,
        weather=weather_service,
        plans=plans,
        skymap=skymap,
        cutouts=cutouts,
        bulk_cutouts=bulk_cutouts,
        export_import=export_import,
        runs=runs,
        capture=capture,
        polar_alignment=polar_alignment,
        target_centering=target_centering,
        heater_automation=heater_automation,
        tasks=tasks,
        object_info=object_info,
        operations=operations,
        limiters={
            # Deliberately mean: a discovery sweep or a probe reaches the device,
            # and firmware 8.46 drops its control port when it is unhappy.
            "device": RateLimiter(rate_per_second=0.5, burst=4),
            "auth": RateLimiter(rate_per_second=0.2, burst=5),
            "bug-reports": RateLimiter(rate_per_second=0.1, burst=3),
            "diagnostics": RateLimiter(rate_per_second=2.0, burst=6),
            "secrets": RateLimiter(rate_per_second=1.0, burst=5),
            # Starting/cancelling a pack download or clearing a survey: a
            # stuck retry loop must not spam the archive.
            "skymap": RateLimiter(rate_per_second=0.5, burst=5),
            # Run controls reach the mount (a stop parks it): a stuck retry
            # loop must not spam the executor's control queue (DESIGN.md 9).
            "runs": RateLimiter(rate_per_second=1.0, burst=10),
            # The Mount tab's occasional controls (slew/sync/tracking/park/
            # unpark/find-home) — deliberately mean, same reasoning as
            # "device": these reach the mount directly.
            "mount": RateLimiter(rate_per_second=1.0, burst=5),
            # MoveAxis alone: a held-down jog button resends this repeatedly
            # and has to stay responsive, so its own more generous bucket
            # keeps a jog session from starving — or being starved by — the
            # occasional controls above. Still bounded: a stuck retry loop
            # must not turn into a hot loop against the mount (DESIGN.md 9).
            "mount-jog": RateLimiter(rate_per_second=8.0, burst=16),
            # The focuser panel's controls (move/halt/autofocus) — same
            # reasoning as "mount": these reach the device directly.
            "focuser": RateLimiter(rate_per_second=1.0, burst=5),
            # Position is polled on a 2s interval while the panel is mounted
            # (``web/src/api/focuser.ts``'s own ``POSITION_POLL_MS``) — a read,
            # not a command, so a more generous bucket than "focuser" above,
            # but still bounded: a stuck retry loop must not turn a poll into
            # a hot loop against the device (DESIGN.md 9).
            "focuser-read": RateLimiter(rate_per_second=2.0, burst=6),
            # Filter-wheel state is polled while the Capture panel is mounted;
            # slot changes use their own deliberately mean command bucket.
            "filterwheel": RateLimiter(rate_per_second=1.0, burst=5),
            "filterwheel-read": RateLimiter(rate_per_second=2.0, burst=6),
            "heater": RateLimiter(rate_per_second=1.0, burst=5),
            "heater-read": RateLimiter(rate_per_second=2.0, burst=6),
            # A DSO glyph click can fan out to three upstream hosts
            # (Wikipedia, Wikidata, Commons) plus an image fetch; a stuck
            # frontend retry loop must not turn one click into a hot loop
            # against all three (DESIGN.md 9).
            "objectinfo": RateLimiter(rate_per_second=1.0, burst=5),
            # Starting an exposure reaches the camera directly, same
            # reasoning as "mount": a stuck retry loop must not spam
            # StartExposure (DESIGN.md 9).
            "capture": RateLimiter(rate_per_second=0.5, burst=4),
        },
        alpaca_close=None if alpaca_runtime is None else alpaca_runtime.aclose,
    )


def create_app(
    config: AppConfig | None = None,
    *,
    gateway: DeviceGateway | None = None,
    database: Database | None = None,
    alpaca_runtime: AlpacaRuntime | None = None,
    tls_material: TlsMaterial | None = None,
) -> FastAPI:
    """Build the app. Tests pass a fake gateway and an in-memory database, or
    an emulator-backed ``alpaca_runtime`` when the driver itself is under test.

    ``tls_material`` is the already-resolved cert/key pair (:func:`ssc.settings.
    tls.ensure_tls_material`) when ``config.tls_enabled`` — it is not resolved
    here so that a caller controls exactly when certificate generation happens
    (``run()`` does it once, before uvicorn is configured with the same paths).
    It is used only to decide whether ``GET /ca.crt`` has a CA certificate to
    serve, and to raise the CA-expiry startup notice.
    """
    resolved = config or AppConfig()

    @contextlib.asynccontextmanager
    async def service_lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = await build_services(
            resolved,
            gateway=gateway,
            database=database,
            alpaca_runtime=alpaca_runtime,
            tls_material=tls_material,
        )
        app.state.services = services
        purged = await services.auth.purge_expired()
        if purged:
            _log.info("purged %d expired session(s)", purged)
        # The only durable source of the pairing token is this line. GET
        # /api/auth/pairing is gated to a loopback caller or an existing
        # session (ssc/api/routes/auth.py) precisely so an unpaired LAN device
        # can never read it — but bound to a single LAN address (rather than
        # the wildcard), uvicorn listens on *that* address only, so nothing is
        # listening on loopback either, and the QR the desktop shell would
        # render is unobtainable. An operator with console access on the host
        # must always be able to get the token; the console is that path.
        _log.info("pairing URL: %s", services.auth.pairing_url(resolved.origin))
        advertiser = MdnsAdvertiser(resolved) if resolved.mdns_enabled else None
        if advertiser is not None:
            await advertiser.start()
        try:
            yield
        finally:
            if advertiser is not None:
                await advertiser.stop()
            services.device.close()
            services.astro.close()
            await services.weather.aclose()
            await services.capture.aclose()
            await services.tasks.cancel_all()
            if isinstance(services.device._gateway, AlpacaDeviceGateway):
                await services.device._gateway._autofocus.flush_persistence()
            await services.polar_alignment.flush_persistence()
            await services.skymap.aclose()
            await services.cutouts.aclose()
            await services.object_info.aclose()
            if services.alpaca_close is not None:
                await services.alpaca_close()
            services.state.close()
            if database is None:
                await services.database.aclose()
            app.state.services = None

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from ssc.api.diagnostics import collect_diagnostics

        with collect_diagnostics(resolved.data_dir / "diagnostics",
                                 enabled=resolved.beta_diagnostics, console_level=resolved.log_level):
            async with service_lifespan(app):
                yield

    app = FastAPI(
        title="Photon Commander",
        version="0",
        summary="Local control and planning for the ZWO Seestar S30 Pro.",
        lifespan=lifespan,
    )
    from ssc.api.diagnostics import DiagnosticRequests
    app.add_middleware(DiagnosticRequests)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(resolved),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    # A private Serve origin is admitted only from a loopback proxy and only
    # after the local CLI confirms this exact service has no public Funnel.
    remote_cache: tuple[float, str | None] = (0.0, None)
    remote_lock = asyncio.Lock()

    async def private_proxy_origin() -> str | None:
        nonlocal remote_cache
        async with remote_lock:
            if time.monotonic() >= remote_cache[0]:
                from ssc.remote_access import inspect_tailscale
                remote = await asyncio.to_thread(inspect_tailscale, resolved.port, host=resolved.host)
                remote_cache = (time.monotonic() + 5, remote.url if remote.state == "serving" else None)
            return remote_cache[1]

    app.add_middleware(BrowserOriginMiddleware, allowed_origins=_allowed_origins(resolved),
                       private_proxy_origin=private_proxy_origin)
    app.add_middleware(TransportProxyMiddleware)
    app.include_router(api_router)
    _document_ws_frames(app)
    if tls_material is not None and tls_material.ca_cert_path is not None:
        _mount_ca_certificate_route(app, tls_material.ca_cert_path)

    static_dir = resolved.static_dir
    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app


def _document_ws_frames(app: FastAPI) -> None:
    """Publish the WS frame union in the OpenAPI document.

    FastAPI documents REST only, so the ``/ws`` contract would otherwise be
    invisible to the generator that produces the TypeScript client — and a
    hand-written client is a client that drifts.
    """
    base = app.openapi

    def openapi() -> dict[str, Any]:
        schema = base()
        if "x-ssc-ws-frame" in schema:
            return schema
        root, definitions = frame_schemas()
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.update(definitions)
        schema["x-ssc-ws-frame"] = root
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def _mount_ca_certificate_route(app: FastAPI, ca_cert_path: Path) -> None:
    """``GET /ca.crt``: the one artefact a phone needs to fetch and install to
    stop seeing a certificate warning (``ssc.settings.tls`` module docstring,
    ``docs/DESIGN.md`` §9). Deliberately unauthenticated — a device that has
    never paired has no session yet, and the CA certificate is not a secret:
    it lets a device *verify* SSC, not control it.
    """

    # Read once at mount time, not per request: this is a small, rarely
    # rotated file, and re-reading it synchronously inside an async handler
    # on every request would block the event loop for no benefit.
    ca_cert_bytes = ca_cert_path.read_bytes()

    @app.get("/ca.crt", include_in_schema=False)
    async def _ca_certificate() -> Response:
        return Response(
            content=ca_cert_bytes,
            media_type="application/x-x509-ca-cert",
            headers={"Content-Disposition": "attachment; filename=ssc-local-ca.crt"},
        )


def create_http_bootstrap_app(config: AppConfig, ca_cert_path: Path | None) -> FastAPI:
    """Serve local-CA enrollment over HTTP, then move browsers to HTTPS.

    A local CA cannot be trusted until the phone has downloaded it, so an
    HTTPS-only appliance creates a setup dead end. This deliberately tiny
    companion has no API routes or static app: ``/ca.crt`` is the one public
    enrollment artifact, and everything else is a permanent redirect to the
    same path/query on the secure listener.
    """

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    if ca_cert_path is not None:
        _mount_ca_certificate_route(app, ca_cert_path)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _redirect_to_https(request: Request, path: str) -> RedirectResponse:
        candidate = request.url.hostname
        safe_host = config.advertised_host
        if candidate == "localhost":
            safe_host = candidate
        elif candidate is not None:
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                pass
            else:
                if address.is_private or address.is_loopback:
                    safe_host = candidate
        authority_host = f"[{safe_host}]" if ":" in safe_host else safe_host
        authority = authority_host if config.port == 443 else f"{authority_host}:{config.port}"
        query = "" if request.url.query == "" else f"?{request.url.query}"
        return RedirectResponse(
            url=f"https://{authority}{request.url.path}{query}",
            status_code=308,
        )

    return app


def _allowed_origins(config: AppConfig) -> list[str]:
    """CORS locked to the origins this service is actually served from.

    Uses ``config.scheme``, not a hardcoded ``http://`` — with TLS on, every
    real request arrives with an ``https://`` ``Origin`` header, and a CORS
    allowlist still built from ``http://`` would reject all of them.
    """
    hosts = (
        {"localhost", "127.0.0.1", *local_ipv4_addresses()}
        if config.host in WILDCARD_HOSTS
        else {config.host}
    )
    if config.host in WILDCARD_HOSTS:
        # Preserve the appliance's existing LAN/mDNS URLs without trusting a
        # name supplied by a request or reverse DNS lookup.
        hostname = socket.gethostname().rstrip(".").lower()
        hosts |= {hostname, f"{hostname}.local", _mdns_server_name(config.instance_name, hostname).rstrip(".")}
    if config.host not in WILDCARD_HOSTS:
        hosts |= {"localhost", "127.0.0.1"}
    authorities = {f"[{host}]" if ":" in host else host for host in hosts}
    return sorted({*(f"{config.scheme}://{host}:{config.port}" for host in authorities), *config.trusted_browser_origins})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ssc-core", description="Photon Commander")
    parser.add_argument("--host", help="address to bind (loopback or RFC 1918 only)")
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-mdns", action="store_true", help="do not advertise _ssc._tcp")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and the bind address, then exit",
    )
    return parser.parse_args(argv)


def load_config(argv: Sequence[str] | None = None) -> AppConfig:
    args = _parse_args(argv)
    overrides: dict[str, object] = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.data_dir is not None:
        overrides["data_dir"] = args.data_dir
    if args.no_mdns:
        overrides["mdns_enabled"] = False
    return AppConfig(**overrides)  # type: ignore[arg-type]


def run(argv: Sequence[str] | None = None) -> int:
    """The ``ssc-core`` console script."""
    args = _parse_args(argv)
    config = load_config(argv)
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    # The device gateway polls ssc.status once a second, and httpx logs every
    # request/response pair at INFO — one "HTTP Request: ... 200 OK" line per
    # second, forever, burying anything real (including our own
    # state-transition logs, which DESIGN.md 4.2 needs a human to be able to
    # find the next morning). Quieten httpx's own logger only; our loggers and
    # genuine httpx errors/warnings are untouched.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        check_bind_address(config.host)
    except BindRefusedError as exc:
        print(f"ssc-core: {exc}", file=sys.stderr)  # noqa: T201 - the message is the point
        return 2

    if args.check_config:
        # Bounded and cheap enough to run inline: this is exactly the command
        # an operator runs to validate a headless install before walking away
        # from it, so it must say the true state of the secret store rather
        # than print "OK" over a keyring that will throw on the first weather
        # API key read (architecture policy invariant 7).
        backend_status = asyncio.run(probe_backend())
        secrets_line = (
            f"  secrets     ok ({backend_status.backend})"
            if backend_status.available
            else f"  secrets     UNAVAILABLE ({backend_status.backend}) — {backend_status.reason}"
        )
        # --check-config runs before the settings store is opened, so it can
        # only report the environment. Say so rather than implying a stored
        # contact is absent when it may simply not have been read yet.
        weather_line = (
            "  weather     ok (Met.no contact from SSC_WEATHER_CONTACT)"
            if config.weather_contact
            else (
                "  weather     no Met.no contact in the environment — "
                "a contact stored in Settings > Weather would still apply"
            )
        )
        if config.tls_enabled:
            try:
                material = ensure_tls_material(
                    tls_dir=config.tls_dir,
                    user_cert_path=config.tls_cert_path,
                    user_key_path=config.tls_key_path,
                )
            except TlsConfigError as exc:
                tls_line = f"  tls         UNAVAILABLE — {exc}"
            else:
                tls_line = (
                    f"  tls         on, leaf expires {material.leaf_expires_at.isoformat()}"
                    + (
                        f", CA {material.ca_cert_path} expires {material.ca_expires_at.isoformat()}"
                        if material.ca_cert_path is not None and material.ca_expires_at is not None
                        else " (operator-supplied certificate)"
                    )
                )
        else:
            tls_line = "  tls         off (SSC_TLS_ENABLED=1 to serve HTTPS)"
        print(  # noqa: T201 - --check-config exists to print
            f"ssc-core: configuration OK\n"
            f"  bind        {config.host}:{config.port}\n"
            f"  data dir    {config.data_dir}\n"
            f"  database    {config.database_path}\n"
            f"  mDNS        {'on' if config.mdns_enabled else 'off'}\n"
            f"  static      {config.static_dir or '(none)'}\n"
            f"  tiles       {config.tiles_dir} (budget {config.tiles_max_bytes // 1024**2} MiB)\n"
            f"  cutouts     {config.cutouts_dir} "
            f"(budget {config.cutouts_max_bytes // 1024**2} MiB)\n"
            f"{secrets_line}\n"
            f"{weather_line}\n"
            f"{tls_line}"
        )
        return 0

    tls_material: TlsMaterial | None = None
    if config.tls_enabled:
        try:
            tls_material = ensure_tls_material(
                tls_dir=config.tls_dir,
                user_cert_path=config.tls_cert_path,
                user_key_path=config.tls_key_path,
            )
        except TlsConfigError as exc:
            print(f"ssc-core: {exc}", file=sys.stderr)  # noqa: T201 - the message is the point
            return 2

    tls_kwargs: dict[str, Any] = (
        {}
        if tls_material is None
        else {
            "ssl_certfile": str(tls_material.cert_path),
            "ssl_keyfile": str(tls_material.key_path),
        }
    )
    app = create_app(config, tls_material=tls_material)
    redirect_server: uvicorn.Server | None = None
    redirect_thread: threading.Thread | None = None
    if tls_material is not None and config.tls_http_redirect_port is not None:
        if config.tls_http_redirect_port == config.port:
            print("ssc-core: TLS and HTTP redirect ports must be different", file=sys.stderr)  # noqa: T201
            return 2
        redirect_server = uvicorn.Server(
            uvicorn.Config(
                create_http_bootstrap_app(config, tls_material.ca_cert_path),
                host=config.host,
                port=config.tls_http_redirect_port,
                log_level=config.log_level.lower(),
            )
        )
        redirect_thread = threading.Thread(
            target=redirect_server.run,
            name="ssc-http-bootstrap",
            daemon=True,
        )
        redirect_thread.start()
        deadline = time.monotonic() + 5
        while redirect_thread.is_alive() and not redirect_server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not redirect_server.started:
            print(  # noqa: T201
                f"ssc-core: HTTP bootstrap listener failed on {config.host}:{config.tls_http_redirect_port}",
                file=sys.stderr,
            )
            redirect_server.should_exit = True
            redirect_thread.join(timeout=1)
            return 2
    try:
        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_level=config.log_level.lower(),
            proxy_headers=False,  # transport peer must be saved by our outer middleware first
            **tls_kwargs,
        )
    finally:
        if redirect_server is not None and redirect_thread is not None:
            redirect_server.should_exit = True
            redirect_thread.join(timeout=5)
    return 0


def main() -> None:  # pragma: no cover - console-script shim
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
