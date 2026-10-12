# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

r"""Wire transport for the native Seestar protocol.

Everything here encodes an *observed* fact from ``docs/M0-FINDINGS.md``
(2026-08-28, ZWO Seestar S30 Pro, **firmware 8.46**). Nothing here is a guess.

* The device exposes a **connection pair**, not one multiplexed socket:

  - ``:4701`` — JSON-RPC 2.0 control. Client speaks first, request/response.
  - ``:4700`` — server-push event/telemetry stream. Silent for ~9 s after
    connect, then pushes JSON unprompted **and closes the connection**. The
    event channel is therefore short-lived by design and the session
    re-attaches; a clean EOF there is normal, not a fault.

  Their lifecycles are **independent**: losing the event stream is a different
  failure from losing control, so this module hands out two transports and
  ``session.py`` supervises them separately.

* Framing is newline-delimited JSON. The device accepts ``\n`` or ``\r\n`` on
  input and always terminates its own replies with ``\r\n``.

* Length-prefixed framing is **rejected** by the device (silently). It is not
  implemented here and must not be added.

* An unknown method is an *ordinary* error (``code`` 103) and leaves the
  connection healthy — see ``rpc.py``. The one observed crash is
  ``get_device_state`` with no ``params``: it resets the connection and the
  control port then refuses connections for several seconds. So
  :class:`ConnectionLostError` and :class:`PortRefusedError` are rare but real,
  and must be handled with backoff rather than a hot retry loop.

Ports are protocol constants, not addresses: no host is named anywhere in this
package (``CLAUDE.md`` invariant 11).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

_log = logging.getLogger(__name__)

#: JSON-RPC 2.0 control port (request/response).
CONTROL_PORT: Final = 4701
#: Server-push event/telemetry port.
EVENT_PORT: Final = 4700

#: What *we* send. The device accepts "\n" too, but replies "\r\n"; we speak its
#: own dialect back at it rather than relying on the more permissive path.
LINE_TERMINATOR: Final = b"\r\n"

DEFAULT_CONNECT_TIMEOUT_S: Final = 5.0
#: ``get_albums`` and ``get_setting`` return large objects; 4 MiB is generous but
#: bounded, so a babbling or desynchronised peer cannot exhaust memory.
DEFAULT_MAX_LINE_BYTES: Final = 4 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Credential redaction
# --------------------------------------------------------------------------- #
#
# The device hands out two cleartext secrets to any LAN client:
#   * ``get_device_state`` key ``ap``      -> the Wi-Fi AP password
#   * ``get_device_state`` key ``setting`` -> the connect password
#   * ``get_setting``                      -> the same connect password
#
# Redaction lives at the wire layer so no upstream caller can forget it. Anything
# that logs, caches, serialises or fixtures a device payload runs it through
# :func:`redact` first.

REDACTED: Final = "<redacted>"

#: Matched case-insensitively as substrings of a key name. Deliberately narrow:
#: ``key_mgmt`` (WPA2-PSK vs open, from ``pi_station_state``) is not a secret and
#: must survive, so a bare "key" marker is wrong.
SENSITIVE_KEY_MARKERS: Final[tuple[str, ...]] = (
    "passw",
    "passwd",
    "pwd",
    "psk",
    "token",
    "secret",
    "challenge",
    "credential",
)

_MAX_REDACT_DEPTH: Final = 12


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def redact(value: object, *, _depth: int = 0) -> object:
    """Return ``value`` with every credential-shaped field replaced.

    Structure-preserving, so a redacted payload is still useful in a log line.
    Never mutates the input.
    """
    if _depth >= _MAX_REDACT_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            out[key] = REDACTED if is_sensitive_key(key) else redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, str | bytes):
        return value
    if isinstance(value, Sequence):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class TransportError(Exception):
    """Base class for every wire-level failure."""


class ConnectError(TransportError):
    """A connection could not be established at all."""


class PortRefusedError(ConnectError):
    """The port actively refused us.

    On the real device this is the tail of the *reset-then-refuse* fault: the
    control server has just restarted and is not listening yet. It is a reason to
    back off, not to give up.
    """


class ConnectionLostError(TransportError):
    """An established connection went away."""

    def __init__(self, message: str, *, was_reset: bool = False) -> None:
        super().__init__(message)
        #: True for ``ECONNRESET`` — read as "the control server restarted".
        self.was_reset = was_reset


class TransportClosedError(TransportError):
    """The transport was closed locally, or used before it was connected."""


class FrameTooLargeError(TransportError):
    """A single line exceeded the configured limit before a terminator arrived."""


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #


@runtime_checkable
class SeestarTransport(Protocol):
    """One duplex, line-oriented channel to the device.

    Implementations must not buffer without bound, and must raise rather than
    return a plausible-looking empty value when the peer goes away.
    """

    @property
    def name(self) -> str:
        """Human-readable channel label, for logs and state reasons."""
        ...

    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> None:
        """Establish the channel.

        Raises:
            PortRefusedError: the peer refused the connection.
            ConnectError: any other failure to connect, including timeout.
        """
        ...

    async def send_line(self, line: str) -> None:
        """Send one frame, terminated with ``\\r\\n``."""
        ...

    async def receive_line(self) -> str:
        """Return the next frame with its terminator stripped.

        Raises:
            ConnectionLostError: the peer reset or closed the connection.
            TransportClosedError: the transport was closed locally.
            FrameTooLargeError: the peer exceeded the line limit.
        """
        ...

    async def aclose(self) -> None:
        """Close the channel. Idempotent; never raises for an already-closed
        channel."""
        ...


class TransportFactory(Protocol):
    """Mints the *pair* of transports a session needs.

    A factory hands out a fresh transport per call, because re-attaching — after
    a control-server restart, or after the event stream's routine close — means a
    brand new connection, not a revived one.
    """

    @property
    def endpoint(self) -> str: ...

    @property
    def host(self) -> str: ...

    @property
    def control_port(self) -> int: ...

    def control(self) -> SeestarTransport: ...

    def events(self) -> SeestarTransport: ...


# --------------------------------------------------------------------------- #
# Line framing
# --------------------------------------------------------------------------- #


class _LineBuffer:
    """Newline framing with a hard size cap.

    Splits on ``\\n`` and strips a single trailing ``\\r``, so both terminators
    the device is known to use are accepted on input.
    """

    def __init__(self, max_bytes: int) -> None:
        self._buf = bytearray()
        self._max_bytes = max_bytes

    def feed(self, data: bytes) -> None:
        if len(self._buf) + len(data) > self._max_bytes:
            raise FrameTooLargeError(f"frame exceeded {self._max_bytes} bytes with no terminator")
        self._buf += data

    def take(self) -> str | None:
        index = self._buf.find(b"\n")
        if index < 0:
            return None
        line = bytes(self._buf[:index])
        del self._buf[: index + 1]
        return line.decode("utf-8", "replace").removesuffix("\r")


# --------------------------------------------------------------------------- #
# TCP
# --------------------------------------------------------------------------- #


class TcpTransport:
    """A real TCP channel to a Seestar."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        name: str | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    ) -> None:
        self._host = host
        self._port = port
        self._name = name or f"{host}:{port}"
        self._connect_timeout = connect_timeout
        self._max_line_bytes = max_line_bytes
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._closed

    async def connect(self) -> None:
        if self._writer is not None:
            raise TransportError(f"{self._name}: already connected")
        self._closed = False
        try:
            async with asyncio.timeout(self._connect_timeout):
                self._reader, self._writer = await asyncio.open_connection(
                    self._host, self._port, limit=self._max_line_bytes
                )
        except ConnectionRefusedError as exc:
            raise PortRefusedError(f"{self._name}: connection refused") from exc
        except TimeoutError as exc:
            raise ConnectError(
                f"{self._name}: connect timed out after {self._connect_timeout}s"
            ) from exc
        except OSError as exc:
            raise ConnectError(f"{self._name}: {type(exc).__name__}: {exc}") from exc

    async def send_line(self, line: str) -> None:
        writer = self._require_writer()
        try:
            writer.write(line.encode("utf-8") + LINE_TERMINATOR)
            await writer.drain()
        except ConnectionResetError as exc:
            raise ConnectionLostError(f"{self._name}: reset while sending", was_reset=True) from exc
        except OSError as exc:
            raise ConnectionLostError(f"{self._name}: {type(exc).__name__}: {exc}") from exc

    async def receive_line(self) -> str:
        reader = self._require_reader()
        try:
            raw = await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            raise ConnectionLostError(f"{self._name}: peer closed the connection") from exc
        except asyncio.LimitOverrunError as exc:
            raise FrameTooLargeError(
                f"{self._name}: frame exceeded {self._max_line_bytes} bytes"
            ) from exc
        except ConnectionResetError as exc:
            raise ConnectionLostError(
                f"{self._name}: connection reset by peer", was_reset=True
            ) from exc
        except OSError as exc:
            raise ConnectionLostError(f"{self._name}: {type(exc).__name__}: {exc}") from exc
        return raw.decode("utf-8", "replace").removesuffix("\n").removesuffix("\r")

    async def aclose(self) -> None:
        self._closed = True
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError as exc:  # pragma: no cover - teardown noise only
            _log.debug("%s: error while closing: %s", self._name, exc)

    def _require_writer(self) -> asyncio.StreamWriter:
        if self._writer is None or self._closed:
            raise TransportClosedError(f"{self._name}: not connected")
        return self._writer

    def _require_reader(self) -> asyncio.StreamReader:
        if self._reader is None or self._closed:
            raise TransportClosedError(f"{self._name}: not connected")
        return self._reader


