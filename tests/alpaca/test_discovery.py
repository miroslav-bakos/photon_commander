"""The UDP :32227 responder, exercised without a socket."""

from __future__ import annotations

import asyncio
import json

import pytest

from ssc.alpaca.discovery import (
    DISCOVERY_MESSAGE,
    DISCOVERY_PORT,
    AlpacaDiscoveryProtocol,
    discovery_response,
    is_discovery_request,
    source_is_local,
    start_discovery_responder,
)
from ssc.alpaca.server import BindRefusedError, ServerConfig


class FakeTransport(asyncio.DatagramTransport):
    """Records what the responder sends, so no socket is needed to test it."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, object]] = []

    def sendto(self, data: bytes, addr: object = None) -> None:
        self.sent.append((data, addr))


def responder(*, answer_remote: bool = False) -> tuple[AlpacaDiscoveryProtocol, FakeTransport]:
    protocol = AlpacaDiscoveryProtocol(5555, answer_remote=answer_remote)
    transport = FakeTransport()
    protocol.connection_made(transport)
    return protocol, transport


def test_the_discovery_port_and_datagram_are_the_registered_ones() -> None:
    assert DISCOVERY_PORT == 32227
    assert DISCOVERY_MESSAGE == "alpacadiscovery1"


@pytest.mark.parametrize(
    "payload",
    [b"alpacadiscovery1", b"alpacadiscovery1\x00", b"ALPACADISCOVERY1", b"alpacadiscovery1 \n"],
)
def test_the_discovery_datagram_is_recognised(payload: bytes) -> None:
    assert is_discovery_request(payload)


@pytest.mark.parametrize(
    "payload",
    [b"", b"hello", b"alpaca", b"\xff\xfe", b"alpacadiscovery1" + b"x" * 128],
)
def test_anything_else_is_not(payload: bytes) -> None:
    assert not is_discovery_request(payload)


def test_a_probe_from_the_lan_is_answered_with_the_api_port() -> None:
    protocol, transport = responder()
    protocol.datagram_received(b"alpacadiscovery1", ("192.168.1.20", 40000))
    assert protocol.answered == 1
    data, addr = transport.sent[0]
    assert json.loads(data) == {"AlpacaPort": 5555}
    assert addr == ("192.168.1.20", 40000)
    assert data == discovery_response(5555)


def test_junk_is_ignored_silently() -> None:
    protocol, transport = responder()
    protocol.datagram_received(b"who is there", ("127.0.0.1", 1))
    assert transport.sent == []
    assert protocol.ignored == 1


def test_a_probe_from_a_routable_address_is_not_answered() -> None:
    protocol, transport = responder()
    protocol.datagram_received(b"alpacadiscovery1", ("8.8.8.8", 40000))
    assert transport.sent == []
    assert protocol.ignored == 1
    assert protocol.answered == 0


@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.5", "10.1.2.3", "172.16.0.1", "::1"])
def test_private_and_loopback_sources_count_as_local(host: str) -> None:
    assert source_is_local(host)


@pytest.mark.parametrize("host", ["8.8.8.8", "93.184.216.34", "not-an-address", ""])
def test_everything_else_does_not(host: str) -> None:
    assert not source_is_local(host)


def test_a_datagram_before_the_transport_is_attached_is_dropped() -> None:
    protocol = AlpacaDiscoveryProtocol(5555)
    protocol.datagram_received(b"alpacadiscovery1", ("127.0.0.1", 1))
    assert protocol.answered == 0
    assert protocol.ignored == 1


async def test_discovery_refuses_to_advertise_a_loopback_only_server() -> None:
    with pytest.raises(BindRefusedError, match="loopback-only"):
        await start_discovery_responder(ServerConfig())
