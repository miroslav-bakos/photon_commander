# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Finding and identifying a scope that only serves the native Alpaca surface.

The failure these cover is the one the owner hit: a device whose JSON-RPC port
accepts TCP and never answers came back from ``probe()`` as a bare
``reachable: true`` with a note that read like a fault, and ``discover()``'s
sweep of that port could not see it at all.

Every seam is injected — no socket is opened here. ``probe()`` and ``discover()``
each take their surface probe, their identifier and their datagram sender.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import replace
from typing import Final

import pytest

from ssc.seestar.discovery import DEFAULT_ALPACA_LISTEN_S, discover, probe
from ssc.seestar.routing import ControlSurface, SurfaceChoice, _decode_alpaca_port
from ssc.seestar.session import DeviceInfo

SUBNET: Final = ipaddress.IPv4Network("192.168.44.0/29")
SCOPE: Final = "192.168.44.2"
OTHER: Final = "192.168.44.3"

NATIVE: Final = SurfaceChoice(
    surface=ControlSurface.NATIVE_ALPACA,
    alpaca_port=32323,
    evidence="UDP 32227 advertised AlpacaPort 32323; configureddevices listed 7 device(s)",
)
NO_ALPACA: Final = SurfaceChoice(
    surface=ControlSurface.JSON_RPC,
    alpaca_port=None,
    evidence="UDP 32227 did not answer; no Alpaca server on the standard port",
)

IDENTIFIED: Final = DeviceInfo(
    host=SCOPE,
    control_port=32323,
    reachable=True,
    model="Seestar S30 Pro",
    serial="SYNTHETIC000001",
    note="identified over the device's own ASCOM Alpaca server on :32323",
)
SEESTAR_SHAPE: Final = (
    "camera",
    "focuser",
    "camera",
    "focuser",
    "filterwheel",
    "telescope",
    "switch",
)


def _surface(choice: SurfaceChoice) -> object:
    async def surface_probe(
        host: str,
        *,
        timeout: float = 3.0,  # noqa: ASYNC109 - mirrors probe_control_surface
    ) -> SurfaceChoice:
        del host, timeout
        return choice

    return surface_probe


def _identifier(info: DeviceInfo, types: tuple[str, ...] = SEESTAR_SHAPE) -> object:
    async def identify(
        host: str,
        *,
        port: int = 32323,
        timeout: float = 5.0,  # noqa: ASYNC109 - mirrors identify_alpaca
    ) -> tuple[DeviceInfo, tuple[str, ...]]:
        del timeout
        return (replace(info, host=host, control_port=port), types)

    return identify


def _answers(answers: dict[str, int]) -> object:
    async def broadcaster(
        targets: Sequence[str],
        *,
        timeout: float = 1.0,  # noqa: ASYNC109 - mirrors alpaca_discovery
        broadcast: bool = False,
    ) -> dict[str, int]:
        del timeout, broadcast
        broadcaster.targets = list(targets)  # type: ignore[attr-defined]
        return dict(answers)

    return broadcaster


async def _never_json_rpc(
    host: str,
    control_port: int,
    event_port: int,
    timeout: float,  # noqa: ASYNC109 - mirrors the JsonRpcProbe seam
) -> DeviceInfo:
    raise AssertionError("the JSON-RPC path must not be reached here")


def _json_rpc(info: DeviceInfo) -> object:
    async def json_rpc_probe(
        host: str,
        control_port: int,
        event_port: int,
        timeout: float,  # noqa: ASYNC109 - mirrors the JsonRpcProbe seam
    ) -> DeviceInfo:
        del control_port, event_port, timeout
        return replace(info, host=host)

    return json_rpc_probe


# --------------------------------------------------------------------------- #
# probe()
# --------------------------------------------------------------------------- #


async def test_a_device_on_the_alpaca_surface_comes_back_identified() -> None:
    info = await probe(
        SCOPE,
        surface_probe=_surface(NATIVE),  # type: ignore[arg-type]
        alpaca_identify=_identifier(IDENTIFIED),  # type: ignore[arg-type]
        json_rpc_probe=_never_json_rpc,
    )

    assert info.reachable is True
    assert info.model == "Seestar S30 Pro"
    assert info.serial == "SYNTHETIC000001"
    # The port that answered, so what the UI remembers is a port that works.
    assert info.control_port == 32323
    # Honest about the one thing this surface cannot tell us.
    assert info.firmware is None
    assert info.firmware_is_verified is False
    assert info.note is not None
    assert NATIVE.evidence in info.note


async def test_a_device_with_no_alpaca_server_still_takes_the_json_rpc_path() -> None:
    expected = DeviceInfo(host=SCOPE, control_port=4701, reachable=True, firmware="8.46")
    info = await probe(
        SCOPE,
        surface_probe=_surface(NO_ALPACA),  # type: ignore[arg-type]
        alpaca_identify=_identifier(IDENTIFIED),  # type: ignore[arg-type]
        json_rpc_probe=_json_rpc(expected),  # type: ignore[arg-type]
    )

    assert info.firmware == "8.46"
    assert info.control_port == 4701


async def test_an_alpaca_server_that_stops_answering_falls_through_rather_than_lying() -> None:
    unreachable = DeviceInfo(
        host=SCOPE, control_port=32323, reachable=False, note="no Alpaca server answered"
    )
    expected = DeviceInfo(host=SCOPE, control_port=4701, reachable=False, note="from json-rpc")
    info = await probe(
        SCOPE,
        surface_probe=_surface(NATIVE),  # type: ignore[arg-type]
        alpaca_identify=_identifier(unreachable),  # type: ignore[arg-type]
        json_rpc_probe=_json_rpc(expected),  # type: ignore[arg-type]
    )

    assert info.note == "from json-rpc"


