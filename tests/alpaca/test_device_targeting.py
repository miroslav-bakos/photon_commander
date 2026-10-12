"""``ssc.discover`` / ``ssc.probe`` / ``ssc.set_device_address``.

These three are the ones that must answer *before* ``Connected = True`` — see
``ssc.alpaca.actions.PRE_CONNECT_ACTIONS`` and CLAUDE.md invariant 11. They
delegate to ``ssc.seestar.discovery``, never to a hardcoded address, and
``ssc.set_device_address`` is the only thing that ever tells
:class:`~ssc.alpaca.backend.SeestarBackend` what to connect to.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from ssc.alpaca import actions
from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.errors import ErrorNumber
from ssc.alpaca.server import ServerConfig, create_app
from ssc.seestar.discovery import DiscoveredDevice
from ssc.seestar.emulator import SeestarEmulator
from ssc.seestar.session import DeviceInfo
from ssc.seestar.transport import CONTROL_PORT
from tests.alpaca.conftest import action, make_targetable_backend, value

# --------------------------------------------------------------------------- #
# All three answer while disconnected
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["ssc.discover", "ssc.probe", "ssc.set_device_address"])
def test_all_three_are_in_pre_connect_actions(name: str) -> None:
    assert name in actions.PRE_CONNECT_ACTIONS
    assert name in actions.SUPPORTED_ACTIONS


async def test_discover_answers_while_disconnected(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_discover(**kwargs: object) -> list[DiscoveredDevice]:
        return []

    monkeypatch.setattr(actions, "seestar_discover", fake_discover)
    body = (await action(http, "ssc.discover")).json()
    assert body["ErrorNumber"] == 0
    assert json.loads(body["Value"]) == {"devices": []}


async def test_probe_answers_while_disconnected(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_probe(host: str, *, control_port: int) -> DeviceInfo:
        return DeviceInfo(host=host, control_port=control_port, reachable=False, note="timed out")

    monkeypatch.setattr(actions, "seestar_probe", fake_probe)
    body = (await action(http, "ssc.probe", '{"host":"192.168.1.77"}')).json()
    assert body["ErrorNumber"] == 0


async def test_set_device_address_answers_while_disconnected(http: httpx.AsyncClient) -> None:
    body = (await action(http, "ssc.set_device_address", '{"host":"192.168.1.77"}')).json()
    assert body["ErrorNumber"] == 0
    assert json.loads(body["Value"]) == {"host": "192.168.1.77", "port": CONTROL_PORT}


async def test_every_other_action_still_requires_connected(http: httpx.AsyncClient) -> None:
    """The gate is surgical: only the three named actions bypass it."""
    body = (await action(http, "ssc.status")).json()
    assert body["ErrorNumber"] == ErrorNumber.NOT_CONNECTED
    body = (await action(http, "ssc.capabilities")).json()
    assert body["ErrorNumber"] == ErrorNumber.NOT_CONNECTED


# --------------------------------------------------------------------------- #
# ssc.discover
# --------------------------------------------------------------------------- #


async def test_discover_delegates_and_shapes_its_result(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_discover(**kwargs: object) -> list[DiscoveredDevice]:
        seen.update(kwargs)
        return [
            DiscoveredDevice(
                host="192.168.1.77",
                control_port=4701,
                info=DeviceInfo(
                    host="192.168.1.77",
                    control_port=4701,
                    reachable=True,
                    firmware="8.46",
                    model="Seestar S30 Pro",
                    serial="SYNTHETIC000001",
                ),
                responded_in_s=0.01,
            )
        ]

    monkeypatch.setattr(actions, "seestar_discover", fake_discover)
    body = (await action(http, "ssc.discover", '{"timeout_s": 2.5}')).json()
    assert body["ErrorNumber"] == 0
    assert seen["timeout"] == 2.5
    payload = json.loads(body["Value"])
    assert payload == {
        "devices": [
            {
                "host": "192.168.1.77",
                "port": 4701,
                "firmware": "8.46",
                "model": "Seestar S30 Pro",
                "serial": "SYNTHETIC000001",
                # What the scan observed, so a device found without a firmware
                # version can say why rather than looking broken.
                "note": None,
            }
        ]
    }


async def test_discover_passes_subnets_through_as_ipv4_networks(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_discover(**kwargs: object) -> list[DiscoveredDevice]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(actions, "seestar_discover", fake_discover)
    body = (await action(http, "ssc.discover", '{"subnets": ["192.168.1.0/24"]}')).json()
    assert body["ErrorNumber"] == 0
    assert [str(n) for n in seen["subnets"]] == ["192.168.1.0/24"]  # type: ignore[misc]


async def test_discover_keeps_the_rfc1918_fence_and_reports_it_as_invalid_value(
    http: httpx.AsyncClient,
) -> None:
    """No prober is injected: a real scan of a public range must never start —
    the RFC 1918 refusal in ssc.seestar.discovery.discover() has to fire first."""
    body = (await action(http, "ssc.discover", '{"subnets": ["8.8.8.0/24"]}')).json()
    assert body["ErrorNumber"] == ErrorNumber.INVALID_VALUE
    assert "RFC 1918" in body["ErrorMessage"]


async def test_discover_rejects_an_unparseable_subnet(http: httpx.AsyncClient) -> None:
    body = (await action(http, "ssc.discover", '{"subnets": ["not-a-network"]}')).json()
    assert body["ErrorNumber"] == ErrorNumber.INVALID_VALUE


# --------------------------------------------------------------------------- #
# ssc.probe
# --------------------------------------------------------------------------- #


async def test_probe_reports_a_full_success(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_probe(host: str, *, control_port: int) -> DeviceInfo:
        seen["host"], seen["control_port"] = host, control_port
        return DeviceInfo(
            host=host,
            control_port=control_port,
            reachable=True,
            firmware="8.46",
            model="Seestar S30 Pro",
            serial="SYNTHETIC000001",
        )

    monkeypatch.setattr(actions, "seestar_probe", fake_probe)
    body = (await action(http, "ssc.probe", '{"host": "192.168.1.77"}')).json()
    assert body["ErrorNumber"] == 0
    assert seen == {"host": "192.168.1.77", "control_port": CONTROL_PORT}
    payload = json.loads(body["Value"])
    assert payload == {
        "reachable": True,
        "handshake": True,
        # The port that answered, so the caller remembers an address that works
        # even when the device is identified on a different port than was asked
        # about (the native Alpaca surface).
        "port": CONTROL_PORT,
        "firmware": "8.46",
        "model": "Seestar S30 Pro",
        "serial": "SYNTHETIC000001",
        "verified": True,
        "reason": None,
    }


async def test_probe_reports_an_unreachable_host(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_probe(host: str, *, control_port: int) -> DeviceInfo:
        return DeviceInfo(
            host=host,
            control_port=control_port,
            reachable=False,
            note="ConnectionRefusedError: ...",
        )

    monkeypatch.setattr(actions, "seestar_probe", fake_probe)
    payload = json.loads(
        (await action(http, "ssc.probe", '{"host": "192.168.1.99"}')).json()["Value"]
    )
    assert payload["reachable"] is False
    assert payload["handshake"] is False
    assert payload["verified"] is False
    assert "ConnectionRefusedError" in payload["reason"]


async def test_probe_distinguishes_tcp_reachable_from_handshake_failed(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that accepted the TCP connection but never answered protocol."""

    async def fake_probe(host: str, *, control_port: int) -> DeviceInfo:
        return DeviceInfo(
            host=host,
            control_port=control_port,
            reachable=False,
            note="connected but did not answer test_connection: boom",
        )

    monkeypatch.setattr(actions, "seestar_probe", fake_probe)
    payload = json.loads(
        (await action(http, "ssc.probe", '{"host": "192.168.1.99"}')).json()["Value"]
    )
    assert payload["reachable"] is True
    assert payload["handshake"] is False