class TcpTransportFactory:
    """The observed control/event port pair on a real device."""

    def __init__(
        self,
        host: str,
        *,
        control_port: int = CONTROL_PORT,
        event_port: int = EVENT_PORT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
    ) -> None:
        self._host = host
        self._control_port = control_port
        self._event_port = event_port
        self._connect_timeout = connect_timeout

    @property
    def endpoint(self) -> str:
        return f"{self._host}:{self._control_port}"

    @property
    def host(self) -> str:
        return self._host

    @property
    def control_port(self) -> int:
        return self._control_port

    def control(self) -> SeestarTransport:
        return TcpTransport(
            self._host,
            self._control_port,
            name=f"control@{self._host}:{self._control_port}",
            connect_timeout=self._connect_timeout,
        )

    def events(self) -> SeestarTransport:
        return TcpTransport(
            self._host,
            self._event_port,
            name=f"events@{self._host}:{self._event_port}",
            connect_timeout=self._connect_timeout,
        )


# --------------------------------------------------------------------------- #
# Emulator
# --------------------------------------------------------------------------- #


class ChannelEndpoint(Protocol):
    """The client end of one in-process connection served by the emulator.

    Byte-oriented on purpose: the emulator does real ``\\r\\n`` framing and the
    transport does real parsing, so the offline path exercises the same framing
    code the wire does — and CI needs no sockets.
    """

    async def send_bytes(self, data: bytes) -> None: ...

    async def receive_bytes(self) -> bytes:
        """Return the next chunk, ``b""`` at a clean EOF.

        Raises:
            ConnectionLostError: the peer reset the connection.
        """
        ...

    def close(self) -> None: ...


