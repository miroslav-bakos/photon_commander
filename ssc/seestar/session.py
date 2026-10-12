# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Connection lifecycle, handshake, capability negotiation and reconnect policy.

This is the **only** file allowed to know about authentication, firmware gates or
capability negotiation, so a firmware change is a one-file fix.

State machine (published on every transition — never a silent reconnect)::

    DISCONNECTED -> CONNECTING -> HANDSHAKING -> READY
                        ^                          |
                        +------- DEGRADED <--------+

What firmware 8.46 actually does (``docs/M0-FINDINGS.md``):

* **No auth.** ``test_connection`` answers immediately with no challenge and no
  token, on a firmware well past the 7.18 change that introduced
  challenge-response. ``pi_is_verified`` returns ``true`` and ``get_setting``
  reports ``connect_lock: false``.
* Because auth may be a *device setting* rather than a firmware gate, the locked
  path is a supported branch here — it just is not the mandatory one. No
  challenge has ever been observed, so no challenge algorithm is implemented:
  a locked device fails loudly with :class:`AuthenticationRequiredError` rather
  than guessing.
* Capability negotiation uses the device's own ``can_*`` flags from
  ``get_device_state(keys=["device"])``. We do not invent a probe.
* ``get_device_state`` **without** ``params`` crashes the control server, which
  then refuses connections for several seconds. That call is blocked here, and
  ``ECONNRESET`` is treated as "the control server restarted": jittered
  exponential backoff with a cap, never a hot loop.
* The event stream on ``:4700`` closes after **15.0 s with no client traffic**
  — an idle timeout, not a push-then-close. A client write resets it, so a
  keepalive holds the stream open indefinitely (measured: 110 s, zero frames, at
  an idle device). Either way the close is *normal*: the session re-attaches
  without degrading. Only repeated failure to re-attach is a degradation, and it
  is reported separately from control, because losing telemetry is not the same
  failure as losing control.
* Code ``210`` is **not** "method not found". The method exists and the device's
  current state does not suit it; it is surfaced as
  :class:`PreconditionNotMetError` with the documented remedy, and it never
  reconnects and never marks a capability absent. See
  :data:`METHOD_PRECONDITIONS`.

Nothing in this module logs, caches or returns credential material. The two
credential-bearing device-state keys (``ap``, ``setting``) are never requested,
and every raw payload passes through ``transport.redact`` before it can be logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol

from ssc.seestar.events import (
    EventParser,
    EventStreamClosed,
    EventStreamOpened,
    SeestarEvent,
)
from ssc.seestar.models import (
    CREDENTIAL_BEARING_KEYS,
    DeviceIdentity,
    DeviceState,
)
from ssc.seestar.rpc import (
    DEFAULT_TIMEOUT_S,
    JsonRpcClient,
    RpcError,
    RpcNotSupportedError,
    RpcResponse,
)
from ssc.seestar.transport import (
    ConnectionLostError,
    TransportError,
    TransportFactory,
    redact,
)

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Method vocabulary — observation, not folklore
# --------------------------------------------------------------------------- #

METHOD_TEST_CONNECTION: Final = "test_connection"
METHOD_GET_DEVICE_STATE: Final = "get_device_state"
METHOD_PI_IS_VERIFIED: Final = "pi_is_verified"
METHOD_GET_SETTING: Final = "get_setting"

#: Read-only methods confirmed to exist on firmware 8.46, every one of them
#: recorded in ``tests/fixtures/wire/8.46/``.
VERIFIED_READ_METHODS: Final[frozenset[str]] = frozenset(
    {
        METHOD_TEST_CONNECTION,
        METHOD_GET_DEVICE_STATE,
        METHOD_GET_SETTING,
        METHOD_PI_IS_VERIFIED,
        "get_app_state",
        "get_camera_info",
        "get_camera_state",
        "get_camera_exp_and_bin",
        "get_controls",
        "get_focuser_position",
        "get_focuser_state",
        "get_wheel_position",
        "get_wheel_setting",
        "get_wheel_state",
        "get_stack_info",
        "get_last_solve_result",
        "get_solve_result",
        "get_albums",
        "get_disk_volume",
        "get_view_state",
        "pi_get_time",
        "pi_station_state",
    }
)

