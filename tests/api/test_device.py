"""Discovery, probing and the remembered address (CLAUDE.md invariant 11)."""

from __future__ import annotations

import json
from typing import Any

from ssc.main import create_app
from ssc.settings.config import AppConfig
from ssc.state.models import LinkStatus
from tests.api.conftest import (
    CLIENT_ADDRESS,
    FakeGateway,
    TestClient,
    descriptor,
    pairing_token,
    services_of,
)


def _drain(websocket: Any, count: int) -> list[dict[str, Any]]:
    return [json.loads(websocket.receive_text()) for _ in range(count)]


def test_probe_reports_an_unreachable_host_honestly(paired: TestClient) -> None:
    response = paired.post("/api/device/probe", json={"host": "10.0.0.9"})
    assert response.status_code == 200
    body = response.json()
    assert body["device"]["reachable"] is False
    assert body["device"]["note"]
    assert body["remembered"] is False
    assert paired.get("/api/device/addresses").json()["remembered"]["host"] is None


def test_offline_backoff_remains_bounded_after_thousands_of_attempts(paired: TestClient) -> None:
    device = services_of(paired).device
    device._reconnect_max_delay_s = 60
    for attempt in (1, 803, 1025, 100_000):
        device._reconnect_attempt = attempt
        delay = device._reconnect_delay()
        assert 0 < delay <= 60
        if attempt > 1:
            assert delay >= 30


def test_saved_healthy_wifi_precedes_unreachable_ethernet_after_restart(paired: TestClient) -> None:
    from ssc.settings.models import DeviceSettings
    device = services_of(paired).device
    device._settings._device = DeviceSettings(host="10.0.0.7", control_port=32323,
        ethernet_host="10.42.0.2", ethernet_control_port=32323,
        last_healthy_host="10.0.0.7", last_healthy_control_port=32323)
    # Recreate the production service using the persisted settings without
    # starting the supervisor or sending any physical commands.
    from ssc.api.services import DeviceService
    resumed = DeviceService(device._gateway, device._state, device._settings, device._tasks, device._astro)
    try:
        assert [c[0] for c in resumed._connection_candidates()] == ["10.0.0.7", "10.42.0.2"]
    finally:
        resumed.close()


def test_a_reachable_probe_becomes_the_remembered_address(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    response = paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    assert response.status_code == 200
    assert response.json()["device"]["firmware"] == "8.46"

    book = paired.get("/api/device/addresses").json()
    assert book["remembered"]["host"] == "10.0.0.7"
    assert book["remembered"]["hardware_profile_id"] == "zwo-s30pro"
    assert book["remembered"]["hardware_profile_source"] == "detected"
    assert [entry["host"] for entry in book["known"]] == ["10.0.0.7"]
    assert book["known"][0]["hardware_profile_id"] == "zwo-s30pro"
    assert paired.get("/api/state").json()["snapshot"]["settings"]["remembered_host"] == "10.0.0.7"


def test_probe_uses_the_device_layers_default_port_when_none_is_given(
    paired: TestClient, gateway: FakeGateway
) -> None:
    paired.post("/api/device/probe", json={"host": "10.0.0.8"})
    assert gateway.probe_calls == [("10.0.0.8", FakeGateway.default_control_port)]


def test_ethernet_address_is_stored_without_replacing_wifi_fallback(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})

    response = paired.put(
        "/api/device/ethernet-address",
        json={"host": "10.42.0.2", "control_port": 32323},
    )

    assert response.status_code == 200
    assert response.json()["host"] == "10.0.0.7"
    assert response.json()["ethernet_host"] == "10.42.0.2"
    summary = paired.get("/api/state").json()["snapshot"]["settings"]
    assert summary["remembered_host"] == "10.0.0.7"
    assert summary["ethernet_host"] == "10.42.0.2"


def test_clearing_ethernet_address_preserves_wifi_fallback(paired: TestClient) -> None:
    paired.put(
        "/api/device/ethernet-address",
        json={"host": "10.42.0.2", "control_port": 32323},
    )
    response = paired.put("/api/device/ethernet-address", json={"host": None})
    assert response.status_code == 200
    assert response.json()["ethernet_host"] is None
    assert response.json()["ethernet_control_port"] is None


