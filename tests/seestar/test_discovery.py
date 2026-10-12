"""Active-probe discovery: bounded, RFC 1918 only, cancellable, honest.

The device does not advertise itself over mDNS or SSDP, so a scan is the only
option. These tests run with sockets disabled and inject the prober, because the
thing worth testing is the *policy*, not the socket.
"""

from __future__ import annotations

import asyncio
import ipaddress

import pytest

from ssc.seestar.discovery import (
    MAX_SCAN_HOSTS,
    DiscoveryError,
    discover,
    local_ipv4_networks,
)
from ssc.seestar.session import DeviceInfo

SUBNET = ipaddress.IPv4Network("192.168.99.0/29")  # 6 usable hosts
TARGET = "192.168.99.4"


def _found(host: str, port: int) -> DeviceInfo:
    return DeviceInfo(
        host=host,
        control_port=port,
        reachable=True,
        firmware="8.46",
        model="Seestar S30 Pro",
        serial="SYNTHETIC000001",
    )


def _missing(host: str, port: int) -> DeviceInfo:
    return DeviceInfo(host=host, control_port=port, reachable=False, note="timed out")


async def _prober(host: str, port: int, timeout_s: float) -> DeviceInfo:
    await asyncio.sleep(0)
    return _found(host, port) if host == TARGET else _missing(host, port)


# --------------------------------------------------------------------------- #
# Policy: never scan outside RFC 1918, never scan an unbounded range
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "network",
    ["8.8.8.0/24", "1.1.1.0/28", "203.0.113.0/24"],
)
async def test_a_public_range_is_refused(network: str) -> None:
    with pytest.raises(DiscoveryError, match="RFC 1918"):
        await discover(subnets=[ipaddress.IPv4Network(network)], prober=_prober)


@pytest.mark.parametrize("network", ["127.0.0.0/24", "169.254.0.0/24"])
async def test_loopback_and_link_local_are_refused(network: str) -> None:
    with pytest.raises(DiscoveryError):
        await discover(subnets=[ipaddress.IPv4Network(network)], prober=_prober)


async def test_an_oversized_range_is_refused_rather_than_truncated() -> None:
    with pytest.raises(DiscoveryError, match=str(MAX_SCAN_HOSTS)):
        await discover(subnets=[ipaddress.IPv4Network("10.0.0.0/16")], prober=_prober)


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #


async def test_a_scan_finds_the_device_and_reports_what_a_picker_needs() -> None:
    progress: list[tuple[int, int]] = []

    found = await discover(
        timeout=5.0,
        subnets=[SUBNET],
        prober=_prober,
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert [device.host for device in found] == [TARGET]
    assert found[0].info.firmware == "8.46"
    assert "Seestar S30 Pro" in found[0].label
    assert "SYNTHETIC000001" in found[0].label
    assert progress[-1] == (6, 6)


async def test_one_bad_host_does_not_abort_the_sweep() -> None:
    async def flaky(host: str, port: int, timeout_s: float) -> DeviceInfo:
        if host == "192.168.99.1":
            raise OSError("network unreachable")
        return await _prober(host, port, timeout_s)

    found = await discover(timeout=5.0, subnets=[SUBNET], prober=flaky)

    assert [device.host for device in found] == [TARGET]


async def test_the_deadline_returns_partial_results_rather_than_losing_them() -> None:
    async def slow(host: str, port: int, timeout_s: float) -> DeviceInfo:
        if host == TARGET:
            return _found(host, port)
        await asyncio.sleep(30)
        return _missing(host, port)

    found = await discover(timeout=0.15, subnets=[SUBNET], prober=slow, concurrency=8)

    assert [device.host for device in found] == [TARGET]


async def test_a_scan_is_cancellable() -> None:
    started = asyncio.Event()

    async def slow(host: str, port: int, timeout_s: float) -> DeviceInfo:
        started.set()
        await asyncio.sleep(30)
        return _missing(host, port)

    task = asyncio.create_task(discover(timeout=30.0, subnets=[SUBNET], prober=slow))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_scan_probes_only_the_hosts_in_the_given_range() -> None:
    probed: list[str] = []

    async def recording(host: str, port: int, timeout_s: float) -> DeviceInfo:
        probed.append(host)
        return _missing(host, port)

    await discover(timeout=5.0, subnets=[SUBNET], prober=recording)

    assert sorted(probed) == sorted(str(address) for address in SUBNET.hosts())
    assert all(ipaddress.IPv4Address(host).is_private for host in probed)


async def test_no_range_means_no_scan_not_a_crash() -> None:
    assert await discover(subnets=[], prober=_prober) == []


# --------------------------------------------------------------------------- #
# Subnet derivation
# --------------------------------------------------------------------------- #


def test_only_private_local_addresses_produce_a_scan_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ssc.seestar.discovery.local_ipv4_addresses",
        lambda: ["192.168.1.23", "127.0.0.1", "203.0.113.9", "169.254.4.4"],
    )

    networks = local_ipv4_networks()

    assert networks == [ipaddress.IPv4Network("192.168.1.0/24")]


def test_derived_ranges_stay_within_the_host_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ssc.seestar.discovery.local_ipv4_addresses", lambda: ["10.1.2.3"])

    for network in local_ipv4_networks():
        assert network.num_addresses <= MAX_SCAN_HOSTS
