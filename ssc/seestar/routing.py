# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Which control surface this device actually has — decided by asking it.

A Seestar is driven either over the newline-delimited JSON-RPC control port
(:4701, verified on 8.46) or over the device's own ASCOM Alpaca server (:32323,
the only surface on builds that have withdrawn :4701). Something has to choose,
and the obvious-looking discriminator — the firmware version — is the one thing
that must **not** be used:

    The unit this was built against reports "9.31", which is not a public
    release. ZWO shipped a bug in the Alpaca update installer and could only
    deliver the fix by re-sending the *same firmware under a higher version
    number*, so that number describes one device rather than a behaviour. Other
    owners will see different numbers for identical behaviour, and some will see
    a *higher* number with *older* behaviour. Version ordering does not track
    capability on this product line.

So SSC feature-probes instead (``DESIGN.md`` §10's own mitigation: record the
version, feature-probe, degrade with a warning). The question "does this device
serve a healthy Alpaca server?" is directly observable and true regardless of
what the build calls itself:

1. UDP :32227 — the device answers ``alpacadiscovery1`` with
   ``{"AlpacaPort": 32323}``. Its own advertisement, so it is asked first.
2. ``GET /management/apiversions`` and ``/management/v1/configureddevices`` on
   that port either return an Alpaca v1 server with devices, or they do not.

Probing the *other* surface is deliberately not symmetric. A JSON-RPC probe
costs a 10 s stall on a device that has withdrawn :4701 (it accepts TCP and
never answers), and ``DESIGN.md`` §10 records that an unsupported call there
takes the control port down for several seconds. The Alpaca probe is cheap
(~70 ms observed), safe and positive evidence, so it runs first and the JSON-RPC
session — which performs its own handshake and reports its own failure honestly
— is what happens when the answer is no.

Whatever is chosen, **the evidence is published** in
:attr:`~ssc.seestar.session.SessionStatus.reason`, so a user on a strange build
number can see what SSC observed rather than being told a version they do not
recognise.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from ssc.client.errors import AlpacaClientError
from ssc.seestar.models import DeviceState
from ssc.seestar.native import (
    DEFAULT_ALPACA_PORT,
    AlpacaClientFactory,
    NativeAlpacaSession,
    default_client_factory,
)
from ssc.seestar.rpc import RpcResponse
from ssc.seestar.session import (
    CAPABILITY_PROBE_KEYS,
    ConnectionState,
    DeviceInfo,
    EventListener,
    JsonRpcSeestarSession,
    SeestarSession,
    SessionError,
    SessionStatus,
    StateListener,
)
from ssc.seestar.transport import CONTROL_PORT, EVENT_PORT, TcpTransportFactory

_log = logging.getLogger(__name__)

#: The device's Alpaca discovery responder. Unicast works (52 ms observed), so
#: SSC asks the one host it is about to connect to rather than broadcasting.
ALPACA_DISCOVERY_PORT: Final = 32227
ALPACA_DISCOVERY_DATAGRAM: Final = b"alpacadiscovery1"

#: Whole-probe budget. Deliberately short: this runs in front of every connect.
DEFAULT_PROBE_TIMEOUT_S: Final = 3.0

#: How long to wait for the device's own UDP advertisement. A *connected*
#: socket returns the instant the answer or an ICMP unreachable arrives, so a
#: longer budget costs nothing on a device that is there — it is only spent on
#: a host that is routable and silent. 1 s was too tight: eight unicast probes
#: at the dev S30 Pro on 2026-09-10 answered in 0.022-0.887 s, one of them
#: within 0.11 s of the old limit, and a slower answer has been seen. Missing
#: it is not fatal (the probe falls back to the standard Alpaca port) but it
#: publishes "UDP did not answer" as evidence about a device that did.
DEFAULT_DISCOVERY_TIMEOUT_S: Final = 3.0

#: Ports that belong to Alpaca and can never carry Seestar JSON-RPC. Guarding
#: a *port* is not guarding an address — invariant 11 forbids assuming where
#: the scope is, not which port a published protocol lives on.
_ALPACA_PORTS: Final[frozenset[int]] = frozenset({DEFAULT_ALPACA_PORT, ALPACA_DISCOVERY_PORT})

