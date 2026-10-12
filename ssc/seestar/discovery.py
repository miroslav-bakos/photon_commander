# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Finding a Seestar on the LAN.

The device **does not announce itself**: a passive mDNS listen, an *active*
``_services._dns-sd._udp.local`` PTR query and an SSDP ``M-SEARCH ssdp:all`` all
came back with nothing from it (``docs/M0-FINDINGS.md``). So autodetection is an
active probe, and it is built to be a good citizen on someone else's network:

* **RFC 1918 only.** Loopback, link-local, multicast and any public range are
  refused outright, and a subnet larger than :data:`MAX_SCAN_HOSTS` is refused
  rather than silently truncated.
* **Bounded and concurrent**, with an overall deadline that returns partial
  results instead of losing them.
* **Cancellable**, with progress reporting for a UI that must show what it is
  doing.
* **One safe call per host**: TCP-connect to the control port and ask
  ``test_connection``. No unknown method is ever aimed at an unknown device.

Since 2026-09-10 there are **two** surfaces to find a scope on, and a scan needs
both because no device serves both:

* the JSON-RPC control port sweep above, which is how an 8.46 device answers;
* **Alpaca's own UDP discovery on :32227.** A device whose JSON-RPC port has
  been withdrawn is invisible to the sweep and answers this instantly (52 ms,
  measured). It runs *alongside* the sweep, so it costs an 8.46 scan nothing.

  **This device does not answer a broadcast**, which the ASCOM discovery spec
  says it should: measured 2026-09-10, both ``255.255.255.255`` and the directed
  ``…/24`` broadcast, from an ephemeral source port and from :32227, four
  combinations, no reply — while the same datagram sent *unicast* to the device
  answers every time. So the pass is a unicast sweep of :32227 across the same
  validated ranges, with the broadcast addresses included as well for firmwares
  that do honour them. It is still one socket and one datagram per host, which
  is far cheaper than the TCP sweep it runs beside.

``probe()`` reports the real result — reachable, firmware, model, serial, or the
actual reason it failed — never a green tick. Remembering the last good address
belongs to the layer above; this module gives it the two things it needs:
:func:`probe` to re-verify a remembered address first, and :func:`discover` to
fall back to a scan when the device has moved.

No address is hardcoded anywhere in this module (``CLAUDE.md`` invariant 11).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from ssc import net as _net
from ssc.seestar.native import identify_alpaca
from ssc.seestar.routing import (
    ControlSurface,
    SurfaceChoice,
    alpaca_discovery,
    probe_control_surface,
)
from ssc.seestar.session import DeviceInfo, identify
from ssc.seestar.transport import CONTROL_PORT, EVENT_PORT, TcpTransportFactory

_log = logging.getLogger(__name__)

DEFAULT_DISCOVERY_TIMEOUT_S: Final = 8.0
DEFAULT_PER_HOST_TIMEOUT_S: Final = 0.6
DEFAULT_CONCURRENCY: Final = 64
DEFAULT_PROBE_TIMEOUT_S: Final = 3.0

#: How long the UDP pass listens for Alpaca answers. It runs concurrently with
#: the sweep, so this is not added to a scan's wall time — but it must be long
#: enough for a device that is busy to answer, and short enough that a scan of
#: an empty network still feels instant.
DEFAULT_ALPACA_LISTEN_S: Final = 1.5

#: Limited broadcast. Never leaves the local link, so it is always safe to send
#: and it reaches a device on an interface whose netmask we misread.
LIMITED_BROADCAST: Final = "255.255.255.255"

#: A /24 is 254 hosts. Anything wider than a /22 is refused rather than scanned:
#: a slow, huge sweep of someone's network is not a feature.
MAX_SCAN_HOSTS: Final = 1024

#: The three RFC 1918 blocks, spelled out. ``IPv4Network.is_private`` is *not*
#: the same test — it also accepts loopback, link-local, CGNAT and the RFC 5737
#: documentation ranges, none of which is a LAN we should be sweeping.
RFC1918_BLOCKS: Final[tuple[ipaddress.IPv4Network, ...]] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)

#: Assumed prefix when deriving a scan range from a local address. Interface
#: Fallback only: used for an interface that reports an address but no usable
#: netmask. Real netmasks come from :mod:`ssc.net`. Callers who know better
#: still pass ``subnets=`` explicitly.
ASSUMED_PREFIX_LEN: Final = 24