def test_wifi_fallback_can_be_assigned_explicitly_without_changing_ethernet(
    paired: TestClient,
) -> None:
    paired.put(
        "/api/device/ethernet-address",
        json={"host": "10.42.0.2", "control_port": 32323},
    )
    response = paired.put(
        "/api/device/wifi-address",
        json={"host": "192.168.1.77", "control_port": 32323},
    )
    assert response.status_code == 200
    assert response.json()["host"] == "192.168.1.77"
    assert response.json()["ethernet_host"] == "10.42.0.2"


def test_discovery_does_not_replace_explicit_network_roles(
    paired: TestClient, gateway: FakeGateway
) -> None:
    paired.put(
        "/api/device/ethernet-address",
        json={"host": "192.168.1.47", "control_port": 32323},
    )
    paired.put(
        "/api/device/wifi-address",
        json={"host": "192.168.1.77", "control_port": 32323},
    )
    gateway.discovered = [descriptor("192.168.1.76")]

    assert paired.post("/api/device/discover", json={}).status_code == 202
    assert _settle(paired, lambda: not services_of(paired).state.snapshot.discovery.running)

    remembered = paired.get("/api/device/addresses").json()["remembered"]
    assert remembered["ethernet_host"] == "192.168.1.47"
    assert remembered["host"] == "192.168.1.77"


