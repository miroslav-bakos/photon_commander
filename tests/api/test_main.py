"""The bind guard, the CLI, and the OpenAPI contract the TS client is built from."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path

import keyring
import keyring.backends.fail
import pytest

from ssc.main import (
    BindRefusedError,
    _mdns_lan_addresses,
    _mdns_server_name,
    check_bind_address,
    create_app,
    create_http_bootstrap_app,
    load_config,
    local_ipv4_addresses,
    run,
)
from ssc.settings.config import AppConfig
from ssc.settings.tls import ensure_tls_material
from tests.api.conftest import FakeGateway, TestClient, services_of


@dataclass(frozen=True, slots=True)
class _FakeAddr:
    family: socket.AddressFamily
    address: str


def test_local_ipv4_addresses_enumerates_every_interface_not_just_the_default_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A UDP-connect "which source address" trick only ever reveals the default
    route's interface. A second NIC with a public address must still show up,
    or the wildcard-bind safety check below can wave a public interface
    through — the exact hole this replaces (see local_ipv4_addresses's own
    docstring)."""
    monkeypatch.setattr(
        "psutil.net_if_addrs",
        lambda: {
            "lo": [_FakeAddr(socket.AF_INET, "127.0.0.1")],
            "eth0": [_FakeAddr(socket.AF_INET, "192.168.1.4")],
            "eth1": [_FakeAddr(socket.AF_INET, "203.0.113.9")],  # the second, public NIC
        },
    )
    assert local_ipv4_addresses() == ["127.0.0.1", "192.168.1.4", "203.0.113.9"]


def test_mdns_advertises_only_addresses_reachable_from_another_machine() -> None:
    assert _mdns_lan_addresses(["127.0.0.1", "0.0.0.0", "192.168.100.13", "192.168.100.14"]) == [
        "192.168.100.13",
        "192.168.100.14",
    ]


def test_mdns_service_server_does_not_compete_for_the_os_hostname() -> None:
    assert _mdns_server_name("Photon Commander", "photon") == ("Photon-Commander-photon.local.")


def test_the_wildcard_is_refused_when_a_secondary_interface_is_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: check_bind_address("0.0.0.0") must catch a public NIC that a
    same-default-route-only enumeration would have missed entirely."""
    monkeypatch.setattr(
        "psutil.net_if_addrs",
        lambda: {
            "lo": [_FakeAddr(socket.AF_INET, "127.0.0.1")],
            "eth0": [_FakeAddr(socket.AF_INET, "192.168.1.4")],
            "eth1": [_FakeAddr(socket.AF_INET, "203.0.113.9")],
        },
    )
    with pytest.raises(BindRefusedError, match=r"203\.0\.113\.9"):
        check_bind_address("0.0.0.0")


@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.10", "10.4.4.4", "172.16.0.1"])
def test_loopback_and_rfc1918_are_allowed(host: str) -> None:
    check_bind_address(host)


@pytest.mark.parametrize("host", ["8.8.8.8", "203.0.113.7", "172.32.0.1", "169.254.1.1"])
def test_a_public_or_link_local_bind_is_refused(host: str) -> None:
    with pytest.raises(BindRefusedError, match="refusing to bind"):
        check_bind_address(host)


def test_a_hostname_is_refused_because_what_it_exposes_is_ambiguous() -> None:
    with pytest.raises(BindRefusedError, match="not an IP address"):
        check_bind_address("myhost.local")


def test_the_wildcard_is_allowed_only_on_a_wholly_private_machine() -> None:
    check_bind_address("0.0.0.0", interfaces=["192.168.1.10", "127.0.0.1"])
    with pytest.raises(BindRefusedError, match=r"203\.0\.113\.5"):
        check_bind_address("0.0.0.0", interfaces=["192.168.1.10", "203.0.113.5"])


def test_tailscale_shared_addresses_do_not_make_the_lan_wildcard_public() -> None:
    check_bind_address(
        "0.0.0.0",
        interfaces=["127.0.0.1", "192.168.100.13", "192.168.100.14", "100.121.112.58"],
    )
    check_bind_address("100.121.112.58")


def test_run_exits_with_an_explanation_rather_than_binding_a_public_interface(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = run(["--host", "203.0.113.7", "--data-dir", str(tmp_path), "--check-config"])
    assert code == 2
    assert "refusing to bind" in capsys.readouterr().err


def test_check_config_validates_without_starting_a_server(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = run(["--host", "127.0.0.1", "--data-dir", str(tmp_path), "--no-mdns", "--check-config"])
    assert code == 0
    out = capsys.readouterr().out
    assert "configuration OK" in out
    assert str(tmp_path) in out
    assert "secrets     ok" in out


def test_check_config_reports_a_degraded_secrets_backend_rather_than_lying(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The Pi reproduction: a systemd *system* service has no session D-Bus,
    ``keyring`` selects the null backend, and every secret operation would
    raise on first use. ``--check-config`` must say so, not print "OK" over
    it — this is the check an operator runs before walking away from a
    headless install (CLAUDE.md invariant 7)."""
    keyring.set_keyring(keyring.backends.fail.Keyring())
    code = run(["--host", "127.0.0.1", "--data-dir", str(tmp_path), "--no-mdns", "--check-config"])
    assert code == 0, "still runnable: only keyed weather providers are gated by this"
    out = capsys.readouterr().out
    assert "secrets     UNAVAILABLE" in out
    assert "keyring.backends.fail.Keyring" in out
    assert "systemd --user" in out, "the operator is told what to actually do about it"


