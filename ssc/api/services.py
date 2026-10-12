# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Device orchestration: discovery, probing, connection, and the inbound sink.

The routers stay thin because this is where the sequencing lives. It talks to
the device only through :class:`~ssc.state.ports.DeviceGateway`, so it never
imports the wire protocol (CLAUDE.md invariant 1) and a test substitutes a fake
gateway without a socket.

Two behaviours worth stating because they are easy to get wrong:

* **One scan at a time, and it is cancellable.** A second request while a scan
  runs is a conflict, not a silent second sweep of the neighbour's network.
* **A probe result is recorded either way.** Reachable or not, the attempt goes
  into the address book; only a reachable one is promoted to the remembered
  address (CLAUDE.md invariant 11 — remembered, never defaulted).

This is also where the **auto-connect supervisor** lives — connecting to the
last-good address on startup, and re-connecting on its own after an unexpected
drop. It sits strictly above the Alpaca seam: it only ever decides *whether and
when* to call :meth:`DeviceService.connect` again, never how the transport
itself retries — that policy, with its own jittered capped backoff, already
lives in ``ssc/seestar/session.py`` and is not this module's to duplicate
(``docs/M0-FINDINGS.md``). An explicit ``disconnect()`` call is remembered as
intent (:attr:`DeviceService._suspended`) so the supervisor never fights a user
who asked to be disconnected.

**The J2000 <-> mount-frame boundary lives here too**, both directions
(``ssc.astro.frames``, landed 2026-09-10). ``ssc/main.py``'s
``AlpacaDeviceGateway`` stays a thin ASCOM client — it reads and writes exactly
what it is given, in whatever frame that is — so the conversion sits in the one
layer both directions already pass through:

* **Inbound** (:meth:`DeviceService.on_mount`): every fresh
  :class:`~ssc.state.models.MountState` is given its J2000
  (:attr:`~ssc.state.models.MountState.ra_j2000_h`/``dec_j2000_deg``) before it
  reaches the snapshot, so a client never has to know the mount's own frame to
  draw where it is pointing.
* **Outbound** (:meth:`DeviceService.mount_slew`/``mount_sync``): a J2000
  target is converted into the mount's own declared frame before it reaches
  the gateway. Getting this direction backwards is worse than skipping it —
  see :func:`_to_mount_frame`'s docstring — so it is the one covered by an
  explicit direction-sensitive test (``tests/api/test_mount.py``).

Both directions refuse rather than guess (:class:`~ssc.state.ports.MountRefusedError`
outbound, ``None`` inbound) whenever the mount's frame, its own reported site,
or the ephemeris kernel is not yet known — CLAUDE.md invariant 7, and
frames.py's own point that a wrong sync is durable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from ssc.api.astro_service import AstroService
from ssc.api.mount_target import MountFrameUnavailableError, resolve_mount_target
from ssc.api.tasks import TaskRegistry
from ssc.astro.frames import (
    EquatorialCoord,
    FrameInversionError,
    MountFrame,
    UnsupportedMountFrameError,
    mount_site,
    to_j2000,
)
from ssc.astro.framing import Profile
from ssc.astro.units import Deg, Hours, hours_to_deg
from ssc.settings.models import DeviceSettings
from ssc.settings.store import SettingsStore
from ssc.state.events import (
    AutoConnectChanged,
    CameraFovChanged,
    DeviceChanged,
    DeviceEventReceived,
    DevicesChanged,
    DiscoveryChanged,
    LinkChanged,
    MountChanged,
    SettingsChanged,
)
from ssc.state.models import (
    AlpacaDevicesState,
    AutoConnectState,
    AutofocusState,
    CameraFieldOfView,
    DeviceDescriptor,
    DeviceEventRecord,
    DiscoveryState,
    LinkState,
    LinkStatus,
    MountAxis,
    MountState,
    MountTrackingRate,
)
from ssc.state.ports import DeviceGateway, DeviceGatewayError, MountRefusedError
from ssc.state.store import StateStore

_log = logging.getLogger(__name__)

DEFAULT_DISCOVERY_TIMEOUT_S: Final = 8.0
DEFAULT_PROBE_TIMEOUT_S: Final = 3.0
FLAT_POSITION_TIMEOUT_S: Final = 60.0
FLAT_POSITION_POLL_S: Final = 0.1
FLAT_POSITION_MIN_ALT_DEG: Final = 89.5

#: The supervisor's own backoff defaults — see ``AppConfig.reconnect_initial_delay_s``
#: docstring for why these are distinct from the transport-level policy.
DEFAULT_RECONNECT_INITIAL_DELAY_S: Final = 2.0
DEFAULT_RECONNECT_MAX_DELAY_S: Final = 60.0

#: Link states the supervisor leaves alone: already under way, already
#: connected (``READY``), or connected with a known degradation. ``DEGRADED``
#: means the control channel is up but some capability is not (session.py's
#: own ``READY <-> DEGRADED`` loop) — not a dropped link, so reconnecting would
#: not help and would only get in the transport layer's way.
_NOT_DISCONNECTED: Final = frozenset(
    {LinkStatus.CONNECTING, LinkStatus.HANDSHAKING, LinkStatus.READY, LinkStatus.DEGRADED}
)

#: Progress is published at most this many times per scan, so a 254-host sweep
#: does not push 254 events at a phone on the far side of the garden.
PROGRESS_STEPS: Final = 20
JOG_LEASE_S: Final = 1.25


class DeviceBusyError(Exception):
    """A scan is already running."""


