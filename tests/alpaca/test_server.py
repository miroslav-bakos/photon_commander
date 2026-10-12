"""The HTTP surface: management, routing, transaction ids, status codes."""

from __future__ import annotations

import httpx
import pytest

from ssc.alpaca.errors import ErrorNumber
from ssc.alpaca.server import (
    BindRefusedError,
    ServerConfig,
    build_devices,
    resolve_bind_host,
)
from tests.alpaca.conftest import api_get, api_put, connect

DEVICES = ("telescope", "camera", "focuser", "filterwheel", "switch")

#: ``(DeviceType, DeviceNumber)`` in the order ``build_devices`` registers
#: them — the seven interfaces a Seestar presents (``docs/M0-FINDINGS.md`` 9.31).
CONFIGURED_DEVICES = (
    ("Telescope", 0),
    ("Camera", 0),
    ("Camera", 1),
    ("Focuser", 0),
    ("Focuser", 1),
    ("Filterwheel", 0),
    ("Switch", 0),
)


async def test_apiversions_is_served_at_both_documented_paths(http: httpx.AsyncClient) -> None:
    for path in ("/management/apiversions", "/management/v1/apiversions"):
        response = await http.get(path)
        assert response.status_code == 200, path
        assert response.json()["Value"] == [1]


async def test_description_names_the_server(http: httpx.AsyncClient) -> None:
    body = (await http.get("/management/v1/description")).json()
    assert body["ErrorNumber"] == 0
    assert body["Value"]["ServerName"] == "SSC Alpaca test"
    assert body["Value"]["Manufacturer"]


async def test_configureddevices_matches_the_devices_actually_served(
    http: httpx.AsyncClient,
) -> None:
    listed = (await http.get("/management/v1/configureddevices")).json()["Value"]
    assert [(entry["DeviceType"], entry["DeviceNumber"]) for entry in listed] == list(
        CONFIGURED_DEVICES
    )
    unique_ids = {entry["UniqueID"] for entry in listed}
    assert len(unique_ids) == len(listed), "each device needs its own UniqueID"

    for entry in listed:
        response = await api_get(
            http,
            entry["DeviceType"].lower(),
            "interfaceversion",
            device_number=entry["DeviceNumber"],
        )
        assert response.status_code == 200, entry


async def test_unique_ids_are_stable_across_server_instances(backend: object) -> None:
    from ssc.alpaca.server import DeviceRegistry

    first = DeviceRegistry(build_devices(backend)).configured()  # type: ignore[arg-type]
    second = DeviceRegistry(build_devices(backend)).configured()  # type: ignore[arg-type]
    assert [entry["UniqueID"] for entry in first] == [entry["UniqueID"] for entry in second]


async def test_client_transaction_id_is_echoed_and_server_ids_increase(
    http: httpx.AsyncClient,
) -> None:
    seen: list[int] = []
    for client_tid in (7, 8, 9):
        response = await api_get(
            http, "telescope", "interfaceversion", ClientTransactionID=client_tid
        )
        body = response.json()
        assert body["ClientTransactionID"] == client_tid
        seen.append(body["ServerTransactionID"])
    assert seen == sorted(seen) and len(set(seen)) == 3


async def test_a_request_without_a_client_transaction_id_is_answered_with_zero(
    http: httpx.AsyncClient,
) -> None:
    body = (await api_get(http, "telescope", "name")).json()
    assert body["ClientTransactionID"] == 0
    assert body["ErrorNumber"] == 0


async def test_put_bodies_are_form_encoded_not_json(http: httpx.AsyncClient) -> None:
    form = await http.put(
        "/api/v1/telescope/0/connected",
        data={"Connected": "True", "ClientTransactionID": "5"},
    )
    assert form.status_code == 200
    assert form.json() == {
        "ClientTransactionID": 5,
        "ServerTransactionID": form.json()["ServerTransactionID"],
        "ErrorNumber": 0,
        "ErrorMessage": "",
    }

    as_json = await http.put("/api/v1/telescope/0/connected", json={"Connected": True})
    assert as_json.status_code == 400, "a JSON body carries no Alpaca parameters"


