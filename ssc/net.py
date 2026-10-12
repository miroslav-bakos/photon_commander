# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""This host's own network interfaces.

A dependency-free leaf: it imports nothing from the rest of ``ssc``, so both
``ssc.main`` (whose bind guard must see every interface) and
``ssc.seestar.discovery`` (which derives the subnets worth scanning) can use it
without either depending on the other. That direction matters —
``ssc.seestar`` importing back up into ``ssc.main`` would invert the dependency
graph that CLAUDE.md invariant 1 exists to keep one-way.

Enumeration is real, via ``psutil``. Two earlier approaches were wrong in ways
worth recording so they are not reintroduced:

* Asking the kernel "what source address would you use to reach this
  documentation address" reveals only the *default route's* interface. A host
  with a second NIC — a Pi on Ethernet and Wi-Fi at once, say — has that second
  address never appear. For the bind guard that let a public interface through
  a wildcard bind; for discovery it meant only one subnet was ever scanned, so
  a scope on the other interface was invisible.
* Assuming /24 because "netmasks are not portably readable without a
  dependency". ``psutil`` is a runtime dependency now, so the real netmask is
  readable and there is no reason to guess.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from dataclasses import dataclass
from typing import Final

import psutil

#: The three private ranges. Anything outside them is not ours to scan, and for
#: the bind guard is the thing being guarded against (DESIGN.md §9).
RFC1918_BLOCKS: Final = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)

#: Used only when an interface reports an address but no usable netmask.
FALLBACK_PREFIX_LEN: Final = 24


@dataclass(frozen=True, slots=True)
class LocalInterface:
    """One IPv4 address actually bound to one of this host's interfaces."""

    name: str
    address: str
    netmask: str | None

    @property
    def is_private(self) -> bool:
        with contextlib.suppress(ValueError):
            return any(ipaddress.IPv4Address(self.address) in b for b in RFC1918_BLOCKS)
        return False

    def network(self, *, fallback_prefix_len: int = FALLBACK_PREFIX_LEN) -> ipaddress.IPv4Network:
        """The subnet this address sits on, from its real netmask where possible."""
        if self.netmask:
            with contextlib.suppress(ValueError):
                return ipaddress.IPv4Network(f"{self.address}/{self.netmask}", strict=False)
        return ipaddress.IPv4Network(f"{self.address}/{fallback_prefix_len}", strict=False)


def local_ipv4_interfaces() -> list[LocalInterface]:
    """Every IPv4 address bound to this host, with its netmask where known."""
    found: list[LocalInterface] = []
    seen: set[str] = set()
    for name, entries in psutil.net_if_addrs().items():
        for entry in entries:
            if entry.family != socket.AF_INET:
                continue
            if entry.address in seen:
                continue
            seen.add(entry.address)
            found.append(
                LocalInterface(
                    name=name,
                    address=entry.address,
                    netmask=getattr(entry, "netmask", None),
                )
            )
    return found


def local_ipv4_addresses() -> list[str]:
    """Every IPv4 address bound to this host, loopback included."""
    return [iface.address for iface in local_ipv4_interfaces()]


def local_ipv4_networks(
    *, fallback_prefix_len: int = FALLBACK_PREFIX_LEN
) -> list[ipaddress.IPv4Network]:
    """The RFC 1918 subnets worth scanning, one per private interface.

    Anything outside the three private blocks is dropped here rather than
    rejected later, so a host with a public address simply contributes no
    range. A multi-homed host contributes one range per interface — which is
    the entire point of enumerating rather than guessing.
    """
    networks: list[ipaddress.IPv4Network] = []
    for iface in local_ipv4_interfaces():
        if not iface.is_private:
            continue
        network = iface.network(fallback_prefix_len=fallback_prefix_len)
        if network not in networks:
            networks.append(network)
    return networks