#: Verbs confirmed on 8.46 **with the user at the scope**. These act. The first
#: four were verified end to end — ``iscope_start_view {"mode":"star"}`` then
#: ``start_auto_goto`` physically slewed the mount, ``mount.move_type`` read
#: ``"ScopeGoto"`` while moving and ``"none"`` once settled.
#:
#: The ``stop_*`` family was mapped from the device's own operation list, which
#: ``get_app_state`` publishes: annotate, solve, capture, pa, pa_3p, auto_goto,
#: stack, export_image, merid_flip, auto_focus, find_star, avi_record, rtmp,
#: auto_exp, restart_guide. Deriving ``stop_<op>`` from it and probing while idle
#: (safe: a stop is a no-op when nothing is running) confirmed the ones below —
#: and, importantly, refuted the rest: ``stop_merid_flip``, ``stop_pa``,
#: ``stop_pa_3p``, ``stop_avi_record``, ``stop_rtmp``, ``stop_auto_exp`` and
#: ``stop_restart_guide`` all answer 103. The convention holds for most of the
#: operation list but **not all of it**, so the list is enumerated, not derived.
VERIFIED_COMMAND_METHODS: Final[frozenset[str]] = frozenset(
    {
        "iscope_start_view",
        "iscope_stop_view",
        "start_auto_goto",
        "stop_auto_goto",
        "stop_annotate",
        "stop_solve",
        "stop_stack",
        "stop_export_image",
        "stop_find_star",
        "stop_exposure",
        "stop_polar_align",
        "stop_create_dark",
    }
)

#: Everything the method gate will let through. Anything not in here is refused
#: locally rather than tried against the device: a ``103`` is harmless, but the
#: device's input validation is demonstrably not uniformly safe.
VERIFIED_METHODS: Final[frozenset[str]] = VERIFIED_READ_METHODS | VERIFIED_COMMAND_METHODS

#: Probed on 8.46 and confirmed **absent** (all returned code 103). Several of
#: these circulate in community projects written against older firmware. Refusing
#: them locally turns a silent round-trip into an explicit "not on this firmware".
KNOWN_ABSENT_METHODS: Final[frozenset[str]] = frozenset(
    {
        "scope_get_equ_coord",
        "scope_get_ra_dec",
        "scope_get_horiz_coord",
        "scope_get_track_state",
        "scope_is_moving",
        "scope_speed_move",
        "get_time",
        "get_sequence_state",
        "get_annotated_result",
        "get_album_list",
        "get_user_location",
        "get_device_info",
        "get_bat_state",
        "get_error_state",
        # There is no scope_* namespace on 8.46 at all, and no park/unpark verb
        # under any name. Community projects written for older firmware use
        # these; porting that vocabulary fails wholesale.
        "scope_park",
        "scope_unpark",
        "unpark",
        "open_arm",
        "raise_arm",
        "move_to_horizon",
        "scope_move_to_horizon",
        # stop_<op> derived from get_app_state's operation list, probed, absent.
        "stop_merid_flip",
        "stop_pa",
        "stop_pa_3p",
        "stop_avi_record",
        "stop_rtmp",
        "stop_auto_exp",
        "stop_restart_guide",
        "stop_capture",
        "stop_auto_focus",
        "stop_view",
    }
)

#: Methods whose *result* contains cleartext credentials. Callers get a loud
#: warning and the raw payload is never logged unredacted.
CREDENTIAL_BEARING_METHODS: Final[frozenset[str]] = frozenset({METHOD_GET_SETTING})

#: ``get_device_state`` keys the session requests during capability probing.
#: ``ap`` and ``setting`` are deliberately excluded — they return cleartext
#: passwords and SSC has no use for them.
CAPABILITY_PROBE_KEYS: Final[tuple[str, ...]] = (
    "device",
    "mount",
    "focuser",
    "camera",
    "pi_status",
    "storage",
    "balance_sensor",
)

#: Firmware versions this driver has actually been exercised against. An
#: unrecognised firmware is DEGRADED with a reason, never a crash and never a
#: shrug (``architecture policy`` invariant 7).
VERIFIED_FIRMWARE: Final[frozenset[str]] = frozenset({"8.46"})

#: A command vocabulary now exists: see :data:`VERIFIED_COMMAND_METHODS`. It is
#: not complete — no ``start_stack``, ``start_solve`` or ``start_auto_focus`` has
#: been called yet, and there is no park/unpark verb on this firmware at all —
#: but the view/goto path is verified end to end and the naming convention is
#: mapped. Still: no verb is *guessed* here. Absence stays absence.
COMMAND_VOCABULARY_KNOWN: Final = True


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class SessionError(Exception):
    """Base class for session-level failures."""


