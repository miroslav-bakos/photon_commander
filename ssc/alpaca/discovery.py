# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Alpaca discovery: the UDP :32227 responder.

A client broadcasts the ASCII datagram ``alpacadiscovery1`` to UDP 32227; every
Alpaca server on the segment answers with the TCP port its API is on. That is the
whole protocol.

Two deliberate restrictions:

* The responder is **off unless LAN exposure is on**. Announcing a loopback-only
  server to the network advertises a port nobody else can reach.
* It answers private and link-local sources only. A discovery probe from a
  routable address is not something a LAN tool should confirm the existence of a
  telescope to.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from typing import Final

from ssc.alpaca.server import BindRefusedError, ServerConfig

_log = logging.getLogger(__name__)

#: The IANA-registered Alpaca discovery port.
DISCOVERY_PORT: Final = 32227

#: The exact datagram a discovering client sends.
DISCOVERY_MESSAGE: Final = "alpacadiscovery1"

#: Longest datagram worth decoding. The real one is 16 bytes.
MAX_DATAGRAM_BYTES: Final = 64


def is_discovery_request(data: bytes) -> bool:
    """True for the discovery datagram, and nothing else."""
    if not data or len(data) > MAX_DATAGRAM_BYTES:
        return False
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return False
    return text.strip().rstrip("\x00").casefold().startswith(DISCOVERY_MESSAGE)


def source_is_local(host: str) -> bool:
    """Whether a discovery source is on a network SSC will answer."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def discovery_response(alpaca_port: int) -> bytes:
    return json.dumps({"AlpacaPort": alpaca_port}).encode("ascii")


class AlpacaDiscoveryProtocol(asyncio.DatagramProtocol):
    """Answers ``alpacadiscovery1`` with this server's API port.

    Written against :class:`asyncio.DatagramProtocol` rather than around a socket
    so the whole behaviour is testable by handing it datagrams directly.
    """

    def __init__(self, alpaca_port: int, *, answer_remote: bool = False) -> None:
        self._alpaca_port = alpaca_port
        self._answer_remote = answer_remote
        self._transport: asyncio.DatagramTransport | None = None
        self.answered = 0
        self.ignored = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not isinstance(transport, asyncio.DatagramTransport):
            raise TypeError("the Alpaca discovery responder needs a datagram transport")
        self._transport = transport

    def connection_lost(self, exc: Exception | None) -> None:
        self._transport = None

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        host = str(addr[0]) if addr else ""
        if not is_discovery_request(data):
            self.ignored += 1
            return
        if not self._answer_remote and not source_is_local(host):
            self.ignored += 1
            _log.warning(
                "ignoring an Alpaca discovery probe from %s: it is not on a private network",
                host,
            )
            return
        transport = self._transport
        if transport is None:
            self.ignored += 1
            return
        transport.sendto(discovery_response(self._alpaca_port), addr)
        self.answered += 1

    def error_received(self, exc: Exception) -> None:
        _log.warning("Alpaca discovery socket error: %s", exc)


async def start_discovery_responder(
    config: ServerConfig,
    *,
    listen_host: str = "0.0.0.0",
    discovery_port: int = DISCOVERY_PORT,
) -> tuple[asyncio.DatagramTransport, AlpacaDiscoveryProtocol]:
    """Bind the responder. Refuses unless the server itself is LAN-exposed."""
    if not config.allow_lan:
        raise BindRefusedError(
            "discovery is disabled while the Alpaca server is loopback-only: "
            "advertising a port nothing on the LAN can reach is worse than silence"
        )
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: AlpacaDiscoveryProtocol(config.port),
        local_addr=(listen_host, discovery_port),
        allow_broadcast=True,
    )
    _log.info(
        "Alpaca discovery responder listening on %s:%d, advertising port %d",
        listen_host,
        discovery_port,
        config.port,
    )
    return transport, protocol


__all__ = [
    "DISCOVERY_MESSAGE",
    "DISCOVERY_PORT",
    "MAX_DATAGRAM_BYTES",
    "AlpacaDiscoveryProtocol",
    "discovery_response",
    "is_discovery_request",
    "source_is_local",
    "start_discovery_responder",
]
