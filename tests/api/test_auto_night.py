from dataclasses import replace

from tests.api.conftest import TestClient

BODY = {
    "start_time": "2026-10-07T10:00:00Z",
    "end_time": "2026-10-08T10:00:00Z",
    "profile_id": "zwo-s30pro",
    "optic_id": "tele",
    "mount_mode": "eq",
    "max_targets": 2,
}


def location(client):
    r = client.put(
        "/api/settings/location/fix",
        json={
            "source": "manual",
            "latitude_deg": 48.71,
            "longitude_deg": 17.85,
            "elevation_m": 202,
            "timezone": "Europe/Bratislava",
        },
    )
    assert r.status_code == 200, r.text


def test_auto_night_rejects_unbounded_window(paired: TestClient):
    assert (
        paired.post(
            "/api/astro/auto-night", json={**BODY, "end_time": "2026-10-10T10:00:00Z"}
        ).status_code
        == 422
    )


def test_preview_empty_selection_has_no_device_actions(paired: TestClient):
    location(paired)
    r = paired.post("/api/astro/auto-night", json={**BODY, "allowed_object_types": []})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["blocks"] == [] and data["document"]["items"] == []
    assert data["note"] == "no_safe_targets"
    assert data["candidates_considered"] == 0


def test_preview_roundtrip_persistence_and_compile(paired: TestClient):
    location(paired)
    r = paired.post("/api/astro/auto-night", json=BODY)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["blocks"]
    document = data["document"]
    assert document["items"][0]["kind"] == "startup"
    assert document["items"][-1]["kind"] == "shutdown"
    saved = paired.post("/api/plans", json={"document": document, "night_date": "2026-10-07"})
    assert saved.status_code == 201, saved.text
    compiled = paired.post(
        f"/api/plans/{document['id']}/compile", json={"night_date": "2026-10-07"}
    )
    assert compiled.status_code == 200, compiled.text

    payload = compiled.json()
    assert not [d for d in payload["diagnostics"] if d["severity"] == "error"]
    items = payload["schedule"]["items"]
    assert not [d for i in items for d in i["diagnostics"] if d["severity"] == "error"]
    assert items[0]["end_utc"] <= items[1]["start_utc"]
    assert items[-2]["end_utc"] <= items[-1]["start_utc"]


def test_wide_does_not_generate_unsupported_lp(paired: TestClient):
    location(paired)
    r = paired.post(
        "/api/astro/auto-night", json={**BODY, "optic_id": "wide", "available_filters": ["LP"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["blocks"] == []
    assert r.json()["note"] == "wide_ir_only_weather_unchecked"


def test_no_filter_wheel_does_not_generate_lp(paired: TestClient):
    location(paired)
    astro = paired.app.state.services.astro
    profile = astro.profile("zwo-s30pro")
    astro.profiles[profile.id] = replace(
        profile, capabilities=replace(profile.capabilities, filter_wheel=False)
    )
    r = paired.post("/api/astro/auto-night", json={**BODY, "available_filters": ["LP"]})
    assert r.status_code == 200, r.text
    assert r.json()["blocks"] == []
