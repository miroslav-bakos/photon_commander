# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The device's *own* ASCOM Alpaca server, driven as a Seestar session.

Some Seestar builds have withdrawn the JSON-RPC control port (:4701 accepts TCP
and never answers) and ship an ASCOM Alpaca server on :32323 instead. On those
builds that server is the only control surface there is, so this module makes it
look like a :class:`~ssc.seestar.session.SeestarSession` to everything above the
seam — same state machine, same published status, same typed device identity.

What it is **not**: a ``SeestarTransport``. That protocol is a line-oriented
duplex byte channel with the device pushing events, and native Alpaca is HTTP
request/response with no push at all — ``receive_line()`` has no honest
implementation over REST (``docs/IMPLEMENTATION.md`` M8). The fork therefore
sits one level up, at the session.

Observed live against the S30 Pro on 2026-09-10 (``docs/M0-FINDINGS.md``,
"Firmware 9.31"), and every fact this module leans on was measured, not assumed:

* **The device will not name its firmware.** ``/management/v1/description``
  reports ``ManufacturerVersion 1.3.1-1``, which is the *ASCOM server's* version
  and is identical across firmwares. So :attr:`DeviceInfo.firmware` stays
  ``None`` and the session sits in :attr:`ConnectionState.DEGRADED` with a
  reason for as long as it is up. That is invariant 7 working, not a fault.
* **Model and serial are real, and come from the device list.**
  ``configureddevices`` names each device ``"<model>_<serial> <role>"``, e.g.
  ``"Seestar S30 Pro_SYNTHETIC000001 Telescope"``.
* **Properties lie before ``Connected``.** Several answer a plausible wrong
  value rather than erroring (camera ``bayeroffsetx/y`` read 0/0, focuser
  ``maxstep`` reads 0), so nothing is read until ``Connected = True`` has been
  set and read back.
* **``Connected = False`` is accepted and ignored.** The device keeps reporting
  ``Connected: True`` afterwards, and ``Disconnect``/``Connecting`` (Platform 7)
  are not implemented — ``interfaceversion`` is 3. :meth:`NativeAlpacaSession.aclose`
  therefore tears down SSC's own side, checks, and *says* the device stayed
  connected instead of claiming a clean disconnect.
* **There are no vendor actions on ``telescope/0``** and no Seestar JSON-RPC
  vocabulary anywhere, so :meth:`call` and :meth:`get_device_state` raise
  :class:`MethodNotAvailableError` naming the surface. They never invent a
  plausible empty result.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Final

from ssc.client.alpaca import AlpacaClient, AlpacaDeviceClient
from ssc.client.errors import AlpacaClientError, AlpacaErrorNumber, AlpacaRemoteError
from ssc.seestar.models import DeviceState
from ssc.seestar.rpc import RpcResponse
from ssc.seestar.session import (
    CAPABILITY_PROBE_KEYS,
    BackoffPolicy,
    ConnectionState,
    DeviceInfo,
    EventListener,
    MethodNotAvailableError,
    SessionError,
    SessionStatus,
    Sleeper,
    StateListener,
)

_log = logging.getLogger(__name__)

#: The ASCOM-registered Alpaca port, and the one this device advertises over its
#: own UDP discovery. A port is not an address: invariant 11 forbids assuming
#: *where* the scope is, never which port a published protocol lives on.
DEFAULT_ALPACA_PORT: Final = 32323

#: The four ASCOM devices SSC needs a link to. ``telescope/0`` is the one whose
#: ``Connected`` drives the session; the rest are read for identity only.
_TELESCOPE: Final = ("telescope", 0)

#: Read verbatim off ``telescope/0`` once connected. Every one is a capability
#: *the device advertises about itself* — no probe invents anything, and none of
#: these moves so much as a focuser.
_TELESCOPE_CAPABILITIES: Final[tuple[str, ...]] = (
    "canpark",
    "canunpark",
    "canfindhome",
    "canslew",
    "canslewasync",
    "canslewaltazasync",
    "cansettracking",
    "cansync",
    "cansetpark",
    "cansetpierside",
)

#: Permanent native-Alpaca protocol limitation. It is informational metadata,
#: not a connection-health failure.
FIRMWARE_UNKNOWN_NOTE: Final = (
    "this device is driven over its own ASCOM Alpaca server, which does not report a "
    "firmware version (ManufacturerVersion is the ASCOM server's own version and is "
    "identical across firmwares), so the firmware behind it is unverified — SSC uses "
    "only what the device advertises about itself"
)