#: Asks a host's Alpaca discovery responder which port its server is on.
AlpacaPortDiscovery = Callable[[str, float], Awaitable[int | None]]

#: Overrides the probe when a user knows better than it does — the escape hatch
#: for a device whose Alpaca server is switched off, or one being driven over
#: JSON-RPC on purpose. Values: ``auto`` (default), ``json-rpc``, ``native-alpaca``.
CONTROL_SURFACE_ENV: Final = "SSC_SEESTAR_CONTROL_SURFACE"


class ControlSurface(StrEnum):
    """How SSC talks to a given device."""

    AUTO = "auto"
    JSON_RPC = "json-rpc"
    NATIVE_ALPACA = "native-alpaca"


@dataclass(frozen=True, slots=True)
class SurfaceChoice:
    """The routing decision, with what was observed to make it."""

    surface: ControlSurface
    #: The Alpaca port that answered, when one did.
    alpaca_port: int | None
    #: Human-readable, and published in the session status. Never a version number.
    evidence: str

    def describe(self) -> str:
        return f"control surface: {self.surface} ({self.evidence})"


def forced_surface(environ: Mapping[str, str] | None = None) -> ControlSurface:
    """Read :data:`CONTROL_SURFACE_ENV`. An unrecognised value fails loudly."""
    raw = (environ if environ is not None else os.environ).get(CONTROL_SURFACE_ENV)
    if raw is None or not raw.strip():
        return ControlSurface.AUTO
    try:
        return ControlSurface(raw.strip().lower())
    except ValueError as exc:
        accepted = ", ".join(sorted(str(surface) for surface in ControlSurface))
        raise SessionError(
            f"{CONTROL_SURFACE_ENV}={raw!r} is not a control surface SSC knows; "
            f"accepted values are {accepted}"
        ) from exc


def _decode_alpaca_port(payload: bytes) -> int | None:
    """``{"AlpacaPort": 32323}`` -> ``32323``. Anything else is not an answer."""
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    port = decoded.get("AlpacaPort")
    return port if isinstance(port, int) and not isinstance(port, bool) else None


