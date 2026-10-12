# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The seam: one Seestar session, seven ASCOM devices sitting on top of it.

``ssc/alpaca/`` is the sole importer of ``ssc/seestar/`` (``CLAUDE.md``
invariant 1) and this module is the only file in it that talks to a session. The
device classes above it see typed reads, ASCOM errors and a verb gate — never an
RPC envelope.

Four jobs:

1. **Connection ownership.** ASCOM ``Connected`` is per-device, but there is one
   device on the LAN. The seven devices are refcounted holders of a single
   session: the first ``Connected = True`` starts it, the last ``Connected =
   False`` closes it.
2. **Error translation.** Device codes and session exceptions become ASCOM error
   numbers, once, here. The distinction that matters most is ``103`` (the method
   does not exist -> ``NotImplemented``) versus ``210`` (it exists but the device
   is in the wrong state -> ``InvalidOperation``).
3. **The verb gate.** A method is sent only if the protocol layer has verified it
   as a getter, or ``capabilities`` has verified it as a command verb. Anything
   else raises ``NotImplemented`` without a round trip.
4. **Address ownership.** No device address is ever assumed (CLAUDE.md
   invariant 11). A backend starts knowing neither host nor port; the
   ``ssc.set_device_address`` action records a target, and the next
   ``Connected = True`` is what actually builds a session for it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from ssc.alpaca.capabilities import (
    CODE_METHOD_NOT_FOUND,
    CODE_NO_SOLVE_DATA,
    CODE_NOT_SUPPORTED,
    unavailable_reason,
)
from ssc.alpaca.errors import (
    ActionNotImplementedError,
    AlpacaError,
    AlpacaNotImplementedError,
    ErrorNumber,
    InvalidOperationError,
    InvalidValueError,
    NotConnectedError,
    UnspecifiedError,
    ValueNotSetError,
)
from ssc.client.alpaca import AlpacaDeviceClient
from ssc.client.errors import AlpacaClientError, AlpacaRemoteError
from ssc.client.imagebytes import ImageBytesFrame
from ssc.seestar.interop import read_battery
from ssc.seestar.models import DeviceState
from ssc.seestar.native import NativeAlpacaSession
from ssc.seestar.routing import RoutedSession, routed_session
from ssc.seestar.rpc import (
    DeviceError,
    RpcConnectionLostError,
    RpcError,
    RpcTimeoutError,
)
from ssc.seestar.session import (
    METHOD_PRECONDITIONS,
    VERIFIED_COMMAND_METHODS,
    VERIFIED_METHODS,
    AuthenticationRequiredError,
    ConnectionState,
    DeviceInfo,
    HazardousCallError,
    MethodNotAvailableError,
    PreconditionNotMetError,
    SeestarSession,
    SessionError,
    SessionStatus,
)
from ssc.seestar.session import (
    NotConnectedError as SessionNotConnectedError,
)
from ssc.seestar.session import (
    UnverifiedMethodError as SessionUnverifiedMethodError,
)
from ssc.settings.secrets import SecretStore

_log = logging.getLogger(__name__)

#: ``get_device_state`` keys the ASCOM devices read. Credential-bearing keys
#: (``ap``, ``setting``) are absent on purpose and must stay absent.
DEVICE_STATE_KEYS: Final[tuple[str, ...]] = (
    "device",
    "mount",
    "focuser",
    "camera",
    "pi_status",
    "storage",
    "balance_sensor",
)

#: How long a cached read stays fresh. Short enough that ``Slewing`` tracks the
#: mount, long enough that a client polling four properties in a row costs one
#: round trip. Every command call clears the cache outright.
DEFAULT_CACHE_TTL_S: Final = 0.25

Clock = Callable[[], float]

#: Builds a fresh session for a given (host, port). Injectable so tests can
#: point ``ssc.set_device_address`` at the in-process emulator instead of a
#: real socket.
SessionFactory = Callable[[str, int], SeestarSession]

#: A status published when no session has ever been built — no device address
#: has been configured yet (architecture policy invariant 11: never a default).
_NO_TARGET_STATUS = SessionStatus(
    state=ConnectionState.DISCONNECTED,
    endpoint="(no device address configured)",
    reason="no device address is configured; call ssc.set_device_address, then Connected = True",
)