# --------------------------------------------------------------------------- #
# discover()
# --------------------------------------------------------------------------- #


async def _sweep_finds_nothing(
    host: str,
    control_port: int,
    timeout: float,  # noqa: ASYNC109 - mirrors the Prober seam
) -> DeviceInfo:
    return DeviceInfo(host=host, control_port=control_port, reachable=False, note="silent")


async def test_a_scan_finds_a_device_the_control_port_sweep_cannot_see() -> None:
    broadcaster = _answers({SCOPE: 32323})
    found = await discover(
        timeout=2.0,
        subnets=[SUBNET],
        prober=_sweep_finds_nothing,
        broadcaster=broadcaster,  # type: ignore[arg-type]
        alpaca_identify=_identifier(IDENTIFIED),  # type: ignore[arg-type]
    )

    assert [device.host for device in found] == [SCOPE]
    assert found[0].control_port == 32323
    assert found[0].info.model == "Seestar S30 Pro"
    assert "Seestar S30 Pro" in found[0].label
    assert "sn SYNTHETIC000001" in found[0].label
    # No firmware was read, so the label must not claim one.
    assert "fw" not in found[0].label


async def test_the_datagram_goes_to_every_host_in_range_and_to_the_broadcasts() -> None:
    broadcaster = _answers({})
    await discover(
        timeout=2.0,
        subnets=[SUBNET],
        prober=_sweep_finds_nothing,
        broadcaster=broadcaster,  # type: ignore[arg-type]
        alpaca_identify=_identifier(IDENTIFIED),  # type: ignore[arg-type]
    )

    targets = broadcaster.targets  # type: ignore[attr-defined]
    # Unicast per host, because this firmware ignores the broadcast the ASCOM
    # discovery spec describes (measured 2026-09-10).
    assert {str(address) for address in SUBNET.hosts()} <= set(targets)
    assert str(SUBNET.broadcast_address) in targets
    assert "255.255.255.255" in targets
    # And nothing outside the range it was asked to scan.
    assert all(
        target in {"255.255.255.255", str(SUBNET.broadcast_address)}
        or ipaddress.IPv4Address(target) in SUBNET
        for target in targets
    )


async def test_an_answer_from_outside_the_scanned_range_is_dropped() -> None:
    found = await discover(
        timeout=2.0,
        subnets=[SUBNET],
        prober=_sweep_finds_nothing,
        broadcaster=_answers({"10.9.9.9": 32323}),  # type: ignore[arg-type]
        alpaca_identify=_identifier(IDENTIFIED),  # type: ignore[arg-type]
    )

    assert found == []


async def test_an_alpaca_server_with_no_mount_is_not_a_scope() -> None:
    found = await discover(
        timeout=2.0,
        subnets=[SUBNET],
        prober=_sweep_finds_nothing,
        broadcaster=_answers({OTHER: 11111}),  # type: ignore[arg-type]
        alpaca_identify=_identifier(IDENTIFIED, ("switch",)),  # type: ignore[arg-type]
    )

    assert found == []


async def test_the_two_passes_report_one_device_not_two() -> None:
    async def sweep_finds_it(
        host: str,
        control_port: int,
        timeout: float,  # noqa: ASYNC109 - mirrors the Prober seam
    ) -> DeviceInfo:
        reachable = host == SCOPE
        return DeviceInfo(
            host=host, control_port=control_port, reachable=reachable, firmware="8.46"
        )

    found = await discover(
        timeout=2.0,
        subnets=[SUBNET],
        prober=sweep_finds_it,
        broadcaster=_answers({SCOPE: 32323}),  # type: ignore[arg-type]
        alpaca_identify=_identifier(IDENTIFIED),  # type: ignore[arg-type]
    )

    assert len(found) == 1
    # The Alpaca entry wins, because that is the surface the connect path picks —
    # a picker must not hand back an address connect would not use.
    assert found[0].control_port == 32323


async def test_a_scan_of_nothing_broadcasts_nothing() -> None:
    broadcaster = _answers({SCOPE: 32323})
    assert (
        await discover(
            subnets=[],
            prober=_sweep_finds_nothing,
            broadcaster=broadcaster,  # type: ignore[arg-type]
        )
        == []
    )
    assert not hasattr(broadcaster, "targets")


async def test_the_udp_pass_does_not_extend_a_scan_past_its_deadline() -> None:
    # The listen window is clamped to the caller's overall budget, never added
    # to it: a 0.2 s scan must not sit for the full default listen.
    assert DEFAULT_ALPACA_LISTEN_S > 0.2
    seen: list[float] = []

    async def slow_broadcaster(
        targets: Sequence[str],
        *,
        timeout: float = 1.0,  # noqa: ASYNC109 - mirrors alpaca_discovery
        broadcast: bool = False,
    ) -> dict[str, int]:
        del targets, broadcast
        seen.append(timeout)
        return {}

    await discover(
        timeout=0.2,
        subnets=[SUBNET],
        prober=_sweep_finds_nothing,
        broadcaster=slow_broadcaster,  # type: ignore[arg-type]
    )

    assert seen == [0.2]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b'{"AlpacaPort": 32323}', 32323),
        (b'{"AlpacaPort": "32323"}', None),
        (b'{"AlpacaPort": true}', None),
        (b"{}", None),
        (b"not json at all", None),
        (b"\xff\xfe", None),
    ],
)
def test_only_a_real_port_counts_as_an_answer(payload: bytes, expected: int | None) -> None:
    assert _decode_alpaca_port(payload) == expected
