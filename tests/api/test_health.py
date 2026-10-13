"""``GET /api/health`` — a liveness check, not a filesystem or activity leak.

code-reviewer finding 6: an unauthenticated caller on the LAN must get nothing
beyond "the process is up". The database path names the OS username and home
directory layout; ``seq`` says how busy the process has been. Neither belongs
in a response any unpaired device can request.
"""

from __future__ import annotations

from tests.api.conftest import TestClient


def test_an_unpaired_caller_gets_nothing_beyond_liveness(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["schema_version"] >= 1
    assert body["seq"] is None
    assert body["database"] is None
    assert body["device_gateway"] is None
    assert body["version"] is None
    assert "home" not in response.text
    assert "sqlite" not in response.text.lower()


def test_a_paired_caller_gets_the_detailed_form(paired: TestClient) -> None:
    response = paired.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["seq"], int)
    assert body["database"]
    assert body["device_gateway"]


def test_paired_health_uses_compiled_version_metadata(paired: TestClient, monkeypatch, tmp_path) -> None:
    import ssc.api.routes.health as health_module
    (tmp_path / "VERSION").write_text("0.0.3+g123456789abc\n")
    monkeypatch.setattr(health_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(health_module.sys, "executable", str(tmp_path / "ssc-core"))
    assert paired.get("/api/health").json()["version"] == "0.0.3+g123456789abc"
    (tmp_path / "VERSION").unlink()
    assert paired.get("/api/health").json()["version"] is None
