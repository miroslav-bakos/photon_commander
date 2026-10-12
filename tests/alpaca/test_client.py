"""The client ssc-core uses, driven against the real driver over ASGI."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from ssc.alpaca import actions
from ssc.client import (
    AlpacaClient,
    AlpacaErrorNumber,
    AlpacaProtocolError,
    AlpacaRemoteError,
    AlpacaTransportError,
    DeviceAddress,
    DiscoveredDevice,
    ProbeResult,
)
from ssc.client.alpaca import _canonical_params
from ssc.seestar.discovery import DiscoveredDevice as SeestarDiscoveredDevice
from ssc.seestar.emulator import SeestarEmulator
from ssc.seestar.session import DeviceInfo
from tests.alpaca.conftest import override_mount


@pytest.fixture
async def client(app: object) -> AsyncIterator[AlpacaClient]:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
        yield AlpacaClient(http=http)


async def test_management_endpoints_round_trip(client: AlpacaClient) -> None:
    assert await client.api_versions() == [1]
    assert (await client.description())["Manufacturer"]
    devices = await client.configured_devices()
    assert {entry["DeviceType"] for entry in devices} == {
        "Telescope",
        "Camera",
        "Focuser",
        "Filterwheel",
        "Switch",
    }


async def test_connect_read_and_disconnect(client: AlpacaClient) -> None:
    telescope = client.device("telescope")
    assert await telescope.is_connected() is False
    await telescope.connect()
    assert await telescope.is_connected() is True

    assert await telescope.get("atpark") is True
    assert await telescope.get("alignmentmode") == 1

    await telescope.disconnect()
    assert await telescope.is_connected() is False


async def test_a_device_error_carries_its_number(client: AlpacaClient) -> None:
    telescope = client.device("telescope")
    await telescope.connect()
    with pytest.raises(AlpacaRemoteError) as caught:
        await telescope.get("rightascension")
    assert caught.value.is_not_implemented
    assert caught.value.number == AlpacaErrorNumber.NOT_IMPLEMENTED
    assert caught.value.message


async def test_notconnected_is_distinguishable_from_notimplemented(
    client: AlpacaClient,
) -> None:
    with pytest.raises(AlpacaRemoteError) as caught:
        await client.device("focuser").get("position")
    assert caught.value.is_not_connected


async def test_actions_are_listed_and_decoded(client: AlpacaClient) -> None:
    telescope = client.device("telescope")
    supported = await telescope.supported_actions()
    assert "ssc.capabilities" in supported
    assert all(name.startswith("ssc.") for name in supported)

    await telescope.connect()
    payload = await telescope.action("ssc.capabilities")
    assert isinstance(payload, dict)
    assert payload["command_vocabulary_complete"] is False


async def test_an_action_with_parameters_reaches_the_driver(
    client: AlpacaClient, emulator: SeestarEmulator
) -> None:
    override_mount(emulator, close=False)  # the user raised the arm from the app
    telescope = client.device("telescope")
    await telescope.connect()
    assert await telescope.action("ssc.start_view", {"mode": "star"}) is not None
    result = await telescope.action(
        "ssc.start_auto_goto", {"ra_hours": 5.5, "dec_degrees": -5.4, "target_name": "M42"}
    )
    assert result is not None
    assert await telescope.get("slewing") is True


async def test_booleans_are_encoded_the_way_ascom_spells_them(client: AlpacaClient) -> None:
    telescope = client.device("telescope")
    await telescope.put("connected", Connected=True)
    assert await telescope.get("connected") is True


async def test_a_malformed_request_is_reported_as_a_transport_error(
    client: AlpacaClient,
) -> None:
    with pytest.raises(AlpacaTransportError, match="HTTP 400"):
        await client.get_property("telescope", 0, "notamember")


async def test_a_mis_echoed_transaction_id_is_refused() -> None:
    """The echo is the only thing tying a response to its request."""

    async def lying_driver(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Value": 1,
                "ClientTransactionID": 999,
                "ServerTransactionID": 1,
                "ErrorNumber": 0,
                "ErrorMessage": "",
            },
        )

    transport = httpx.MockTransport(lying_driver)
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
        client = AlpacaClient(http=http)
        with pytest.raises(AlpacaProtocolError, match="echoed ClientTransactionID"):
            await client.get_property("telescope", 0, "atpark")


async def test_an_error_number_without_a_message_is_a_protocol_violation() -> None:
    async def silent_driver(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ClientTransactionID": 1,
                "ServerTransactionID": 1,
                "ErrorNumber": 0x400,
                "ErrorMessage": "",
            },
        )

    transport = httpx.MockTransport(silent_driver)
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
        client = AlpacaClient(http=http)
        with pytest.raises(AlpacaProtocolError, match="empty ErrorMessage"):
            await client.get_property("telescope", 0, "atpark")


async def test_action_accepts_a_bare_string_reply_not_just_json() -> None:
    """The device's own native camera vendor actions answer a bare string —
    measured live, ``GetFanSpeed`` -> ``'0'``, ``GetSN`` -> a UUID
    (``docs/M0-FINDINGS.md``). Neither is valid JSON on its own; both must
    come back exactly as sent, not be rejected as malformed."""

    async def bare_string_driver(request: httpx.Request) -> httpx.Response:
        content = httpx.QueryParams(request.content.decode())
        value = "3f2504e0-4f89-41d3-9a0c-0305e82c3301" if "GetSN" in content["Action"] else "0"
        return httpx.Response(
            200,
            json={
                "Value": value,
                "ClientTransactionID": int(content["ClientTransactionID"]),
                "ServerTransactionID": 1,
                "ErrorNumber": 0,
                "ErrorMessage": "",
            },
        )

    transport = httpx.MockTransport(bare_string_driver)
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
        client = AlpacaClient(http=http)
        assert await client.action("camera", 0, "GetSN") == "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        assert await client.action("camera", 0, "GetFanSpeed") == 0


async def test_client_transaction_ids_increase(client: AlpacaClient) -> None:
    for _ in range(3):
        await client.api_versions()
    # A fourth call must still be accepted and echoed; the driver checks the echo.
    assert await client.api_versions() == [1]


# --------------------------------------------------------------------------- #
# ssc.discover / ssc.probe / ssc.set_device_address, from the client side
# --------------------------------------------------------------------------- #


async def test_discover_works_before_connecting_and_returns_typed_devices(
    client: AlpacaClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_discover(**kwargs: object) -> list[SeestarDiscoveredDevice]:
        return [
            SeestarDiscoveredDevice(
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
    devices = await client.discover(timeout_s=1.5)
    assert devices == [
        DiscoveredDevice(
            host="192.168.1.77",
            port=4701,
            firmware="8.46",
            model="Seestar S30 Pro",
            serial="SYNTHETIC000001",
        )
    ]


async def test_probe_works_before_connecting_and_returns_a_typed_result(
    client: AlpacaClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_probe(host: str, *, control_port: int) -> DeviceInfo:
        seen["host"], seen["control_port"] = host, control_port
        return DeviceInfo(
            host=host, control_port=control_port, reachable=True, firmware="8.46", serial="SYNTHETIC000001"
        )

    monkeypatch.setattr(actions, "seestar_probe", fake_probe)
    result = await client.probe("192.168.1.77")
    assert seen == {"host": "192.168.1.77", "control_port": 4701}
    assert result == ProbeResult(
        reachable=True,
        handshake=True,
        firmware="8.46",
        model=None,
        serial="SYNTHETIC000001",
        verified=True,
        reason=None,
        port=4701,
    )


async def test_probe_reports_a_genuine_failure_not_a_bare_boolean(
    client: AlpacaClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_probe(host: str, *, control_port: int) -> DeviceInfo:
        return DeviceInfo(
            host=host,
            control_port=control_port,
            reachable=False,
            note="ConnectionRefusedError: ...",
        )

    monkeypatch.setattr(actions, "seestar_probe", fake_probe)
    result = await client.probe("192.168.1.200")
    assert result.reachable is False
    assert result.handshake is False
    assert result.reason and "ConnectionRefusedError" in result.reason


async def test_set_device_address_works_before_connecting(client: AlpacaClient) -> None:
    address = await client.set_device_address("192.168.1.77")
    assert address == DeviceAddress(host="192.168.1.77", port=4701)


async def test_set_device_address_honours_an_explicit_port(client: AlpacaClient) -> None:
    address = await client.set_device_address("192.168.1.77", port=4711)
    assert address == DeviceAddress(host="192.168.1.77", port=4711)


async def test_set_device_address_rejects_a_public_host(client: AlpacaClient) -> None:
    with pytest.raises(AlpacaRemoteError) as caught:
        await client.set_device_address("8.8.8.8")
    assert caught.value.number == AlpacaErrorNumber.INVALID_VALUE


# --------------------------------------------------------------------------- #
# Live-state reads a Live tab would poll
# --------------------------------------------------------------------------- #


async def test_status_device_state_and_capabilities_are_decoded_objects(
    client: AlpacaClient,
) -> None:
    telescope = client.device("telescope")
    with pytest.raises(AlpacaRemoteError) as caught:
        await telescope.status()
    assert caught.value.is_not_connected

    await telescope.connect()
    status = await telescope.status()
    assert status["connected"] is True

    state = await telescope.device_state()
    assert state["camera"]["chip_size"] == {"height_px": 3840, "width_px": 2160}

    capabilities = await telescope.capabilities()
    assert isinstance(capabilities["operations"], list)


def test_put_parameters_use_ascom_canonical_capitalisation() -> None:
    """ASCOM PUT parameter names are case-sensitive, and the S30 Pro's native
    Alpaca server enforces it: a ``gain=300`` body is refused with
    ``HTTP 400 - Bad Form Key Capitalization(s): Gain, received: gain``,
    observed live on 2026-09-10 while trying to set camera gain.

    Callers spell these correctly, but SSC's own Alpaca server deliberately
    lower-cases everything it receives (``ssc.alpaca.wire.RequestParams``, so
    it accepts what lenient clients send), and anything crossing that seam on
    its way out to a device arrived already flattened.
    """
    assert dict(_canonical_params({"gain": 300})) == {"Gain": 300}
    assert dict(_canonical_params({"GAIN": 300})) == {"Gain": 300}
    assert dict(_canonical_params({"Gain": 300})) == {"Gain": 300}
    assert dict(_canonical_params({"connected": True})) == {"Connected": True}
    assert dict(_canonical_params({"rightascension": 1.5, "declination": -2.0})) == {
        "RightAscension": 1.5,
        "Declination": -2.0,
    }


def test_an_unknown_put_parameter_is_passed_through_untouched() -> None:
    """Never guess a capitalisation SSC does not know: sending what the caller
    asked for and letting the device refuse it is honest; inventing a spelling
    would produce a request nobody wrote."""
    assert dict(_canonical_params({"vendor_thing": 1})) == {"vendor_thing": 1}