async def test_put_parameter_names_are_case_insensitive(http: httpx.AsyncClient) -> None:
    response = await http.put("/api/v1/telescope/0/connected", data={"connected": "true"})
    assert response.status_code == 200
    assert response.json()["ErrorNumber"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/dome/0/connected",
        "/api/v1/telescope/3/connected",
        "/api/v1/telescope/x/connected",
        "/api/v1/telescope/-1/connected",
        "/api/v1/telescope/0/notamember",
    ],
)
async def test_a_malformed_request_is_http_400(http: httpx.AsyncClient, path: str) -> None:
    response = await http.get(path)
    assert response.status_code == 400
    assert response.text.strip()


async def test_a_read_only_member_written_and_a_write_only_member_read_are_400(
    http: httpx.AsyncClient,
) -> None:
    assert (await api_put(http, "telescope", "slewing", Slewing=True)).status_code == 400
    assert (await api_get(http, "telescope", "abortslew")).status_code == 400


@pytest.mark.parametrize(
    ("device", "member"),
    [
        ("telescope", "atpark"),
        ("camera", "gain"),
        ("focuser", "position"),
        ("filterwheel", "names"),
        ("switch", "maxswitch"),
    ],
)
async def test_members_answer_notconnected_before_connect(
    http: httpx.AsyncClient, device: str, member: str
) -> None:
    response = await api_get(http, device, member)
    assert response.status_code == 200
    body = response.json()
    assert body["ErrorNumber"] == ErrorNumber.NOT_CONNECTED
    assert body["ErrorMessage"]


@pytest.mark.parametrize("device", DEVICES)
async def test_the_common_members_answer_while_disconnected(
    http: httpx.AsyncClient, device: str
) -> None:
    for member in (
        "connected",
        "description",
        "driverinfo",
        "driverversion",
        "interfaceversion",
        "name",
        "supportedactions",
    ):
        body = (await api_get(http, device, member)).json()
        assert body["ErrorNumber"] == 0, (device, member, body)
    assert (await api_get(http, device, "connected")).json()["Value"] is False


async def test_connect_and_disconnect_share_one_session(http: httpx.AsyncClient) -> None:
    await connect(http, "telescope", "camera")
    assert (await api_get(http, "telescope", "connected")).json()["Value"] is True
    assert (await api_get(http, "camera", "connected")).json()["Value"] is True

    await api_put(http, "telescope", "connected", Connected=False)
    assert (await api_get(http, "telescope", "connected")).json()["Value"] is False
    # The camera still holds the session open.
    assert (await api_get(http, "camera", "connected")).json()["Value"] is True

    await api_put(http, "camera", "connected", Connected=False)
    assert (await api_get(http, "camera", "connected")).json()["Value"] is False


async def test_commandblind_and_friends_are_notimplemented(linked: httpx.AsyncClient) -> None:
    for member in ("commandblind", "commandbool", "commandstring"):
        body = (await api_put(linked, "telescope", member, Command="x", Raw=False)).json()
        assert body["ErrorNumber"] == ErrorNumber.NOT_IMPLEMENTED, member
        assert body["ErrorMessage"]


# --------------------------------------------------------------------------- #
# Bind policy (DESIGN.md §9)
# --------------------------------------------------------------------------- #


def test_loopback_needs_no_opt_in() -> None:
    assert resolve_bind_host(None) == "127.0.0.1"
    assert resolve_bind_host("127.0.0.1") == "127.0.0.1"
    assert resolve_bind_host("::1") == "::1"


def test_a_lan_address_requires_the_opt_in() -> None:
    with pytest.raises(BindRefusedError, match="loopback-only"):
        resolve_bind_host("10.0.0.5")
    assert resolve_bind_host("10.0.0.5", allow_lan=True) == "10.0.0.5"


def test_a_public_address_is_refused_even_with_the_opt_in() -> None:
    with pytest.raises(BindRefusedError, match="publicly routable"):
        resolve_bind_host("8.8.8.8", allow_lan=True)


def test_a_hostname_is_refused_rather_than_resolved() -> None:
    with pytest.raises(BindRefusedError, match="not an IP address"):
        resolve_bind_host("telescope.local", allow_lan=True)


def test_the_wildcard_binds_only_behind_the_opt_in() -> None:
    with pytest.raises(BindRefusedError):
        resolve_bind_host("0.0.0.0")
    assert resolve_bind_host("0.0.0.0", allow_lan=True) == "0.0.0.0"


def test_the_default_config_is_loopback() -> None:
    assert ServerConfig().bind_host() == "127.0.0.1"
