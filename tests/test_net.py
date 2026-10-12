"""Interface enumeration must see every interface, not just the default route's.

Two bugs lived here before. The bind guard waved a public interface through a
wildcard bind because it never saw the second NIC, and discovery scanned only
one subnet so a scope on the other interface was invisible. Both came from
asking the kernel for a *route* rather than enumerating interfaces, so these
tests pin the enumeration behaviour rather than the values on any one host.
"""

from __future__ import annotations

import ipaddress
import socket
from collections import namedtuple

import pytest

from ssc import net

_Addr = namedtuple("_Addr", "family address netmask broadcast ptp")


def _addr(address: str, netmask: str | None) -> _Addr:
    return _Addr(socket.AF_INET, address, netmask, None, None)


def _fake_host(monkeypatch: pytest.MonkeyPatch, interfaces: dict[str, list[_Addr]]) -> None:
    monkeypatch.setattr(net.psutil, "net_if_addrs", lambda: interfaces)


def test_a_multi_homed_host_contributes_a_subnet_per_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: a routing guess saw eth0 only, so wlan0's subnet was never scanned."""
    _fake_host(
        monkeypatch,
        {
            "lo": [_addr("127.0.0.1", "255.0.0.0")],
            "eth0": [_addr("192.168.1.4", "255.255.255.0")],
            "wlan0": [_addr("10.42.0.7", "255.255.255.0")],
        },
    )
    networks = net.local_ipv4_networks()
    assert ipaddress.IPv4Network("192.168.1.0/24") in networks
    assert ipaddress.IPv4Network("10.42.0.0/24") in networks


def test_the_real_netmask_is_used_rather_than_assuming_slash_24(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_host(monkeypatch, {"eth0": [_addr("10.0.0.5", "255.255.0.0")]})
    assert net.local_ipv4_networks() == [ipaddress.IPv4Network("10.0.0.0/16")]


def test_an_interface_with_no_netmask_falls_back_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_host(monkeypatch, {"eth0": [_addr("192.168.5.9", None)]})
    assert net.local_ipv4_networks() == [ipaddress.IPv4Network("192.168.5.0/24")]


def test_public_addresses_contribute_no_scan_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scanning outside RFC 1918 is never ours to do (DESIGN.md §9)."""
    _fake_host(
        monkeypatch,
        {
            "eth0": [_addr("203.0.113.7", "255.255.255.0")],
            "eth1": [_addr("172.16.3.2", "255.240.0.0")],
        },
    )
    assert net.local_ipv4_networks() == [ipaddress.IPv4Network("172.16.0.0/12")]


def test_loopback_is_reported_as_an_address_but_is_not_a_scan_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bind guard needs to see loopback; discovery must not scan it."""
    _fake_host(monkeypatch, {"lo": [_addr("127.0.0.1", "255.0.0.0")]})
    assert net.local_ipv4_addresses() == ["127.0.0.1"]
    assert net.local_ipv4_networks() == []


def test_discovery_and_the_bind_guard_share_one_implementation() -> None:
    """They diverged once; the duplicate is what let discovery keep the old bug."""
    from ssc import main
    from ssc.seestar import discovery

    assert discovery.local_ipv4_addresses() == net.local_ipv4_addresses()
    assert main.local_ipv4_addresses() == net.local_ipv4_addresses()
