"""``ssc/api/routes/plans.py``: CRUD, structural validation, compiling one
night, and the ``TargetIntent`` handoff (DESIGN.md 4).

Compiling needs the pinned ``de440s`` ephemeris kernel, exactly like
``tests/api/test_astro.py``: skipped when it is not present on disk,
``SSC_REQUIRE_EPHEMERIS=1`` turns that skip into a failure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ssc.astro.events import EphemerisMissingError, load_ephemeris
from ssc.settings.config import AppConfig
from tests.api.conftest import TestClient

GOLDEN_PLAN_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "scheduler" / "golden_plan.json"
)


def _ephemeris_available() -> bool:
    try:
        with load_ephemeris():
            return True
    except EphemerisMissingError:
        return False


requires_ephemeris = pytest.mark.skipif(
    not _ephemeris_available() and os.environ.get("SSC_REQUIRE_EPHEMERIS") != "1",
    reason="ephemeris kernel not present; see ssc.astro.events.load_ephemeris",
)


def _plan_document(plan_id: str = "p1", name: str = "test plan") -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": plan_id,
        "name": name,
        "items": [
            {"kind": "startup", "id": "startup", "start": {"kind": "asap"}},
            {
                "kind": "shutdown",
                "id": "shutdown",
                "start": {"kind": "relative", "offset_min": 5},
            },
        ],
        "created_at": "2026-08-01T12:00:00Z",
        "updated_at": "2026-08-01T12:00:00Z",
    }


def _set_location(
    client: TestClient,
    *,
    latitude: float = 45.0,
    longitude: float = 10.0,
    elevation_m: float | None = None,
    timezone: str | None = None,
) -> None:
    body: dict[str, object] = {
        "source": "manual",
        "latitude_deg": latitude,
        "longitude_deg": longitude,
    }
    if elevation_m is not None:
        body["elevation_m"] = elevation_m
    if timezone is not None:
        body["timezone"] = timezone
    response = client.put("/api/settings/location/fix", json=body)
    assert response.status_code == 200, response.text


# -- CRUD ---------------------------------------------------------------- #


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_plans_endpoints_require_a_session_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    gated = bool(config.auth_required)
    assert (client.get("/api/plans").status_code == 401) is gated
    assert (
        client.post("/api/plans", json={"document": _plan_document()}).status_code == 401
    ) is gated


def test_create_then_list_then_get(paired: TestClient) -> None:
    create = paired.post("/api/plans", json={"document": _plan_document()})
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["revision"] == 1
    assert body["document"]["id"] == "p1"

    listed = paired.get("/api/plans")
    assert listed.status_code == 200
    summaries = listed.json()["plans"]
    assert [s["id"] for s in summaries] == ["p1"]
    assert summaries[0]["revision"] == 1

    fetched = paired.get("/api/plans/p1")
    assert fetched.status_code == 200
    assert fetched.json()["document"]["name"] == "test plan"


def test_a_plan_can_be_assigned_to_and_removed_from_a_calendar_night(
    paired: TestClient,
) -> None:
    create = paired.post(
        "/api/plans",
        json={"document": _plan_document(), "night_date": "2026-09-24"},
    )
    assert create.status_code == 201, create.text
    assert create.json()["night_date"] == "2026-09-24"
    assert paired.get("/api/plans").json()["plans"][0]["night_date"] == "2026-09-24"

    clear = paired.put("/api/plans/p1/night", json={"night_date": None})
    assert clear.status_code == 200, clear.text
    assert clear.json()["night_date"] is None
    assert paired.get("/api/plans").json()["plans"][0]["night_date"] is None


def test_apply_night_moves_fixed_times_together_and_guards_revision(paired: TestClient) -> None:
    document = _plan_document()
    document["items"] = [
        {"kind": "startup", "id": "first", "start": {
            "kind": "absolute", "local_datetime": "2026-10-02T22:10:37", "fold": 0}},
        {"kind": "shutdown", "id": "last", "start": {
            "kind": "absolute", "local_datetime": "2026-10-03T02:35:40", "fold": 1}},
    ]
    created = paired.post("/api/plans", json={"document": document})
    assert created.status_code == 201
    # The date picker may already have changed calendar metadata.
    paired.put("/api/plans/p1/night", json={"night_date": "2026-10-04"})
    applied = paired.post("/api/plans/p1/apply-night", json={
        "night_date": "2026-10-04", "expected_revision": 1})
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["night_date"] == "2026-10-04"
    assert result["revision"] == 2
    assert [item["start"]["local_datetime"] for item in result["document"]["items"]] == [
        "2026-10-04T22:10:37", "2026-10-05T02:35:40"]
    assert result["document"]["items"][1]["start"]["fold"] == 1
    conflict = paired.post("/api/plans/p1/apply-night", json={
        "night_date": "2026-10-05", "expected_revision": 1})
    assert conflict.status_code == 409
    assert paired.get("/api/plans/p1").json() == result


def test_apply_night_preserves_relative_times(paired: TestClient) -> None:
    document = _plan_document()
    created = paired.post("/api/plans", json={"document": document}).json()
    response = paired.post("/api/plans/p1/apply-night", json={
        "night_date": "2026-10-04", "expected_revision": 1})
    assert response.status_code == 200, response.text
    assert response.json()["document"]["items"] == created["document"]["items"]


def test_create_a_duplicate_id_is_a_409(paired: TestClient) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    response = paired.post("/api/plans", json={"document": _plan_document()})
    assert response.status_code == 409


def test_get_an_unknown_plan_is_a_404(paired: TestClient) -> None:
    assert paired.get("/api/plans/no-such-plan").status_code == 404


def test_update_bumps_the_revision(paired: TestClient) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    renamed = _plan_document(name="renamed")
    response = paired.put("/api/plans/p1", json={"document": renamed, "expected_revision": 1})
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2
    assert response.json()["document"]["name"] == "renamed"


def test_update_with_a_stale_revision_is_a_409(paired: TestClient) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    paired.put(
        "/api/plans/p1", json={"document": _plan_document(name="v2"), "expected_revision": 1}
    )
    response = paired.put(
        "/api/plans/p1", json={"document": _plan_document(name="v3-stale"), "expected_revision": 1}
    )
    assert response.status_code == 409


def test_update_with_a_mismatched_document_id_is_a_422(paired: TestClient) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    other = _plan_document(plan_id="not-p1")
    response = paired.put("/api/plans/p1", json={"document": other, "expected_revision": 1})
    assert response.status_code == 422


def test_delete_removes_the_plan(paired: TestClient) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    response = paired.delete("/api/plans/p1")
    assert response.status_code == 204
    assert paired.get("/api/plans/p1").status_code == 404


def test_delete_an_unknown_plan_is_a_404(paired: TestClient) -> None:
    assert paired.delete("/api/plans/no-such-plan").status_code == 404


def test_a_bad_schema_version_is_a_422(paired: TestClient) -> None:
    bad = _plan_document()
    bad["schema_version"] = 99
    response = paired.post("/api/plans", json={"document": bad})
    assert response.status_code == 422


def test_plans_appear_in_the_state_snapshot(paired: TestClient) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    state = paired.get("/api/state")
    assert state.status_code == 200
    plans = state.json()["snapshot"]["plans"]
    assert [p["id"] for p in plans] == ["p1"]


# -- validate -------------------------------------------------------------- #


def test_validate_returns_the_missing_shutdown_warning(paired: TestClient) -> None:
    document = _plan_document()
    document["items"] = [document["items"][0]]  # drop the shutdown item
    response = paired.post("/api/plans/validate", json={"document": document})
    assert response.status_code == 200, response.text
    codes = [d["code"] for d in response.json()["diagnostics"]]
    assert "missing_shutdown" in codes


def test_validate_does_not_need_a_location(client: TestClient) -> None:
    # No location set at all, but validate is structural-only.
    from tests.api.conftest import pairing_token

    response = client.post("/api/auth/pair", json={"token": pairing_token(client)})
    assert response.status_code == 200
    response = client.post("/api/plans/validate", json={"document": _plan_document()})
    assert response.status_code == 200


# -- compile ---------------------------------------------------------------- #


def test_compile_without_a_location_is_a_409(paired: TestClient) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    response = paired.post("/api/plans/p1/compile", json={})
    assert response.status_code == 409


def test_compile_without_a_timezone_is_a_409(paired: TestClient) -> None:
    _set_location(paired)  # no timezone
    paired.post("/api/plans", json={"document": _plan_document()})
    response = paired.post("/api/plans/p1/compile", json={})
    assert response.status_code == 409


def test_compile_an_unknown_plan_is_a_404(paired: TestClient) -> None:
    _set_location(paired, timezone="Europe/London")
    response = paired.post("/api/plans/no-such-plan/compile", json={})
    assert response.status_code == 404


@requires_ephemeris
def test_compile_the_golden_plan_resolves_start_utc_for_every_item(paired: TestClient) -> None:
    _set_location(
        paired, latitude=51.4934, longitude=-0.0098, elevation_m=45.0, timezone="Europe/London"
    )
    document = json.loads(GOLDEN_PLAN_PATH.read_text())
    create = paired.post("/api/plans", json={"document": document})
    assert create.status_code == 201, create.text

    response = paired.post(
        "/api/plans/golden-plan-ngc2244/compile", json={"night_date": "2026-08-28"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schedule"]["night"]["date"] == "2026-08-28"
    items = body["schedule"]["items"]
    assert len(items) == len(document["items"])
    for item in items:
        assert item["start_utc"] is not None, item
    assert isinstance(body["diagnostics"], list)


# -- from-intent ------------------------------------------------------------- #


def test_planning_handoff_creates_dusk_to_dawn_capture_then_chains_next_target(
    paired: TestClient,
) -> None:
    plan = _plan_document("planning-night", "Plan for 2026-09-18")
    plan["items"] = []
    target = {"name": "M31", "ra_j2000_h": 0.71, "dec_j2000_deg": 41.27}
    first = paired.post(
        "/api/plans/actions/schedule-intent",
        json={"new_plan": plan, "night_date": "2026-09-18", "intent": target},
    )
    assert first.status_code == 200, first.text
    first_item = first.json()["document"]["items"][0]
    assert first_item["start"] == {"kind": "event", "event": "astronomical_dusk", "offset_min": 0.0}
    assert first_item["end"] == {"kind": "event", "event": "astronomical_dawn", "offset_min": 0.0}
    assert paired.get("/api/plans").json()["plans"][0]["night_date"] == "2026-09-18"

    second = paired.post(
        "/api/plans/actions/schedule-intent",
        json={"plan_id": "planning-night", "intent": {**target, "mosaic": {"ra_num": 2, "dec_num": 2}}},
    )
    assert second.status_code == 200, second.text
    items = second.json()["document"]["items"]
    assert len(items) == 2
    assert items[1]["kind"] == "mosaic"
    assert items[1]["start"] == {"kind": "asap"}
    assert items[1]["end"] == {"kind": "event", "event": "astronomical_dawn", "offset_min": 0.0}


def test_planning_separate_panels_split_darkness_equally(paired: TestClient) -> None:
    plan = _plan_document("panel-night", "Plan for 2026-09-18")
    plan["items"] = []
    response = paired.post(
        "/api/plans/actions/schedule-intent",
        json={
            "new_plan": plan,
            "duration_min": 130,
            "intent": {
                "name": "IC 1396", "ra_j2000_h": 21.6, "dec_j2000_deg": 57.5,
                "mosaic": {"ra_num": 3, "dec_num": 1, "overlap_pct": 20},
                "panels": [
                    {"row": 0, "col": 0, "ra_j2000_h": 21.61, "dec_j2000_deg": 57.48},
                    {"row": 0, "col": 1, "ra_j2000_h": 21.69, "dec_j2000_deg": 57.49},
                    {"row": 0, "col": 2, "ra_j2000_h": 21.77, "dec_j2000_deg": 57.50},
                ],
            },
        },
    )
    assert response.status_code == 200, response.text
    first, middle, last = response.json()["document"]["items"]
    assert first["start"]["event"] == "astronomical_dusk"
    assert first["end"] == {"kind": "duration", "minutes": 130.0}
    assert middle["start"] == {"kind": "asap"}
    assert middle["end"] == first["end"]
    assert last["start"] == {"kind": "asap"}
    assert last["end"] == {"kind": "event", "event": "astronomical_dawn", "offset_min": 0.0}


def test_planning_unsplit_mosaic_uses_duration_per_panel(paired: TestClient) -> None:
    plan = _plan_document("whole-mosaic-night", "Plan for 2026-09-18")
    plan["items"] = []
    response = paired.post(
        "/api/plans/actions/schedule-intent",
        json={
            "new_plan": plan,
            "duration_min": 97.5,
            "intent": {
                "name": "IC 1396", "ra_j2000_h": 21.6, "dec_j2000_deg": 57.5,
                "mosaic": {"ra_num": 2, "dec_num": 2, "overlap_pct": 20},
            },
        },
    )
    assert response.status_code == 200, response.text
    (mosaic,) = response.json()["document"]["items"]
    assert mosaic["kind"] == "mosaic"
    assert mosaic["start"] == {"kind": "event", "event": "astronomical_dusk", "offset_min": 0.0}
    assert mosaic["end"] == {"kind": "duration", "minutes": 97.5}


def test_from_intent_appends_a_goto_and_stack_item(paired: TestClient) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    response = paired.post(
        "/api/plans/p1/items/from-intent",
        json={
            "intent": {
                "name": "NGC 2244",
                "ra_j2000_h": 6.5317,
                "dec_j2000_deg": 4.9407,
            }
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 2
    items = body["document"]["items"]
    assert items[-1]["id"] == body["item_id"]
    assert items[-1]["kind"] == "goto_and_stack"
    # schema_version 2 nests the target so a body can be named without
    # coordinates (DESIGN.md 3.6); a catalogue target keeps its numbers.
    assert items[-1]["target"] == {
        "kind": "catalog",
        "name": "NGC 2244",
        "ra_j2000_h": 6.5317,
        "dec_j2000_deg": 4.9407,
    }
    assert items[-1]["start"]["kind"] == "asap"


def test_from_intent_with_a_real_grid_appends_a_mosaic_item(paired: TestClient) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    response = paired.post(
        "/api/plans/p1/items/from-intent",
        json={
            "intent": {
                "name": "NGC 2244",
                "ra_j2000_h": 6.5317,
                "dec_j2000_deg": 4.9407,
                "mosaic": {"ra_num": 2, "dec_num": 2, "overlap_pct": 20},
            }
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["document"]["items"][-1]["kind"] == "mosaic"


def test_from_intent_atomically_appends_exact_centres_as_separate_panels(
    paired: TestClient,
) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    response = paired.post(
        "/api/plans/p1/items/from-intent",
        json={
            "duration_min": 20,
            "intent": {
                "name": "IC 1396",
                "ra_j2000_h": 21.6493,
                "dec_j2000_deg": 57.4891,
                "mosaic": {"ra_num": 2, "dec_num": 1, "overlap_pct": 20},
                "panels": [
                    {"row": 0, "col": 0, "ra_j2000_h": 21.61, "dec_j2000_deg": 57.48},
                    {"row": 0, "col": 1, "ra_j2000_h": 21.69, "dec_j2000_deg": 57.49},
                ],
            },
        },
    )
    assert response.status_code == 200, response.text
    panels = response.json()["document"]["items"][-2:]
    assert [panel["kind"] for panel in panels] == ["goto_and_stack", "goto_and_stack"]
    assert [panel["target"] for panel in panels] == [
        {
            "kind": "catalog",
            "name": "IC 1396 1",
            "ra_j2000_h": 21.61,
            "dec_j2000_deg": 57.48,
        },
        {
            "kind": "catalog",
            "name": "IC 1396 2",
            "ra_j2000_h": 21.69,
            "dec_j2000_deg": 57.49,
        },
    ]
    assert response.json()["item_id"] == panels[0]["id"]


def test_from_body_intent_persists_identity_without_stale_coordinates(paired: TestClient) -> None:
    paired.post("/api/plans", json={"document": _plan_document()})
    response = paired.post(
        "/api/plans/p1/items/from-intent",
        json={"intent": {"body": "moon"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["document"]["items"][-1]["target"] == {
        "kind": "body",
        "body": "moon",
    }


def test_from_intent_on_an_unknown_plan_is_a_404(paired: TestClient) -> None:
    response = paired.post(
        "/api/plans/no-such-plan/items/from-intent",
        json={"intent": {"ra_j2000_h": 6.5317, "dec_j2000_deg": 4.9407}},
    )
    assert response.status_code == 404
