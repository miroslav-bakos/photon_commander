"""Framing, lifecycle and credential redaction at the wire layer."""

from __future__ import annotations

import asyncio

import pytest

from ssc.seestar.transport import (
    REDACTED,
    ChannelEndpoint,
    ConnectionLostError,
    EmulatorTransport,
    FrameTooLargeError,
    PortRefusedError,
    SeestarTransport,
    TcpTransport,
    TcpTransportFactory,
    TransportClosedError,
    redact,
)


class FakeEndpoint:
    """A byte pipe standing in for the emulator's channel."""

    def __init__(self, *chunks: bytes) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self._inbox: asyncio.Queue[bytes | BaseException] = asyncio.Queue()
        for chunk in chunks:
            self._inbox.put_nowait(chunk)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive_bytes(self) -> bytes:
        item = await self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True

    def push(self, item: bytes | BaseException) -> None:
        self._inbox.put_nowait(item)


def _transport(endpoint: FakeEndpoint, **kwargs: int) -> EmulatorTransport:
    async def opener() -> ChannelEndpoint:
        return endpoint

    return EmulatorTransport("test", opener, **kwargs)


async def test_accepts_both_terminators_and_reassembles_split_chunks() -> None:
    endpoint = FakeEndpoint(b'{"a":1}\r\n{"b":2}\n{"c"', b":3}\r\n")
    transport = _transport(endpoint)
    await transport.connect()

    assert await transport.receive_line() == '{"a":1}'
    assert await transport.receive_line() == '{"b":2}'
    assert await transport.receive_line() == '{"c":3}'


async def test_we_send_the_terminator_the_device_uses() -> None:
    endpoint = FakeEndpoint()
    transport = _transport(endpoint)
    await transport.connect()

    await transport.send_line('{"id":1}')

    assert endpoint.sent == [b'{"id":1}\r\n']


async def test_clean_eof_is_a_lost_connection_not_an_empty_line() -> None:
    endpoint = FakeEndpoint(b"")
    transport = _transport(endpoint)
    await transport.connect()

    with pytest.raises(ConnectionLostError) as caught:
        await transport.receive_line()
    assert caught.value.was_reset is False


async def test_reset_is_flagged_distinctly_from_a_clean_close() -> None:
    endpoint = FakeEndpoint()
    endpoint.push(ConnectionLostError("reset", was_reset=True))
    transport = _transport(endpoint)
    await transport.connect()

    with pytest.raises(ConnectionLostError) as caught:
        await transport.receive_line()
    assert caught.value.was_reset is True


async def test_a_babbling_peer_cannot_exhaust_memory() -> None:
    endpoint = FakeEndpoint(b"x" * 64)
    transport = _transport(endpoint, max_line_bytes=32)
    await transport.connect()

    with pytest.raises(FrameTooLargeError):
        await transport.receive_line()


async def test_using_a_transport_before_connect_raises() -> None:
    transport = _transport(FakeEndpoint())
    with pytest.raises(TransportClosedError):
        await transport.send_line("{}")


async def test_close_is_idempotent_and_closes_the_endpoint() -> None:
    endpoint = FakeEndpoint()
    transport = _transport(endpoint)
    await transport.connect()

    await transport.aclose()
    await transport.aclose()

    assert endpoint.closed is True
    assert transport.is_connected is False


async def test_a_refusing_opener_surfaces_as_port_refused() -> None:
    async def opener() -> ChannelEndpoint:
        raise PortRefusedError("control port restarting")

    transport = EmulatorTransport("test", opener)
    with pytest.raises(PortRefusedError):
        await transport.connect()


def test_both_implementations_satisfy_the_protocol() -> None:
    tcp = TcpTransport("host.invalid", 4701)
    emu = _transport(FakeEndpoint())
    assert isinstance(tcp, SeestarTransport)
    assert isinstance(emu, SeestarTransport)


def test_the_factory_pairs_control_and_events_on_the_observed_ports() -> None:
    factory = TcpTransportFactory("host.invalid")
    assert factory.control_port == 4701
    assert "4701" in factory.control().name
    assert "4700" in factory.events().name


# --------------------------------------------------------------------------- #
# Redaction — the device hands out two cleartext passwords
# --------------------------------------------------------------------------- #


def test_redaction_covers_both_observed_credential_paths() -> None:
    payload = {
        "ap": {"ssid": "scope", "passwd": "hunter2"},
        "setting": {"connect_lock": False, "password": "hunter2"},
    }

    cleaned = redact(payload)

    assert cleaned == {
        "ap": {"ssid": "scope", "passwd": REDACTED},
        "setting": {"connect_lock": False, "password": REDACTED},
    }
    assert "hunter2" not in repr(cleaned)


def test_redaction_keeps_key_mgmt_which_is_a_mode_not_a_secret() -> None:
    cleaned = redact({"station": {"ssid": "net", "key_mgmt": "WPA2-PSK"}})
    assert cleaned == {"station": {"ssid": "net", "key_mgmt": "WPA2-PSK"}}


def test_redaction_walks_lists_and_leaves_the_input_untouched() -> None:
    payload = {"clients": [{"token": "abc"}, {"name": "ok"}]}

    cleaned = redact(payload)

    assert cleaned == {"clients": [{"token": REDACTED}, {"name": "ok"}]}
    assert payload["clients"][0]["token"] == "abc"