class NotConnectedError(SessionError):
    """A call was attempted while the control channel was down."""


class AuthenticationRequiredError(SessionError):
    """The device is locked and SSC cannot complete a handshake.

    No challenge-response exchange has ever been observed on this hardware, so
    none is implemented. Failing loudly is correct; guessing an algorithm is not.
    """


class UnverifiedMethodError(SessionError):
    """The method has not been verified against the device's firmware."""


class MethodNotAvailableError(SessionError):
    """The method was probed on this firmware and confirmed absent."""


class HazardousCallError(SessionError):
    """The call is known to crash the device's control server."""


class PreconditionNotMetError(SessionError):
    """The device answered ``210`` — the method exists, this state does not suit.

    Raised in place of :class:`~ssc.seestar.rpc.RpcNotSupportedError` so the
    caller gets the *documented* precondition rather than the device's bare
    "not supported". Three things this is not, all of them tempting:

    * not :class:`MethodNotAvailableError` — the verb is real and will work;
    * not a reason to reconnect — the control channel is fine;
    * not ASCOM ``NotImplemented``. ``ssc/alpaca/`` must map this to
      ``InvalidOperationException``, never to ``0x400``, or a working feature is
      disabled for the rest of the session.
    """

    def __init__(self, method: str, requires: str | None, detail: str) -> None:
        super().__init__(
            f"{method} is not available in the device's current state ({detail})"
            + (f"; it requires {requires}" if requires else "")
        )
        self.method = method
        self.requires = requires


@dataclass(frozen=True, slots=True)
class MethodPrecondition:
    """The device state a verb needs before it will do anything.

    The device does not publish these; each one costs a supervised run at real
    hardware to establish, so the set is small and every entry names the firmware
    it was observed on.
    """

    method: str
    #: Human-readable, and the string the user is shown.
    requires: str
    #: How a caller checks it, so this is actionable rather than folklore.
    check: str
    verified_on: str


#: Observed state gates. ``start_auto_goto`` is the only one established so far;
#: it is also the one that matters most, because getting it wrong either bricks
#: the goto path (reading 210 as "not implemented") or slews the mount.
METHOD_PRECONDITIONS: Final[Mapping[str, MethodPrecondition]] = MappingProxyType(
    {
        "start_auto_goto": MethodPrecondition(
            method="start_auto_goto",
            requires=(
                'star view mode running (iscope_start_view {"mode": "star"}) '
                "and the arm open (mount.close == false, which only the user can "
                "do — no 8.46 verb raises it)"
            ),
            check=(
                "get_view_state / get_app_state show a running view, and "
                "get_device_state(keys=['mount']) reports close == false"
            ),
            verified_on="8.46",
        ),
    }
)