#: The other permanent protocol characteristic: no push channel exists here.
NO_EVENT_CHANNEL_NOTE: Final = (
    "the native Alpaca surface has no event channel; device state is polled, not pushed"
)

#: How often ``Connected`` is re-read to keep ``control_connected`` honest. HTTP
#: has no connection to lose, so a link that has silently gone away is only
#: visible by asking. 5 s is slow enough to be invisible on the device and fast
#: enough that a scope that was switched off does not linger as "connected".
DEFAULT_HEALTH_POLL_S: Final = 5.0

DEFAULT_REQUEST_TIMEOUT_S: Final = 5.0

#: Read every health poll *alongside* ``Connected``, because ``Connected`` on
#: its own is not a health signal on this device. Measured against the dev S30
#: Pro on 2026-09-10, twenty minutes apart:
#:
#: * ``telescope/0/connected`` read ``true`` while every mount member answered
#:   ``[0x4ff] GET_SCOPE_GET_HORIZ_COORD fail: TELEPHOTO not connected`` — the
#:   device's Alpaca server was up and its mount subsystem was wedged;
#: * ``telescope/0/connected`` read ``false`` while the same members answered
#:   real altitudes and coordinates.
#:
#: So it is true when the mount is unusable and false when it is fine. One real
#: telemetry read is the only honest test, and it is the read the Mount tab
#: needs anyway.
HEALTH_MEMBER: Final = "altitude"

#: Consecutive unanswered health polls tolerated before the link is declared
#: lost. The same device intermittently took longer than the 5 s request budget
#: to answer ``connected`` while wedged, and one slow read used to tear the
#: whole session down and reconnect — a ~15 s flap loop that left mount
#: telemetry permanently empty. A missed read is degradation, published as
#: such; three in a row is a lost link.
HEALTH_FAILURE_BUDGET: Final = 3

#: Builds a client for one device address. Injectable so tests drive a fake
#: Alpaca server through ``httpx.MockTransport`` instead of a socket.
AlpacaClientFactory = Callable[[str, int], AlpacaClient]


def default_client_factory(host: str, port: int) -> AlpacaClient:
    return AlpacaClient(f"http://{host}:{port}", request_timeout_s=DEFAULT_REQUEST_TIMEOUT_S)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def split_device_name(name: str) -> tuple[str | None, str | None]:
    """``"Seestar S30 Pro_SYNTHETIC000001 Telescope"`` -> ``("Seestar S30 Pro", "SYNTHETIC000001")``.

    Anything that does not match that shape yields ``None`` for the part that
    could not be read. A guessed serial is worse than an absent one.
    """
    model, separator, rest = name.partition("_")
    if not separator or not model.strip():
        return (name.strip() or None, None)
    serial = rest.split(" ", 1)[0].strip()
    return (model.strip(), serial or None)


#: What a probe can honestly say about a device it has *not* connected to.
#: Capabilities are deliberately absent: reading them needs ``Connected = True``,
#: and a "Test connection" that leaves the device connected is not a test.
PROBE_NOTE_TEMPLATE: Final = (
    "identified over the device's own ASCOM Alpaca server on :{port} — {devices} configured "
    "device(s), Alpaca v1. It reports no firmware version there ({manufacturer_version} is "
    "the ASCOM server's own version), and capabilities were not read because that needs a "
    "connect, which a probe must not do"
)


def identity_from_devices(
    devices: Sequence[Mapping[str, object]],
) -> tuple[str | None, str | None]:
    """Model and serial off the configured-device list, or ``None`` for either.

    The telescope entry is preferred only because it is the device SSC drives;
    every entry carries the same ``"<model>_<serial> <role>"`` name.
    """
    for entry in devices:
        if str(entry.get("DeviceType", "")).lower() == "telescope":
            return split_device_name(str(entry.get("DeviceName", "")))
    for entry in devices:
        model, serial = split_device_name(str(entry.get("DeviceName", "")))
        if model is not None:
            return (model, serial)
    return (None, None)