#: Opens one in-process channel. Raises :class:`PortRefusedError` while the
#: emulated control server is in its post-reset refusal window.
ChannelOpener = Callable[[], Awaitable[ChannelEndpoint]]


class EmulatorTransport:
    """A channel to the in-process emulator. No sockets, so CI needs no network."""

    def __init__(
        self,
        name: str,
        opener: ChannelOpener,
        *,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    ) -> None:
        self._name = name
        self._opener = opener
        self._endpoint: ChannelEndpoint | None = None
        self._buffer = _LineBuffer(max_line_bytes)
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_connected(self) -> bool:
        return self._endpoint is not None and not self._closed

    async def connect(self) -> None:
        if self._endpoint is not None:
            raise TransportError(f"{self._name}: already connected")
        self._closed = False
        self._endpoint = await self._opener()

    async def send_line(self, line: str) -> None:
        endpoint = self._require()
        await endpoint.send_bytes(line.encode("utf-8") + LINE_TERMINATOR)

    async def receive_line(self) -> str:
        endpoint = self._require()
        while True:
            line = self._buffer.take()
            if line is not None:
                return line
            data = await endpoint.receive_bytes()
            if not data:
                raise ConnectionLostError(f"{self._name}: peer closed the connection")
            self._buffer.feed(data)

    async def aclose(self) -> None:
        self._closed = True
        endpoint, self._endpoint = self._endpoint, None
        if endpoint is not None:
            endpoint.close()

    def _require(self) -> ChannelEndpoint:
        if self._endpoint is None or self._closed:
            raise TransportClosedError(f"{self._name}: not connected")
        return self._endpoint


class EmulatorTransportFactory:
    """Pairs two emulator channels behind the same interface a real device uses."""

    def __init__(
        self,
        control_opener: ChannelOpener,
        event_opener: ChannelOpener,
        *,
        endpoint: str = "emulator",
    ) -> None:
        self._control_opener = control_opener
        self._event_opener = event_opener
        self._endpoint = endpoint

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def host(self) -> str:
        return self._endpoint

    @property
    def control_port(self) -> int:
        return CONTROL_PORT

    def control(self) -> SeestarTransport:
        return EmulatorTransport("control@emulator", self._control_opener)

    def events(self) -> SeestarTransport:
        return EmulatorTransport("events@emulator", self._event_opener)


if TYPE_CHECKING:  # structural conformance, checked by mypy and free at runtime

    def _conformance(
        tcp: TcpTransport,
        emu: EmulatorTransport,
        tcp_factory: TcpTransportFactory,
        emu_factory: EmulatorTransportFactory,
    ) -> tuple[SeestarTransport, SeestarTransport, TransportFactory, TransportFactory]:
        return tcp, emu, tcp_factory, emu_factory
