# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""JSON-RPC 2.0 over one Seestar control channel.

Observed envelope (``docs/M0-FINDINGS.md``, firmware 8.46)::

    -> {"id": 1, "method": "test_connection"}
    <- {"jsonrpc":"2.0","Timestamp":"969.942410482","method":"test_connection",
        "result":"server connected!","code":0,"id":1}

    -> {"id": 2, "method": "scope_get_equ_coord"}
    <- {"jsonrpc":"2.0","Timestamp":"1723.51","method":"scope_get_equ_coord",
        "error":"method not found","code":103,"id":2}

Three facts drive the design:

* ``Timestamp`` is a **device uptime in seconds, as a string**. It is not a wall
  clock and must never be parsed as a date — see :func:`parse_device_uptime`.
* A non-zero ``code`` is the **ordinary** error path. It leaves the connection
  healthy and must never trigger a reconnect. Three codes have been observed:
  ``103`` (method not found), ``210`` (the method exists but not in this state)
  and ``215`` (no solve data). ``210`` gets its own exception type because the
  remedy — fix the device's state, then retry — is nothing like the others'.
* A timed-out or reset call raises a distinct error type. It never returns a
  plausible-looking empty result, because on this device an empty result is a
  real answer (``get_view_state`` when idle, and an invalid ``get_device_state``
  key, both return ``{}`` with ``code`` 0).

The request we send is byte-for-byte the shape observed to work: ``id``,
``method`` and ``params`` when there are any. We deliberately do **not** add a
``jsonrpc`` member to requests — the device has one known input-validation crash,
so this is not the place to experiment with fields nobody has seen it accept.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from ssc.seestar.transport import (
    ConnectionLostError,
    SeestarTransport,
    TransportClosedError,
    TransportError,
    redact,
)

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S: Final = 10.0
#: Simple backpressure: at most this many requests are outstanding on the wire at
#: once. The device is a small embedded server; pipelining hundreds of calls is a
#: good way to find another input-validation crash.
DEFAULT_MAX_IN_FLIGHT: Final = 8

#: Error codes, **built from observation only**. Unlisted codes are reported as
#: "unknown"; do not add a row here without a recorded exchange.
ERROR_CODES: Final[Mapping[int, str]] = MappingProxyType(
    {
        0: "ok",
        103: "method not found",
        210: "not supported",
        215: "no solve data",
    }
)

CODE_OK: Final = 0
CODE_METHOD_NOT_FOUND: Final = 103
#: The method **exists** and is unavailable in the device's current state.
#: Recorded in ``tests/fixtures/wire/8.46/error_210_not_supported.jsonl``:
#: ``start_auto_goto`` without star view running answers 210, and the mount does
#: not move. Confusing this with 103 is the expensive mistake — it would
#: permanently disable a verb that works fine once the precondition holds.
CODE_NOT_SUPPORTED: Final = 210
CODE_NO_SOLVE_DATA: Final = 215


class RpcError(Exception):
    """Base class for every JSON-RPC level failure."""


class RpcTimeoutError(RpcError):
    """The call did not answer within its budget. Distinct on purpose."""

    def __init__(self, method: str, request_id: int, timeout: float) -> None:
        super().__init__(f"{method} (id={request_id}) timed out after {timeout}s")
        self.method = method
        self.request_id = request_id
        self.timeout = timeout


class RpcConnectionLostError(RpcError):
    """The control connection went away with this call in flight.

    Distinct from :class:`RpcTimeoutError` because the remedy differs: this one
    means the control server restarted and the session must reconnect.
    """


class RpcRemoteError(RpcError):
    """The device answered with a non-zero ``code``. The connection is fine."""

    def __init__(self, method: str, error: DeviceError) -> None:
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error

    @property
    def code(self) -> int:
        return self.error.code


class RpcNotSupportedError(RpcRemoteError):
    """Code 210: the method exists but not in this device state.

    Deliberately a distinct type from every other failure, because three wrong
    readings of it are all tempting and all costly:

    * It is **not** :data:`CODE_METHOD_NOT_FOUND`. The verb is real. Mapping it
      to ASCOM ``NotImplemented`` (``0x400``) would disable a working feature for
      the rest of the session.
    * It is **not** a connection fault. The control channel is healthy and this
      must never trigger a reconnect — recorded proof is in the 210 fixture,
      where the very next call succeeds on the same socket.
    * It is **not** a timeout or an empty result. Something specific is missing
      from the device's state, and the caller can usually fix it.
    """

    @property
    def requires(self) -> str | None:
        """What the device needed, if the caller supplied it. Never guessed."""
        return self._requires

    def __init__(self, method: str, error: DeviceError, requires: str | None = None) -> None:
        super().__init__(method, error)
        self._requires = requires