def _default_session_factory(host: str, port: int) -> SeestarSession:
    """Build a real session for this address. The production path once one is set.

    Which of the two control surfaces the device actually has is decided by
    ``ssc.seestar.routing`` — by probing the device, never by its firmware
    version — and the evidence is published in the session's own status.
    """
    return routed_session(host, port)


class SeestarBackend:
    """Shared device access for the seven ASCOM interfaces.

    A session can arrive two ways:

    * **Pre-built** (``session=``) — what tests and the ConformU harness pass:
      an already-targeted :class:`SeestarSession` (over the emulator, say).
      Reused across connect/disconnect cycles exactly as before.
    * **Built on demand** (``session_factory=``, default a real TCP session) —
      the production path. The backend starts with no session and no address;
      ``ssc.set_device_address`` supplies the target, and the first
      ``Connected = True`` afterwards builds the session from it. No address is
      ever assumed (CLAUDE.md invariant 11).
    """

    def __init__(
        self,
        session: SeestarSession | None = None,
        *,
        session_factory: SessionFactory = _default_session_factory,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
        ready_budget_s: float = 10.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self._session: SeestarSession | None = session
        self._session_factory = session_factory
        self._target_address: tuple[str, int] | None = None
        self._cache_ttl_s = cache_ttl_s
        self._ready_budget_s = ready_budget_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._holders: set[str] = set()
        self._started = False
        self._cache: dict[str, tuple[float, object]] = {}
        #: ``"filterwheel/0"``-style keys already ``Connected = True`` on the
        #: device's own Alpaca server — see ``_ensure_native_connected``.
        self._native_connected: set[str] = set()
        self._battery_task: asyncio.Task[None] | None = None
        self._battery_host: str | None = None
        self._battery_next_read = 0.0
        self._battery: dict[str, object] = {"percent": None, "measured_at": None, "note": "Interop key not configured"}

    # -- connection ---------------------------------------------------------- #

    @property
    def is_connected(self) -> bool:
        """True while the control channel is genuinely up.

        This is deliberately ``status.control_connected``, not ``state is
        READY``. ``DEGRADED`` covers two conditions that leave control fully
        working — an unverified firmware, or a lost event stream — and both
        must keep serving members (``AbortSlew`` most of all: a mount that is
        physically slewing must never lose its emergency stop because
        *telemetry* dropped). Only ``DISCONNECTED``/``CONNECTING``/
        ``HANDSHAKING`` genuinely lack a control link.
        """
        return (
            self._started and self._session is not None and self._session.status.control_connected
        )

    @property
    def status(self) -> SessionStatus:
        return _NO_TARGET_STATUS if self._session is None else self._session.status

    @property
    def device_info(self) -> DeviceInfo | None:
        return None if self._session is None else self._session.device

    @property
    def holders(self) -> frozenset[str]:
        return frozenset(self._holders)

    @property
    def target_address(self) -> tuple[str, int] | None:
        """The ``(host, port)`` the next ``Connected = True`` will use, if set."""
        return self._target_address

    async def set_target_address(self, host: str, port: int) -> None:
        """Record the address ``ssc.set_device_address`` asked for.

        Takes effect on the *next* connect, never the current one: a session
        already up stays up. While control is down, fully discard the stale
        session and its holders so the next ``acquire()`` can actually rebuild
        against this target. A lost session can still have ``_started`` and a
        telescope holder set; merely changing ``_target_address`` otherwise
        makes ``acquire()`` return early forever and prevents Wi-Fi fallback.
        """
        async with self._lock:
            self._target_address = (host, port)
            if self.is_connected:
                return
            stale = self._session
            self._holders.clear()
            self._native_connected.clear()
            self._started = False
            self._session = None
            self._cache.clear()
            if stale is not None:
                await stale.aclose()

    async def acquire(self, holder: str) -> None:
        """Connect on behalf of one ASCOM device. Idempotent per holder."""
        async with self._lock:
            if holder in self._holders and self._started:
                return
            if not self._started:
                if self._session is None:
                    if self._target_address is None:
                        raise NotConnectedError(
                            "no device address is configured; call "
                            "ssc.set_device_address before Connected = True "
                            "(CLAUDE.md invariant 11 — SSC never assumes one)"
                        )
                    host, port = self._target_address
                    self._session = self._session_factory(host, port)
                session = self._session
                try:
                    await session.start()
                except SessionError as exc:
                    await session.aclose()
                    self._started = False
                    self._cache.clear()
                    raise NotConnectedError(str(exc)) from exc
                self._started = True
                try:
                    # DEGRADED counts as connected: the session only enters it
                    # once the control channel is up (``_publish`` in
                    # ``ssc/seestar/session.py``), for a reason — unverified
                    # firmware, a lost event stream — that does not stop the
                    # mount from being controlled. Refusing to connect here
                    # would hide the ``ssc.status``/``ssc.capabilities``
                    # surface that exists to explain the degradation
                    # (architecture policy invariant 7). A session that never reaches a
                    # control-up state (auth locked, unreachable host) stays
                    # DISCONNECTED/CONNECTING/HANDSHAKING and still times out
                    # below.
                    await session.wait_for_state(
                        {ConnectionState.READY, ConnectionState.DEGRADED},
                        timeout=self._ready_budget_s,
                    )
                except TimeoutError:
                    status = session.status
                    await session.aclose()
                    self._started = False
                    self._cache.clear()
                    raise NotConnectedError(
                        f"device did not become ready within {self._ready_budget_s}s "
                        f"(state {status.state}: {status.reason or 'no reason published'})"
                    ) from None
            self._holders.add(holder)

    async def release(self, holder: str) -> None:
        """Disconnect one ASCOM device; close the session when the last leaves."""
        async with self._lock:
            self._holders.discard(holder)
            if self._holders or not self._started:
                return
            if self._battery_task is not None:
                self._battery_task.cancel()
                await asyncio.gather(self._battery_task, return_exceptions=True)
            await self._release_native_extras()
            if self._session is not None:
                await self._session.aclose()
            self._started = False
            self._cache.clear()

    async def aclose(self) -> None:
        async with self._lock:
            self._holders.clear()
            if self._battery_task is not None:
                self._battery_task.cancel()
                await asyncio.gather(self._battery_task, return_exceptions=True)
            await self._release_native_extras()
            if self._started and self._session is not None:
                await self._session.aclose()
            self._started = False
            self._cache.clear()

    # -- calling ------------------------------------------------------------- #

    def require_connected(self, what: str) -> None:
        if self.is_connected:
            return
        status = self.status
        raise NotConnectedError(
            f"{what} requires a connected device; the session is {status.state}"
            + (f" ({status.reason})" if status.reason else "")
        )

    async def call(
        self, method: str, params: Mapping[str, object] | None = None, *, cache: bool = False
    ) -> object:
        """Send one method, subject to the verb gate. Returns the raw result.

        Args:
            cache: serve from, and populate, the short-lived read cache. Only
                valid for parameterless getters; a command call always clears it.
        """
        self.require_connected(method)
        self._gate(method)
        session = self._live_session()
        if cache and params is None:
            hit = self._cache_get(method)
            if hit is not None:
                return hit[0]

        try:
            response = await session.call(method, params, raise_on_error=False)
        except (SessionNotConnectedError, RpcConnectionLostError) as exc:
            raise NotConnectedError(f"{method}: {exc}") from exc
        except PreconditionNotMetError as exc:
            raise InvalidOperationError(f"{method}: {exc}") from exc
        except AuthenticationRequiredError as exc:
            raise NotConnectedError(f"{method}: {exc}") from exc
        except (SessionUnverifiedMethodError, MethodNotAvailableError) as exc:
            raise AlpacaNotImplementedError(f"{method}: {exc}") from exc
        except HazardousCallError as exc:
            raise InvalidOperationError(f"{method}: {exc}") from exc
        except RpcTimeoutError as exc:
            raise UnspecifiedError(f"{method}: {exc}") from exc
        except RpcError as exc:
            raise UnspecifiedError(f"{method} failed: {exc}") from exc
        except SessionError as exc:
            # The session refused the call for a reason of its own — a
            # credential-bearing key, say. A refusal is a result, not a fault.
            raise InvalidValueError(f"{method}: {exc}") from exc

        if method in VERIFIED_COMMAND_METHODS:
            # A command verb may have changed the device; nothing cached is
            # trustworthy any more, least of all mount.move_type.
            self.invalidate()

        error = response.any_error
        if error is not None:
            raise translate_device_error(method, error)
        if cache and params is None:
            self._cache_put(method, response.result)
        return response.result

    async def device_state(self, keys: Sequence[str] = DEVICE_STATE_KEYS) -> DeviceState:
        """Typed ``get_device_state``, cached. Never requests a credential key."""
        cache_key = f"get_device_state:{','.join(keys)}"
        hit = self._cache_get(cache_key)
        if hit is not None:
            state = hit[0]
            if isinstance(state, DeviceState):
                return state
        self.require_connected("get_device_state")
        session = self._live_session()
        try:
            state = await session.get_device_state(keys)
        except (SessionNotConnectedError, RpcConnectionLostError) as exc:
            raise NotConnectedError(f"get_device_state: {exc}") from exc
        except HazardousCallError as exc:
            raise InvalidOperationError(f"get_device_state: {exc}") from exc
        except SessionError as exc:
            # Requesting a credential-bearing key lands here. The protocol layer
            # refuses it; the client is told which key, and never sees a password.
            raise InvalidValueError(f"get_device_state: {exc}") from exc
        except RpcError as exc:
            raise UnspecifiedError(f"get_device_state failed: {exc}") from exc
        self._cache_put(cache_key, state)
        return state

    def invalidate(self) -> None:
        """Drop every cached read. Called after anything that moves the device."""
        self._cache.clear()

    # -- native Alpaca pass-through ------------------------------------------- #
    #
    # On a device whose control surface is its own ASCOM Alpaca server
    # (``ssc.seestar.routing`` — probed, never version-gated), ``call()`` and
    # ``device_state()`` above are the wrong shape entirely: there is no Seestar
    # JSON-RPC vocabulary to gate or cache, only another ASCOM server. The
    # device classes prefer this path for any standard member the device also
    # exposes (architecture policy invariant 2 — standard first), falling back to the
    # JSON-RPC mapping above only when this surface is not the one in use.

    @property
    def native_surface_active(self) -> bool:
        """True once a session is up *and* it is driven over the device's own
        Alpaca server, rather than Seestar JSON-RPC."""
        return self.is_connected and self._native_session() is not None

    def native_device(self, device_type: str, device_number: int = 0) -> AlpacaDeviceClient | None:
        """The device's own Alpaca proxy for ``device_type/device_number``.

        ``None`` when the active surface is not native Alpaca, or no session is
        up — never a client pointed at nothing.
        """
        native = self._native_session()
        if native is None:
            return None
        client = native.native_client
        return None if client is None else client.device(device_type, device_number)

    async def native_get(
        self, device_type: str, member: str, device_number: int = 0, **params: object
    ) -> object:
        """``GET`` one member straight off the device's own Alpaca server."""
        device = self._require_native_device(device_type, member, device_number)
        await self._ensure_native_connected(device)
        try:
            return await device.get(member, **params)
        except AlpacaClientError as exc:
            raise translate_native_error(f"{device_type}.{member}", exc) from exc

    async def native_put(
        self, device_type: str, member: str, device_number: int = 0, **params: object
    ) -> object:
        """``PUT`` one member straight onto the device's own Alpaca server."""
        device = self._require_native_device(device_type, member, device_number)
        await self._ensure_native_connected(device)
        try:
            return await device.put(member, **params)
        except AlpacaClientError as exc:
            raise translate_native_error(f"{device_type}.{member}", exc) from exc

    async def native_image_bytes(self, device_type: str, device_number: int = 0) -> ImageBytesFrame:
        """``Camera.ImageArray``, pulled off the device's own server as ImageBytes.

        Unlike :meth:`native_get`, this never routes through the generic
        JSON ``GET`` pass-through: a plain ``GET`` with no ``Accept:
        application/imagebytes`` header gets the device's nested-JSON array,
        2.5x the size for the same frame (``docs/M0-FINDINGS.md``). This asks
        for the binary transfer explicitly and decodes it with
        :func:`ssc.client.imagebytes.decode_image_bytes` — the one decoder
        this project has verified against real hardware — never a second one.
        """
        device = self._require_native_device(device_type, "imagearray", device_number)
        await self._ensure_native_connected(device)
        try:
            return await device.image_array()
        except AlpacaClientError as exc:
            raise translate_native_error(f"{device_type}.imagearray", exc) from exc

    def _require_native_device(
        self, device_type: str, member: str, device_number: int
    ) -> AlpacaDeviceClient:
        self.require_connected(f"{device_type}.{member}")
        device = self.native_device(device_type, device_number)
        if device is None:
            raise AlpacaNotImplementedError(
                f"{device_type}.{member}: no native Alpaca pass-through is active for "
                "this session (the device is being driven over Seestar JSON-RPC instead)"
            )
        return device

    async def _ensure_native_connected(self, device: AlpacaDeviceClient) -> None:
        """``Connected = True`` on this specific native sub-device, once.

        ``NativeAlpacaSession`` only connects ``telescope/0`` for itself (that is
        what drives the session's own state machine); ``focuser/0``,
        ``focuser/1`` and ``filterwheel/0`` are separate ASCOM device instances
        on the same server and refuse most properties with ``1031`` until each is
        connected in its own right (``docs/M0-FINDINGS.md`` trap 1). Connecting
        ``telescope/0`` again here is a harmless idempotent no-op.
        """
        key = f"{device.device_type}/{device.device_number}"
        if key in self._native_connected:
            return
        try:
            await device.connect()
            if not await device.is_connected():
                raise AlpacaClientError(
                    f"{key} accepted Connected = True and still reports Connected: false"
                )
        except AlpacaClientError as exc:
            raise translate_native_error(f"{key}.Connected", exc) from exc
        self._native_connected.add(key)

    async def _release_native_extras(self) -> None:
        """Best-effort ``Connected = False`` on every sub-device this backend
        connected beyond what ``NativeAlpacaSession`` already owns.

        Observed to be ignored by this firmware (``docs/M0-FINDINGS.md``:
        ``Connected = False`` is accepted and the device stays connected), so
        this is symmetry and a courteous ASCOM citizen, not something later code
        may rely on succeeding — failures are logged, never raised.
        """
        if not self._native_connected:
            return
        native = self._native_session()
        client = None if native is None else native.native_client
        for key in self._native_connected:
            if client is None:
                continue
            device_type, _, number_text = key.partition("/")
            try:
                await client.device(device_type, int(number_text)).disconnect()
            except AlpacaClientError as exc:
                _log.info("native %s: Connected = False did not complete cleanly: %s", key, exc)
        self._native_connected.clear()

    def _native_session(self) -> NativeAlpacaSession | None:
        session = self._session
        if isinstance(session, RoutedSession):
            session = session.delegate
        return session if isinstance(session, NativeAlpacaSession) else None

    def battery_status(self) -> dict[str, object]:
        """Start a non-blocking, non-overlapping refresh through the Alpaca seam."""
        host = self.device_info.host if self.device_info is not None else None
        if host != self._battery_host:
            self._battery_host = host
            self._battery = {"percent": None, "measured_at": None, "note": "Waiting for battery telemetry"}
            self._battery_next_read = 0.0
        if self.is_connected and self._clock() >= self._battery_next_read and (
            self._battery_task is None or self._battery_task.done()
        ):
            self._battery_next_read = self._clock() + 60
            self._battery_task = asyncio.create_task(self._refresh_battery())
        return dict(self._battery)

    async def _refresh_battery(self) -> None:
        try:
            async with asyncio.timeout(8):
                await self._read_idle_battery()
        except TimeoutError:
            self._battery["note"] = "Battery refresh timed out"

    async def _read_idle_battery(self) -> None:
        session = self._native_session()
        device = self.device_info
        if session is None or device is None:
            self._battery["note"] = "Interop telemetry unavailable on this connection"
            return
        try:
            pem = await SecretStore().get("seestar.interop")
            if pem is None:
                self._battery["note"] = "Configure seestar.interop in the OS keyring"
                return
            # Both sensors share hardware resources. Never query device state
            # while either native camera reports an active acquisition.
            for number in (0, 1):
                camera = self.native_device("camera", number)
                if camera is not None and await camera.get("camerastate") != 0:
                    self._battery["note"] = "Waiting for an idle camera gap"
                    self._battery_next_read = self._clock() + 1
                    return
            reading = await read_battery(device.host, pem)
            if self._native_session() is session and self.is_connected:
                self._battery = reading
        except Exception as exc:
            # Exception text can contain payloads or secrets: expose type only.
            if self._native_session() is session:
                self._battery["note"] = f"Battery refresh unavailable ({type(exc).__name__})"

    # -- internals ----------------------------------------------------------- #

    def _live_session(self) -> SeestarSession:
        """The session backing this call. Only valid once ``is_connected`` is true.

        ``require_connected`` is always called first, so this never actually
        raises — it exists so a call site does not have to carry a
        ``SeestarSession | None`` narrowing of its own.
        """
        if self._session is None:  # pragma: no cover - require_connected already refused
            raise NotConnectedError("no Seestar session is active")
        return self._session

    @staticmethod
    def _gate(method: str) -> None:
        """Refuse anything the protocol layer has not verified on this firmware.

        ``VERIFIED_METHODS`` is the single source of truth — read getters plus
        the command verbs that have been driven at real hardware. The session
        would refuse an unverified method anyway; refusing here turns it into an
        ASCOM ``NotImplemented`` with a reason instead of an exception with a
        stack trace, and costs no round trip.
        """
        if method not in VERIFIED_METHODS:
            raise AlpacaNotImplementedError(unavailable_reason(method))

    def _cache_get(self, key: str) -> tuple[object] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._cache[key]
            return None
        return (value,)

    def _cache_put(self, key: str, value: object) -> None:
        self._cache[key] = (self._clock() + self._cache_ttl_s, value)


def translate_device_error(method: str, error: DeviceError) -> AlpacaError:
    """Map a Seestar response code onto the right ASCOM error number.

    The code table is built from observation, never assumption
    (``docs/M0-FINDINGS.md``). Unknown codes are reported as-is rather than
    guessed at.
    """
    detail = f"{method}: device returned {error}"
    if error.code == CODE_METHOD_NOT_FOUND:
        return AlpacaNotImplementedError(f"{detail} — the method does not exist on this firmware")
    if error.code == CODE_NOT_SUPPORTED:
        precondition = METHOD_PRECONDITIONS.get(method)
        requirement = (
            f"it requires {precondition.requires} (check: {precondition.check})"
            if precondition is not None
            else "the precondition for this verb has not been established"
        )
        return InvalidOperationError(
            f"{detail} — the method exists but is not available in the device's "
            f"current state: {requirement}"
        )
    if error.code == CODE_NO_SOLVE_DATA:
        return ValueNotSetError(f"{detail} — no plate-solve result is available")
    _log.warning("unmapped Seestar error code from %s: %s", method, error)
    return UnspecifiedError(f"{detail} — this code is not in SSC's observed error table")


#: The device's own Alpaca server reports errors using the same reserved ASCOM
#: numbers this driver does (``docs/M0-FINDINGS.md`` confirms ``1031 == 0x407``
#: NotConnected, ``1279 == 0x4FF`` Unspecified, ``1025 == 0x401`` InvalidValue —
#: decimal in the docs, hex in the wire). So a native pass-through error is
#: translated by number, not reinterpreted: whatever the real device says is
#: authoritative, and this driver adds nothing to it.
_NATIVE_ERROR_TYPES: Final[Mapping[int, type[AlpacaError]]] = {
    ErrorNumber.NOT_IMPLEMENTED: AlpacaNotImplementedError,
    ErrorNumber.INVALID_VALUE: InvalidValueError,
    ErrorNumber.VALUE_NOT_SET: ValueNotSetError,
    ErrorNumber.NOT_CONNECTED: NotConnectedError,
    ErrorNumber.INVALID_OPERATION: InvalidOperationError,
    ErrorNumber.ACTION_NOT_IMPLEMENTED: ActionNotImplementedError,
    ErrorNumber.UNSPECIFIED: UnspecifiedError,
}


def translate_native_error(member: str, error: AlpacaClientError) -> AlpacaError:
    """Map a failure from the device's own Alpaca server onto this driver's.

    A device-reported error (:class:`AlpacaRemoteError`) is re-raised as the
    matching local exception, by number, carrying the device's own message
    verbatim — nothing invented, nothing reinterpreted. A number outside the
    table above is reported as ``Unspecified`` with the raw number kept in the
    message rather than silently folded into a nearby one. Anything else
    (a transport failure, a broken transaction id) means *this driver* could
    not reach the device's server, which is honestly ``NotConnected``.
    """
    if isinstance(error, AlpacaRemoteError):
        error_type = _NATIVE_ERROR_TYPES.get(error.number)
        if error_type is not None:
            return error_type(f"{member}: {error.message}")
        return UnspecifiedError(f"{member}: [{error.number:#05x}] {error.message}")
    return NotConnectedError(f"{member}: {error}")


__all__ = [
    "DEFAULT_CACHE_TTL_S",
    "DEVICE_STATE_KEYS",
    "SeestarBackend",
    "SessionFactory",
    "translate_device_error",
    "translate_native_error",
]
