"""Pairing, session cookies, PIN gating (DESIGN.md §9).

``auth_required`` is off by default (CLAUDE.md 2026-09-09: loginless on the
owner's own LAN, with the machinery kept behind a setting). Every test here
that exercises *gating* — as opposed to the pairing/session mechanics
themselves, which are real and enforced either way — is parametrized over
``config`` so both postures are covered: ``False`` (the default) proves the
bypass holds, ``True`` proves today's behaviour is unchanged.
"""

from __future__ import annotations

import pytest

from ssc.api.security import SESSION_COOKIE
from ssc.remote_access import RemoteAccessStatus
from ssc.settings.config import AppConfig
from tests.api.conftest import TestClient, pairing_token, services_of


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_state_requires_a_session_iff_auth_required(client: TestClient, config: AppConfig) -> None:
    response = client.get("/api/state")
    assert response.status_code == (401 if config.auth_required else 200)


def test_a_bad_pairing_token_is_rejected(client: TestClient) -> None:
    response = client.post("/api/auth/pair", json={"token": "not-the-token"})
    assert response.status_code == 401
    assert SESSION_COOKIE not in response.cookies


def test_pairing_sets_an_httponly_samesite_lax_cookie(client: TestClient) -> None:
    response = client.post("/api/auth/pair", json={"token": pairing_token(client)})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert client.get("/api/state").status_code == 200


def test_pairing_request_bodies_forbid_unknown_fields(client: TestClient) -> None:
    response = client.post(
        "/api/auth/pair", json={"token": pairing_token(client), "scope": "control"}
    )
    assert response.status_code == 422
    assert "scope" in response.text


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_logout_revokes_the_session_iff_auth_required(
    paired: TestClient, config: AppConfig
) -> None:
    """The cookie is deleted either way — ``logout`` itself does not consult
    ``auth_required`` — but only matters for whether the *next* request is
    let through: with auth off, every caller is let through regardless."""
    assert paired.post("/api/auth/logout").status_code == 204
    assert paired.get("/api/state").status_code == (401 if config.auth_required else 200)


def test_rotating_the_pairing_token_invalidates_the_old_qr(paired: TestClient) -> None:
    old = pairing_token(paired)
    new = str(paired.post("/api/auth/pairing/rotate").json()["token"])
    assert new != old
    services_of(paired).limiters.pop("auth")
    assert paired.post("/api/auth/pair", json={"token": old}).status_code == 401
    assert paired.post("/api/auth/pair", json={"token": new}).status_code == 200


def test_rotating_the_pairing_token_logs_the_new_url(
    paired: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The console is the only way to reach a fresh QR on a LAN-only bind
    (CLAUDE.md/DESIGN.md §9 — see ssc.main's own startup log line)."""
    caplog.set_level("INFO", logger="ssc")
    new = str(paired.post("/api/auth/pairing/rotate").json()["token"])
    messages = [record.getMessage() for record in caplog.records]
    assert any("pairing URL rotated" in message and new in message for message in messages)


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_a_control_pin_downgrades_sessions_and_gates_control(
    paired: TestClient, config: AppConfig
) -> None:
    assert paired.put("/api/auth/control-pin", json={"pin": "8421"}).status_code == 200

    # The PIN mechanics themselves are real either way: this very session's
    # *stored* scope really did drop to view-only, and the wrong PIN really
    # is rejected — neither consults auth_required.
    assert paired.get("/api/auth/session").json()["scope"] == "view"
    assert paired.post("/api/auth/pin", json={"pin": "0000"}).status_code == 403

    denied = paired.post("/api/device/probe", json={"host": "10.0.0.5"})
    if config.auth_required:
        # Gating is on: a view-only session cannot drive anything.
        assert denied.status_code == 403
    else:
        # Gating is off: ControlDep is a bypass regardless of the real
        # session's scope, so the same request that was just refused above
        # succeeds here.
        assert denied.status_code == 200

    upgraded = paired.post("/api/auth/pin", json={"pin": "8421"})
    assert upgraded.status_code == 200
    assert upgraded.json()["scope"] == "control"
    assert paired.post("/api/device/probe", json={"host": "10.0.0.5"}).status_code == 200


def test_a_short_pin_is_refused(paired: TestClient) -> None:
    assert paired.put("/api/auth/control-pin", json={"pin": "12"}).status_code == 422


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_the_pairing_token_is_not_readable_from_the_lan_without_a_session(
    config: AppConfig, gateway: object
) -> None:
    """With auth off there is nothing left to guard the token against: any
    device that can reach the port can already view and control the mount, so
    a LAN caller reading the pairing token too is consistent, not a hole."""
    from ssc.main import create_app

    app = create_app(config, gateway=gateway)  # type: ignore[arg-type]
    with TestClient(app, client=("192.168.100.50", 51000)) as lan_client:
        response = lan_client.get("/api/auth/pairing")
        assert response.status_code == (401 if config.auth_required else 200)


@pytest.mark.parametrize("config", [True], indirect=True)
def test_a_loopback_reverse_proxy_is_not_mistaken_for_the_local_console(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/auth/pairing",
        headers={
            "Tailscale-User-Login": "observer@example.com",
            "X-Forwarded-Proto": "https",
        },
    )
    assert response.status_code in {401, 403}


def test_local_pairing_qr_uses_the_verified_private_serve_url(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = RemoteAccessStatus(
        state="serving",
        detail="Private HTTPS remote access is active inside your tailnet.",
        detected_path="/usr/bin/tailscale",
        dns_name="observatory.example.ts.net",
        url="https://observatory.example.ts.net",
        funnel_enabled=False,
        setup_command="sudo tailscale serve --bg 127.0.0.1:7777",
    )
    monkeypatch.setattr("ssc.api.routes.auth.inspect_tailscale", lambda _port, **_kwargs: status)

    response = client.get("/api/auth/pairing")
    assert response.status_code == 200, response.text
    assert response.json()["origin"] == "https://observatory.example.ts.net"
    assert response.json()["pairing_url"].startswith("https://observatory.example.ts.net/?t=")


def test_local_pairing_qr_never_advertises_a_public_funnel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = RemoteAccessStatus(
        state="public_exposure",
        detail="SSC is exposed through Tailscale Funnel.",
        detected_path="/usr/bin/tailscale",
        dns_name="observatory.example.ts.net",
        url="https://observatory.example.ts.net",
        funnel_enabled=True,
        setup_command="sudo tailscale serve --bg 127.0.0.1:7777",
    )
    monkeypatch.setattr("ssc.api.routes.auth.inspect_tailscale", lambda _port, **_kwargs: status)

    response = client.get("/api/auth/pairing")
    assert response.status_code == 200, response.text
    assert response.json()["origin"] != "https://observatory.example.ts.net"


def test_auth_endpoints_are_rate_limited(client: TestClient) -> None:
    statuses = [
        client.post("/api/auth/pair", json={"token": "wrong"}).status_code for _ in range(10)
    ]
    assert 429 in statuses, "a brute-force loop must hit the limiter"