class DiscoveryError(Exception):
    """A scan was asked to do something it must not do."""


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """One device that answered ``test_connection``."""

    host: str
    control_port: int
    info: DeviceInfo
    responded_in_s: float

    @property
    def label(self) -> str:
        """What a picker should show: address, model, firmware, serial."""
        bits = [self.host]
        if self.info.model:
            bits.append(self.info.model)
        if self.info.firmware:
            bits.append(f"fw {self.info.firmware}")
        if self.info.serial:
            bits.append(f"sn {self.info.serial}")
        return " · ".join(bits)


#: Probes one host over JSON-RPC. Injectable so the scan logic is testable
#: without a network.
Prober = Callable[[str, int, float], Coroutine[object, object, DeviceInfo]]

#: Observes which control surface a host serves. Injectable for the same reason.
SurfaceProbe = Callable[..., Coroutine[object, object, SurfaceChoice]]

#: Probes one host over JSON-RPC with an explicit event port. The fallback seam.
JsonRpcProbe = Callable[[str, int, int, float], Coroutine[object, object, DeviceInfo]]

#: Identifies a host over its own Alpaca server, without connecting to it.
AlpacaIdentifier = Callable[..., Coroutine[object, object, tuple[DeviceInfo, tuple[str, ...]]]]

#: Sends the Alpaca discovery datagram and collects ``{responder: port}``.
AlpacaBroadcaster = Callable[..., Coroutine[object, object, dict[str, int]]]


async def _probe_json_rpc(
    host: str,
    control_port: int,
    event_port: int,
    timeout: float,  # noqa: ASYNC109 - the caller's budget
) -> DeviceInfo:
    factory = TcpTransportFactory(
        host, control_port=control_port, event_port=event_port, connect_timeout=timeout
    )
    return await identify(factory, timeout=timeout)


async def probe(
    host: str,
    *,
    control_port: int = CONTROL_PORT,
    event_port: int = EVENT_PORT,
    timeout: float = DEFAULT_PROBE_TIMEOUT_S,  # noqa: ASYNC109 - the caller's budget
    surface_probe: SurfaceProbe = probe_control_surface,
    alpaca_identify: AlpacaIdentifier = identify_alpaca,
    json_rpc_probe: JsonRpcProbe = _probe_json_rpc,
) -> DeviceInfo:
    """Test one address and report what is actually there.

    Asks the same question the connect path asks — *which control surface does
    this device serve* (:mod:`ssc.seestar.routing`) — so "Test connection" and
    "Connect" can never disagree about a device. A device on the native Alpaca
    surface comes back **identified**, with the model and serial it advertises
    and with ``firmware`` honestly ``None``; it is not connected to in the
    process.

    Never raises for an unreachable host — that is a result, not an error, and it
    is carried in :attr:`DeviceInfo.note`.
    """
    choice = await surface_probe(host, timeout=timeout)
    if choice.surface is ControlSurface.NATIVE_ALPACA and choice.alpaca_port is not None:
        info, _types = await alpaca_identify(host, port=choice.alpaca_port, timeout=timeout)
        if info.reachable:
            return replace(info, note=f"{info.note}; {choice.evidence}")
        # The server answered the surface probe and then did not identify —
        # report it, and still give the JSON-RPC port its turn below.
        _log.info("alpaca identify of %s failed after a positive probe: %s", host, info.note)
    return await json_rpc_probe(host, control_port, event_port, timeout)