async def test_probe_reports_an_unverified_firmware(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_probe(host: str, *, control_port: int) -> DeviceInfo:
        return DeviceInfo(host=host, control_port=control_port, reachable=True, firmware="9.99")

    monkeypatch.setattr(actions, "seestar_probe", fake_probe)
    payload = json.loads(
        (await action(http, "ssc.probe", '{"host": "192.168.1.99"}')).json()["Value"]
    )
    assert payload["reachable"] is True
    assert payload["verified"] is False


async def test_probe_uses_the_given_port(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_probe(host: str, *, control_port: int) -> DeviceInfo:
        seen["control_port"] = control_port
        return DeviceInfo(host=host, control_port=control_port, reachable=False)

    monkeypatch.setattr(actions, "seestar_probe", fake_probe)
    await action(http, "ssc.probe", '{"host": "192.168.1.99", "port": 9999}')
    assert seen["control_port"] == 9999


# --------------------------------------------------------------------------- #
# ssc.set_device_address
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("host", ["192.168.1.77", "10.0.0.5", "172.16.4.4", "127.0.0.1"])
async def test_set_device_address_accepts_private_and_loopback_hosts(
    http: httpx.AsyncClient, host: str
) -> None:
    body = (await action(http, "ssc.set_device_address", json.dumps({"host": host}))).json()
    assert body["ErrorNumber"] == 0
    assert json.loads(body["Value"]) == {"host": host, "port": CONTROL_PORT}


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", "203.0.113.9"])
async def test_set_device_address_rejects_a_public_host(http: httpx.AsyncClient, host: str) -> None:
    body = (await action(http, "ssc.set_device_address", json.dumps({"host": host}))).json()
    assert body["ErrorNumber"] == ErrorNumber.INVALID_VALUE
    assert "publicly routable" in body["ErrorMessage"]


async def test_set_device_address_rejects_a_hostname(http: httpx.AsyncClient) -> None:
    """SSC does not resolve a name to decide whether a target is safe."""
    body = (await action(http, "ssc.set_device_address", '{"host": "seestar.local"}')).json()
    assert body["ErrorNumber"] == ErrorNumber.INVALID_VALUE


async def test_set_device_address_honours_an_explicit_port(http: httpx.AsyncClient) -> None:
    body = (
        await action(http, "ssc.set_device_address", '{"host": "192.168.1.77", "port": 4711}')
    ).json()
    assert json.loads(body["Value"]) == {"host": "192.168.1.77", "port": 4711}


async def test_set_device_address_rejects_unknown_keys(http: httpx.AsyncClient) -> None:
    body = (
        await action(http, "ssc.set_device_address", '{"host": "192.168.1.77", "flavor": "x"}')
    ).json()
    assert body["ErrorNumber"] == ErrorNumber.INVALID_VALUE
    assert "unknown parameter" in body["ErrorMessage"]


# --------------------------------------------------------------------------- #
# The backend actually connects to the configured target
# --------------------------------------------------------------------------- #


@pytest.fixture
async def targetable(
    emulator: SeestarEmulator,
) -> AsyncIterator[tuple[SeestarBackend, list[tuple[str, int]], httpx.AsyncClient]]:
    backend, calls = make_targetable_backend(emulator)
    app = create_app(backend, config=ServerConfig(server_name="SSC Alpaca test"))
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
        try:
            yield backend, calls, http
        finally:
            await backend.aclose()


async def test_connecting_without_an_address_is_notconnected(
    targetable: tuple[SeestarBackend, list[tuple[str, int]], httpx.AsyncClient],
) -> None:
    _, _, http = targetable
    body = (await http.put("/api/v1/telescope/0/connected", data={"Connected": "True"})).json()
    assert body["ErrorNumber"] == ErrorNumber.NOT_CONNECTED
    assert "ssc.set_device_address" in body["ErrorMessage"]


async def test_set_device_address_is_what_the_next_connect_uses(
    targetable: tuple[SeestarBackend, list[tuple[str, int]], httpx.AsyncClient],
) -> None:
    backend, calls, http = targetable
    body = (
        await action(http, "ssc.set_device_address", '{"host": "192.168.50.5", "port": 4701}')
    ).json()
    assert body["ErrorNumber"] == 0
    assert backend.target_address == ("192.168.50.5", 4701)

    connected = await http.put("/api/v1/telescope/0/connected", data={"Connected": "True"})
    assert connected.json()["ErrorNumber"] == 0
    assert calls == [("192.168.50.5", 4701)]
    assert await value(http, "telescope", "connected") is True


async def test_reconnecting_after_a_new_address_rebuilds_the_session(
    targetable: tuple[SeestarBackend, list[tuple[str, int]], httpx.AsyncClient],
) -> None:
    _, calls, http = targetable
    await action(http, "ssc.set_device_address", '{"host": "192.168.50.5"}')
    await http.put("/api/v1/telescope/0/connected", data={"Connected": "True"})
    await http.put("/api/v1/telescope/0/connected", data={"Connected": "False"})

    await action(http, "ssc.set_device_address", '{"host": "192.168.50.9"}')
    await http.put("/api/v1/telescope/0/connected", data={"Connected": "True"})
    assert calls == [("192.168.50.5", CONTROL_PORT), ("192.168.50.9", CONTROL_PORT)]


async def test_retarget_after_a_lost_session_discards_stale_holders(
    targetable: tuple[SeestarBackend, list[tuple[str, int]], httpx.AsyncClient],
) -> None:
    backend, calls, http = targetable
    await action(http, "ssc.set_device_address", '{"host":"192.168.50.5"}')
    await http.put("/api/v1/telescope/0/connected", data={"Connected": "True"})
    assert backend._session is not None
    await backend._session.aclose()
    assert backend.is_connected is False

    await action(http, "ssc.set_device_address", '{"host":"192.168.50.9"}')
    connected = await http.put("/api/v1/telescope/0/connected", data={"Connected": "True"})

    assert connected.json()["ErrorNumber"] == 0
    assert calls == [("192.168.50.5", CONTROL_PORT), ("192.168.50.9", CONTROL_PORT)]


async def test_changing_the_target_while_connected_does_not_disturb_the_live_session(
    targetable: tuple[SeestarBackend, list[tuple[str, int]], httpx.AsyncClient],
) -> None:
    backend, calls, http = targetable
    await action(http, "ssc.set_device_address", '{"host": "192.168.50.5"}')
    await http.put("/api/v1/telescope/0/connected", data={"Connected": "True"})
    assert len(calls) == 1

    # The target changes mid-session; the current link is untouched.
    await action(http, "ssc.set_device_address", '{"host": "192.168.50.9"}')
    assert backend.is_connected is True
    assert len(calls) == 1
    assert backend.target_address == ("192.168.50.9", CONTROL_PORT)