# --------------------------------------------------------------------------- #
# Connection state
# --------------------------------------------------------------------------- #


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    READY = "ready"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """What SSC knows about a device. ``None`` always means *unknown*."""

    host: str
    control_port: int
    reachable: bool
    firmware: str | None = None
    firmware_int: int | None = None
    model: str | None = None
    serial: str | None = None
    is_verified: bool | None = None
    #: The device's own ``can_*`` flags, verbatim.
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    device_uptime_s: float | None = None
    #: Why a probe failed, or what is unverified about this device.
    note: str | None = None

    @property
    def firmware_is_verified(self) -> bool:
        return self.firmware is not None and self.firmware in VERIFIED_FIRMWARE


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """A published snapshot of the connection. Every transition emits one."""

    state: ConnectionState
    endpoint: str
    reason: str | None = None
    device: DeviceInfo | None = None
    control_connected: bool = False
    events_connected: bool = False
    #: Events discarded because subscribers could not keep up. Surfaced, not hidden.
    events_dropped: int = 0
    connect_attempts: int = 0
    changed_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def same_as(self, other: SessionStatus) -> bool:
        """Equality ignoring the timestamp, so we only publish real changes."""
        return replace(self, changed_at=other.changed_at) == other


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Jittered exponential backoff with a cap.

    Half-jitter rather than full jitter, so the delay can never collapse towards
    zero. Holding the device's control port down with a hot retry loop is the
    specific failure this exists to prevent.
    """

    initial_s: float = 1.0
    factor: float = 2.0
    cap_s: float = 30.0

    def delay(self, attempt: int, rng: random.Random) -> float:
        base = min(self.cap_s, self.initial_s * self.factor ** max(0, attempt - 1))
        return base / 2.0 + rng.random() * (base / 2.0)


StateListener = Callable[[SessionStatus], None]
EventListener = Callable[[SeestarEvent], None]
Sleeper = Callable[[float], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# --------------------------------------------------------------------------- #
# One-shot identification (used by discovery and by "Test connection")
# --------------------------------------------------------------------------- #


async def identify(
    factory: TransportFactory,
    *,
    timeout: float = 3.0,  # noqa: ASYNC109 - a probe budget is the public contract
) -> DeviceInfo:
    """Connect, identify, disconnect. Reports the real result, never a green tick.

    Sends only ``test_connection`` and ``get_device_state(keys=["device"])`` —
    both verified safe. Never raises for an unreachable host: an unreachable
    device is a *result*, carried in :attr:`DeviceInfo.note`.
    """
    control = factory.control()
    try:
        await control.connect()
    except TransportError as exc:
        return DeviceInfo(
            host=factory.host,
            control_port=factory.control_port,
            reachable=False,
            note=f"{type(exc).__name__}: {exc}",
        )

    rpc = JsonRpcClient(control, default_timeout=timeout)
    await rpc.start()
    try:
        try:
            hello = await rpc.call(METHOD_TEST_CONNECTION, timeout=timeout)
        except (RpcError, TransportError) as exc:
            return DeviceInfo(
                host=factory.host,
                control_port=factory.control_port,
                reachable=False,
                note=f"connected but did not answer test_connection: {exc}",
            )

        identity: DeviceIdentity | None = None
        note: str | None = None
        try:
            state = await _device_state(rpc, ("device",), timeout=timeout)
            identity = state.device
            if identity is None:
                note = "device answered test_connection but reported no identity"
        except (RpcError, TransportError) as exc:
            note = f"identity unavailable: {exc}"

        return _device_info(factory, hello, identity, note)
    finally:
        await rpc.aclose()


def _device_info(
    factory: TransportFactory,
    hello: RpcResponse,
    identity: DeviceIdentity | None,
    note: str | None,
) -> DeviceInfo:
    info = DeviceInfo(
        host=factory.host,
        control_port=factory.control_port,
        reachable=True,
        firmware=identity.firmware_version if identity else None,
        firmware_int=identity.firmware_version_int if identity else None,
        model=(identity.user_product_model or identity.product_model) if identity else None,
        serial=identity.serial_number if identity else None,
        is_verified=identity.is_verified if identity else None,
        capabilities=dict(identity.capabilities) if identity else {},
        device_uptime_s=hello.device_uptime_s,
        note=note,
    )
    if info.note is None and not info.firmware_is_verified:
        return replace(info, note=_firmware_note(info.firmware))
    return info


def _firmware_note(firmware: str | None) -> str:
    if firmware is None:
        return "firmware version not reported — capabilities unverified"
    return (
        f"firmware {firmware} has not been verified against this driver "
        f"(verified: {', '.join(sorted(VERIFIED_FIRMWARE)) or 'none'})"
    )


async def _device_state(
    rpc: JsonRpcClient,
    keys: Sequence[str],
    *,
    timeout: float | None = None,  # noqa: ASYNC109 - mirrors JsonRpcClient.call
) -> DeviceState:
    """``get_device_state`` with a mandatory, credential-free key list."""
    requested = _validate_device_state_keys(keys)
    response = await rpc.call(METHOD_GET_DEVICE_STATE, {"keys": list(requested)}, timeout=timeout)
    payload = response.result
    if not isinstance(payload, Mapping):
        return DeviceState(missing_keys=requested)
    return DeviceState.from_payload({str(k): v for k, v in payload.items()}, requested)


def _validate_device_state_keys(keys: Iterable[str]) -> tuple[str, ...]:
    requested = tuple(keys)
    if not requested:
        # This exact call is what crashes the control server.
        raise HazardousCallError(
            "get_device_state with no keys resets the device's control server and the "
            "control port then refuses connections for several seconds — pass keys"
        )
    leaking = sorted(set(requested) & CREDENTIAL_BEARING_KEYS)
    if leaking:
        raise SessionError(
            f"refusing to request credential-bearing get_device_state keys: {leaking}"
        )
    return requested


# --------------------------------------------------------------------------- #
# The session seam
# --------------------------------------------------------------------------- #


class SeestarSession(Protocol):
    """What everything above ``ssc/seestar/`` may assume about a live device link.

    There are two implementations, and the fork is here rather than anywhere
    above it, because this is the only place the two firmwares differ:

    * :class:`JsonRpcSeestarSession` — the newline-delimited JSON-RPC control
      port (:4701) plus the event stream (:4700). Verified on 8.46.
    * :class:`~ssc.seestar.native.NativeAlpacaSession` — the device's *own*
      ASCOM Alpaca server (:32323), which is the only control surface on builds
      that have withdrawn the JSON-RPC port.

    Which one a given device gets is decided by observation, never by a version
    number — see :mod:`ssc.seestar.routing`.
    """

    @property
    def status(self) -> SessionStatus:
        """The published connection snapshot. Never ``None``, never a guess."""
        ...

    @property
    def state(self) -> ConnectionState: ...

    @property
    def device(self) -> DeviceInfo | None:
        """What is known about the device; ``None`` means *not yet identified*."""
        ...

    def subscribe_state(self, listener: StateListener) -> Callable[[], None]: ...

    def subscribe_events(self, listener: EventListener) -> Callable[[], None]:
        """Register an event listener. A surface with no event channel accepts
        the subscription and never calls it — it says so in :attr:`status`."""
        ...

    async def start(self) -> None: ...

    async def aclose(self) -> None: ...

    async def wait_for_state(
        self,
        states: Collection[ConnectionState],
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 - the caller's budget
    ) -> SessionStatus: ...

    async def call(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors JsonRpcClient.call
        raise_on_error: bool = True,
        allow_unverified: bool = False,
    ) -> RpcResponse:
        """Send one Seestar JSON-RPC method.

        A surface that has no such vocabulary raises
        :class:`MethodNotAvailableError` naming the surface — it never invents a
        plausible empty result.
        """
        ...

    async def get_device_state(
        self,
        keys: Sequence[str] = CAPABILITY_PROBE_KEYS,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors JsonRpcClient.call
    ) -> DeviceState: ...


# --------------------------------------------------------------------------- #
# The JSON-RPC session
# --------------------------------------------------------------------------- #


class JsonRpcSeestarSession:
    """The JSON-RPC control surface: owns the control/event connection pair.

    The two connections are supervised by two independent tasks: the control
    channel drives the headline state, the event channel contributes a separate
    health signal. Neither one's failure silently masks the other's.

    Verified on firmware 8.46. Builds that have withdrawn :4701 accept the TCP
    connection and never answer, so this session reaches ``HANDSHAKING`` and
    times out there — honestly, and with a reason — which is why the choice of
    surface is made by probe before one of these is built at all
    (:mod:`ssc.seestar.routing`).
    """

    def __init__(
        self,
        factory: TransportFactory,
        *,
        backoff: BackoffPolicy | None = None,
        rng: random.Random | None = None,
        sleeper: Sleeper = asyncio.sleep,
        call_timeout: float = DEFAULT_TIMEOUT_S,
        event_parser: EventParser | None = None,
        event_queue_size: int = 256,
        event_reattach_delay_s: float = 0.2,
        event_failure_threshold: int = 3,
    ) -> None:
        self._factory = factory
        self._backoff = backoff or BackoffPolicy()
        self._rng = rng or random.Random()
        self._sleep = sleeper
        self._call_timeout = call_timeout
        self._parser = event_parser or EventParser()
        self._event_reattach_delay_s = event_reattach_delay_s
        self._event_failure_threshold = event_failure_threshold

        self._stop = asyncio.Event()
        self._rpc: JsonRpcClient | None = None
        self._device: DeviceInfo | None = None
        self._phase = ConnectionState.DISCONNECTED
        self._phase_reason: str | None = None
        self._control_up = False
        self._connect_attempts = 0
        self._events_connected = False
        self._events_failures = 0
        self._events_reason: str | None = None
        self._events_dropped = 0

        self._status = SessionStatus(state=ConnectionState.DISCONNECTED, endpoint=factory.endpoint)
        self._state_listeners: list[StateListener] = []
        self._event_listeners: list[EventListener] = []
        self._waiters: list[asyncio.Future[SessionStatus]] = []
        self._queue: asyncio.Queue[SeestarEvent] = asyncio.Queue(maxsize=event_queue_size)
        self._tasks: list[asyncio.Task[None]] = []

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

    def subscribe_state(self, listener: StateListener) -> Callable[[], None]:
        self._state_listeners.append(listener)
        return lambda: self._state_listeners.remove(listener)

    def subscribe_events(self, listener: EventListener) -> Callable[[], None]:
        self._event_listeners.append(listener)
        return lambda: self._event_listeners.remove(listener)

    async def start(self) -> None:
        if self._tasks:
            raise SessionError("session already started")
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._run_control(), name="seestar-control"),
            asyncio.create_task(self._run_events(), name="seestar-events"),
            asyncio.create_task(self._run_dispatch(), name="seestar-event-dispatch"),
        ]

    async def aclose(self) -> None:
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        rpc, self._rpc = self._rpc, None
        if rpc is not None:
            await rpc.aclose()
        self._control_up = False
        self._events_connected = False
        self._enter(ConnectionState.DISCONNECTED, "session closed")

    async def wait_for_state(
        self,
        states: Collection[ConnectionState],
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 - test/caller budget
    ) -> SessionStatus:
        """Block until the published state is one of ``states``."""
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
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors JsonRpcClient.call
        raise_on_error: bool = True,
        allow_unverified: bool = False,
    ) -> RpcResponse:
        """Issue a call, subject to the firmware method gate.

        Args:
            allow_unverified: bypass the "verified on this firmware" check. For
                the emulator and for fixture capture with the user present — not
                for production paths. Hazardous calls are refused regardless.
        """
        self._guard_method(method, params, allow_unverified=allow_unverified)
        rpc = self._rpc
        if rpc is None or not self._control_up:
            raise NotConnectedError(f"cannot call {method}: control channel is {self.state}")
        if method in CREDENTIAL_BEARING_METHODS:
            _log.warning(
                "%s returns cleartext credentials; the result must never be logged, "
                "cached, displayed or written to a fixture unredacted",
                method,
            )
        try:
            return await rpc.call(
                method, params, timeout=timeout or self._call_timeout, raise_on_error=raise_on_error
            )
        except RpcNotSupportedError as exc:
            # Code 210. The channel is healthy and the verb is real, so this
            # returns to the caller as a precondition failure and nothing else
            # happens: no reconnect, no state transition, no capability being
            # struck off. See PreconditionNotMetError.
            precondition = METHOD_PRECONDITIONS.get(method)
            raise PreconditionNotMetError(
                method,
                precondition.requires if precondition else None,
                str(exc.error),
            ) from exc

    async def get_device_state(
        self,
        keys: Sequence[str] = CAPABILITY_PROBE_KEYS,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors JsonRpcClient.call
    ) -> DeviceState:
        """Typed ``get_device_state``. Keys are mandatory and credential-free."""
        rpc = self._rpc
        if rpc is None or not self._control_up:
            raise NotConnectedError(f"cannot read device state: control channel is {self.state}")
        return await _device_state(rpc, keys, timeout=timeout or self._call_timeout)

    # -- method gate -------------------------------------------------------- #

    def _guard_method(
        self,
        method: str,
        params: Mapping[str, object] | None,
        *,
        allow_unverified: bool,
    ) -> None:
        if method == METHOD_GET_DEVICE_STATE:
            keys = (params or {}).get("keys")
            if not isinstance(keys, Sequence) or isinstance(keys, str) or not keys:
                raise HazardousCallError(
                    "get_device_state with no keys resets the device's control server; "
                    "pass params={'keys': [...]} or use get_device_state()"
                )
            _validate_device_state_keys([str(k) for k in keys])
            return
        if method in KNOWN_ABSENT_METHODS:
            raise MethodNotAvailableError(
                f"{method} was probed on firmware 8.46 and does not exist (code 103)"
            )
        if method not in VERIFIED_METHODS and not allow_unverified:
            raise UnverifiedMethodError(
                f"{method} has not been verified against this device's firmware; "
                "verify it against a fixture or the emulator first"
            )

    # -- handshake (auth lives here and nowhere else) ------------------------ #

    def _handshake_step(self, method: str) -> None:
        """Publish what the handshake is currently waiting for.

        A stalled handshake must never be reasonless (invariant 7). Firmware
        9.31 is why this exists: its control port accepts TCP and answers
        nothing, so the handshake simply hangs on the first call — and because
        the RPC budget and the caller's readiness budget are both
        :data:`DEFAULT_TIMEOUT_S`, the call's own timeout loses that race by
        milliseconds and never gets to publish a reason. The only thing the user
        was told was "no reason published".
        """
        self._enter(ConnectionState.HANDSHAKING, f"waiting for a reply to {method}")

    async def _handshake(self, rpc: JsonRpcClient) -> DeviceInfo:
        self._handshake_step(METHOD_TEST_CONNECTION)
        hello = await rpc.call(METHOD_TEST_CONNECTION, timeout=self._call_timeout)

        # Auth gate. On 8.46 this returns True and nothing further is required.
        # A locked device is a supported *branch*, not the mandatory path.
        verified: bool | None = None
        self._handshake_step(METHOD_PI_IS_VERIFIED)
        try:
            verified_response = await rpc.call(METHOD_PI_IS_VERIFIED, timeout=self._call_timeout)
            if isinstance(verified_response.result, bool):
                verified = verified_response.result
        except RpcError as exc:
            _log.info("pi_is_verified unavailable (%s); continuing unauthenticated", exc)
        if verified is False:
            raise AuthenticationRequiredError(
                "device reports pi_is_verified=false (locked). No challenge-response "
                "exchange has been observed on any firmware, so none is implemented — "
                "unlock the device in the vendor app, or capture the handshake first"
            )

        identity, note = await self._probe_capabilities(rpc)
        info = _device_info(self._factory, hello, identity, note)
        if not info.firmware_is_verified:
            _log.warning("connected to an unverified device: %s", info.note)
        return info

    async def _probe_capabilities(
        self, rpc: JsonRpcClient
    ) -> tuple[DeviceIdentity | None, str | None]:
        """Read the device's own ``can_*`` flags. No behavioural probing."""
        self._handshake_step(METHOD_GET_DEVICE_STATE)
        try:
            state = await _device_state(rpc, CAPABILITY_PROBE_KEYS, timeout=self._call_timeout)
        except RpcError as exc:
            return None, f"capability probe failed: {exc}"
        note: str | None = None
        unanswered = sorted({*state.empty_keys, *state.missing_keys})
        if unanswered:
            # An unrecognised key is dropped from the reply with code 0, so this
            # is genuinely ambiguous and is reported as such rather than read as
            # "this device does not have that".
            note = (
                f"device returned nothing for {', '.join(unanswered)} — "
                "no data, or not a valid key on this firmware"
            )
        if state.device is None:
            return None, note or "device state carried no identity section"
        return state.device, note

    # -- control supervisor ------------------------------------------------- #

    async def _run_control(self) -> None:
        while not self._stop.is_set():
            self._enter(ConnectionState.CONNECTING, None)
            control = self._factory.control()
            try:
                await control.connect()
            except TransportError as exc:
                await self._retry(f"connect failed: {exc}")
                continue

            rpc = JsonRpcClient(
                control,
                default_timeout=self._call_timeout,
                on_unsolicited=self._on_unsolicited_control,
            )
            await rpc.start()
            try:
                device = await self._handshake(rpc)
            except AuthenticationRequiredError as exc:
                await rpc.aclose()
                await self._retry(f"authentication required: {exc}")
                continue
            except (RpcError, TransportError, SessionError) as exc:
                await rpc.aclose()
                await self._retry(f"handshake failed: {type(exc).__name__}: {exc}")
                continue

            self._connect_attempts = 0
            self._device = device
            self._rpc = rpc
            self._control_up = True
            self._phase_reason = None
            self._publish()

            failure = await rpc.wait_closed()
            self._control_up = False
            self._rpc = None
            await rpc.aclose()
            if self._stop.is_set():
                break
            await self._retry(_describe_control_loss(failure))

    async def _retry(self, reason: str) -> None:
        self._connect_attempts += 1
        self._enter(ConnectionState.DISCONNECTED, reason)
        delay = self._backoff.delay(self._connect_attempts, self._rng)
        _log.warning(
            "control channel down (attempt %d): %s — reconnecting in %.2fs",
            self._connect_attempts,
            reason,
            delay,
        )
        await self._sleep(delay)

    def _on_unsolicited_control(self, raw: Mapping[str, object]) -> None:
        """Anything the control channel says without being asked.

        Firmware 8.46 says nothing. This is the seam where a greeting or an
        auth challenge on some other firmware would first become visible — so it
        is recorded by *shape*, never by content.
        """
        _log.info(
            "unsolicited control frame (keys: %s): %s",
            sorted(raw),
            redact(raw),
        )

    # -- event supervisor (independent lifecycle) --------------------------- #

    async def _run_events(self) -> None:
        while not self._stop.is_set():
            transport = self._factory.events()
            try:
                await transport.connect()
            except TransportError as exc:
                self._events_failures += 1
                self._events_connected = False
                if self._events_failures >= self._event_failure_threshold:
                    self._events_reason = (
                        f"event stream unreachable after {self._events_failures} attempts: {exc}"
                    )
                self._publish()
                await self._sleep(self._backoff.delay(self._events_failures, self._rng))
                continue

            self._events_failures = 0
            self._events_connected = True
            self._events_reason = None
            self._publish()
            self._offer(EventStreamOpened(endpoint=self._factory.endpoint, at=_utcnow()))

            expected = True
            reason = "closed by device"
            try:
                while True:
                    line = await transport.receive_line()
                    event = self._parser.parse(line)
                    if event is not None:
                        self._offer(event)
            except ConnectionLostError as exc:
                # The device pushes an event and then closes: a clean EOF here is
                # routine. A reset is not.
                expected = not exc.was_reset
                reason = str(exc)
            except TransportError as exc:
                expected = False
                reason = str(exc)
            finally:
                await transport.aclose()

            self._events_connected = False
            self._offer(
                EventStreamClosed(
                    endpoint=self._factory.endpoint,
                    reason=reason,
                    expected=expected,
                    at=_utcnow(),
                )
            )
            if not expected:
                self._events_failures += 1
                if self._events_failures >= self._event_failure_threshold:
                    self._events_reason = f"event stream lost: {reason}"
            self._publish()
            if self._stop.is_set():
                break
            await self._sleep(self._event_reattach_delay_s)

    def _offer(self, event: SeestarEvent) -> None:
        """Enqueue with backpressure. Nothing here awaits, so nothing blocks the
        reader; when subscribers fall behind we drop the *oldest* event and say so."""
        while True:
            try:
                self._queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
                self._events_dropped += 1
                if self._events_dropped % 100 == 1:
                    _log.warning(
                        "event subscribers are falling behind; %d event(s) dropped",
                        self._events_dropped,
                    )

    async def _run_dispatch(self) -> None:
        while True:
            event = await self._queue.get()
            for listener in list(self._event_listeners):
                try:
                    listener(event)
                except Exception:
                    _log.exception("event listener raised for %s", type(event).__name__)

    # -- state publication -------------------------------------------------- #

    def _enter(self, phase: ConnectionState, reason: str | None) -> None:
        self._phase = phase
        self._phase_reason = reason
        self._publish()

    def _degradations(self) -> list[str]:
        reasons: list[str] = []
        device = self._device
        if device is not None and not device.firmware_is_verified:
            reasons.append(device.note or _firmware_note(device.firmware))
        if self._events_reason is not None:
            reasons.append(self._events_reason)
        return reasons

    def _publish(self) -> None:
        if self._control_up:
            degradations = self._degradations()
            state = ConnectionState.DEGRADED if degradations else ConnectionState.READY
            reason = "; ".join(degradations) or None
        else:
            state = self._phase
            reason = self._phase_reason

        candidate = SessionStatus(
            state=state,
            endpoint=self._factory.endpoint,
            reason=reason,
            device=self._device,
            control_connected=self._control_up,
            events_connected=self._events_connected,
            events_dropped=self._events_dropped,
            connect_attempts=self._connect_attempts,
            changed_at=self._status.changed_at,
        )
        if candidate.same_as(self._status):
            return
        self._status = replace(candidate, changed_at=_utcnow())
        _log.info(
            "seestar %s: %s%s", self._factory.endpoint, state, f" ({reason})" if reason else ""
        )
        for listener in list(self._state_listeners):
            try:
                listener(self._status)
            except Exception:
                _log.exception("state listener raised")
        for waiter in list(self._waiters):
            if not waiter.done():
                waiter.set_result(self._status)


def _describe_control_loss(failure: BaseException | None) -> str:
    if isinstance(failure, ConnectionLostError) and failure.was_reset:
        return f"control connection reset (ECONNRESET) — the control server restarted: {failure}"
    if failure is None:
        return "control connection closed"
    return f"control connection lost: {type(failure).__name__}: {failure}"
