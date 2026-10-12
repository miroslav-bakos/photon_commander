# SPDX-License-Identifier: GPL-3.0-only
"""Browser origin admission must run before mutation or WS subscription."""

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.api.conftest import TestClient, services_of


@pytest.mark.parametrize("config", [False, True], indirect=True)
@pytest.mark.parametrize("origin", ["https://untrusted.example", "null", "http://127.0.0.1:7777.evil"])
def test_foreign_origin_cannot_disconnect(client: TestClient, config, origin: str) -> None:
    services = services_of(client)
    assert not services.device._suspended
    response = client.post("/api/device/disconnect", headers={"Origin": origin})
    assert response.status_code == 403
    assert not services.device._suspended


def test_native_and_same_origin_controls_remain_available(client: TestClient) -> None:
    for headers in ({}, {"Origin": "http://127.0.0.1:7777"}, {"Origin": "http://127.0.0.1:7777"}):
        assert client.post("/api/device/disconnect", headers=headers).status_code == 204


def test_duplicate_origin_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/device/disconnect",
        headers=[("Origin", "http://127.0.0.1:7777"), ("Origin", "https://untrusted.example")],
    )
    assert response.status_code == 403
    assert not services_of(client).device._suspended


@pytest.mark.parametrize("origin", ["https://untrusted.example", "null"])
def test_foreign_websocket_is_rejected_before_snapshot(client: TestClient, origin: str) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as rejected,
        client.websocket_connect("/ws", headers={"Origin": origin}),
    ):
        pytest.fail("foreign browser was admitted")
    assert rejected.value.code == 1008


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://127.0.0.1:7777"}])
def test_native_and_same_origin_websocket_receive_snapshot(client: TestClient, headers) -> None:
    with client.websocket_connect("/ws", headers=headers) as ws:
        assert ws.receive_json()["type"] == "snapshot"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_attacker_host_is_rejected_even_when_origin_matches(client, method):
    response = client.request(method, "/api/auth/pairing" if method == "GET" else "/api/device/disconnect",
                              headers={"Host": "attacker.example:7777", "Origin": "http://attacker.example:7777"})
    assert response.status_code == 403
    assert not services_of(client).device._suspended


def test_attacker_host_without_origin_cannot_read_local_pairing_token(client):
    assert client.get("/api/auth/pairing", headers={"Host": "attacker.example:7777"}).status_code == 403


@pytest.mark.parametrize("host", ["node.test.ts.net", "127.0.0.1:7777"])
async def test_verified_private_proxy_origin_supports_preserved_or_rewritten_host(host):
    from ssc.api.browser_origin import BrowserOriginMiddleware, TransportProxyMiddleware
    sent = []
    async def app(scope, receive, send):
        await send({"type":"http.response.start", "status":204, "headers":[]})
        await send({"type":"http.response.body", "body":b""})
    async def verified(): return "https://node.test.ts.net"
    async def receive(): return {"type":"http.request", "body":b""}
    async def send(message): sent.append(message)
    middleware = TransportProxyMiddleware(BrowserOriginMiddleware(app, allowed_origins=["http://127.0.0.1:7777"], private_proxy_origin=verified))
    scope = {"type":"http", "method":"POST", "scheme":"http", "path":"/api/device/disconnect", "query_string":b"", "client":("127.0.0.1",1),
             "headers":[(b"host", host.encode()), (b"origin",b"https://node.test.ts.net"), (b"x-forwarded-for",b"100.64.1.2"),(b"x-forwarded-proto",b"https")]}
    await middleware(scope, receive, send)
    assert sent[0]['status'] == 204


async def test_unverified_public_proxy_stays_rejected():
    from ssc.api.browser_origin import BrowserOriginMiddleware, TransportProxyMiddleware
    sent = []
    async def app(scope, receive, send): pytest.fail("public proxy admitted")
    async def unverified(): return None
    async def receive(): return {"type":"http.request", "body":b""}
    async def send(message): sent.append(message)
    middleware = TransportProxyMiddleware(BrowserOriginMiddleware(app, allowed_origins=["http://127.0.0.1:7777"], private_proxy_origin=unverified))
    scope = {"type":"http", "method":"POST", "scheme":"http", "path":"/api/device/disconnect", "query_string":b"", "client":("127.0.0.1",1),
             "headers":[(b"host",b"node.test.ts.net"),(b"origin",b"https://node.test.ts.net"),(b"x-forwarded-for",b"100.64.1.2"),(b"x-forwarded-proto",b"https")]}
    await middleware(scope, receive, send)
    assert sent[0]['status'] == 403


def test_ipv6_loopback_origin_is_bracketed():
    from ssc.api.browser_origin import _origin
    from ssc.main import _allowed_origins
    from ssc.settings.config import AppConfig
    origins = _allowed_origins(AppConfig(host="::1"))
    assert "http://[::1]:7777" in origins
    assert all(_origin(value) is not None for value in origins)


@pytest.mark.parametrize("kind", ["http", "websocket"])
@pytest.mark.parametrize("host,accepted", [("photon.local:7777", True), ("photon:7777", True),
                                         ("photon-commander-photon.local:7777", True),
                                         ("photon.attacker.example:7777", False), ("photon.local:7778", False)])
async def test_wildcard_lan_names_are_exact_locally_derived_authorities(monkeypatch, kind, host, accepted):
    from ssc.api.browser_origin import BrowserOriginMiddleware
    from ssc.main import _allowed_origins
    from ssc.settings.config import AppConfig
    monkeypatch.setattr("ssc.main.socket.gethostname", lambda: "photon")
    monkeypatch.setattr("ssc.main.local_ipv4_addresses", lambda: ["192.168.100.13"])
    origins = _allowed_origins(AppConfig(host="0.0.0.0", instance_name="photon-commander"))
    assert "http://photon.local:7777" not in _allowed_origins(AppConfig(host="127.0.0.1"))
    admitted = []
    sent = []
    async def app(scope, receive, send): admitted.append(True)
    async def receive(): return {"type": "http.request", "body": b""}
    async def send(message): sent.append(message)
    scope = {"type": kind, "method": "POST", "scheme": "ws" if kind == "websocket" else "http",
             "path": "/", "query_string": b"", "client": ("192.168.1.77", 45001),
             "headers": [(b"host", host.encode()), (b"origin", f"http://{host}".encode())]}
    await BrowserOriginMiddleware(app, allowed_origins=origins)(scope, receive, send)
    assert bool(admitted) is accepted
    if not accepted:
        assert sent[0].get("status", sent[0].get("code")) == (1008 if kind == "websocket" else 403)