async def alpaca_discovery(
    targets: Sequence[str],
    *,
    timeout: float = DEFAULT_DISCOVERY_TIMEOUT_S,  # noqa: ASYNC109 - a probe budget
    broadcast: bool = False,
) -> dict[str, int]:
    """Send ``alpacadiscovery1`` to each target; collect ``{responder: port}``.

    Two modes, because they want opposite things:

    * **One unicast target** connects the socket, so an ICMP port-unreachable
      from a host with no Alpaca server fails in microseconds instead of burning
      the whole timeout. This is the hot path in front of every connect.
    * **Broadcast** cannot connect the socket and has no idea how many devices
      will answer, so it listens for the full timeout and returns everything.

    Never raises for silence: an absent answer is a result.
    """
    if not targets:
        return {}
    loop = asyncio.get_running_loop()
    found: dict[str, int] = {}
    finished = asyncio.Event()
    connected = len(targets) == 1 and not broadcast

    class _Protocol(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
            port = _decode_alpaca_port(data)
            if port is not None:
                found[str(addr[0])] = port
            if connected:
                finished.set()

        def error_received(self, exc: Exception) -> None:
            # ICMP unreachable: nothing is listening there. Only conclusive for
            # a connected socket, so a broadcast pass keeps waiting.
            if connected:
                _log.debug("alpaca discovery to %s: %s", targets[0], exc)
                finished.set()

    try:
        transport, _ = await loop.create_datagram_endpoint(
            _Protocol,
            remote_addr=(targets[0], ALPACA_DISCOVERY_PORT) if connected else None,
            local_addr=None if connected else ("0.0.0.0", 0),
            allow_broadcast=broadcast or None,
        )
    except OSError as exc:
        _log.debug("alpaca discovery could not open a socket: %s", exc)
        return {}
    try:
        for target in targets:
            try:
                if connected:
                    transport.sendto(ALPACA_DISCOVERY_DATAGRAM)
                else:
                    transport.sendto(ALPACA_DISCOVERY_DATAGRAM, (target, ALPACA_DISCOVERY_PORT))
            except OSError as exc:
                # One unroutable broadcast address must not lose the others.
                _log.debug("alpaca discovery to %s: %s", target, exc)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                if connected:
                    await finished.wait()
                else:
                    await asyncio.sleep(timeout)
    finally:
        transport.close()
    return found


async def advertised_alpaca_port(
    host: str,
    timeout: float = DEFAULT_DISCOVERY_TIMEOUT_S,  # noqa: ASYNC109 - a probe budget
) -> int | None:
    """Ask one device's own Alpaca discovery responder, unicast. ``None`` if silent."""
    return (await alpaca_discovery([host], timeout=timeout)).get(host)


async def probe_control_surface(
    host: str,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_S,  # noqa: ASYNC109 - a probe budget
    client_factory: AlpacaClientFactory = default_client_factory,
    discovery_timeout_s: float = DEFAULT_DISCOVERY_TIMEOUT_S,
    discover_port: AlpacaPortDiscovery = advertised_alpaca_port,
) -> SurfaceChoice:
    """Observe which surface ``host`` serves. Never raises for an absent one.

    Both halves are injectable — the UDP advertisement and the HTTP reads — so a
    test can route without a socket and without a device.
    """
    advertised = await discover_port(host, discovery_timeout_s)
    port = advertised or DEFAULT_ALPACA_PORT
    origin = (
        f"UDP {ALPACA_DISCOVERY_PORT} advertised AlpacaPort {port}"
        if advertised
        else f"UDP {ALPACA_DISCOVERY_PORT} did not answer within {discovery_timeout_s:g}s, "
        f"trying the standard Alpaca port {port}"
    )

    client = client_factory(host, port)
    try:
        async with asyncio.timeout(timeout):
            versions = await client.api_versions()
            devices = await client.configured_devices()
    except (AlpacaClientError, TimeoutError, OSError) as exc:
        return SurfaceChoice(
            surface=ControlSurface.JSON_RPC,
            alpaca_port=None,
            evidence=(
                f"{origin}; no Alpaca server answered there "
                f"({type(exc).__name__}: {exc}), so the JSON-RPC control port is the "
                f"only surface left to try"
            ),
        )
    finally:
        await client.aclose()

    if 1 not in versions:
        return SurfaceChoice(
            surface=ControlSurface.JSON_RPC,
            alpaca_port=None,
            evidence=(
                f"{origin}; the server there reported apiversions {versions!r} and SSC "
                f"speaks Alpaca v1, so the JSON-RPC control port is the only surface left"
            ),
        )
    return SurfaceChoice(
        surface=ControlSurface.NATIVE_ALPACA,
        alpaca_port=port,
        evidence=(
            f"{origin}; /management/v1/configureddevices listed {len(devices)} device(s) "
            f"on Alpaca v1"
        ),
    )


#: Observes one host and reports which surface it serves. Injectable so a test
#: routes without a network.
SurfaceProbe = Callable[[str], Awaitable[SurfaceChoice]]


class RoutedSession:
    """Chooses a control surface at :meth:`start`, then is that session.

    The choice needs I/O and ``SeestarBackend`` builds its session synchronously,
    so the probe happens inside ``start()`` — which is also the only honest place
    for it, since the answer can change when a user updates their firmware
    between two connects.

    Every status this publishes carries the routing evidence appended to its
    reason, so "why is SSC talking to my scope this way" is answerable from the
    Settings panel without reading a log.
    """

    def __init__(
        self,
        host: str,
        control_port: int,
        *,
        surface: ControlSurface = ControlSurface.AUTO,
        probe: SurfaceProbe | None = None,
        native_factory: Callable[[str, int], SeestarSession] | None = None,
        jsonrpc_factory: Callable[[str, int], SeestarSession] | None = None,
    ) -> None:
        self._host = host
        self._control_port = control_port
        self._requested = surface
        self._probe = probe
        self._native_factory = native_factory or _default_native_factory
        self._jsonrpc_factory = jsonrpc_factory or _default_jsonrpc_factory
        self._choice: SurfaceChoice | None = None
        self._delegate: SeestarSession | None = None
        self._pending_state: list[StateListener] = []
        self._pending_events: list[EventListener] = []
        self._status = SessionStatus(
            state=ConnectionState.DISCONNECTED,
            endpoint=host,
            reason="no control surface has been chosen yet",
        )

    # -- public surface ----------------------------------------------------- #

    @property
    def choice(self) -> SurfaceChoice | None:
        """What was decided and why, once :meth:`start` has run."""
        return self._choice

    @property
    def delegate(self) -> SeestarSession | None:
        """The session actually driving control, once :meth:`start` has run.

        Exposed so a caller that needs surface-specific behaviour — ``ssc/alpaca/``
        talking directly to the device's own Alpaca server when that is the
        surface in use — does not need private-attribute access across the seam.
        ``None`` before :meth:`start` or after :meth:`aclose`.
        """
        return self._delegate

    @property
    def status(self) -> SessionStatus:
        delegate = self._delegate
        return self._status if delegate is None else self._decorate(delegate.status)

    @property
    def state(self) -> ConnectionState:
        return self.status.state

    @property
    def device(self) -> DeviceInfo | None:
        return None if self._delegate is None else self._delegate.device

    def subscribe_state(self, listener: StateListener) -> Callable[[], None]:
        delegate = self._delegate
        if delegate is not None:
            return delegate.subscribe_state(self._relay(listener))
        self._pending_state.append(listener)
        return lambda: self._pending_state.remove(listener)

    def subscribe_events(self, listener: EventListener) -> Callable[[], None]:
        delegate = self._delegate
        if delegate is not None:
            return delegate.subscribe_events(listener)
        self._pending_events.append(listener)
        return lambda: self._pending_events.remove(listener)

    async def start(self) -> None:
        if self._delegate is not None:
            raise SessionError("session already started")
        choice = await self._select()
        if (
            self._requested is ControlSurface.AUTO
            and choice.surface is ControlSurface.JSON_RPC
            and self._control_port in _ALPACA_PORTS
        ):
            reason = (
                f"No usable native Alpaca surface is answering at "
                f"{self._host}:{self._control_port}. Probe evidence: {choice.evidence}. "
                "Check device service availability and the Alpaca Protocol setting. "
                "SSC did not try legacy JSON-RPC on "
                f"{self._host}:{CONTROL_PORT} because current Seestar firmware can "
                "leave that port open without answering commands."
            )
            self._choice = choice
            self._status = SessionStatus(
                state=ConnectionState.DISCONNECTED,
                endpoint=f"{self._host}:{self._control_port}",
                reason=reason,
            )
            _log.warning("seestar %s: %s", self._host, reason)
            raise SessionError(reason)
        if choice.surface is ControlSurface.NATIVE_ALPACA:
            port = choice.alpaca_port or DEFAULT_ALPACA_PORT
            self._choice = choice
            _log.info("seestar %s: %s", self._host, choice.describe())
            delegate = self._native_factory(self._host, port)
        else:
            port = self._jsonrpc_control_port()
            if port != self._control_port:
                choice = replace(
                    choice,
                    evidence=(
                        f"{choice.evidence} — and :{self._control_port}, the port this "
                        f"address was remembered with, is an Alpaca port rather than a "
                        f"Seestar JSON-RPC one, so :{port} is used instead"
                    ),
                )
            self._choice = choice
            _log.info("seestar %s: %s", self._host, choice.describe())
            delegate = self._jsonrpc_factory(self._host, port)
        self._delegate = delegate

        for listener in self._pending_state:
            delegate.subscribe_state(self._relay(listener))
        for event_listener in self._pending_events:
            delegate.subscribe_events(event_listener)
        self._pending_state.clear()
        self._pending_events.clear()
        await delegate.start()

    async def aclose(self) -> None:
        delegate, self._delegate = self._delegate, None
        if delegate is None:
            return
        try:
            await delegate.aclose()
        finally:
            self._status = self._decorate(delegate.status)
            self._choice = None

    async def wait_for_state(
        self,
        states: Collection[ConnectionState],
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 - the caller's budget
    ) -> SessionStatus:
        return self._decorate(await self._require().wait_for_state(states, timeout=timeout))

    async def call(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors the session protocol
        raise_on_error: bool = True,
        allow_unverified: bool = False,
    ) -> RpcResponse:
        return await self._require().call(
            method,
            params,
            timeout=timeout,
            raise_on_error=raise_on_error,
            allow_unverified=allow_unverified,
        )

    async def get_device_state(
        self,
        keys: Sequence[str] = CAPABILITY_PROBE_KEYS,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors the session protocol
    ) -> DeviceState:
        return await self._require().get_device_state(keys, timeout=timeout)

    # -- internals ---------------------------------------------------------- #

    async def _select(self) -> SurfaceChoice:
        if self._requested is not ControlSurface.AUTO:
            return SurfaceChoice(
                surface=self._requested,
                alpaca_port=DEFAULT_ALPACA_PORT
                if self._requested is ControlSurface.NATIVE_ALPACA
                else None,
                evidence=f"chosen by {CONTROL_SURFACE_ENV}, not by probing the device",
            )
        probe = self._probe
        if probe is None:
            return await probe_control_surface(self._host)
        return await probe(self._host)

    def _jsonrpc_control_port(self) -> int:
        """The port a JSON-RPC session may actually be pointed at.

        The remembered address carries whichever port last worked, and on a
        device driven over its own Alpaca server that is :32323. When the
        Alpaca probe then fails transiently, falling back to "JSON-RPC on the
        remembered port" aims the JSON-RPC session at the Alpaca server, where
        it can never succeed — observed in the field as ``connection lost with
        request 1 in flight``. A fallback that cannot work is worse than none:
        it hides the real failure behind a second, invented one.
        """
        if self._control_port not in _ALPACA_PORTS:
            return self._control_port
        _log.warning(
            "seestar %s: :%d is an Alpaca port and cannot speak Seestar JSON-RPC; "
            "the JSON-RPC fallback will use the control port :%d instead",
            self._host,
            self._control_port,
            CONTROL_PORT,
        )
        return CONTROL_PORT

    def _require(self) -> SeestarSession:
        delegate = self._delegate
        if delegate is None:
            raise SessionError("no control surface has been chosen yet; call start() first")
        return delegate

    def _relay(self, listener: StateListener) -> StateListener:
        def relay(status: SessionStatus) -> None:
            listener(self._decorate(status))

        return relay

    def _decorate(self, status: SessionStatus) -> SessionStatus:
        """Append the routing evidence to whatever the delegate published."""
        choice = self._choice
        if choice is None:
            return status
        reason = "; ".join(part for part in (status.reason, choice.describe()) if part)
        return replace(status, reason=reason)


def _default_native_factory(host: str, port: int) -> SeestarSession:
    return NativeAlpacaSession(host, port=port)


def _default_jsonrpc_factory(host: str, port: int) -> SeestarSession:
    return JsonRpcSeestarSession(
        TcpTransportFactory(host, control_port=port, event_port=EVENT_PORT)
    )


def routed_session(host: str, control_port: int) -> SeestarSession:
    """The production session factory: probe first, then be the right session."""
    return RoutedSession(host, control_port, surface=forced_surface())


__all__ = [
    "ALPACA_DISCOVERY_DATAGRAM",
    "ALPACA_DISCOVERY_PORT",
    "CONTROL_SURFACE_ENV",
    "DEFAULT_PROBE_TIMEOUT_S",
    "ControlSurface",
    "RoutedSession",
    "SurfaceChoice",
    "advertised_alpaca_port",
    "forced_surface",
    "probe_control_surface",
    "routed_session",
]