def device_types(devices: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    """The ASCOM device types this server configures, lowercased, in order."""
    return tuple(str(entry.get("DeviceType", "")).lower() for entry in devices)


async def identify_alpaca(
    host: str,
    *,
    port: int = DEFAULT_ALPACA_PORT,
    client_factory: AlpacaClientFactory = default_client_factory,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_S,  # noqa: ASYNC109 - the caller's budget
) -> tuple[DeviceInfo, tuple[str, ...]]:
    """Identify a device over its Alpaca server **without connecting to it**.

    Two management reads and nothing else: no ``Connected = True``, no property
    read, nothing that this firmware would then refuse to undo (it ignores
    ``Connected = False``). Returns the identity and the device types observed,
    so a caller can see the shape it answered with rather than being told what
    it "is".

    Never raises for an absent server — that is a result, in
    :attr:`DeviceInfo.note`.
    """
    client = client_factory(host, port)
    try:
        async with asyncio.timeout(timeout):
            versions = await client.api_versions()
            if 1 not in versions:
                return (
                    DeviceInfo(
                        host=host,
                        control_port=port,
                        reachable=False,
                        note=f"an Alpaca server answered on :{port} reporting apiversions "
                        f"{versions!r}; SSC speaks Alpaca v1",
                    ),
                    (),
                )
            devices = await client.configured_devices()
            description = await client.description()
    except (AlpacaClientError, TimeoutError, OSError) as exc:
        return (
            DeviceInfo(
                host=host,
                control_port=port,
                reachable=False,
                note=f"no Alpaca server answered on :{port} ({type(exc).__name__}: {exc})",
            ),
            (),
        )
    finally:
        await client.aclose()

    model, serial = identity_from_devices(devices)
    manufacturer_version = str(description.get("ManufacturerVersion", "ManufacturerVersion"))
    return (
        DeviceInfo(
            host=host,
            control_port=port,
            reachable=True,
            firmware=None,
            firmware_int=None,
            model=model,
            serial=serial,
            is_verified=None,
            capabilities={},
            note=PROBE_NOTE_TEMPLATE.format(
                port=port,
                devices=len(devices),
                manufacturer_version=manufacturer_version,
            ),
        ),
        device_types(devices),
    )


class _LinkLostError(Exception):
    """The ASCOM link to ``telescope/0`` is gone; only a reconnect restores it.

    Distinct from a health read that merely went unanswered, and from the
    device refusing telemetry while the link is up — those three are different
    facts about the device and must not collapse into one "link lost".
    """


class NativeAlpacaSession:
    """A :class:`~ssc.seestar.session.SeestarSession` over the device's own Alpaca server.

    One supervisor task owns the state machine, exactly as the JSON-RPC session
    does: ``CONNECTING -> HANDSHAKING -> READY``, and back to ``CONNECTING``
    with a published reason and jittered backoff whenever the link stops
    answering. It never reconnects silently.

    Missing firmware metadata and push events are permanent characteristics of
    this surface, so they do not reduce a healthy link to ``DEGRADED``. That
    state is reserved for an actual partial failure such as unavailable mount
    telemetry while control remains connected.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = DEFAULT_ALPACA_PORT,
        client_factory: AlpacaClientFactory = default_client_factory,
        backoff: BackoffPolicy | None = None,
        rng: random.Random | None = None,
        sleeper: Sleeper = asyncio.sleep,
        health_poll_s: float = DEFAULT_HEALTH_POLL_S,
    ) -> None:
        self._host = host
        self._port = port
        self._endpoint = f"{host}:{port}"
        self._client_factory = client_factory
        self._backoff = backoff or BackoffPolicy()
        self._rng = rng or random.Random()
        self._sleep = sleeper
        self._health_poll_s = health_poll_s

        self._client: AlpacaClient | None = None
        self._device: DeviceInfo | None = None
        self._control_up = False
        #: What is wrong with an otherwise-up link, right now. Permanent
        #: protocol limitations live on ``DeviceInfo.note`` instead.
        self._health_note: str | None = None
        self._connect_attempts = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

        self._status = SessionStatus(state=ConnectionState.DISCONNECTED, endpoint=self._endpoint)
        self._state_listeners: list[StateListener] = []
        self._event_listeners: list[EventListener] = []
        self._waiters: list[asyncio.Future[SessionStatus]] = []

    # -- public surface ----------------------------------------------------- #

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def state(self) -> ConnectionState:
        return self._status.state

    @property
    def device(self) -> DeviceInfo | None:
        return self._device

    @property
    def native_client(self) -> AlpacaClient | None:
        """The device's own Alpaca client, once the handshake is up; ``None`` otherwise.

        This is the pass-through hook ``ssc/alpaca/`` needs: when a Seestar's own
        Alpaca server is the active control surface, mapping a standard ASCOM
        member onto it is closest to a direct proxy onto this client's
        ``telescope/0`` / ``focuser/0`` / ``focuser/1`` / ``filterwheel/0`` — see
        ``ssc/alpaca/backend.py``. Only handed out while ``_control_up`` is true,
        so a caller never gets a client to a link this session has already given
        up on.
        """
        return self._client if self._control_up else None

    def subscribe_state(self, listener: StateListener) -> Callable[[], None]:
        self._state_listeners.append(listener)
        return lambda: self._state_listeners.remove(listener)

    def subscribe_events(self, listener: EventListener) -> Callable[[], None]:
        """Accepted and never called — this surface has no push channel.

        The subscription is honoured rather than refused so callers above the
        seam stay on one code path. :data:`NO_EVENT_CHANNEL_NOTE` is published
        as device information so nobody mistakes silence for an idle device.
        """
        self._event_listeners.append(listener)
        return lambda: self._event_listeners.remove(listener)

    async def start(self) -> None:
        if self._task is not None:
            raise SessionError("session already started")
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="seestar-native-alpaca")

    async def aclose(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        note = await self._release_device()
        self._control_up = False
        self._health_note = None
        self._enter(
            ConnectionState.DISCONNECTED, "; ".join(["session closed", *filter(None, [note])])
        )

    async def wait_for_state(
        self,
        states: Collection[ConnectionState],
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 - the caller's budget
    ) -> SessionStatus:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self._status.state in states:
                return self._status
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"state {self._status.state} did not reach "
                    f"{sorted(str(s) for s in states)} within {timeout}s"
                )
            waiter: asyncio.Future[SessionStatus] = loop.create_future()
            self._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, remaining)
            except TimeoutError:
                continue
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    async def call(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors the session protocol
        raise_on_error: bool = True,
        allow_unverified: bool = False,
    ) -> RpcResponse:
        """Always raises: there is no Seestar JSON-RPC vocabulary on this surface."""
        del params, timeout, raise_on_error, allow_unverified
        raise MethodNotAvailableError(self._no_vocabulary(method))

    async def get_device_state(
        self,
        keys: Sequence[str] = CAPABILITY_PROBE_KEYS,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors the session protocol
    ) -> DeviceState:
        """Always raises. ``get_device_state`` is a JSON-RPC method; this surface
        answers the equivalent through standard ASCOM members instead."""
        del keys, timeout
        raise MethodNotAvailableError(self._no_vocabulary("get_device_state"))

    # -- the state machine -------------------------------------------------- #

    def _no_vocabulary(self, method: str) -> str:
        return (
            f"{method}: this device is driven over its own ASCOM Alpaca server at "
            f"{self._endpoint}, which has no Seestar JSON-RPC vocabulary — read the "
            f"equivalent standard ASCOM member instead"
        )

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._connect_attempts += 1
            try:
                await self._handshake()
            except AlpacaClientError as exc:
                await self._retry(f"native Alpaca handshake failed: {exc}")
                continue
            try:
                await self._monitor()
            except AlpacaClientError as exc:
                self._control_up = False
                await self._retry(f"native Alpaca link lost: {exc}")

    async def _handshake(self) -> None:
        """Identify the server, connect ``telescope/0``, read what it advertises.

        Every call here is a read except the one ``Connected = True``, which is
        an ASCOM connect and moves nothing.
        """
        self._enter(
            ConnectionState.CONNECTING, f"opening the native Alpaca surface at {self._endpoint}"
        )
        await self._close_client()
        client = self._client_factory(self._host, self._port)
        self._client = client

        versions = await client.api_versions()
        if 1 not in versions:
            raise AlpacaClientError(
                f"{self._endpoint} answered /management/apiversions with {versions!r}; "
                "SSC speaks Alpaca v1"
            )
        devices = await client.configured_devices()
        model, serial = identity_from_devices(devices)

        self._enter(
            ConnectionState.HANDSHAKING,
            f"connecting telescope/0 on {self._endpoint}",
        )
        telescope = client.device(*_TELESCOPE)
        await telescope.connect()
        if not await telescope.is_connected():
            raise AlpacaClientError(
                "telescope/0 accepted Connected = True and still reports Connected: false"
            )

        capabilities = await self._read_capabilities(client)
        self._health_note = await self._first_telemetry_note(telescope)
        self._device = DeviceInfo(
            host=self._host,
            control_port=self._port,
            reachable=True,
            firmware=None,
            firmware_int=None,
            model=model,
            serial=serial,
            is_verified=None,
            capabilities=capabilities,
            note=f"{FIRMWARE_UNKNOWN_NOTE}; {NO_EVENT_CHANNEL_NOTE}",
        )
        self._control_up = True
        self._connect_attempts = 0
        self._publish()

    async def _read_capabilities(self, client: AlpacaClient) -> dict[str, bool]:
        """The mount's own ``can*`` flags, verbatim. A flag that errors is absent,
        never guessed at — an unreadable capability is unknown, not false."""
        telescope = client.device(*_TELESCOPE)
        capabilities: dict[str, bool] = {}
        for member in _TELESCOPE_CAPABILITIES:
            try:
                value = await telescope.get(member)
            except AlpacaClientError as exc:
                _log.info("telescope/0.%s is not readable on this device: %s", member, exc)
                continue
            if isinstance(value, bool):
                capabilities[member] = value
        return capabilities

    async def _first_telemetry_note(self, telescope: AlpacaDeviceClient) -> str | None:
        """The mount-subsystem check that runs before the handshake claims success.

        A device whose Alpaca server is up but whose mount subsystem is not
        serving reads would otherwise be published as a healthy link with an
        empty Mount tab and no stated reason (CLAUDE.md invariant 7). An
        unanswered probe is *not* a failed handshake — the link is real and the
        device may simply be slow — so it degrades with a reason instead.
        """
        try:
            return await self._telemetry_note(telescope)
        except AlpacaClientError as exc:
            return (
                f"the device did not answer telescope/{_TELESCOPE[1]}.{HEALTH_MEMBER} during "
                f"the handshake ({exc}), so mount telemetry is not known to be working"
            )

    async def _telemetry_note(self, telescope: AlpacaDeviceClient) -> str | None:
        """Read one mount member. ``None`` when it answered; a reason when the
        device refused it; :class:`_LinkLostError` when the refusal *was* the link.

        A device-side refusal is deliberately **not** a reconnect. Reconnecting
        does not restore a wedged mount subsystem — it only churns the device's
        Alpaca server, which is exactly the flap this replaced — and while the
        link is up a user's own recovery command (``FindHome``) still has a
        route to the device.
        """
        try:
            await telescope.get(HEALTH_MEMBER)
        except AlpacaRemoteError as exc:
            if exc.number == AlpacaErrorNumber.NOT_CONNECTED:
                raise _LinkLostError(
                    f"the device refused telescope/{_TELESCOPE[1]}.{HEALTH_MEMBER} with "
                    f"{exc.number:#05x} (not connected) while still reporting "
                    f"Connected: true"
                ) from exc
            return (
                f"the device's own mount subsystem is not serving reads: "
                f"telescope/{_TELESCOPE[1]}.{HEALTH_MEMBER} answered [{exc.number:#05x}] "
                f"{exc.message} — SSC's link to the Alpaca server is up, so this is the "
                f"device's fault, not the connection's, and mount telemetry is unavailable "
                f"until it recovers"
            )
        return None

    async def _health_read(self, telescope: AlpacaDeviceClient) -> str | None:
        """One health cycle: is the link there, and does the mount answer?"""
        if not await telescope.is_connected():
            raise _LinkLostError("the device dropped telescope/0's ASCOM connection")
        return await self._telemetry_note(telescope)

    async def _monitor(self) -> None:
        """Keep ``control_connected`` honest by asking, since HTTP cannot tell us.

        Three outcomes, kept apart because they are three different facts:
        the link is gone (reconnect), the device did not answer (degrade, and
        give up only after :data:`HEALTH_FAILURE_BUDGET` in a row), or the
        device answered a refusal (degrade and stay, naming what it said).
        """
        client = self._client
        if client is None:  # pragma: no cover - _handshake always sets it
            raise AlpacaClientError("no Alpaca client after a successful handshake")
        telescope = client.device(*_TELESCOPE)
        unanswered = 0
        while not self._stop.is_set():
            await self._sleep(self._health_poll_s)
            if self._stop.is_set():
                return
            try:
                note = await self._health_read(telescope)
            except _LinkLostError as exc:
                self._control_up = False
                self._health_note = None
                self._enter(ConnectionState.CONNECTING, f"{exc}; reconnecting")
                return
            except AlpacaClientError as exc:
                unanswered += 1
                if unanswered >= HEALTH_FAILURE_BUDGET:
                    raise AlpacaClientError(
                        f"{unanswered} consecutive health reads went unanswered "
                        f"{self._health_poll_s:g}s apart; last: {exc}"
                    ) from exc
                self._degrade(
                    f"the device has not answered the last {unanswered} health read(s) "
                    f"{self._health_poll_s:g}s apart ({exc}); giving up after "
                    f"{HEALTH_FAILURE_BUDGET}"
                )
                continue
            unanswered = 0
            self._degrade(note)

    async def _retry(self, reason: str) -> None:
        delay = self._backoff.delay(self._connect_attempts, self._rng)
        self._enter(ConnectionState.CONNECTING, f"{reason}; retrying in {delay:.1f}s")
        await self._close_client()
        await self._sleep(delay)

    async def _release_device(self) -> str | None:
        """Send ``Connected = False`` and report what the device actually did.

        The observed device accepts it and stays connected; saying "disconnected"
        anyway would be a green tick over a device that is still holding the link.
        """
        client, self._client = self._client, None
        if client is None:
            return None
        note: str | None = None
        try:
            telescope = client.device(*_TELESCOPE)
            await telescope.disconnect()
            if await telescope.is_connected():
                note = (
                    "the device ignored Connected = False and still reports itself connected "
                    "(observed on this firmware); SSC has released its own side of the link"
                )
                _log.warning("%s: %s", self._endpoint, note)
        except AlpacaClientError as exc:
            note = f"Connected = False did not complete: {exc}"
            _log.warning("%s: %s", self._endpoint, note)
        finally:
            await client.aclose()
        return note

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(AlpacaClientError):
                await client.aclose()

    # -- state publication -------------------------------------------------- #

    def _degrade(self, note: str | None) -> None:
        """Record what is wrong with an up link — or that nothing is, any more."""
        if note == self._health_note:
            return
        self._health_note = note
        self._publish()

    def _enter(self, phase: ConnectionState, reason: str | None) -> None:
        self._publish(phase, reason)

    def _publish(self, phase: ConnectionState | None = None, reason: str | None = None) -> None:
        if self._control_up:
            # Protocol limitations are informational device metadata. Only an
            # actual current health problem makes an otherwise-up link degraded.
            state = (
                ConnectionState.DEGRADED
                if self._health_note is not None
                else ConnectionState.READY
            )
            published_reason = self._health_note
        else:
            state = phase if phase is not None else self._status.state
            published_reason = reason

        candidate = SessionStatus(
            state=state,
            endpoint=self._endpoint,
            reason=published_reason,
            device=self._device,
            control_connected=self._control_up,
            events_connected=False,
            events_dropped=0,
            connect_attempts=self._connect_attempts,
            changed_at=self._status.changed_at,
        )
        if candidate.same_as(self._status):
            return
        self._status = replace(candidate, changed_at=_utcnow())
        _log.info(
            "seestar %s (native Alpaca): %s%s",
            self._endpoint,
            state,
            f" ({published_reason})" if published_reason else "",
        )
        for listener in list(self._state_listeners):
            try:
                listener(self._status)
            except Exception:
                _log.exception("state listener raised")
        for waiter in list(self._waiters):
            if not waiter.done():
                waiter.set_result(self._status)


__all__ = [
    "DEFAULT_ALPACA_PORT",
    "DEFAULT_HEALTH_POLL_S",
    "FIRMWARE_UNKNOWN_NOTE",
    "HEALTH_FAILURE_BUDGET",
    "HEALTH_MEMBER",
    "NO_EVENT_CHANNEL_NOTE",
    "PROBE_NOTE_TEMPLATE",
    "AlpacaClientFactory",
    "NativeAlpacaSession",
    "default_client_factory",
    "device_types",
    "identify_alpaca",
    "identity_from_devices",
    "split_device_name",
]
