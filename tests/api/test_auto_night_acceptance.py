"""Owner acceptance: preview is inert and a saved night recompiles honestly."""

from datetime import datetime, timedelta

from ssc.api.plans_service import site_of
from ssc.astro.events import target_position
from ssc.astro.units import Deg, Hours
from tests.api.conftest import FakeGateway, TestClient


def test_saved_night_preserves_utc_windows_and_never_starts_hardware(
    paired: TestClient,
    gateway: FakeGateway,
) -> None:
    paired.put(
        "/api/settings/location/fix",
        json={
            "source": "manual",
            "latitude_deg": 48.71,
            "longitude_deg": 17.85,
            "elevation_m": 202,
            "timezone": "Europe/Bratislava",
        },
    ).raise_for_status()
    before = paired.get("/api/plans").json()
    request = {
        "start_time": "2026-10-07T10:00:00Z",
        "end_time": "2026-10-08T10:00:00Z",
        "profile_id": "zwo-s30pro",
        "optic_id": "tele",
        "mount_mode": "eq",
        "allowed_object_types": ["galaxy_spiral", "emission_nebula"],
        "available_filters": ["IR", "LP"],
        "min_altitude_deg": 30,
        "max_targets": 3,
        "block_minutes": 90,
    }
    response = paired.post("/api/astro/auto-night", json=request)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["blocks"]
    assert paired.get("/api/plans").json() == before
    assert not gateway.mount_calls and not gateway.connect_calls
    assert paired.get("/api/state").json()["snapshot"]["run"] is None

    document = preview["document"]
    saved = paired.post("/api/plans", json={"document": document, "night_date": "2026-10-07"})
    assert saved.status_code == 201, saved.text
    persisted = paired.get(f"/api/plans/{document['id']}").json()
    assert persisted["document"] == saved.json()["document"]
    compiled_response = paired.post(
        f"/api/plans/{document['id']}/compile",
        json={"night_date": "2026-10-07"},
    )
    assert compiled_response.status_code == 200, compiled_response.text
    compiled = compiled_response.json()["schedule"]
    dark_start = datetime.fromisoformat(compiled["night"]["dark_start_utc"])
    dark_end = datetime.fromisoformat(compiled["night"]["dark_end_utc"])
    all_diagnostics = [d for item in compiled["items"] for d in item["diagnostics"]]
    assert not [d for d in all_diagnostics if d["severity"] == "error"], all_diagnostics
    assert all(
        dark_start
        <= datetime.fromisoformat(g["start_time"])
        < datetime.fromisoformat(g["end_time"])
        <= dark_end
        for g in preview["gaps"]
    )
    assert preview["scheduled_hours"] == len(preview["blocks"]) * 1.5
    imaging = [item for item in compiled["items"] if item["kind"] == "goto_and_stack"]
    for block, item in zip(preview["blocks"], imaging, strict=True):
        assert datetime.fromisoformat(item["start_utc"]) == datetime.fromisoformat(
            block["start_time"]
        )
        assert datetime.fromisoformat(item["end_utc"]) == datetime.fromisoformat(block["end_time"])
        assert item["target"]["name"] == block["designation"]
        assert block["object_type"] in request["allowed_object_types"]
        assert dark_start <= datetime.fromisoformat(block["start_time"])
        assert datetime.fromisoformat(block["end_time"]) <= dark_end
        # Independently resolve the stored plan's coordinates at both edges and
        # the middle, instead of trusting the preview's aggregate altitude.
        services = paired.app.state.services
        site = site_of(services.settings.active_location())
        start = datetime.fromisoformat(block["start_time"])
        finish = datetime.fromisoformat(block["end_time"])
        for at in (start, start + (finish - start) / 2, finish):
            position = target_position(
                services.astro.ephemeris,
                site,
                Hours(item["target"]["ra_j2000_deg"] / 15),
                Deg(item["target"]["dec_j2000_deg"]),
                at,
            )
            assert position.altitude_deg >= 30
    assert datetime.fromisoformat(compiled["items"][0]["end_utc"]) <= datetime.fromisoformat(
        imaging[0]["start_utc"]
    )
    assert datetime.fromisoformat(compiled["items"][-1]["start_utc"]) == datetime.fromisoformat(
        imaging[-1]["end_utc"]
    )
    assert datetime.fromisoformat(compiled["items"][-1]["end_utc"]) <= dark_end + timedelta(
        seconds=1
    )
    assert not gateway.mount_calls and not gateway.connect_calls
    assert paired.get("/api/state").json()["snapshot"]["run"] is None