class RpcProtocolError(RpcError):
    """A frame arrived that is not a well-formed response envelope."""


@dataclass(frozen=True, slots=True)
class DeviceError:
    """A ``code`` / ``error`` pair as the device reports it."""

    code: int
    message: str | None = None

    @property
    def label(self) -> str:
        """The observed meaning of :attr:`code`, or ``"unknown"``."""
        return ERROR_CODES.get(self.code, "unknown")

    def __str__(self) -> str:
        return f"code {self.code} ({self.label}): {self.message or '—'}"


def parse_device_uptime(value: object) -> float | None:
    """Parse the ``Timestamp`` member.

    It is **seconds of device uptime as a string** (``"969.942410482"``), not a
    wall clock and not an epoch. Returns ``None`` when it is missing or
    unparseable — ``None`` means unknown, never zero.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class RpcResponse:
    """One decoded response envelope."""

    request_id: int
    method: str
    code: int
    result: object = None
    error: str | None = None
    device_uptime_s: float | None = None
    raw: Mapping[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == CODE_OK and self.error is None

    @property
    def device_error(self) -> DeviceError | None:
        """The top-level error, if any."""
        if self.ok:
            return None
        return DeviceError(self.code, self.error)

    @property
    def nested_error(self) -> DeviceError | None:
        """An error envelope nested *inside* ``result``.

        Observed on the solve getters: the call itself succeeds (``code`` 0) but
        ``result`` carries its own ``code``/``error``. Callers that only checked
        the outer code would read "success" from a failure.
        """
        result = self.result
        if not isinstance(result, Mapping):
            return None
        code = result.get("code")
        message = result.get("error")
        if not isinstance(code, int) or isinstance(code, bool):
            return None
        if code == CODE_OK:
            return None
        return DeviceError(code, message if isinstance(message, str) else None)

    @property
    def any_error(self) -> DeviceError | None:
        """The outer error if there is one, else the nested one."""
        return self.device_error or self.nested_error

    def redacted(self) -> object:
        """The raw envelope with credential-shaped fields removed, for logging."""
        return redact(self.raw)


def _decode(line: str) -> Mapping[str, object]:
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RpcProtocolError(f"response was not JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RpcProtocolError(f"response was {type(decoded).__name__}, expected an object")
    return decoded


def _to_response(raw: Mapping[str, object]) -> RpcResponse:
    request_id = raw.get("id")
    if not isinstance(request_id, int) or isinstance(request_id, bool):
        raise RpcProtocolError("response has no integer 'id'")
    method = raw.get("method")
    code = raw.get("code")
    error = raw.get("error")
    return RpcResponse(
        request_id=request_id,
        method=method if isinstance(method, str) else "",
        code=code if isinstance(code, int) and not isinstance(code, bool) else CODE_OK,
        result=raw.get("result"),
        error=error if isinstance(error, str) else None,
        device_uptime_s=parse_device_uptime(raw.get("Timestamp")),
        raw=raw,
    )


#: Called with any frame that is not a response to an outstanding request. On the
#: control channel this should be rare; it is the seam through which an
#: unsolicited greeting or challenge would first become visible.
UnsolicitedHandler = Callable[[Mapping[str, object]], None]


class JsonRpcClient:
    """Request/response correlation over a single :class:`SeestarTransport`.

    One reader task owns the socket. Nothing else reads it, so nothing blocks the
    event loop and every in-flight caller is resolved from one place.
    """

    def __init__(
        self,
        transport: SeestarTransport,
        *,
        default_timeout: float = DEFAULT_TIMEOUT_S,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
        on_unsolicited: UnsolicitedHandler | None = None,
    ) -> None:
        self._transport = transport
        self._default_timeout = default_timeout
        self._slots = asyncio.Semaphore(max_in_flight)
        self._on_unsolicited = on_unsolicited
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[RpcResponse]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._failure: BaseException | None = None

    @property
    def is_running(self) -> bool:
        return self._reader_task is not None and not self._closed.is_set()

    @property
    def failure(self) -> BaseException | None:
        """Why the reader stopped, or ``None`` while it is running."""
        return self._failure

    async def start(self) -> None:
        if self._reader_task is not None:
            raise RpcError("client already started")
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"seestar-rpc-{self._transport.name}"
        )

    async def call(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - per-call budget is the public contract
        raise_on_error: bool = True,
    ) -> RpcResponse:
        """Issue one call and wait for its correlated response.

        Args:
            raise_on_error: raise :class:`RpcRemoteError` on a non-zero ``code``.
                Pass ``False`` when a non-zero code is a legitimate answer — e.g.
                ``215`` from the solve getters when nothing has been solved yet.

        Raises:
            RpcTimeoutError: no answer within the budget.
            RpcConnectionLostError: the connection went away mid-call.
            RpcRemoteError: the device reported a non-zero ``code``.
        """
        if self._reader_task is None:
            raise RpcError("client not started")
        if self._failure is not None:
            raise RpcConnectionLostError(f"control channel is down: {self._failure}")

        budget = self._default_timeout if timeout is None else timeout
        async with self._slots:  # backpressure: bounded requests on the wire
            request_id = next(self._ids)
            payload: dict[str, object] = {"id": request_id, "method": method}
            if params:
                payload["params"] = dict(params)
            future: asyncio.Future[RpcResponse] = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            try:
                try:
                    await self._transport.send_line(json.dumps(payload, separators=(",", ":")))
                except TransportError as exc:
                    raise RpcConnectionLostError(f"{method}: send failed: {exc}") from exc
                try:
                    response = await asyncio.wait_for(future, budget)
                except TimeoutError as exc:
                    raise RpcTimeoutError(method, request_id, budget) from exc
            finally:
                # Cancellation lands here too: drop the correlation entry so a
                # late reply cannot resolve an abandoned caller.
                self._pending.pop(request_id, None)

        if raise_on_error and not response.ok:
            error = DeviceError(response.code, response.error)
            if response.code == CODE_NOT_SUPPORTED:
                raise RpcNotSupportedError(method, error)
            raise RpcRemoteError(method, error)
        return response

    async def aclose(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            # gather() returns the CancelledError rather than re-raising it, so a
            # teardown never masks a cancellation of the *calling* task.
            await asyncio.gather(task, return_exceptions=True)
        self._fail_all(TransportClosedError("rpc client closed"))
        await self._transport.aclose()

    async def wait_closed(self) -> BaseException | None:
        """Block until the reader stops, then return why."""
        await self._closed.wait()
        return self._failure

    # -- internals ---------------------------------------------------------- #

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._transport.receive_line()
                self._dispatch(line)
        except asyncio.CancelledError:
            self._fail_all(TransportClosedError("rpc reader cancelled"))
            raise
        except (ConnectionLostError, TransportClosedError) as exc:
            self._fail_all(exc)
        except Exception as exc:  # unexpected: surface it, never swallow it
            _log.exception("%s: rpc reader failed", self._transport.name)
            self._fail_all(exc)

    def _dispatch(self, line: str) -> None:
        if not line.strip():
            return
        try:
            raw = _decode(line)
        except RpcProtocolError as exc:
            _log.warning("%s: %s", self._transport.name, exc)
            return
        request_id = raw.get("id")
        if isinstance(request_id, int) and not isinstance(request_id, bool):
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                try:
                    future.set_result(_to_response(raw))
                except RpcProtocolError as exc:
                    future.set_exception(exc)
                return
        self._deliver_unsolicited(raw)

    def _deliver_unsolicited(self, raw: Mapping[str, object]) -> None:
        handler = self._on_unsolicited
        if handler is None:
            _log.debug("%s: unsolicited frame %s", self._transport.name, redact(raw))
            return
        try:
            handler(raw)
        except Exception:
            _log.exception("%s: unsolicited handler raised", self._transport.name)

    def _fail_all(self, exc: BaseException) -> None:
        if self._failure is None:
            self._failure = exc
        self._closed.set()
        for request_id, future in list(self._pending.items()):
            self._pending.pop(request_id, None)
            if not future.done():
                future.set_exception(
                    RpcConnectionLostError(f"connection lost with request {request_id} in flight")
                )