def test_check_config_reports_tls_off_by_default(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = run(["--host", "127.0.0.1", "--data-dir", str(tmp_path), "--no-mdns", "--check-config"])
    assert code == 0
    assert "tls         off" in capsys.readouterr().out


def test_check_config_reports_generated_tls_material(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSC_TLS_ENABLED", "1")
    code = run(["--host", "127.0.0.1", "--data-dir", str(tmp_path), "--no-mdns", "--check-config"])
    assert code == 0
    out = capsys.readouterr().out
    assert "tls         on, leaf expires" in out
    assert (tmp_path / "tls" / "ca.crt").is_file()


def test_run_refuses_to_start_with_a_missing_user_supplied_certificate(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md invariant 7: TLS requested but unusable is a loud, fatal
    error — never a silent fall back to plain HTTP."""
    monkeypatch.setenv("SSC_TLS_ENABLED", "1")
    monkeypatch.setenv("SSC_TLS_CERT_PATH", str(tmp_path / "missing.crt"))
    monkeypatch.setenv("SSC_TLS_KEY_PATH", str(tmp_path / "missing.key"))
    code = run(["--host", "127.0.0.1", "--data-dir", str(tmp_path), "--no-mdns"])
    assert code == 2
    assert "does not exist" in capsys.readouterr().err


def test_the_cli_never_supplies_a_device_address() -> None:
    config = load_config(["--host", "127.0.0.1"])
    assert not [
        field for field in AppConfig.model_fields if "device" in field or "seestar" in field
    ]
    assert config.host == "127.0.0.1"


def test_the_ws_frame_union_is_published_for_client_generation(
    config: AppConfig, gateway: FakeGateway
) -> None:
    schema = create_app(config, gateway=gateway).openapi()
    frame = schema["x-ssc-ws-frame"]
    assert frame["discriminator"]["propertyName"] == "type"
    assert set(frame["discriminator"]["mapping"]) == {"snapshot", "event", "error", "heartbeat"}
    for name in (
        "SnapshotFrame",
        "EventFrame",
        "ErrorFrame",
        "HeartbeatFrame",
        "LinkChanged",
        "DiscoveryChanged",
    ):
        assert name in schema["components"]["schemas"]
    heartbeat = schema["components"]["schemas"]["HeartbeatFrame"]
    assert {"epoch", "seq"} <= set(heartbeat["required"])
    assert heartbeat["properties"]["type"]["const"] == "heartbeat"


def test_the_pairing_url_is_logged_at_startup(
    caplog: pytest.LogCaptureFixture, config: AppConfig, gateway: FakeGateway
) -> None:
    """Bound to a single LAN address, uvicorn never listens on loopback, and
    GET /api/auth/pairing is gated to a loopback caller or an existing session
    (ssc/api/routes/auth.py) — so the console is the only remaining way for an
    operator to ever get the token. It must always be there."""
    caplog.set_level("INFO", logger="ssc")
    app = create_app(config, gateway=gateway)
    with TestClient(app) as started:
        token = services_of(started).auth.pairing_token
    messages = [record.getMessage() for record in caplog.records]
    assert any("pairing URL" in message and token in message for message in messages)


def test_no_auth_on_a_lan_bind_warns_at_startup(
    caplog: pytest.LogCaptureFixture, tmp_path: Path, gateway: FakeGateway
) -> None:
    """CLAUDE.md 2026-09-09: an open, unauthenticated instance is a real
    security posture, not a detail — an operator reading the log must be able
    to see that this one is open to its LAN."""
    config = AppConfig(host="192.168.100.50", data_dir=tmp_path, mdns_enabled=False, auth_required=False)
    caplog.set_level("WARNING", logger="ssc")
    with TestClient(create_app(config, gateway=gateway)):
        pass
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        record.levelname == "WARNING" and "auth_required is off" in record.getMessage()
        for record in caplog.records
    )
    assert any("192.168.100.50" in message for message in messages)


def test_no_auth_on_loopback_does_not_warn(
    caplog: pytest.LogCaptureFixture, config: AppConfig, gateway: FakeGateway
) -> None:
    """Loopback-only plus no auth is unremarkable: nothing but this process
    can ever reach it."""
    caplog.set_level("WARNING", logger="ssc")
    with TestClient(create_app(config, gateway=gateway)):
        pass
    assert not [r for r in caplog.records if "auth_required is off" in r.getMessage()]


@pytest.mark.parametrize("config", [True], indirect=True)
def test_auth_required_on_a_lan_bind_does_not_warn(
    caplog: pytest.LogCaptureFixture, config: AppConfig, tmp_path: Path, gateway: FakeGateway
) -> None:
    lan_config = config.model_copy(update={"host": "192.168.100.50"})
    caplog.set_level("WARNING", logger="ssc")
    with TestClient(create_app(lan_config, gateway=gateway)):
        pass
    assert not [r for r in caplog.records if "auth_required is off" in r.getMessage()]


def test_the_initial_snapshot_surfaces_an_unavailable_secrets_backend(
    config: AppConfig, gateway: FakeGateway
) -> None:
    """CLAUDE.md invariant 7: degraded and visible, not fatal and not silent.
    The first ``GET /api/state`` a client ever sees must already carry this —
    there is no separate runtime event for it because nothing changes the
    keyring backend mid-process."""
    keyring.set_keyring(keyring.backends.fail.Keyring())
    app = create_app(config, gateway=gateway)
    with TestClient(app) as started:
        snapshot = services_of(started).state.snapshot
    assert snapshot.secrets is not None
    assert snapshot.secrets.available is False
    assert snapshot.secrets.backend == "keyring.backends.fail.Keyring"
    assert snapshot.secrets.reason is not None
    assert [n for n in snapshot.notices if n.code == "secrets_backend_unavailable"]


def test_the_initial_snapshot_reports_secrets_available_with_a_working_backend(
    client: TestClient,
) -> None:
    """The ``memory_keyring`` autouse fixture is a working backend — no notice,
    no false degradation reported when there is nothing wrong."""
    snapshot = services_of(client).state.snapshot
    assert snapshot.secrets is not None
    assert snapshot.secrets.available is True
    assert snapshot.secrets.reason is None
    assert not [n for n in snapshot.notices if n.code == "secrets_backend_unavailable"]


def test_health_is_available_without_a_session(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["schema_version"] == 1


def test_cors_is_locked_to_the_served_origin(client: TestClient) -> None:
    allowed = client.get("/api/health", headers={"Origin": "http://127.0.0.1:7777"})
    assert allowed.headers.get("access-control-allow-origin") == "http://127.0.0.1:7777"
    foreign = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in foreign.headers


def test_cors_allows_the_https_origin_once_tls_is_on(tmp_path: Path, gateway: FakeGateway) -> None:
    """Reported bug's root cause: with a hardcoded ``http://`` scheme, turning
    TLS on would have every real browser request rejected by CORS, because
    the actual ``Origin`` header is ``https://``."""
    config = AppConfig(host="127.0.0.1", data_dir=tmp_path, mdns_enabled=False, tls_enabled=True)
    with TestClient(create_app(config, gateway=gateway), base_url=config.origin) as tls_client:
        allowed = tls_client.get("/api/health", headers={"Origin": "https://127.0.0.1:7777"})
        assert allowed.headers.get("access-control-allow-origin") == "https://127.0.0.1:7777"
        plain = tls_client.get("/api/health", headers={"Origin": "http://127.0.0.1:7777"})
        assert "access-control-allow-origin" not in plain.headers


def test_the_pairing_url_uses_https_once_tls_is_on(
    caplog: pytest.LogCaptureFixture, tmp_path: Path, gateway: FakeGateway
) -> None:
    config = AppConfig(host="127.0.0.1", data_dir=tmp_path, mdns_enabled=False, tls_enabled=True)
    caplog.set_level("INFO", logger="ssc")
    with TestClient(create_app(config, gateway=gateway)):
        pass
    messages = [record.getMessage() for record in caplog.records]
    assert any("pairing URL: https://" in message for message in messages)


def test_ca_certificate_is_served_unauthenticated_when_tls_is_generated(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    config = AppConfig(host="127.0.0.1", data_dir=tmp_path, mdns_enabled=False, tls_enabled=True)
    material = ensure_tls_material(tls_dir=config.tls_dir, user_cert_path=None, user_key_path=None)
    app = create_app(config, gateway=gateway, tls_material=material)
    with TestClient(app, base_url=config.origin) as tls_client:
        response = tls_client.get("/ca.crt")
    assert response.status_code == 200
    assert response.content == material.ca_cert_path.read_bytes()  # type: ignore[union-attr]
    assert response.headers["content-type"] == "application/x-x509-ca-cert"


def test_http_bootstrap_serves_the_ca_and_redirects_everything_else(
    tmp_path: Path,
) -> None:
    config = AppConfig(host="192.168.100.13", port=443, tls_enabled=True, tls_http_redirect_port=80)
    ca_path = tmp_path / "ca.crt"
    ca_path.write_bytes(b"public test CA")
    with TestClient(create_http_bootstrap_app(config, ca_path)) as bootstrap:
        certificate = bootstrap.get("/ca.crt")
        redirected = bootstrap.get(
            "/planning?target=M31",
            headers={"host": "192.168.100.14"},
            follow_redirects=False,
        )

    assert certificate.status_code == 200
    assert certificate.content == b"public test CA"
    assert redirected.status_code == 308
    assert redirected.headers["location"] == "https://192.168.100.14/planning?target=M31"


def test_no_ca_certificate_route_when_tls_is_off(client: TestClient) -> None:
    assert client.get("/ca.crt").status_code == 404


def test_no_ca_certificate_route_for_a_user_supplied_certificate(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    """There is no CA of ours in that chain to hand out."""
    from tests.settings.test_tls import _write_self_signed

    cert_path, key_path = _write_self_signed(tmp_path, "byo", expired=False)
    config = AppConfig(
        host="127.0.0.1",
        data_dir=tmp_path,
        mdns_enabled=False,
        tls_enabled=True,
        tls_cert_path=cert_path,
        tls_key_path=key_path,
    )
    material = ensure_tls_material(
        tls_dir=config.tls_dir, user_cert_path=cert_path, user_key_path=key_path
    )
    app = create_app(config, gateway=gateway, tls_material=material)
    with TestClient(app, base_url=config.origin) as tls_client:
        assert tls_client.get("/ca.crt").status_code == 404