def test_unknown_model_requires_manual_profile_confirmation(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7", model="Future Seestar")
    assert paired.post("/api/device/probe", json={"host": "10.0.0.7"}).status_code == 200
    book = paired.get("/api/device/addresses").json()
    assert book["remembered"]["hardware_profile_id"] is None

    selected = paired.put(
        "/api/device/hardware-profile", json={"profile_id": "zwo-s50"}
    )
    assert selected.status_code == 200
    assert selected.json()["hardware_profile_id"] == "zwo-s50"
    assert selected.json()["hardware_profile_source"] == "manual"
    assert paired.get("/api/device/addresses").json()["known"][0][
        "hardware_profile_id"
    ] == "zwo-s50"


def test_hardware_profile_rejects_unknown_id(paired: TestClient) -> None:
    response = paired.put("/api/device/hardware-profile", json={"profile_id": "zwo-s99"})
    assert response.status_code == 422


def test_detected_model_replaces_a_conflicting_manual_profile(
    paired: TestClient, gateway: FakeGateway
) -> None:
    paired.put("/api/device/hardware-profile", json={"profile_id": "zwo-s50"})
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7", model="Seestar S30 Pro")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    remembered = paired.get("/api/device/addresses").json()["remembered"]
    assert remembered["hardware_profile_id"] == "zwo-s30pro"
    assert remembered["hardware_profile_source"] == "detected"


def test_probe_refuses_a_public_host_the_way_discover_refuses_a_public_subnet(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """discover() is fenced to RFC 1918; probe/connect must be too, or a typed
    host closes the exact gap the fence exists to hold (code-reviewer notes)."""
    response = paired.post("/api/device/probe", json={"host": "8.8.8.8"})
    assert response.status_code == 422
    assert gateway.probe_calls == []


def test_probe_refuses_a_hostname(paired: TestClient, gateway: FakeGateway) -> None:
    response = paired.post("/api/device/probe", json={"host": "seestar.local"})
    assert response.status_code == 422
    assert gateway.probe_calls == []


def test_connect_refuses_a_public_host(paired: TestClient, gateway: FakeGateway) -> None:
    response = paired.post("/api/device/connect", json={"host": "203.0.113.1"})
    assert response.status_code == 422
    assert gateway.connected is None


def test_probe_accepts_loopback_for_local_development(
    paired: TestClient, gateway: FakeGateway
) -> None:
    response = paired.post("/api/device/probe", json={"host": "127.0.0.1"})
    assert response.status_code == 200
    assert gateway.probe_calls == [("127.0.0.1", FakeGateway.default_control_port)]


def test_discovery_streams_progress_and_results_over_the_ws(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.discovered = [descriptor("192.168.1.77")]
    gateway.progress_hosts = 40

    with paired.websocket_connect("/ws") as websocket:
        _drain(websocket, 1)  # the initial snapshot
        response = paired.post("/api/device/discover", json={})
        assert response.status_code == 202
        scan_id = response.json()["scan_id"]

        frames: list[dict[str, Any]] = []
        while True:
            frame = json.loads(websocket.receive_text())
            frames.append(frame)
            event = frame["event"]
            if event["type"] == "discovery.changed" and not event["discovery"]["running"]:
                break

    started, *_middle, finished = [
        frame["event"]["discovery"]
        for frame in frames
        if frame["event"]["type"] == "discovery.changed"
    ]
    assert started["running"] is True
    assert started["scan_id"] == scan_id
    assert finished["running"] is False
    assert finished["error"] is None
    assert [device["host"] for device in finished["found"]] == ["192.168.1.77"]
    assert any(state["completed"] for state in _middle), (
        "progress must be reported, not just the end"
    )


def test_a_refused_scan_surfaces_the_reason_rather_than_an_empty_result(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.discover_error = "refusing to scan 8.8.8.0/24: not an RFC 1918 range"
    paired.post("/api/device/discover", json={})

    services = services_of(paired)
    for _ in range(50):
        if not services.state.snapshot.discovery.running:
            break
        paired.get("/api/health")
    discovery = services.state.snapshot.discovery
    assert discovery.running is False
    assert discovery.error is not None
    assert "RFC 1918" in discovery.error


def test_connecting_without_an_address_is_a_conflict_not_a_default(paired: TestClient) -> None:
    response = paired.post("/api/device/connect", json={})
    assert response.status_code == 409
    assert "no address" in response.json()["detail"]


def test_connect_uses_the_remembered_address_and_publishes_the_link(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    assert paired.post("/api/device/connect", json={}).status_code == 202
    assert gateway.connected == ("10.0.0.7", FakeGateway.default_control_port)

    link = paired.get("/api/state").json()["snapshot"]["link"]
    assert link["status"] == "ready"
    assert link["endpoint"] == f"10.0.0.7:{FakeGateway.default_control_port}"


def test_connect_without_probe_detects_the_exact_hardware_profile(
    paired: TestClient, gateway: FakeGateway
) -> None:
    for model, expected in (
        ("Seestar S30", "zwo-s30"),
        ("Seestar S30 Pro", "zwo-s30pro"),
    ):
        host = "10.0.0.30" if expected == "zwo-s30" else "10.0.0.31"
        gateway.reachable[host] = descriptor(host, model=model, serial=expected)

        response = paired.post("/api/device/connect", json={"host": host})

        assert response.status_code == 202
        remembered = paired.get("/api/device/addresses").json()["remembered"]
        assert remembered["hardware_profile_id"] == expected
        assert remembered["hardware_profile_source"] == "detected"
        state = paired.get("/api/state").json()["snapshot"]
        assert state["settings"]["hardware_profile_id"] == expected
        assert state["device"]["model"] == model


def test_connect_without_probe_does_not_guess_an_unknown_model(
    paired: TestClient, gateway: FakeGateway
) -> None:
    host = "10.0.0.32"
    gateway.reachable[host] = descriptor(host, model="Future Smart Telescope")

    assert paired.post("/api/device/connect", json={"host": host}).status_code == 202

    remembered = paired.get("/api/device/addresses").json()["remembered"]
    assert remembered["hardware_profile_id"] is None
    assert remembered["hardware_profile_source"] is None


def test_connect_pushes_the_configured_location_onto_the_devices_own_site(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """This device has no location of its own to report (``ssc.main``'s
    ``AlpacaDeviceGateway.set_site`` docstring), so SSC's configured location
    is what gives it one — needed before ``ssc.astro.frames`` can convert
    to/from the frame it declares."""
    response = paired.put(
        "/api/settings/location/fix",
        json={
            "source": "manual",
            "latitude_deg": 48.2,
            "longitude_deg": 16.4,
            "elevation_m": 180.0,
        },
    )
    assert response.status_code == 200, response.text

    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    assert paired.post("/api/device/connect", json={}).status_code == 202
    assert gateway.site_calls == [(48.2, 16.4, 180.0)]


def test_connect_with_no_configured_location_pushes_nothing(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    assert paired.post("/api/device/connect", json={}).status_code == 202
    assert gateway.site_calls == []


def test_forgetting_an_address_clears_the_remembered_one(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    port = FakeGateway.default_control_port
    assert paired.delete(f"/api/device/addresses/10.0.0.7/{port}").status_code == 204
    book = paired.get("/api/device/addresses").json()
    assert book["remembered"]["host"] is None
    assert book["known"] == []


def test_device_endpoints_are_rate_limited(paired: TestClient) -> None:
    statuses = [
        paired.post("/api/device/probe", json={"host": "10.0.0.9"}).status_code for _ in range(8)
    ]
    assert 429 in statuses
    assert "Retry-After" in paired.post("/api/device/probe", json={"host": "10.0.0.9"}).headers


# -- auto-connect (product requirement: "automatically connect to last -------- #
#    connected scope and keep the connection up") ---------------------------- #


def _settle(client: TestClient, predicate: Any, *, attempts: int = 300) -> bool:
    """Poll ``/api/health`` so the supervisor's background task gets to run,
    until ``predicate()`` is true or the budget runs out."""
    for _ in range(attempts):
        if predicate():
            return True
        client.get("/api/health")
    return bool(predicate())


def test_a_remembered_address_is_connected_to_automatically_on_startup(
    config: AppConfig,
) -> None:
    """Requirement 1. A *second* process, given only the store the first one
    left behind, connects on its own — no probe, no ``POST /connect``."""
    setup_gateway = FakeGateway()
    setup_gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    with TestClient(create_app(config, gateway=setup_gateway), client=CLIENT_ADDRESS) as setup:
        setup.post("/api/auth/pair", json={"token": pairing_token(setup)})
        response = setup.post("/api/device/probe", json={"host": "10.0.0.7"})
        assert response.status_code == 200
        assert response.json()["remembered"] is True

    fresh_gateway = FakeGateway()
    with TestClient(create_app(config, gateway=fresh_gateway), client=CLIENT_ADDRESS) as started:
        assert _settle(started, lambda: fresh_gateway.connected is not None)
    assert fresh_gateway.connected == ("10.0.0.7", FakeGateway.default_control_port)


def test_auto_connect_prefers_ethernet_and_falls_back_to_wifi(config: AppConfig) -> None:
    setup_gateway = FakeGateway()
    setup_gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    with TestClient(create_app(config, gateway=setup_gateway), client=CLIENT_ADDRESS) as setup:
        setup.post("/api/auth/pair", json={"token": pairing_token(setup)})
        setup.post("/api/device/probe", json={"host": "10.0.0.7"})
        response = setup.put(
            "/api/device/ethernet-address",
            json={"host": "10.42.0.2", "control_port": 32323},
        )
        assert response.status_code == 200

    fresh_gateway = FakeGateway()
    fresh_gateway.connect_errors["10.42.0.2"] = OSError("Ethernet unplugged")
    with TestClient(create_app(config, gateway=fresh_gateway), client=CLIENT_ADDRESS) as started:
        assert _settle(started, lambda: fresh_gateway.connected is not None)

        fresh_gateway.push_link(LinkStatus.DISCONNECTED)
        assert _settle(started, lambda: len(fresh_gateway.connect_calls) >= 3)

    assert fresh_gateway.connect_calls[:2] == [
        ("10.42.0.2", 32323),
        ("10.0.0.7", FakeGateway.default_control_port),
    ]
    assert fresh_gateway.connect_calls[2] == (
        "10.0.0.7",
        FakeGateway.default_control_port,
    )
    assert fresh_gateway.connected == ("10.0.0.7", FakeGateway.default_control_port)


def test_auto_connect_stops_after_preferred_ethernet_succeeds(config: AppConfig) -> None:
    with TestClient(create_app(config, gateway=FakeGateway()), client=CLIENT_ADDRESS) as setup:
        setup.post("/api/auth/pair", json={"token": pairing_token(setup)})
        assert setup.put(
            "/api/device/ethernet-address",
            json={"host": "10.42.0.2", "control_port": 32323},
        ).status_code == 200

    fresh_gateway = FakeGateway()
    with TestClient(create_app(config, gateway=fresh_gateway), client=CLIENT_ADDRESS) as started:
        assert _settle(started, lambda: fresh_gateway.connected is not None)

    assert fresh_gateway.connect_calls == [("10.42.0.2", 32323)]
    assert fresh_gateway.connected == ("10.42.0.2", 32323)


def test_auto_connect_rediscovers_a_dhcp_moved_wifi_fallback(config: AppConfig) -> None:
    setup_gateway = FakeGateway()
    setup_gateway.reachable["192.168.1.77"] = descriptor("192.168.1.77")
    with TestClient(create_app(config, gateway=setup_gateway), client=CLIENT_ADDRESS) as setup:
        setup.post("/api/auth/pair", json={"token": pairing_token(setup)})
        setup.post("/api/device/probe", json={"host": "192.168.1.77"})
        setup.put(
            "/api/device/ethernet-address",
            json={"host": "192.168.1.47", "control_port": 32323},
        )

    fresh_gateway = FakeGateway()
    fresh_gateway.connect_errors["192.168.1.47"] = OSError("Ethernet unplugged")
    fresh_gateway.connect_errors["192.168.1.77"] = OSError("old DHCP address")
    moved = descriptor("192.168.1.76")
    fresh_gateway.discovered = [moved]
    fresh_gateway.reachable[moved.host] = moved
    with TestClient(create_app(config, gateway=fresh_gateway), client=CLIENT_ADDRESS) as started:
        assert started.post("/api/auth/pair", json={"token": pairing_token(started)}).status_code == 200
        assert _settle(
            started,
            lambda: started.get("/api/device/addresses").json()["remembered"]["host"]
            == "192.168.1.76",
        )
        remembered = started.get("/api/device/addresses").json()["remembered"]

    assert fresh_gateway.connect_calls[:3] == [
        ("192.168.1.47", 32323),
        ("192.168.1.77", FakeGateway.default_control_port),
        ("192.168.1.76", FakeGateway.default_control_port),
    ]
    assert remembered["ethernet_host"] == "192.168.1.47"
    assert remembered["host"] == "192.168.1.76"


def test_a_dropped_link_is_reconnected_without_hand_holding(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """Requirement 2. The scope reboots, Wi-Fi blips, whatever — the link goes
    to ``disconnected`` without anyone calling ``POST /disconnect``, and the
    supervisor reconnects on its own."""
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    assert paired.post("/api/device/connect", json={}).status_code == 202
    assert len(gateway.connect_calls) == 1

    assert paired.portal is not None
    paired.portal.call(gateway.push_link, LinkStatus.DISCONNECTED)  # an unexpected drop

    # The supervisor may already have reconnected by the time this is checked
    # (its own backoff is configured tiny for the test) — the point under test
    # is the eventual, unattended recovery below, not this transient state.
    assert _settle(paired, lambda: len(gateway.connect_calls) >= 2), (
        "a dropped link must be re-established without a client re-issuing connect"
    )

    snapshot = paired.get("/api/state").json()["snapshot"]
    assert snapshot["link"]["status"] == "ready"
    assert snapshot["auto_connect"]["suspended"] is False
    assert snapshot["auto_connect"]["attempt"] == 0


def test_a_failed_connect_lands_disconnected_so_the_supervisor_can_retry(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """A connect attempt that fails must not leave the link at `connecting`.

    The supervisor skips `CONNECTING` by design — it reads as "already under
    way" — so a status nobody clears is a deadlock rather than a pause.
    Observed on the owner's instance 2026-09-10: the scope dropped off the
    network, the attempt failed, and 36 minutes later `auto_connect` still
    reported enabled and not suspended with attempt 0 and no next attempt,
    while the link sat at `connecting` with a null reason.
    """
    # Connect for real first, so an address is remembered and the supervisor
    # is actually armed — then take the scope away, which is the sequence the
    # owner hit.
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    paired.post("/api/device/connect", json={})
    assert paired.get("/api/state").json()["snapshot"]["link"]["status"] == "ready"

    gateway.connect_raises = OSError("[Errno 113] No route to host")
    paired.portal.call(gateway.push_link, LinkStatus.DISCONNECTED)
    for _ in range(200):
        paired.get("/api/health")

    link = paired.get("/api/state").json()["snapshot"]["link"]
    assert link["status"] == "disconnected", "a failed attempt must not stay at connecting"
    assert link["reason"] is not None, "and it must say why (invariant 7)"


def test_the_supervisor_survives_a_failing_connect_and_keeps_retrying(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """An exception escaping `connect` used to kill the supervisor task.

    Only `CancelledError` is suppressed around that loop, so a raising
    gateway ended auto-connect for the life of the process — silently, since
    a dead supervisor still reports `enabled: true`.
    """
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    paired.post("/api/device/connect", json={})
    before = len(gateway.connect_calls)

    gateway.connect_raises = OSError("[Errno 113] No route to host")
    paired.portal.call(gateway.push_link, LinkStatus.DISCONNECTED)
    for _ in range(200):
        paired.get("/api/health")
    assert len(gateway.connect_calls) > before + 1, "the supervisor stopped retrying after a failure"

    # And it recovers once the device comes back, without an explicit connect.
    gateway.connect_raises = None
    for _ in range(200):
        paired.get("/api/health")
    assert paired.get("/api/state").json()["snapshot"]["link"]["status"] == "ready"


def test_disconnect_lands_disconnected_even_if_no_link_was_ever_established(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """A disconnect issued while a connect attempt was still failing.

    `on_link` only fires when the gateway tears a link *down*, so when the
    handshake never completed there was no event to publish and the snapshot
    stayed at `connecting` for the life of the process — the supervisor
    ignores `connecting` by design (it looks under way) and the UI offered no
    way out, so Disconnect appeared dead. Observed against firmware 9.31,
    whose handshake stalls: `POST /connect` returned 202 after its timeout,
    the link sat at `connecting` with a null reason forever, and `POST
    /disconnect` returned 204 and changed nothing.
    """
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    gateway.handshake_never_completes = True

    paired.post("/api/device/connect", json={})
    assert paired.get("/api/state").json()["snapshot"]["link"]["status"] == "connecting"

    assert paired.post("/api/device/disconnect").status_code == 204

    link = paired.get("/api/state").json()["snapshot"]["link"]
    assert link["status"] == "disconnected"
    # And it says why, rather than leaving the user to guess (invariant 7).
    assert link["reason"] is not None

    # It must also stay disconnected: the supervisor treats it as intent.
    for _ in range(100):
        paired.get("/api/health")
    assert paired.get("/api/state").json()["snapshot"]["link"]["status"] == "disconnected"


def test_an_explicit_disconnect_is_not_overridden_by_the_supervisor(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """Requirement 3. An explicit disconnect means "stay disconnected" until
    the user asks otherwise — the supervisor must never immediately undo it."""
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    paired.post("/api/device/connect", json={})
    assert len(gateway.connect_calls) == 1

    assert paired.post("/api/device/disconnect").status_code == 204
    assert gateway.connected is None

    # Give the supervisor plenty of scheduling opportunities to (wrongly) retry.
    for _ in range(100):
        paired.get("/api/health")

    assert len(gateway.connect_calls) == 1, "an explicit disconnect must not be auto-reconnected"
    snapshot = paired.get("/api/state").json()["snapshot"]
    assert snapshot["link"]["status"] == "disconnected"
    assert snapshot["auto_connect"]["suspended"] is True

    # Asking otherwise — an explicit connect — clears the intent again.
    assert paired.post("/api/device/connect", json={}).status_code == 202
    assert len(gateway.connect_calls) == 2
    assert paired.get("/api/state").json()["snapshot"]["auto_connect"]["suspended"] is False


def test_turning_auto_connect_off_stops_automatic_reconnection(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.reachable["10.0.0.7"] = descriptor("10.0.0.7")
    paired.post("/api/device/probe", json={"host": "10.0.0.7"})
    paired.post("/api/device/connect", json={})
    assert len(gateway.connect_calls) == 1

    response = paired.put("/api/device/auto-connect", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["auto_connect"] is False
    assert paired.get("/api/device/addresses").json()["remembered"]["auto_connect"] is False

    assert paired.portal is not None
    paired.portal.call(gateway.push_link, LinkStatus.DISCONNECTED)
    for _ in range(100):
        paired.get("/api/health")

    assert len(gateway.connect_calls) == 1, "auto-connect being off must be honoured"
    snapshot = paired.get("/api/state").json()["snapshot"]
    assert snapshot["auto_connect"]["enabled"] is False
    assert snapshot["auto_connect"]["reason"] == "auto-connect is off"


def test_auto_connect_defaults_on(paired: TestClient) -> None:
    body = paired.get("/api/device/addresses").json()
    assert body["remembered"]["auto_connect"] is True
    assert paired.get("/api/state").json()["snapshot"]["auto_connect"]["enabled"] is True
