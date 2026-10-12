"""``ssc/api/routes/runs.py`` + ``ssc/api/run_service.py``: a plan executed
end to end through the hosted driver against the emulator (DESIGN.md 4.2).

The app here is the *production* wiring — ``ssc-alpaca`` hosted in-process on
an ephemeral loopback port, ``ssc-core`` a client of it (CLAUDE.md invariant
2) — with only the driver's backend pointed at the in-process emulator instead
of a socket to a scope. So "start a run" really does reach ``start_auto_goto``.

Compiling needs the pinned ephemeris kernel, exactly like ``tests/api/test_plans.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ssc.api.autofocus_service import AutofocusConditionsError, AutofocusRestoreError
from ssc.api.plans_service import site_of
from ssc.astro.bodies import BodyId
from ssc.astro.frames import EquatorialCoord, MountFrame, frame_offset_arcsec
from ssc.astro.targets import resolve_position
from ssc.astro.units import Deg
from ssc.main import AlpacaRuntime, create_app
from ssc.plan.models import BodyTargetSpec
from ssc.scheduler.executor import (
    DeviceCommandError,
    DeviceFocusConditionsError,
    JournalRecord,
    RecordKind,
)
from ssc.seestar.emulator import EmulatorConfig, SeestarEmulator
from ssc.settings.config import AppConfig
from ssc.state.events import STATE_EVENT_ADAPTER
from ssc.state.models import StateSnapshot
from ssc.state.ports import FocuserRefusedError
from ssc.state.reducer import replay
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from ssc.store.runs import RunRepository
from tests.alpaca.conftest import make_targetable_backend, record_calls
from tests.api.conftest import CLIENT_ADDRESS, TestClient, pairing_token, services_of
from tests.api.test_plans import _set_location, requires_ephemeris
from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule

pytestmark = requires_ephemeris


@pytest.fixture(autouse=True)
def historical_execution_without_real_dawn_watchdog(monkeypatch):
    """These fixtures replay August nights with an accelerated execution clock.

    Their scope is wire commands and journal replay, not today's dawn. The
    independent real-clock interlock is exercised in test_dawn_safety_guard.
    """
    from ssc.api.run_service import RunService

    original = RunService.__init__

    def initialize(self, **kwargs):
        kwargs["dawn_safety_enabled"] = False
        original(self, **kwargs)

    monkeypatch.setattr(RunService, "__init__", initialize)


def test_scheduled_focus_runner_uses_longer_exposures_and_clears_warning(
    ready: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_of(ready)
    exposures = []

    async def focus(**options):
        exposures.append(options["exposure_s"])
        if options["exposure_s"] < 4:
            raise FocuserRefusedError("weak star field") from AutofocusConditionsError(
                "weak star field"
            )

    monkeypatch.setattr(services.device, "focuser_start_autofocus", focus)
    assert services.runs._device is not None
    ready.portal.call(services.runs._device.autofocus)
    assert exposures == [2, 4]
    assert all(
        notice.id != "scheduled-focus-recovery" for notice in services.state.snapshot.notices
    )


def test_scheduled_focus_runner_surfaces_cloud_warning_as_recoverable_condition(
    ready: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_of(ready)
    exposures = []

    async def focus(**options):
        exposures.append(options["exposure_s"])
        raise FocuserRefusedError("no stars") from AutofocusConditionsError("no stars")

    monkeypatch.setattr(services.device, "focuser_start_autofocus", focus)
    assert services.runs._device is not None
    with pytest.raises(DeviceFocusConditionsError):
        ready.portal.call(services.runs._device.autofocus)
    assert exposures == [2, 4, 8]
    notice = next(
        notice
        for notice in services.state.snapshot.notices
        if notice.id == "scheduled-focus-recovery"
    )
    assert "waiting 10 minutes" in notice.message.casefold()


def test_scheduled_focus_runner_does_not_soften_failed_focus_restore(
    ready: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_of(ready)

    async def focus(**options):
        raise FocuserRefusedError("restore failed") from AutofocusRestoreError("restore failed")

    monkeypatch.setattr(services.device, "focuser_start_autofocus", focus)
    assert services.runs._device is not None
    with pytest.raises(DeviceCommandError) as raised:
        ready.portal.call(services.runs._device.autofocus)
    assert not raised.value.recoverable


#: Silences collapsed; a goto that settles fast enough for a test but slowly
#: enough that ``Slewing`` is seen true before false.
FAST = EmulatorConfig(
    event_stream_silence_s=0.01,
    close_event_stream_after_push=False,
    event_idle_timeout_s=None,
    reset_refusal_s=0.05,
    goto_settle_s=0.3,
)

NIGHT = "2026-08-28"


class EmulatedRuntime(AlpacaRuntime):
    """The real hosted driver, on an ephemeral port, with the emulator behind it."""

    def __init__(self, emulator: SeestarEmulator) -> None:
        super().__init__(port=0)
        self.emulator = emulator
        self.backend, self.session_addresses = make_targetable_backend(emulator, cache_ttl_s=0.0)

    async def aclose(self) -> None:
        await super().aclose()
        await self.emulator.aclose()


@pytest.fixture
def emulator() -> SeestarEmulator:
    return SeestarEmulator(FAST)


@pytest.fixture
def run_client(
    config: AppConfig, emulator: SeestarEmulator, socket_enabled: None
) -> Iterator[TestClient]:
    del socket_enabled  # AlpacaRuntime binds a real loopback socket
    app = create_app(config, alpaca_runtime=EmulatedRuntime(emulator))
    with TestClient(app, client=CLIENT_ADDRESS) as test_client:
        yield test_client


@pytest.fixture
def ready(run_client: TestClient) -> TestClient:
    """Paired with control scope, located, and the driver pointed at the emulator."""
    response = run_client.post("/api/auth/pair", json={"token": pairing_token(run_client)})
    assert response.status_code == 200, response.text
    _set_location(
        run_client, latitude=51.4934, longitude=-0.0098, elevation_m=45.0, timezone="Europe/London"
    )
    # Loopback is the one non-RFC1918 address the driver accepts; the emulator
    # ignores it (architecture policy invariant 11 — no address is ever assumed).
    connected = run_client.post("/api/device/connect", json={"host": "127.0.0.1"})
    assert connected.status_code == 202, connected.text
    return run_client


def _plan(plan_id: str, items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": plan_id,
        "name": f"plan {plan_id}",
        "items": items,
        "created_at": "2026-08-01T12:00:00Z",
        "updated_at": "2026-08-01T12:00:00Z",
    }


def _slew_plan() -> dict[str, object]:
    """startup -> slew, both already due: the run executes immediately."""
    return _plan(
        "slew",
        [
            {
                "kind": "startup",
                "id": "startup",
                "start": {"kind": "absolute", "local_datetime": f"{NIGHT}T21:00:00"},
            },
            {
                "kind": "slew",
                "id": "slew",
                "start": {"kind": "relative", "offset_min": 0},
                "target_name": "M42",
                "ra_j2000_h": 5.5,
                "dec_j2000_deg": -5.4,
            },
        ],
    )


def _moon_plan() -> dict[str, object]:
    """startup -> slew to the Moon, both already due: the run executes
    immediately, so the item's position is resolved at execution time, which
    is "now" for this test too — see
    ``test_body_target_resolves_at_execution_time_not_compile_time``."""
    return _plan(
        "moon",
        [
            {
                "kind": "startup",
                "id": "startup",
                "start": {"kind": "absolute", "local_datetime": f"{NIGHT}T21:00:00"},
            },
            {
                "kind": "slew",
                "id": "slew",
                "start": {"kind": "relative", "offset_min": 0},
                "target": {"kind": "body", "body": "moon"},
            },
        ],
    )


def _waiting_plan() -> dict[str, object]:
    """Two items years away: the run parks in ``WaitUntil`` until controlled."""
    return _plan(
        "waiting",
        [
            {
                "kind": "startup",
                "id": "startup",
                "start": {"kind": "absolute", "local_datetime": "2035-08-28T21:00:00"},
            },
            {
                "kind": "shutdown",
                "id": "shutdown",
                "start": {"kind": "relative", "offset_min": 5},
            },
        ],
    )


def _create(client: TestClient, document: dict[str, object]) -> None:
    response = client.post("/api/plans", json={"document": document})
    assert response.status_code == 201, response.text


def _start(client: TestClient, plan_id: str) -> dict[str, Any]:
    # These historical execution fixtures deliberately use a compilation clock
    # within their selected night; launch replaces it with the real execution clock.
    from types import SimpleNamespace

    services_of(client).runs._clock = SimpleNamespace(now=lambda: datetime(2026, 8, 28, 22, tzinfo=UTC))
    response = client.post("/api/runs", json={"plan_id": plan_id, "night_date": NIGHT})
    assert response.status_code == 201, response.text
    run = response.json()["run"]
    assert isinstance(run, dict)
    return run


def _current(client: TestClient) -> dict[str, Any] | None:
    response = client.get("/api/runs/current")
    assert response.status_code == 200, response.text
    run = response.json()["run"]
    assert run is None or isinstance(run, dict)
    return run


def _wait_for(
    client: TestClient, predicate: Callable[[dict[str, Any]], bool], *, timeout_s: float = 10.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = _current(client)
        if last is not None and predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s}s; last section: {last}")


def _items(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["item_id"]: item for item in run["items"]}


def _state(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/state")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


# -- access ------------------------------------------------------------------------ #


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_runs_require_a_session_iff_auth_required(client: TestClient, config: AppConfig) -> None:
    gated = bool(config.auth_required)
    assert (client.get("/api/runs/current").status_code == 401) is gated
    assert (client.post("/api/runs", json={"plan_id": "x"}).status_code == 401) is gated
    assert (client.post("/api/runs/x/pause").status_code == 401) is gated


def test_start_with_no_hosted_driver_is_a_503(paired: TestClient) -> None:
    """The standard test app substitutes a fake gateway and hosts no driver:
    that is refused out loud, never faked (CLAUDE.md invariant 7)."""
    _set_location(paired, timezone="Europe/London")
    _create(paired, _waiting_plan())
    response = paired.post("/api/runs", json={"plan_id": "waiting"})
    assert response.status_code == 503, response.text
    assert "no device driver" in response.json()["detail"]


def test_start_without_a_location_is_a_409(run_client: TestClient) -> None:
    response = run_client.post("/api/auth/pair", json={"token": pairing_token(run_client)})
    assert response.status_code == 200, response.text
    _create(run_client, _waiting_plan())
    response = run_client.post("/api/runs", json={"plan_id": "waiting"})
    assert response.status_code == 409, response.text


def test_plan_editor_revision_does_not_change_persisted_active_run(ready: TestClient) -> None:
    plan = _waiting_plan()
    _create(ready, plan)
    started = _start(ready, "waiting")
    assert started["plan_revision"] == 1

    updated = dict(plan)
    updated["name"] = "updated while running"
    response = ready.put(
        "/api/plans/waiting",
        json={"document": updated, "expected_revision": 1},
    )
    assert response.status_code == 200, response.text

    current = _current(ready)
    assert current is not None
    assert current["plan_revision"] == 1
    assert current["plan_name"] == plan["name"]
    services = services_of(ready)
    saved_run = ready.portal.call(services.runs._runs.get, started["run_id"])
    assert saved_run.plan_revision == 1
    assert saved_run.plan.name == plan["name"]
    assert ready.get("/api/plans/waiting").json()["revision"] == 2
    future = dict(plan, id="future", name="Next night")
    _create(ready, future)
    applied = ready.post("/api/plans/future/apply-night", json={"night_date": "2035-08-29", "expected_revision": 1})
    assert applied.status_code == 200, applied.text
    compiled_future = ready.post("/api/plans/future/compile", json={"night_date": "2035-08-29"})
    assert compiled_future.status_code == 200, compiled_future.text
    assert _current(ready)["run_id"] == started["run_id"]
    assert ready.post("/api/runs", json={"plan_id": "future", "night_date": "2035-08-29"}).status_code == 409


def test_start_an_unknown_plan_is_a_404(ready: TestClient) -> None:
    response = ready.post("/api/runs", json={"plan_id": "nope"})
    assert response.status_code == 404, response.text


# -- execution ------------------------------------------------------------------------ #


def test_start_slews_the_emulator_and_completes(
    ready: TestClient, emulator: SeestarEmulator
) -> None:
    emulator.set_arm(closed=False)
    gotos = record_calls(emulator, "start_auto_goto")
    views = record_calls(emulator, "iscope_start_view")
    _create(ready, _slew_plan())

    started = _start(ready, "slew")
    assert started["status"] == "running"
    assert started["plan_id"] == "slew"
    assert started["night_date"] == NIGHT
    assert set(started["available_actions"]) == {"pause", "skip", "stop", "retry"}

    done = _wait_for(ready, lambda run: run["status"] == "completed")
    # Firmware 8.46 can open the observing view but cannot command its filter
    # wheel. The expired startup window settles with that error in its journal
    # while the independent slew item still executes.
    assert _items(done)["startup"]["status"] == "done"
    assert "window ended" in _items(done)["startup"]["reason"]
    assert _items(done)["slew"]["status"] == "done", _items(done)["slew"]["reason"]
    assert done["current_item_id"] is None
    assert done["ended_reason"] == "all items settled"
    assert done["available_actions"] == []
    assert views == [{"mode": "star"}]
    assert len(gotos) == 1
    assert gotos[0]["ra"] == pytest.approx(5.5)
    assert gotos[0]["dec"] == pytest.approx(-5.4)

    # The post-mortem: the journal, in order, with the executor's reasons.
    detail = ready.get(f"/api/runs/{done['run_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["capture_summary"]["saved_frames"] == 0
    assert "final_mount" in detail.json()
    journal = detail.json()["journal"]
    assert [entry["seq"] for entry in journal] == list(range(1, len(journal) + 1))
    assert journal[0]["kind"] == "run_started"
    assert journal[-1]["kind"] == "run_ended"
    assert journal[-1]["run_status"] == "completed"
    effects = [
        (entry["effect"]["name"], entry["effect_outcome"])
        for entry in journal
        if entry["effect"] is not None and entry["effect_outcome"] is not None
    ]
    # ``set_tracking_rate`` is issued for every target-bearing item (the rate
    # travels with the target, DESIGN.md 3.6) and is *advisory*: this driver
    # reports the S30 Pro as sidereal-only on firmware 8.46, so the attempt is
    # journalled as failed, with the driver's own reason, and the item carries on.
    assert effects == [
        ("startup", "completed"),
        ("unpark", "completed"),
        ("set_filter", "failed"),
        ("set_tracking_rate", "failed"),
        ("slew_to", "completed"),
    ]
    rate_entry = next(
        e
        for e in journal
        if (e["effect"] or {}).get("name") == "set_tracking_rate"
        and e["effect_outcome"] is not None
    )
    assert "sidereal rate only" in rate_entry["reason"]
    assert "will drift" in rate_entry["reason"]
    assert detail.json()["run"] == done
    # And the snapshot agrees with the endpoint.
    assert _state(ready)["snapshot"]["run"] == done


def test_separate_mosaic_panels_send_each_exact_coordinate_to_seestar(
    ready: TestClient, emulator: SeestarEmulator
) -> None:
    emulator.set_arm(closed=False)
    gotos = record_calls(emulator, "start_auto_goto")
    _create(ready, _plan("separate-panels", []))
    response = ready.post(
        "/api/plans/separate-panels/items/from-intent",
        json={
            "start": {"kind": "absolute", "local_datetime": f"{NIGHT}T21:00:00"},
            "duration_min": 1,
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

    _start(ready, "separate-panels")
    done = _wait_for(ready, lambda run: run["status"] == "completed")
    assert [item["status"] for item in done["items"]] == ["done", "done"]
    # Live stacking is deliberately unavailable on the recorded firmware, so
    # each expired window settles after logging the unavailable operation. Both items must still
    # deliver their own centre through Alpaca to the Seestar adapter.
    assert [(call["ra"], call["dec"]) for call in gotos] == [
        (pytest.approx(21.61), pytest.approx(57.48)),
        (pytest.approx(21.69), pytest.approx(57.49)),
    ]


@requires_ephemeris
def test_body_target_resolves_at_execution_time_not_compile_time(
    ready: TestClient, emulator: SeestarEmulator
) -> None:
    """A plan naming the Moon carries no coordinates at all (DESIGN.md 3.6) —
    they exist only once the item is actually issued
    (``EphemerisTargetResolver``, wired into ``RunService`` 2026-09-10;
    without it this item fails loudly with "no target resolver was
    supplied" rather than slewing to a guess). The run starts and completes
    within this test, so comparing the emulator's received coordinates
    against a *live* resolution bracketing the call is what tells "resolved
    at execution time" apart from "resolved once at compile" — a compile-time
    value would be off by whatever the Moon moved in between, which the second
    half of this test measures directly.
    """
    emulator.set_arm(closed=False)
    gotos = record_calls(emulator, "start_auto_goto")
    _create(ready, _moon_plan())

    before = datetime.now(tz=UTC)
    started = _start(ready, "moon")
    assert started["status"] == "running"
    done = _wait_for(ready, lambda run: run["status"] == "completed")
    after = datetime.now(tz=UTC)
    assert _items(done)["slew"]["status"] == "done", _items(done)["slew"]["reason"]
    assert len(gotos) == 1
    sent_ra_hours = float(gotos[0]["ra"])  # type: ignore[arg-type]
    sent_dec_deg = float(gotos[0]["dec"])  # type: ignore[arg-type]

    astro = services_of(ready).astro
    assert astro.ephemeris is not None, "the ephemeris kernel must be present for this test"
    location = services_of(ready).settings.active_location()
    assert location is not None
    site = site_of(location)
    midpoint = before + (after - before) / 2

    resolved = resolve_position(
        astro.ephemeris, site, BodyTargetSpec(body=BodyId.MOON).to_ref(), midpoint
    )
    sent = EquatorialCoord(
        ra_deg=Deg(sent_ra_hours * 15.0), dec_deg=Deg(sent_dec_deg), frame=MountFrame.J2000
    )
    live = EquatorialCoord(
        ra_deg=resolved.ra_j2000_deg, dec_deg=resolved.dec_j2000_deg, frame=MountFrame.J2000
    )
    live_offset_arcsec = frame_offset_arcsec(sent, live)
    assert live_offset_arcsec < 30.0, (
        f"slew target was {live_offset_arcsec} arcsec from the Moon's own live position; "
        "resolution did not happen at (or near) execution time"
    )

    # And: what a compile-time resolution would have sent — this same body,
    # resolved 6h before the instant the slew actually went out — is a real,
    # arcminute-scale distance away, not a rounding difference. That is the
    # number "resolved at execution time" is actually buying.
    stale = resolve_position(
        astro.ephemeris,
        site,
        BodyTargetSpec(body=BodyId.MOON).to_ref(),
        midpoint - timedelta(hours=6),
    )
    stale_coord = EquatorialCoord(
        ra_deg=stale.ra_j2000_deg, dec_deg=stale.dec_j2000_deg, frame=MountFrame.J2000
    )
    drift_arcmin = frame_offset_arcsec(live, stale_coord) / 60.0
    assert drift_arcmin > 60.0, (
        "expected a real, arcminute-scale 6h drift to distinguish 'resolved now' from "
        f"'resolved at compile time', got {drift_arcmin:.1f}'"
    )


def test_a_second_start_while_one_is_executing_is_a_409(ready: TestClient) -> None:
    _create(ready, _waiting_plan())
    first = _start(ready, "waiting")
    response = ready.post("/api/runs", json={"plan_id": "waiting"})
    assert response.status_code == 409, response.text
    assert first["run_id"] in response.json()["detail"]
    assert _current(ready) is not None
    assert _current(ready)["run_id"] == first["run_id"]  # type: ignore[index]


def test_controls_change_the_snapshot(ready: TestClient) -> None:
    _create(ready, _waiting_plan())
    run = _start(ready, "waiting")
    run_id = run["run_id"]
    waiting = _wait_for(ready, lambda r: r["journal_length"] >= 1)
    assert _items(waiting)["startup"]["status"] == "waiting"
    assert waiting["current_item_id"] == "startup"

    paused = ready.post(f"/api/runs/{run_id}/pause", json={"reason": "clouds rolling in"})
    assert paused.status_code == 200, paused.text
    section = _wait_for(ready, lambda r: r["status"] == "paused")
    assert section["last_decision"] == "clouds rolling in"
    assert "resume" in section["available_actions"]

    assert ready.post(f"/api/runs/{run_id}/resume").status_code == 200
    _wait_for(ready, lambda r: r["status"] == "running")

    skipped = ready.post(f"/api/runs/{run_id}/skip", json={"item_id": "startup"})
    assert skipped.status_code == 200, skipped.text
    section = _wait_for(ready, lambda r: _items(r)["startup"]["status"] == "skipped")
    assert section["current_item_id"] == "shutdown"
    assert _items(section)["shutdown"]["status"] == "waiting"

    stopped = ready.post(f"/api/runs/{run_id}/stop", json={"reason": "done for tonight"})
    assert stopped.status_code == 200, stopped.text
    section = _wait_for(ready, lambda r: r["status"] == "stopped")
    assert _items(section)["shutdown"]["status"] == "aborted"
    assert _items(section)["shutdown"]["reason"] == "done for tonight"
    assert section["ended_at"] is not None
    assert section["available_actions"] == []

    # A stop parks; Park is NotImplemented on 8.46 and the journal says so
    # in the driver's words rather than pretending the mount parked.
    journal = ready.get(f"/api/runs/{run_id}").json()["journal"]
    park = [e for e in journal if e["control"] == "stop" and e["effect_outcome"] is not None]
    assert len(park) == 1
    assert park[0]["effect_outcome"] == "failed"
    assert "scope_park" in park[0]["reason"]

    # Nothing is executing any more: a control is a 409, a new start is allowed.
    assert ready.post(f"/api/runs/{run_id}/pause").status_code == 409
    assert _start(ready, "waiting")["status"] == "running"


def test_a_control_for_a_run_that_is_not_executing_is_a_409(ready: TestClient) -> None:
    assert ready.post("/api/runs/nope/pause").status_code == 409
    assert ready.get("/api/runs/nope").status_code == 404


# -- interruption ------------------------------------------------------------------- #


async def _seed_unfinished_run(config: AppConfig, run_id: str) -> None:
    """What a killed process leaves behind: a ``running`` row and a journal
    that stops mid-run."""
    database = await open_database(config.database_path)
    try:
        await migrate(database)
        repo = RunRepository(database)
        plan = build_plan()
        await repo.create(
            run_id=run_id,
            plan=plan,
            plan_revision=1,
            schedule=build_schedule(plan),
            started_at=BASE_UTC,
        )
        await repo.journal.append(
            JournalRecord(
                at=BASE_UTC, run_id=run_id, kind=RecordKind.RUN_STARTED, reason="run started"
            )
        )
    finally:
        await database.aclose()


def test_an_unfinished_run_is_surfaced_as_interrupted_and_resumable(
    config: AppConfig, emulator: SeestarEmulator, socket_enabled: None
) -> None:
    del socket_enabled
    asyncio.run(_seed_unfinished_run(config, "left-behind"))

    app = create_app(config, alpaca_runtime=EmulatedRuntime(emulator))
    with TestClient(app, client=CLIENT_ADDRESS) as client:
        client.post("/api/auth/pair", json={"token": pairing_token(client)})
        # Surfaced before any client asked, with exactly one action offered.
        run = _state(client)["snapshot"]["run"]
        assert run is not None
        assert run["run_id"] == "left-behind"
        assert run["status"] == "interrupted"
        assert run["available_actions"] == ["resume_interrupted"]
        assert run["journal_length"] == 1
        assert "stopped while this run was in progress" in run["ended_reason"]

        # A control on it is a 409: nothing is executing.
        assert client.post("/api/runs/left-behind/pause").status_code == 409
        # Resuming needs a location, like starting does.
        assert client.post("/api/runs/left-behind/resume-interrupted").status_code == 409
        _set_location(client, timezone="Europe/London")

        resumed = client.post("/api/runs/left-behind/resume-interrupted")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["run"]["status"] == "running"
        # The fixture schedule is in the past and the driver is unaddressed, so
        # unavailable operations are logged and expired windows settle without terminal failure — the journal grew
        # from where it was left, it was not restarted.
        done = _wait_for(client, lambda r: r["status"] == "completed")
        assert done["journal_length"] > 1
        journal = client.get("/api/runs/left-behind").json()["journal"]
        assert [e["kind"] for e in journal].count("run_started") == 1
        assert _items(done)["startup"]["status"] == "done"
        assert any("no device address is configured" in entry["reason"] for entry in journal)
        # Resumed once; a second resume is refused.
        assert client.post("/api/runs/left-behind/resume-interrupted").status_code == 409


# -- the snapshot contract ---------------------------------------------------------- #


def test_run_events_replay_to_the_fresh_snapshot(ready: TestClient) -> None:
    """The reconnect check (CLAUDE.md invariant 4) for this section: every
    ``run.changed`` a client applies must land it on exactly the server's
    snapshot."""
    _create(ready, _waiting_plan())
    with ready.websocket_connect("/ws") as websocket:
        hello = json.loads(websocket.receive_text())
        reconstructed = StateSnapshot.model_validate(hello["snapshot"])
        epoch, seq = hello["epoch"], hello["seq"]
    run_id = _start(ready, "waiting")["run_id"]
    _wait_for(ready, lambda r: r["journal_length"] >= 1)
    assert ready.post(f"/api/runs/{run_id}/pause").status_code == 200
    _wait_for(ready, lambda r: r["status"] == "paused")
    assert ready.post(f"/api/runs/{run_id}/stop").status_code == 200
    _wait_for(ready, lambda r: r["status"] == "stopped")

    with ready.websocket_connect(f"/ws?seq={seq}&epoch={epoch}") as websocket:
        fresh = _state(ready)
        frames: list[dict[str, Any]] = []
        while not frames or frames[-1]["seq"] < fresh["seq"]:
            frames.append(json.loads(websocket.receive_text()))
    assert {f["type"] for f in frames} == {"event"}
    assert {f["event"]["type"] for f in frames} >= {"run.changed"}
    reconstructed = replay(
        reconstructed, [STATE_EVENT_ADAPTER.validate_python(f["event"]) for f in frames]
    )
    assert reconstructed.model_dump(mode="json") == fresh["snapshot"]
    assert fresh["snapshot"]["run"]["status"] == "stopped"
    assert datetime.fromisoformat(fresh["snapshot"]["run"]["ended_at"]).tzinfo is not None
    assert datetime.now(tz=UTC) > datetime.fromisoformat(fresh["snapshot"]["run"]["started_at"])


def test_expired_night_is_rejected_without_creating_a_run(ready: TestClient) -> None:
    _create(ready, _waiting_plan())
    response = ready.post("/api/runs", json={"plan_id": "waiting", "night_date": "2026-01-01"})
    assert response.status_code == 409, response.text
    assert "already ended" in response.json()["detail"]
    assert services_of(ready).runs.active is None
    assert services_of(ready).state.snapshot.run is None


def test_stop_interrupts_scheduled_autofocus_after_durable_stop(ready: TestClient) -> None:
    import asyncio

    from ssc.scheduler.executor import ControlKind

    services = services_of(ready)
    device = services.runs._device
    assert device is not None
    cleanup_saw_stop: list[bool] = []

    async def stuck_focus() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            records = await services.runs._publishing_journal.read_all(services.runs.active.run_id)
            cleanup_saw_stop.append(any(record.control is ControlKind.STOP for record in records))

    device.set_autofocus_runner(stuck_focus)
    _create(ready, _plan("focus-stop", [{"kind": "autofocus", "id": "focus", "start": {
        "kind": "absolute", "local_datetime": f"{NIGHT}T21:00:00"}}]))
    started = _start(ready, "focus-stop")
    _wait_for(ready, lambda _: device.autofocus_running)
    response = ready.post(f"/api/runs/{started['run_id']}/stop", json={"reason": "owner Stop"})
    assert response.status_code == 200, response.text
    done = _wait_for(ready, lambda r: r["status"] == "stopped")
    assert cleanup_saw_stop == [True]
    assert _items(done)["focus"]["status"] == "aborted"
    assert not device.autofocus_running


def test_stop_journals_before_cancelling_scheduled_calibration(ready: TestClient) -> None:
    from ssc.scheduler.executor import ControlKind

    services = services_of(ready)
    device = services.runs._device
    assert device is not None
    finished = asyncio.Event()
    cleanup_saw_stop = []

    async def waiting_calibration(**_options):
        services.capture._publish(calibration=services.capture.section.calibration.model_copy(
            update={"status": "running", "kind": "dark"}
        ))
        await finished.wait()

    async def cancel_after_stop():
        records = await services.runs._publishing_journal.read_all(services.runs.active.run_id)
        cleanup_saw_stop.append(any(r.control is ControlKind.STOP for r in records))
        finished.set()

    device.set_calibration_runner(waiting_calibration)
    services.runs._calibration_canceller = cancel_after_stop
    _create(ready, _plan("calibration-stop", [{"kind": "capture_calibration", "id": "dark",
        "safe_state": False, "generate_master": False, "start": {
            "kind": "absolute", "local_datetime": f"{NIGHT}T21:00:00"}}]))
    started = _start(ready, "calibration-stop")
    _wait_for(ready, lambda _: services.capture.section.calibration.status == "running")
    try:
        response = ready.post(f"/api/runs/{started['run_id']}/stop", json={"reason": "owner Stop"})
        assert response.status_code == 200, response.text
        assert cleanup_saw_stop == [True]
        assert response.json()["run"]["status"] == "stopped"
    finally:
        ready.portal.call(finished.set)
    done = _wait_for(ready, lambda r: r["status"] == "stopped")
    assert _items(done)["dark"]["status"] == "aborted"


def test_run_stop_does_not_cancel_manual_batch_during_calibration_preparation(ready: TestClient) -> None:
    from unittest.mock import AsyncMock

    services = services_of(ready)
    device = services.runs._device
    assert device is not None
    entered = asyncio.Event()
    release = asyncio.Event()

    async def waiting_filter(_position):
        entered.set()
        await release.wait()

    device.set_filter = waiting_filter
    canceller = AsyncMock()
    services.runs._calibration_canceller = canceller
    services.capture._publish(calibration=services.capture.section.calibration.model_copy(
        update={"status": "running", "kind": "dark"}
    ))
    _create(ready, _plan("manual-calibration-stop", [{"kind": "capture_calibration", "id": "dark",
        "safe_state": True, "generate_master": False, "start": {
            "kind": "absolute", "local_datetime": f"{NIGHT}T21:00:00"}}]))
    started = _start(ready, "manual-calibration-stop")
    _wait_for(ready, lambda _: entered.is_set())
    try:
        response = ready.post(f"/api/runs/{started['run_id']}/stop", json={"reason": "owner Stop"})
        assert response.status_code == 200, response.text
        canceller.assert_not_awaited()
    finally:
        ready.portal.call(release.set)
    _wait_for(ready, lambda r: r["status"] == "stopped")