async def discover(
    timeout: float = DEFAULT_DISCOVERY_TIMEOUT_S,  # noqa: ASYNC109 - the public contract
    *,
    subnets: Sequence[ipaddress.IPv4Network] | None = None,
    control_port: int = CONTROL_PORT,
    concurrency: int = DEFAULT_CONCURRENCY,
    per_host_timeout: float = DEFAULT_PER_HOST_TIMEOUT_S,
    on_progress: Callable[[int, int], None] | None = None,
    prober: Prober | None = None,
    alpaca_listen_s: float = DEFAULT_ALPACA_LISTEN_S,
    broadcaster: AlpacaBroadcaster = alpaca_discovery,
    alpaca_identify: AlpacaIdentifier = identify_alpaca,
) -> list[DiscoveredDevice]:
    """Scan this host's own RFC 1918 subnets for a Seestar, both ways at once.

    Two passes run concurrently, because a given device answers exactly one of
    them: a TCP sweep of ``control_port`` (an 8.46 device), and an Alpaca
    discovery sweep on UDP :32227 (a device whose JSON-RPC port is withdrawn —
    unicast, because this firmware ignores the broadcast the spec describes).
    A device found on both — which no observed firmware is — is reported once,
    on the surface the connect path would choose.

    Args:
        timeout: overall deadline. Whatever has been found by then is returned;
            outstanding probes are cancelled.
        subnets: override the auto-detected ranges. Still validated.
        on_progress: called as ``(completed, total)`` after each host. Only the
            sweep reports progress; the broadcast has no host count to report.
        prober: injection seam for tests; defaults to the JSON-RPC probe.
        broadcaster: injection seam for the UDP pass.
        alpaca_identify: injection seam for identifying what answered it.

    Raises:
        DiscoveryError: a subnet is outside RFC 1918 or too large to scan.
    """
    networks = list(subnets) if subnets is not None else local_ipv4_networks()
    for network in networks:
        _validate_network(network)
    if not networks:
        # No range to scan is an explicit "scan nothing", and that includes not
        # broadcasting: a host with no RFC 1918 interface is not on a LAN SSC
        # has any business shouting on.
        return []
    hosts = _hosts(networks)

    probe_host: Prober = prober or _default_prober
    semaphore = asyncio.Semaphore(max(1, concurrency))
    loop = asyncio.get_running_loop()
    completed = 0
    found: list[DiscoveredDevice] = []

    async def one(host: str) -> None:
        nonlocal completed
        async with semaphore:
            started = loop.time()
            try:
                info = await probe_host(host, control_port, per_host_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a bad host must not abort the sweep
                _log.debug("probe of %s failed: %s", host, exc)
                info = DeviceInfo(
                    host=host,
                    control_port=control_port,
                    reachable=False,
                    note=f"{type(exc).__name__}: {exc}",
                )
            if info.reachable:
                found.append(
                    DiscoveredDevice(
                        host=host,
                        control_port=control_port,
                        info=info,
                        responded_in_s=loop.time() - started,
                    )
                )
        completed += 1
        if on_progress is not None:
            try:
                on_progress(completed, len(hosts))
            except Exception:
                _log.exception("discovery progress callback raised")

    tasks = [asyncio.create_task(one(host), name=f"seestar-probe-{host}") for host in hosts]
    alpaca = asyncio.create_task(
        _discover_alpaca(
            networks,
            hosts,
            listen_s=min(alpaca_listen_s, timeout),
            broadcaster=broadcaster,
            identify=alpaca_identify,
        ),
        name="seestar-alpaca-discovery",
    )
    try:
        await asyncio.wait([*tasks, alpaca], timeout=timeout)
    finally:
        for task in (*tasks, alpaca):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    by_host = {device.host: device for device in found}
    if alpaca.done() and not alpaca.cancelled():
        failure = alpaca.exception()
        if failure is not None:
            # The UDP pass failing is a real, reportable loss of coverage — a
            # device on the Alpaca surface simply will not be found — not
            # something to swallow because the sweep happened to succeed.
            _log.warning("alpaca discovery pass failed: %r", failure)
        else:
            # The Alpaca answer wins a tie: it is the surface the connect path
            # would choose for that host, so the address handed back is one
            # that will actually work.
            for device in alpaca.result():
                by_host[device.host] = device
    return sorted(by_host.values(), key=lambda device: ipaddress.IPv4Address(device.host))


async def _discover_alpaca(
    networks: Sequence[ipaddress.IPv4Network],
    hosts: Sequence[str],
    *,
    listen_s: float,
    broadcaster: AlpacaBroadcaster,
    identify: AlpacaIdentifier,
) -> list[DiscoveredDevice]:
    """One Alpaca discovery datagram per host, then identify whatever answered.

    Targets are every host in the *validated* ranges — because the observed
    firmware ignores broadcasts — plus each network's directed broadcast and the
    limited broadcast, which never leaves the link and costs two more datagrams.
    Answers from outside those networks are dropped: this scan reports what is on
    the user's own LAN and nothing else.
    """
    targets = [*hosts, *(str(network.broadcast_address) for network in networks)]
    targets.append(LIMITED_BROADCAST)
    loop = asyncio.get_running_loop()
    answered = await broadcaster(targets, timeout=listen_s, broadcast=True)
    answers: dict[str, int] = {}
    for responder, port in answered.items():
        if _within(responder, networks):
            answers[responder] = port
        else:
            # A broadcast reaches whatever is on the link. Reporting a device
            # outside the ranges this scan was allowed to touch would be a
            # result the user never asked for.
            _log.info("ignoring an Alpaca answer from %s: outside the scanned ranges", responder)
    if not answers:
        return []

    async def identify_one(host: str, port: int) -> DiscoveredDevice | None:
        # Timed around the identify, not around the listen window: the window is
        # a budget SSC chose, and reporting it as the device's response time
        # would be a made-up number in a field a picker shows.
        started = loop.time()
        info, types = await identify(host, port=port, timeout=listen_s)
        elapsed = loop.time() - started
        if not info.reachable:
            _log.debug("%s answered Alpaca discovery but did not identify: %s", host, info.note)
            return None
        if "telescope" not in types:
            # An Alpaca server with no mount is somebody else's device, not a
            # scope SSC can drive. Say what was seen rather than filtering
            # silently at a level the user cannot inspect.
            _log.info("ignoring Alpaca server at %s: no telescope device (saw %s)", host, types)
            return None
        return DiscoveredDevice(host=host, control_port=port, info=info, responded_in_s=elapsed)

    results = await asyncio.gather(
        *(identify_one(host, port) for host, port in sorted(answers.items())),
        return_exceptions=True,
    )
    devices: list[DiscoveredDevice] = []
    for result in results:
        if isinstance(result, BaseException):
            _log.debug("alpaca identify raised during discovery: %s", result)
            continue
        if result is not None:
            devices.append(result)
    return devices


async def _default_prober(
    host: str,
    control_port: int,
    timeout: float,  # noqa: ASYNC109 - matches the Prober signature
) -> DeviceInfo:
    """The sweep's per-host probe: JSON-RPC only, deliberately.

    The public :func:`probe` also asks the Alpaca surface, which is right for
    one address a user typed and wrong for 254 of them — the broadcast pass
    covers that surface for the whole subnet in one datagram.
    """
    return await _probe_json_rpc(host, control_port, EVENT_PORT, timeout)


# --------------------------------------------------------------------------- #
# Subnet enumeration and validation
# --------------------------------------------------------------------------- #


def _within(host: str, networks: Sequence[ipaddress.IPv4Network]) -> bool:
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        return False
    return any(address in network for network in networks)


def is_rfc1918(network: ipaddress.IPv4Network) -> bool:
    return any(network.subnet_of(block) for block in RFC1918_BLOCKS)


def _validate_network(network: ipaddress.IPv4Network) -> None:
    if network.is_loopback or network.is_link_local or network.is_multicast:
        raise DiscoveryError(f"refusing to scan {network}: loopback/link-local/multicast")
    if not is_rfc1918(network):
        raise DiscoveryError(f"refusing to scan {network}: not an RFC 1918 range")
    if network.num_addresses > MAX_SCAN_HOSTS:
        raise DiscoveryError(
            f"refusing to scan {network}: {network.num_addresses} addresses exceeds "
            f"the {MAX_SCAN_HOSTS} host cap — narrow the range"
        )


def _hosts(networks: Iterable[ipaddress.IPv4Network]) -> list[str]:
    seen: dict[str, None] = {}
    for network in networks:
        for address in network.hosts():
            seen.setdefault(str(address), None)
    return list(seen)


def local_ipv4_addresses() -> list[str]:
    """This host's own IPv4 addresses. Delegates to :mod:`ssc.net`."""
    return _net.local_ipv4_addresses()


def local_ipv4_networks(*, prefix_len: int = ASSUMED_PREFIX_LEN) -> list[ipaddress.IPv4Network]:
    """The RFC 1918 ranges worth scanning, one per private interface.

    Delegates to :mod:`ssc.net`, which enumerates every interface and uses each
    one's real netmask. A multi-homed host — a Pi on Ethernet and Wi-Fi at once
    — therefore contributes a range per interface instead of only the default
    route's, so a scope on the other subnet is still found. ``prefix_len`` is
    now only the fallback for an interface that reports no usable netmask.
    """
    return _net.local_ipv4_networks(fallback_prefix_len=prefix_len)