class DeviceService:
    """Everything the Settings tab's Scope section needs, in one place."""

    def __init__(
        self,
        gateway: DeviceGateway,
        state: StateStore,
        settings: SettingsStore,
        tasks: TaskRegistry,
        astro: AstroService,
        *,
        discovery_timeout_s: float = DEFAULT_DISCOVERY_TIMEOUT_S,
        probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
        reconnect_initial_delay_s: float = DEFAULT_RECONNECT_INITIAL_DELAY_S,
        reconnect_max_delay_s: float = DEFAULT_RECONNECT_MAX_DELAY_S,
    ) -> None:
        self._gateway = gateway
        self._state = state
        self._settings = settings
        self._tasks = tasks
        #: The loaded ephemeris kernel (if any) the J2000 <-> mount-frame
        #: conversion needs — shared with the Planning tab rather than a
        #: second kernel load (``ssc.api.astro_service``'s own docstring:
        #: "construct once, share freely").
        self._astro = astro
        self._discovery_timeout_s = discovery_timeout_s
        self._probe_timeout_s = probe_timeout_s
        self._scan: asyncio.Task[None] | None = None
        self._scan_id: str | None = None
        self._detach = gateway.attach(self)

        # -- auto-connect supervisor ----------------------------------------- #
        self._reconnect_initial_delay_s = reconnect_initial_delay_s
        self._reconnect_max_delay_s = reconnect_max_delay_s
        self._rng = random.Random()
        self._auto_connect_enabled = settings.device.auto_connect
        #: Set by an explicit ``disconnect()`` call; cleared by an explicit
        #: ``connect()`` or by turning auto-connect back on. Never inferred
        #: from ``LinkState`` — see the module docstring.
        self._connection_generation = 0
        self._suspended = False
        self._link_status = LinkStatus.DISCONNECTED
        self._last_successful_endpoint: tuple[str, int] | None = (
            (settings.device.last_healthy_host, settings.device.last_healthy_control_port)
            if settings.device.last_healthy_host is not None
            and settings.device.last_healthy_control_port is not None else None
        )
        self._reconnect_attempt = 0
        self._wake = asyncio.Event()
        self._last_auto_connect = AutoConnectState(enabled=self._auto_connect_enabled)
        self._supervisor_task: asyncio.Task[None] | None = None
        self._profile_listener: Callable[[Profile | None], None] = lambda _profile: None
        self._jog_deadlines: dict[MountAxis, float] = {}
        self._jog_tasks: dict[MountAxis, asyncio.Task[None]] = {}
        self._jog_lease_s = JOG_LEASE_S

    def close(self) -> None:
        self._detach()

    def set_profile_listener(self, listener: Callable[[Profile | None], None]) -> None:
        """Attach the runtime consumer of model-specific capture geometry."""
        self._profile_listener = listener

    def start_supervisor(self) -> None:
        """Begin the auto-connect supervisor as a task owned by ``TaskRegistry``.

        Idempotent, and deliberately not called from ``__init__`` — the caller
        (``ssc.main.build_services``) decides when the app is far enough along
        to start trying, and this never blocks whoever calls it: the first
        connect attempt, if any, happens inside the spawned task.
        """
        if self._supervisor_task is None:
            self._supervisor_task = self._tasks.spawn(self._supervise(), name="device-auto-connect")

    @property
    def default_control_port(self) -> int:
        """Asked of the device layer rather than duplicated up here."""
        return self._gateway.default_control_port

    # -- discovery ---------------------------------------------------------- #

    @property
    def scanning(self) -> bool:
        return self._scan is not None and not self._scan.done()

    def start_discovery(self, *, timeout_s: float | None = None) -> str:
        """Begin a bounded scan. Returns the scan id; raises if one is running."""
        if self.scanning:
            raise DeviceBusyError("a discovery scan is already running")
        scan_id = uuid.uuid4().hex
        self._scan_id = scan_id
        self._state.apply(
            DiscoveryChanged(
                discovery=DiscoveryState(
                    running=True,
                    scan_id=scan_id,
                    started_at=datetime.now(tz=UTC),
                )
            )
        )
        self._scan = self._tasks.spawn(
            self._run_discovery(scan_id, timeout_s or self._discovery_timeout_s),
            name=f"discovery-{scan_id}",
        )
        return scan_id

    async def cancel_discovery(self) -> bool:
        """Cancel the running scan. Returns whether there was one."""
        scan = self._scan
        if scan is None or scan.done():
            return False
        scan.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scan
        return True

    async def _run_discovery(self, scan_id: str, timeout_s: float) -> None:
        found: list[DeviceDescriptor] = []
        started_at = datetime.now(tz=UTC)
        step = 0

        def on_progress(completed: int, total: int) -> None:
            nonlocal step
            stride = max(1, total // PROGRESS_STEPS)
            if completed % stride and completed != total:
                return
            step += 1
            self._state.apply(
                DiscoveryChanged(
                    discovery=DiscoveryState(
                        running=True,
                        scan_id=scan_id,
                        started_at=started_at,
                        completed=completed,
                        total=total,
                        found=list(found),
                    )
                )
            )

        try:
            found = await self._gateway.discover(timeout=timeout_s, on_progress=on_progress)
        except asyncio.CancelledError:
            self._publish_scan_end(scan_id, started_at, found, cancelled=True, error=None)
            raise
        except DeviceGatewayError as exc:
            _log.warning("discovery refused: %s", exc)
            self._publish_scan_end(scan_id, started_at, found, cancelled=False, error=str(exc))
            return
        except Exception as exc:  # a failed scan is a reported result, not a crash
            _log.exception("discovery failed")
            self._publish_scan_end(
                scan_id, started_at, found, cancelled=False, error=f"{type(exc).__name__}: {exc}"
            )
            return

        for device in found:
            matched = self._astro.match_profile(device.model)
            await self._settings.remember_address(
                device,
                hardware_profile_id=None if matched is None else matched.id,
                promote=(
                    self._settings.device.host is None
                    and self._settings.device.ethernet_host is None
                ),
            )
            if device.reachable:
                self._profile_listener(matched)
        self._publish_scan_end(scan_id, started_at, found, cancelled=False, error=None)
        if found:
            self._state.apply(SettingsChanged(settings=self._settings.summary()))

    def _publish_scan_end(
        self,
        scan_id: str,
        started_at: datetime,
        found: list[DeviceDescriptor],
        *,
        cancelled: bool,
        error: str | None,
    ) -> None:
        previous = self._state.snapshot.discovery
        self._state.apply(
            DiscoveryChanged(
                discovery=DiscoveryState(
                    running=False,
                    scan_id=scan_id,
                    started_at=started_at,
                    finished_at=datetime.now(tz=UTC),
                    completed=previous.completed,
                    total=previous.total,
                    found=list(found),
                    cancelled=cancelled,
                    error=error,
                )
            )
        )

    # -- probing and connecting --------------------------------------------- #

    async def probe(
        self, host: str, *, control_port: int, timeout_s: float | None = None
    ) -> DeviceDescriptor:
        """Test one address for real, then remember it if it answered."""
        descriptor = await self._gateway.probe(
            host, control_port=control_port, timeout=timeout_s or self._probe_timeout_s
        )
        configured = self._settings.device
        await self._remember_detected_profile(
            descriptor,
            promote=configured.host is None and configured.ethernet_host is None,
        )
        if descriptor.reachable:
            self._state.apply(DeviceChanged(device=descriptor))
        return descriptor

    async def _remember_detected_profile(
        self, descriptor: DeviceDescriptor, *, promote: bool = True
    ) -> None:
        """Persist an exact live-model match and publish it to UI consumers.

        Discovery and probing used to be the only paths that identified the
        hardware profile. A direct connection performs its own live poll too,
        so treating that descriptor differently left Planning on whichever
        bundled profile happened to be first. Unknown models still flow
        through as ``None`` and therefore require the existing explicit
        operator confirmation; this method never guesses from a partial name.
        """
        before = self._settings.device
        matched = self._astro.match_profile(descriptor.model)
        await self._settings.remember_address(
            descriptor,
            hardware_profile_id=None if matched is None else matched.id,
            promote=promote,
        )
        if matched is not None and not promote:
            await self._settings.set_detected_hardware_profile(matched.id)
        if descriptor.reachable:
            self._profile_listener(matched)
        if self._settings.device != before:
            self._state.apply(SettingsChanged(settings=self._settings.summary()))

    async def set_hardware_profile(self, profile_id: str) -> DeviceSettings:
        """Persist an operator-confirmed profile and update runtime consumers."""
        profile = self._astro.profile(profile_id)
        device = await self._settings.set_hardware_profile(profile.id)
        self._state.apply(SettingsChanged(settings=self._settings.summary()))
        self._profile_listener(profile)
        return device

    async def connect(
        self, host: str, *, control_port: int, promote_remembered: bool = True,
        _generation: int | None = None
    ) -> None:
        """Connect, explicitly. Clears any earlier "stay disconnected" intent."""
        if _generation is None:
            self._connection_generation += 1
            _generation = self._connection_generation
            self._suspended = False
        self._check_connection_intent(_generation)
        self._link_status = LinkStatus.CONNECTING
        self._state.apply(
            LinkChanged(
                link=LinkState(
                    status=LinkStatus.CONNECTING,
                    endpoint=f"{host}:{control_port}",
                    changed_at=datetime.now(tz=UTC),
                )
            )
        )
        self._wake_supervisor()
        try:
            await self._gateway.connect(host, control_port=control_port)
        except Exception as exc:
            self._check_connection_intent(_generation)
            # A failed attempt must land back on DISCONNECTED, or the link
            # stays at CONNECTING for the life of the process and nothing
            # ever retries: the supervisor skips CONNECTING by design (it
            # reads as "already under way"), so a state nobody clears is a
            # deadlock, not a pause. Observed on the owner's instance
            # 2026-09-10 — the scope dropped off the network, the attempt
            # failed, and 36 minutes later `auto_connect` still said
            # enabled/not-suspended with attempt 0 and no next attempt
            # scheduled, while the link sat at CONNECTING with a null reason.
            # Same shape as the Disconnect bug fixed earlier the same day.
            self._link_status = LinkStatus.DISCONNECTED
            self._state.apply(
                LinkChanged(
                    link=LinkState(
                        status=LinkStatus.DISCONNECTED,
                        endpoint=f"{host}:{control_port}",
                        reason=f"connect failed: {exc}",
                        changed_at=datetime.now(tz=UTC),
                    )
                )
            )
            self._wake_supervisor()
            raise
        if _generation != self._connection_generation or self._suspended:
            if self._suspended:
                await self._gateway.disconnect()
            self._check_connection_intent(_generation)
        self._last_successful_endpoint = (host, control_port)
        # The real gateway's successful handshake includes one complete live
        # poll before returning, and publishes that descriptor through
        # ``on_device``. Use it here too: users can connect a typed/remembered
        # address without first running Probe or Discovery.
        connected = self._state.snapshot.device
        if connected is not None and connected.reachable and connected.host == host:
            configured = self._settings.device
            configured_wifi = (
                configured.host,
                configured.control_port or self._gateway.default_control_port,
            )
            may_promote = promote_remembered and (
                configured.host is None
                or (host, control_port) == configured_wifi
            )
            await self._remember_detected_profile(connected, promote=may_promote)
        await self._settings.remember_healthy_endpoint(host, control_port)
        # This device has no location of its own to report (module docstring
        # / ssc.astro.frames): push SSC's configured observing location onto
        # it so it has a SiteLatitude/SiteLongitude to read back, which the
        # mount-frame conversion below needs. Nothing to push is not an
        # error — the conversion simply refuses until one is configured.
        location = self._settings.active_location()
        if location is not None:
            await self._gateway.set_site(
                latitude_deg=location.latitude_deg,
                longitude_deg=location.longitude_deg,
                elevation_m=location.elevation_m,
            )

    def _check_connection_intent(self, generation: int) -> None:
        if generation != self._connection_generation or self._suspended:
            raise DeviceGatewayError("connection cancelled by newer operator intent")

    def _connection_candidates(self) -> list[tuple[str, int, str]]:
        """Prefer the live session's last healthy route, then wired and Wi-Fi."""
        device = self._settings.device
        candidates: list[tuple[str, int, str]] = []
        if device.ethernet_host is not None:
            candidates.append(
                (
                    device.ethernet_host,
                    device.ethernet_control_port or self._gateway.default_control_port,
                    "Ethernet",
                )
            )
        if device.host is not None:
            wifi = (
                device.host,
                device.control_port or self._gateway.default_control_port,
                "Wi-Fi fallback",
            )
            if not candidates or wifi[:2] != candidates[0][:2]:
                candidates.append(wifi)
        if self._last_successful_endpoint is not None:
            for index, candidate in enumerate(candidates):
                if candidate[:2] == self._last_successful_endpoint:
                    candidates.insert(0, candidates.pop(index))
                    break
        return candidates

    async def connect_preferred(self, *, control_port: int | None = None) -> None:
        """Try Ethernet then Wi-Fi, then rediscover a DHCP-moved fallback."""
        self._connection_generation += 1
        generation = self._connection_generation
        self._suspended = False
        candidates = self._connection_candidates()
        if not candidates:
            raise DeviceGatewayError(
                "no address: discover the scope or enter one before connecting"
            )
        failures: list[str] = []
        for host, configured_port, route in candidates:
            self._check_connection_intent(generation)
            port = control_port or configured_port
            try:
                await self.connect(
                    host,
                    control_port=port,
                    promote_remembered=route != "Ethernet",
                    _generation=generation,
                )
                return
            except Exception as exc:
                failures.append(f"{route} {host}:{port}: {exc}")

        self._check_connection_intent(generation)
        attempted = {(host, control_port or port) for host, port, _route in candidates}
        known = await self._settings.known_addresses()
        self._check_connection_intent(generation)
        configured_endpoints = {(host, port) for host, port, _route in candidates}
        expected_serials = {
            entry.serial
            for entry in known
            if entry.serial is not None and (entry.host, entry.control_port) in configured_endpoints
        }
        try:
            discovered = await self._gateway.discover(timeout=self._discovery_timeout_s)
        except Exception as exc:
            failures.append(f"rediscovery: {exc}")
            raise DeviceGatewayError("; ".join(failures)) from exc

        self._check_connection_intent(generation)
        reachable = [
            device
            for device in discovered
            if device.reachable
            and (device.host, device.control_port) not in attempted
            and (not expected_serials or device.serial in expected_serials)
        ]
        if not expected_serials and len(reachable) != 1:
            reachable = []
        for device in reachable:
            self._check_connection_intent(generation)
            await self._remember_detected_profile(device, promote=False)
            self._check_connection_intent(generation)
            try:
                await self.connect(
                    device.host,
                    control_port=device.control_port,
                    promote_remembered=False,
                    _generation=generation,
                )
            except Exception as exc:
                failures.append(
                    f"rediscovered fallback {device.host}:{device.control_port}: {exc}"
                )
                continue
            self._check_connection_intent(generation)
            await self.set_wifi_address(device.host, control_port=device.control_port)
            return
        raise DeviceGatewayError("; ".join(failures))

    async def ensure_ready(self) -> None:
        """Recover an unintentional outage without revoking operator Disconnect."""
        if self._suspended:
            raise DeviceGatewayError("recovery waiting: disconnected by request")
        if self._link_status is LinkStatus.READY:
            return
        await self.connect_preferred()

    async def disconnect(self) -> None:
        """Disconnect, explicitly. The supervisor must not undo this on its own.

        The ``DISCONNECTED`` link state is published here rather than left to
        ``on_link``. That callback only fires when the gateway actually tears
        a link *down*, so a disconnect issued while a connect attempt was
        still failing — no live link ever existed — produced no event at all,
        and the snapshot stayed at ``CONNECTING`` for the life of the process:
        the supervisor ignores ``CONNECTING`` by design (it looks under way),
        and the UI offered no way out, so the Disconnect button appeared dead.
        Observed against firmware 9.31, whose handshake does not complete.
        Publishing it directly mirrors what :meth:`connect` already does with
        ``CONNECTING``; a later genuine event from the gateway carries the
        same status and is harmless.
        """
        self._connection_generation += 1
        self._suspended = True
        self._link_status = LinkStatus.DISCONNECTED
        self._state.apply(
            LinkChanged(
                link=LinkState(
                    status=LinkStatus.DISCONNECTED,
                    endpoint=self._state.snapshot.link.endpoint,
                    reason="disconnected by request",
                    changed_at=datetime.now(tz=UTC),
                )
            )
        )
        self._wake_supervisor()
        await self._gateway.disconnect()

    async def mount_alignment(self) -> bool | None:
        """Live ASCOM alignment mode collapsed to EQ/alt-az, or ``None``.

        Used by the Planning tab (``ssc/api/routes/astro.py``) to warn, never
        to decide.
        """
        return await self._gateway.mount_alignment()

    async def mount_alignment_mode(self) -> int | None:
        """Live ``Telescope.AlignmentMode`` — see
        ``ssc.state.ports.DeviceGateway.mount_alignment_mode``. Used by
        ``ssc.api.polar_alignment_service.PolarAlignmentService`` to refuse
        an alt-az mount before committing to a polar-alignment run, never to decide
        anything silently on its own."""
        return await self._gateway.mount_alignment_mode()

    # -- mount control (DESIGN.md §4.4b) ------------------------------------- #
    #
    # Thin delegation to the gateway, same as everything above: the sequencing
    # and the ASCOM member choices live there, behind the seam this service
    # exists to keep ``ssc/api/routes`` from crossing. The one exception is
    # the coordinates themselves: a slew/sync target arrives here as J2000
    # (every catalogue entry, resolved body and plate solve in SSC is), and
    # ``ssc/main.py``'s gateway is a thin ASCOM client that sends exactly what
    # it is given — so converting to the mount's own declared frame has to
    # happen on this side of the seam, not that one (module docstring).

    async def mount_slew(self, *, ra_hours: float, dec_deg: float) -> None:
        device_ra_hours, device_dec_deg = self._to_mount_frame(ra_hours=ra_hours, dec_deg=dec_deg)
        await self._gateway.mount_slew(ra_hours=device_ra_hours, dec_deg=device_dec_deg)

    async def mount_slew_polar(
        self, *, ra_hours: float, dec_deg: float, fixed_mount_dec_deg: float | None
    ) -> float:
        """Acquire the field, then keep commanded declination fixed in the device frame.

        Normal GOTOs hold catalogue declination, which becomes a different device
        declination at every RA in an apparent-of-date frame. A PA circle fit
        needs constant mechanical declination. Return the first command's device
        declination so the PA owner can reuse it for every subsequent point.
        This does not claim that firmware always obeys the command perfectly.
        """
        device_ra, converted_dec = self._to_mount_frame(ra_hours=ra_hours, dec_deg=dec_deg)
        device_dec = converted_dec if fixed_mount_dec_deg is None else fixed_mount_dec_deg
        if not -90.0 <= device_dec <= 90.0:
            raise MountRefusedError(
                "polar sweep device declination must be finite and within +/-90 degrees"
            )
        await self._gateway.mount_slew(ra_hours=device_ra, dec_deg=device_dec)
        return device_dec

    async def mount_slew_zenith(self) -> None:
        """Slew to the apparent zenith and leave the mount with tracking off.

        The post-slew tracking transition is part of this server-owned action:
        a slew-capable driver may change tracking while it moves, so a separate
        client request before the slew cannot guarantee the final flat-panel
        state.
        """
        mount = self._state.snapshot.mount
        if (
            mount is None
            or mount.sidereal_time_hours is None
            or mount.site_latitude_deg is None
            or mount.site_longitude_deg is None
        ):
            raise MountRefusedError(
                "cannot calculate the zenith until the mount reports sidereal time and site latitude"
            )
        if self._astro.ephemeris is None:
            raise MountRefusedError(
                f"cannot calculate the zenith: {self._astro.ephemeris_error or 'ephemeris unavailable'}"
            )
        site = mount_site(Deg(mount.site_latitude_deg), Deg(mount.site_longitude_deg))
        instant = self._astro.ephemeris.timescale.from_datetime(datetime.now(tz=UTC))
        observer: Any = self._astro.ephemeris.observer(site)
        apparent: Any = observer.at(instant).from_altaz(alt_degrees=90.0, az_degrees=0.0)
        right_ascension, declination, _distance = apparent.radec()
        before_slew_polled_at = mount.polled_at
        await self.mount_slew(
            ra_hours=float(right_ascension.hours), dec_deg=float(declination.degrees)
        )
        await self._wait_for_flat_position(after_polled_at=before_slew_polled_at)
        tracking = await self.mount_set_tracking(enabled=False, rate=None)
        if tracking.tracking is not False:
            raise MountRefusedError(
                f"the mount reached the flat-panel position but reports tracking={tracking.tracking!r}"
            )

    async def _wait_for_flat_position(self, *, after_polled_at: datetime | None) -> None:
        """Wait for fresh telemetry proving the async zenith slew has ended.

        Requiring altitude as well as ``slewing=False`` handles both paths:
        an ordinary move cannot mistake the pre-command idle sample for
        completion, while a repeated command at zenith still completes on the
        next poll and proceeds to the mandatory tracking-off transition.
        """
        deadline = asyncio.get_running_loop().time() + FLAT_POSITION_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            mount = self._state.snapshot.mount
            if (
                mount is not None
                and not mount.stale
                and mount.polled_at is not None
                and (after_polled_at is None or mount.polled_at > after_polled_at)
                and mount.slewing is False
                and mount.alt_deg is not None
                and mount.alt_deg >= FLAT_POSITION_MIN_ALT_DEG
            ):
                return
            await asyncio.sleep(FLAT_POSITION_POLL_S)
        raise MountRefusedError(
            f"the mount did not finish positioning at the zenith within "
            f"{FLAT_POSITION_TIMEOUT_S:.0f}s"
        )

    async def mount_sync(self, *, ra_hours: float, dec_deg: float) -> None:
        # A sync writes straight into the mount's pointing model — an
        # unconverted target here is not a display bug, it is durable state
        # corruption (frames.py's module docstring), which is exactly why
        # this goes through the same conversion as slew rather than a
        # trusted-caller shortcut.
        device_ra_hours, device_dec_deg = self._to_mount_frame(ra_hours=ra_hours, dec_deg=dec_deg)
        await self._gateway.mount_sync(ra_hours=device_ra_hours, dec_deg=device_dec_deg)

    async def mount_move_axis(self, *, axis: MountAxis, rate_deg_s: float) -> None:
        await self._gateway.mount_move_axis(axis=axis, rate_deg_s=rate_deg_s)
        if rate_deg_s == 0:
            self._jog_deadlines.pop(axis, None)
            task = self._jog_tasks.pop(axis, None)
            if task is not None:
                task.cancel()
            self._publish_jog_state()
            return
        loop = asyncio.get_running_loop()
        self._jog_deadlines[axis] = loop.time() + self._jog_lease_s
        if axis not in self._jog_tasks or self._jog_tasks[axis].done():
            task = self._tasks.spawn(self._expire_jog(axis), name=f"mount-jog-{axis}")
            self._jog_tasks[axis] = task
        self._publish_jog_state()

    async def _expire_jog(self, axis: MountAxis) -> None:
        try:
            while axis in self._jog_deadlines:
                remaining = self._jog_deadlines[axis] - asyncio.get_running_loop().time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                self._jog_deadlines.pop(axis, None)
                try:
                    await self._gateway.mount_move_axis(axis=axis, rate_deg_s=0)
                finally:
                    self._publish_jog_state()
                return
        finally:
            self._jog_tasks.pop(axis, None)

    def _publish_jog_state(self) -> None:
        mount = self._state.snapshot.mount
        if mount is None:
            return
        deadline = max(self._jog_deadlines.values(), default=None)
        expires = None
        if deadline is not None:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            expires = datetime.now(tz=UTC) + timedelta(seconds=remaining)
        self._state.apply(
            MountChanged(
                mount=mount.model_copy(
                    update={
                        "jog_primary_active": "primary" in self._jog_deadlines,
                        "jog_secondary_active": "secondary" in self._jog_deadlines,
                        "jog_lease_expires_at": expires,
                    }
                )
            )
        )

    async def mount_stop(self) -> None:
        self._jog_deadlines.clear()
        for task in self._jog_tasks.values():
            task.cancel()
        self._jog_tasks.clear()
        await self._gateway.mount_stop()
        self._publish_jog_state()

    async def mount_set_tracking(
        self, *, enabled: bool, rate: MountTrackingRate | None
    ) -> MountState:
        # The gateway's own read-back, given the same J2000 treatment as a
        # poll tick's reading (``on_mount``) before it goes back to the
        # caller as this call's direct response — otherwise a client reading
        # this response and one reading the next ``GET /api/state`` would see
        # different things for the same reading.
        state = await self._gateway.mount_set_tracking(enabled=enabled, rate=rate)
        return self._with_j2000(state)

    def _to_mount_frame(self, *, ra_hours: float, dec_deg: float) -> tuple[float, float]:
        """A J2000 target -> the frame the connected mount declares.

        Refuses (:class:`~ssc.state.ports.MountRefusedError`, surfaced as a
        409 by ``ssc/api/routes/mount.py``) rather than guessing whenever the
        mount's frame, its own reported site, or the ephemeris kernel is not
        yet known, or the mount declares a frame ``ssc.astro.frames`` does not
        convert (B1950/J2050/other) — see that module's own docstring on why
        a wrong sync is durable rather than merely wrong. A mount already
        declaring J2000 is passed through unconverted.

        The orchestration itself lives in ``ssc.api.mount_target`` — shared
        with the scheduler's own device port (``ssc.api.device_commands``) so
        there is exactly one place this is done, not two copies free to drift.
        """
        try:
            return resolve_mount_target(
                mount=self._state.snapshot.mount,
                ephemeris=self._astro.ephemeris,
                ephemeris_error=self._astro.ephemeris_error,
                ra_hours=ra_hours,
                dec_deg=dec_deg,
                when=datetime.now(tz=UTC),
            )
        except MountFrameUnavailableError as exc:
            raise MountRefusedError(str(exc)) from exc

    async def mount_park(self) -> None:
        await self._gateway.mount_park()

    async def mount_unpark(self) -> None:
        await self._gateway.mount_unpark()

    async def mount_unpark_if_needed(self) -> bool:
        """Unpark exactly once when a semantic backend command requires motion."""
        mount = self._state.snapshot.mount
        if mount is None or (mount.at_park is not True and mount.at_home is not True):
            return False
        await self._gateway.mount_unpark()
        return True

    async def mount_find_home(self) -> None:
        await self._gateway.mount_find_home()

    # -- focuser control (bead ssc-bkv.7) --------------------------------------- #

    async def focuser_position(self) -> tuple[int | None, str | None]:
        return await self._gateway.focuser_position()

    async def focuser_is_moving(self) -> tuple[bool | None, str | None]:
        return await self._gateway.focuser_is_moving()

    async def focuser_step_size(self) -> tuple[int | None, str | None]:
        return await self._gateway.focuser_step_size()

    async def focuser_move(self, *, position: int) -> None:
        await self._gateway.focuser_move(position=position)

    async def focuser_halt(self) -> None:
        await self._gateway.focuser_halt()

    async def focuser_start_autofocus(
        self, *, step: int, fine_step: int, exposure_s: float, settle_s: float
    ) -> None:
        await self._gateway.focuser_start_autofocus(
            step=step, fine_step=fine_step, exposure_s=exposure_s, settle_s=settle_s
        )

    async def focuser_autofocus_state(self) -> AutofocusState:
        return await self._gateway.focuser_autofocus_state()

    # -- filter-wheel control (bead ssc-bkv.6) ------------------------------- #

    async def filterwheel_get_names(self) -> tuple[list[str] | None, str | None]:
        return await self._gateway.filterwheel_get_names()

    async def filterwheel_get_position(self) -> tuple[int | None, str | None]:
        return await self._gateway.filterwheel_get_position()

    async def filterwheel_set_position(self, *, position: int) -> None:
        await self._gateway.filterwheel_set_position(position=position)

    async def heater_get_state(self) -> tuple[bool | None, str | None]:
        return await self._gateway.heater_get_state()

    async def heater_set_state(self, *, enabled: bool) -> None:
        await self._gateway.heater_set_state(enabled=enabled)

    # -- auto-connect ---------------------------------------------------------- #

    async def set_auto_connect(self, enabled: bool) -> DeviceSettings:
        """The persisted master switch. Turning it on also clears a standing
        "stay disconnected" intent — that is the "ask otherwise" a suspended
        user gets by opting back into automatic management."""
        device = await self._settings.set_auto_connect(enabled)
        self._auto_connect_enabled = enabled
        if enabled:
            self._suspended = False
        self._state.apply(SettingsChanged(settings=self._settings.summary()))
        self._wake_supervisor()
        return device

    async def set_ethernet_address(
        self, host: str | None, *, control_port: int | None
    ) -> DeviceSettings:
        """Persist the preferred wired endpoint and wake a disconnected supervisor."""
        device = await self._settings.set_ethernet_address(host, control_port)
        self._state.apply(SettingsChanged(settings=self._settings.summary()))
        self._wake_supervisor()
        return device

    async def set_wifi_address(
        self, host: str | None, *, control_port: int | None
    ) -> DeviceSettings:
        """Persist the explicit Wi-Fi fallback and wake a disconnected supervisor."""
        device = await self._settings.set_wifi_address(host, control_port)
        self._state.apply(SettingsChanged(settings=self._settings.summary()))
        self._wake_supervisor()
        return device

    def _wake_supervisor(self) -> None:
        self._wake.set()

    def _supervisor_disabled(self) -> bool:
        """Auto-connect switched off, an explicit disconnect, or nothing to
        connect to. Distinct from "the link happens to be up right now" —
        that case resets nothing, it just has nothing to do (see ``_supervise``)."""
        return (
            not self._auto_connect_enabled
            or self._suspended
            or not self._connection_candidates()
        )

    def _reconnect_delay(self) -> float:
        """Half-jitter exponential backoff with a cap — this supervisor's own
        policy for *whether to try again*, distinct from and layered above the
        transport-level retry ``ssc.seestar.session.BackoffPolicy`` already
        performs once a connection attempt is actually under way."""
        base = min(
            self._reconnect_max_delay_s,
            self._reconnect_initial_delay_s * 2.0 ** min(60, max(0, self._reconnect_attempt - 1)),
        )
        return base / 2.0 + self._rng.random() * (base / 2.0)

    async def _wait_woken(self, timeout: float | None) -> bool:  # noqa: ASYNC109 - a backoff budget, not a wrapper
        """Wait for an external wake (a link/settings/intent change), or a
        timeout. Returns whether it was woken early — a wake always means
        "reassess now", never "attempt now" by itself."""
        self._wake.clear()
        if timeout is None:
            await self._wake.wait()
            return True
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def _idle_reason(self) -> str | None:
        if not self._auto_connect_enabled:
            return "auto-connect is off"
        if self._suspended:
            return "disconnected by request; not reconnecting automatically"
        if not self._connection_candidates():
            return "no remembered address"
        return None  # pragma: no cover - _supervisor_disabled() guards every caller

    def _publish_auto_connect(
        self, *, next_attempt_at: datetime | None, reason: str | None
    ) -> None:
        state = AutoConnectState(
            enabled=self._auto_connect_enabled,
            suspended=self._suspended,
            attempt=self._reconnect_attempt,
            next_attempt_at=next_attempt_at,
            reason=reason,
        )
        if state == self._last_auto_connect:
            return
        self._last_auto_connect = state
        self._state.apply(AutoConnectChanged(auto_connect=state))

    async def _supervise(self) -> None:
        """Connect on startup, and reconnect after an unintentional drop.

        Only ``LinkStatus.DISCONNECTED`` is ever acted on: ``CONNECTING`` and
        ``HANDSHAKING`` are already under way, and ``READY``/``DEGRADED`` both
        mean the control channel is up (a capability gap is not a dropped
        link). ``_reconnect_attempt`` is reset to 0 only when there is
        genuinely nothing to reconnect to or want (``_supervisor_disabled``) or
        once the link is actually confirmed up again (``on_link``) — never
        merely because a connect attempt is in flight, or backoff would
        collapse to zero on every retry.

        Runs until cancelled (lifespan shutdown). Every iteration either
        publishes why it is waiting and waits, or makes exactly one connect
        attempt — never both, and never a hot loop: a fresh attempt is only
        made after either a real wake (something changed) or a full backoff
        delay has elapsed.
        """
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                if self._supervisor_disabled():
                    self._reconnect_attempt = 0
                    self._publish_auto_connect(next_attempt_at=None, reason=self._idle_reason())
                    await self._wait_woken(None)
                    continue

                if self._link_status in _NOT_DISCONNECTED:
                    # Already up, or already under way — nothing to decide
                    # until ``on_link`` wakes this loop with a real change.
                    self._publish_auto_connect(next_attempt_at=None, reason=None)
                    await self._wait_woken(None)
                    continue

                if self._reconnect_attempt > 0:
                    delay = self._reconnect_delay()
                    next_at = datetime.now(tz=UTC) + timedelta(seconds=delay)
                    self._publish_auto_connect(
                        next_attempt_at=next_at,
                        reason=f"reconnecting after a dropped link (attempt {self._reconnect_attempt})",
                    )
                    if await self._wait_woken(delay):
                        continue
                else:
                    self._publish_auto_connect(
                        next_attempt_at=None, reason="connecting to the remembered address"
                    )

                if self._supervisor_disabled() or self._link_status in _NOT_DISCONNECTED:
                    continue

                self._reconnect_attempt += 1
                try:
                    await self.connect_preferred()
                except Exception as exc:
                    # The supervisor outlives a failed attempt. Only
                    # CancelledError is suppressed around this loop, so an
                    # exception escaping here killed the task outright and
                    # auto-connect stopped for the life of the process —
                    # silently, since a dead supervisor still reports
                    # `enabled: true`. `connect` has already published
                    # DISCONNECTED with the reason, so the next pass sees a
                    # retryable state and backs off normally.
                    _log.warning(
                        "auto-connect: attempt %d failed: %s", self._reconnect_attempt, exc
                    )

    # -- DeviceSink --------------------------------------------------------- #

    def on_link(self, link: LinkState) -> None:
        if self._suspended and link.status != LinkStatus.DISCONNECTED:
            return
        self._link_status = link.status
        if link.status in (LinkStatus.READY, LinkStatus.DEGRADED):
            # The control channel is up either way; a future drop from here
            # starts backoff fresh rather than continuing to escalate.
            self._reconnect_attempt = 0
        self._state.apply(LinkChanged(link=link))
        self._wake_supervisor()

    def on_device(self, device: DeviceDescriptor | None) -> None:
        self._state.apply(DeviceChanged(device=device))

    def on_mount(self, mount: MountState | None) -> None:
        self._state.apply(MountChanged(mount=None if mount is None else self._with_j2000(mount)))

    def on_devices(self, devices: AlpacaDevicesState | None) -> None:
        self._state.apply(DevicesChanged(devices=devices))

    def on_camera_fov(self, fov: CameraFieldOfView | None) -> None:
        self._state.apply(CameraFovChanged(camera_fov=fov))

    def _with_j2000(self, mount: MountState) -> MountState:
        """``mount``, with :attr:`~ssc.state.models.MountState.ra_j2000_h`/
        ``dec_j2000_deg`` filled in from ``ra_hours``/``dec_deg`` — a fresh
        reading is always in the mount's own frame (``equatorial_system``),
        never assumed J2000, so a client can draw the believed-pointing
        indicator without knowing that.

        Never raises: a poll tick must keep publishing telemetry even when the
        conversion cannot be done yet, so this degrades to leaving the two
        fields ``None`` (logged, at whatever level fits how surprising it is)
        rather than dropping the reading or crashing the poll loop (CLAUDE.md
        invariant 7).
        """
        deadline = max(self._jog_deadlines.values(), default=None)
        remaining = (
            None
            if deadline is None
            else max(0.0, deadline - asyncio.get_running_loop().time())
        )
        mount = mount.model_copy(
            update={
                "jog_primary_active": "primary" in self._jog_deadlines,
                "jog_secondary_active": "secondary" in self._jog_deadlines,
                "jog_lease_expires_at": (
                    None if remaining is None else datetime.now(tz=UTC) + timedelta(seconds=remaining)
                ),
            }
        )
        ra_j2000_h, dec_j2000_deg = self._position_to_j2000(mount)
        if ra_j2000_h is None:
            return mount
        return mount.model_copy(update={"ra_j2000_h": ra_j2000_h, "dec_j2000_deg": dec_j2000_deg})

    def _position_to_j2000(self, mount: MountState) -> tuple[float | None, float | None]:
        if mount.ra_hours is None or mount.dec_deg is None or mount.polled_at is None:
            return None, None
        if mount.equatorial_system is None:
            _log.debug("mount: equatorial frame not known yet; not drawing a J2000 position")
            return None, None
        if mount.equatorial_system is MountFrame.J2000:
            return mount.ra_hours, mount.dec_deg
        ephemeris = self._astro.ephemeris
        if ephemeris is None:
            _log.warning(
                "mount: cannot convert its %r position to J2000, ephemeris unavailable (%s)",
                mount.equatorial_system.value,
                self._astro.ephemeris_error,
            )
            return None, None
        if mount.site_latitude_deg is None or mount.site_longitude_deg is None:
            _log.debug("mount: site not known yet; not converting its reported position")
            return None, None
        coord = EquatorialCoord(
            ra_deg=hours_to_deg(Hours(mount.ra_hours)),
            dec_deg=Deg(mount.dec_deg),
            frame=mount.equatorial_system,
        )
        site = mount_site(Deg(mount.site_latitude_deg), Deg(mount.site_longitude_deg))
        try:
            j2000 = to_j2000(ephemeris, site, coord, mount.polled_at)
        except (UnsupportedMountFrameError, FrameInversionError) as exc:
            _log.warning("mount: could not convert its reported position to J2000: %s", exc)
            return None, None
        return float(j2000.ra_h), float(j2000.dec_deg)

    def on_device_event(self, event: DeviceEventRecord) -> None:
        self._state.apply(DeviceEventReceived(event=event))


__all__ = [
    "DEFAULT_DISCOVERY_TIMEOUT_S",
    "DEFAULT_PROBE_TIMEOUT_S",
    "DEFAULT_RECONNECT_INITIAL_DELAY_S",
    "DEFAULT_RECONNECT_MAX_DELAY_S",
    "DeviceBusyError",
    "DeviceService",
]
