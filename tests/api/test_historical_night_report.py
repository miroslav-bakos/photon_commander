# SPDX-License-Identifier: GPL-3.0-only
"""Historical reports use durable executions and acquisitions, without a device."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from ssc.api.run_service import historical_night_report
from ssc.store.capture import CaptureRepository
from ssc.store.database import open_database
from ssc.store.plans import PlanRepository
from ssc.store.runs import RunRepository
from tests.api.conftest import services_of
from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule


async def seed(database, folder: Path):
    plans = PlanRepository(database)
    original = build_plan()
    await plans.create(original)
    runs = RunRepository(database)
    for run_id in ("first", "restarted"):
        await runs.create(
            run_id=run_id,
            plan=original,
            plan_revision=1,
            schedule=build_schedule(original),
            started_at=BASE_UTC,
        )
    # Its UTC start is identical, but the persisted schedule night is different.
    other_schedule = build_schedule(original).model_copy(
        update={"night": build_schedule(original).night.model_copy(update={"date": "2026-01-02"})}
    )
    await runs.create(
        run_id="other-night",
        plan=original,
        plan_revision=1,
        schedule=other_schedule,
        started_at=BASE_UTC,
    )
    captures = CaptureRepository(database)

    async def frame(
        run_id, name, exposure, *, kind="light", outcome=None, complete=True, block="goto"
    ):
        frame_id = await captures.begin_frame(
            path=folder / f"{name}.fits",
            date_obs=BASE_UTC,
            object_name="M42" if block == "goto" else None,
            filter_name=None,
            frame_type=kind,
            exposure_s=exposure,
            gain=None,
            ccd_temp_c=None,
            width=4,
            height=4,
            run_id=run_id,
            block_id=block,
        )
        if complete:
            await captures.complete_frame(frame_id)
        if outcome is not None:
            await captures.record_stack_outcome(frame_id, outcome)
            await captures.record_stack_outcome(frame_id, outcome)  # idempotent replay
        return frame_id

    await frame("first", "accepted", 30, outcome="accepted")
    await frame("restarted", "rejected", 60, outcome="rejected")
    await frame("restarted", "unknown-exposure", None)
    await frame("restarted", "error", None, outcome="error")
    await frame("restarted", "pending", 60, complete=False)
    await frame("restarted", "dark", 10, kind="dark", block=None)
    removed = await frame("first", "removed", 90, outcome="accepted")
    await captures.delete_complete_frame(removed)
    await frame("other-night", "other", 1000, outcome="accepted")
    await plans.update(
        original.id, original.model_copy(update={"name": "Later edited plan"}), expected_revision=1
    )


def test_night_endpoint_uses_executed_snapshots_and_reconciles_retained_frames(client, tmp_path):
    services = services_of(client)
    client.portal.call(seed, services.database, tmp_path)
    response = client.get("/api/runs/nights/2026-01-01")
    assert response.status_code == 200
    report = response.json()
    assert len(report["executions"]) == 2
    assert {execution["plan"]["name"] for execution in report["executions"]} == {
        "Crash-resume fixture"
    }
    assert {execution["run"]["plan_revision"] for execution in report["executions"]} == {1}
    lights = report["lights"]
    assert lights["saved_frames"] == 4
    assert lights["pending_frames"] == 1
    assert lights["captured_integration_s"] == 90
    assert lights["unknown_exposure_frames"] == 2
    assert lights["accepted_frames"] == 1
    assert lights["rejected_frames"] == 1
    assert lights["stack_error_frames"] == 1
    assert lights["accepted_integration_s"] == 30
    assert lights["rejected_integration_s"] == 60
    assert lights["stack_error_integration_s"] is None
    assert lights["unknown_outcome_exposure_frames"] == 1
    assert lights["outcome_measured_frames"] == 3
    assert lights["stack_unmeasured_frames"] == 1
    assert report["calibration"]["saved_frames"] == 1
    assert report["calibration"]["captured_integration_s"] == 10
    assert sum(obj["lights"]["saved_frames"] for obj in report["objects"]) == lights["saved_frames"]
    assert (
        sum(execution["lights"]["saved_frames"] for execution in report["executions"])
        == lights["saved_frames"]
    )
    for execution in report["executions"]:
        assert (
            sum(item["lights"]["saved_frames"] for item in execution["items"])
            == execution["lights"]["saved_frames"]
        )
        goto = next(item for item in execution["items"] if item["item_id"] == "goto")
        assert goto["planned_duration_s"] == 600
        assert goto["panel_index"] == 0
        assert goto["panel_attribution_available"] is True
    for measurement in report["measurements"].values():
        assert measurement["value"] is None
        assert measurement["sample_count"] == 0
        assert measurement["total_samples"] == 4
        assert measurement["source"] is None
    # Existing detail route keeps its shape and handles its own ID.
    assert client.get("/api/runs/first").status_code == 200
    assert client.get("/api/runs/nights/not-a-date").status_code == 422


def test_empty_night_keeps_unknown_outcomes_distinct_from_zero_saved(client):
    response = client.get("/api/runs/nights/2026-01-01")
    assert response.status_code == 200
    report = response.json()
    assert report["executions"] == []
    assert report["lights"]["saved_frames"] == 0
    assert report["lights"]["captured_integration_s"] == 0
    assert report["lights"]["accepted_frames"] is None
    assert report["lights"]["rejected_frames"] is None


async def test_report_after_reopening_real_sqlite(tmp_path):
    from ssc.store.migrations import migrate

    path = tmp_path / "report.sqlite3"
    database = await open_database(path)
    await migrate(database)
    await seed(database, tmp_path)
    await database.aclose()
    reopened = await open_database(path)
    try:
        report = await historical_night_report(
            RunRepository(reopened),
            CaptureRepository(reopened),
            date(2026, 1, 1),
            now=datetime(2026, 1, 3, tzinfo=UTC),
        )
        assert report.lights.saved_frames == 4
        assert report.lights.captured_integration_s == 90
        assert report.executions[0].plan.name == "Crash-resume fixture"
        assert (
            await PlanRepository(reopened).get("test-plan")
        ).document.name == "Later edited plan"
    finally:
        await reopened.aclose()


async def test_multiple_panels_are_explicitly_unattributed(tmp_path):
    from datetime import timedelta

    from ssc.scheduler.compiler import CompiledPanel
    from ssc.store.migrations import migrate

    database = await open_database(tmp_path / "panels.sqlite3")
    try:
        await migrate(database)
        plan = build_plan()
        await PlanRepository(database).create(plan)
        schedule = build_schedule(plan)
        panels = tuple(
            CompiledPanel(
                row=0,
                col=i,
                ra_j2000_deg=None,
                dec_j2000_deg=None,
                start_utc=BASE_UTC + timedelta(minutes=i * 5),
                end_utc=BASE_UTC + timedelta(minutes=(i + 1) * 5),
            )
            for i in range(2)
        )
        items = tuple(
            item.model_copy(update={"panels": panels}) if item.item_id == "goto" else item
            for item in schedule.items
        )
        await RunRepository(database).create(
            run_id="multiple-panels",
            plan=plan,
            plan_revision=1,
            schedule=schedule.model_copy(update={"items": items}),
            started_at=BASE_UTC,
        )
        report = await historical_night_report(
            RunRepository(database), CaptureRepository(database), date(2026, 1, 1), now=BASE_UTC
        )
        goto = next(item for item in report.executions[0].items if item.item_id == "goto")
        assert goto.panel_index is None
        assert goto.panel_attribution_available is False
    finally:
        await database.aclose()


def test_historical_night_requires_session_when_authentication_enabled(client):
    services = services_of(client)
    client.portal.call(services.settings.set_auth_required, True)
    response = client.get("/api/runs/nights/2026-01-01")
    assert response.status_code == 401
