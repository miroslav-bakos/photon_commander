# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``ssc.api.run_service.RunService`` driving a ``take_exposures`` block end
to end (dispatch: wiring a real ``CapturePort`` into ``run(...)`` at
``ssc/api/run_service.py:394``, the seam ``scheduler-engine`` left named in
``74fa1c9``).

Swaps ``services.runs`` for a ``RunService`` built with a fake
``DeviceCommands``/``DeviceEvents`` pair (``tests.scheduler.fakes`` — the
mount side of this is not the point of this dispatch) and a real
``AlpacaCapturePort`` over a fake camera/telescope/filterwheel plus the app's
own sqlite handle, exactly the "swap the one service under test" pattern
``tests.api.test_capture_routes`` uses for the manual capture route.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ssc.api.deps import Services
from ssc.api.device_commands import AlpacaCapturePort
from ssc.api.run_service import RunService
from ssc.store.capture import CaptureRepository
from ssc.store.runs import RunRepository
from tests.api.conftest import TestClient, services_of
from tests.api.test_capture_routes import (
    FakeCamera,
    _camera,
    _configure_folder,
    _filterwheel,
    _image_frame,
    _telescope,
)
from tests.api.test_plans import requires_ephemeris
from tests.scheduler.fakes import FakeClock, FakeDeviceCommands, FakeDeviceEvents

pytestmark = requires_ephemeris

NIGHT = "2026-08-28"


@pytest.fixture(autouse=True)
def exposure_night_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep preflight, execution and restart within the real compiled night."""

    class ExposureClock(FakeClock):
        async def sleep_until(self, when: datetime) -> None:
            await super().sleep_until(when)
            await asyncio.sleep(0)

    clock = ExposureClock(datetime(2026, 8, 28, 19, tzinfo=UTC))
    monkeypatch.setattr("ssc.api.run_service.WallClock", lambda wake=None: clock)


def test_focus_quality_warning_and_new_target_reset(paired: TestClient) -> None:
    services = services_of(paired)
    commands = FakeDeviceCommands()
    quality = (Path("baseline-0.fits"), 2.0, 30)

    async def recenter(ra: float, dec: float) -> None:
        pass

    port = AlpacaCapturePort(
        run_id="quality-monitor",
        device_commands=commands,
        camera=None,
        telescope=None,
        filterwheel=None,
        repo=CaptureRepository(services.database),
        settings=services.settings,
        state=services.state,
        recenter=recenter,
        focus_quality=lambda: quality,
    )
    assert paired.portal is not None
    for frame in range(3):
        quality = (Path(f"baseline-{frame}.fits"), 2.0, 30)
        assert not paired.portal.call(port.focus_required)
    for frame in range(3):
        quality = (Path(f"cloud-{frame}.fits"), 3.5, 30)
        assert paired.portal.call(port.focus_required) is (frame == 2)
    assert any(
        notice.id == "scheduled-focus-recovery" for notice in services.state.snapshot.notices
    )
    paired.portal.call(port.autofocus)
    assert not paired.portal.call(port.focus_required)

    async def acquire_target() -> None:
        await port.center_target(ra_j2000_h=5.5, dec_j2000_deg=-5.4)

    paired.portal.call(acquire_target)
    assert not paired.portal.call(port.focus_required)
    # The previous field's final cloudy frame must not count for the new field.
    for frame in range(2):
        quality = (Path(f"new-field-{frame}.fits"), None, 0)
        assert not paired.portal.call(port.focus_required)


def _exposure_plan(*, frames: int = 3) -> dict[str, object]:
    """startup -> slew -> a small fixed-count ``take_exposures`` block, all
    already due, mirroring ``tests.fixtures.scheduler.golden_exposure_plan``'s
    own item shapes."""
    return {
        "schema_version": 3,
        "id": "exposures",
        "name": "exposures",
        "created_at": "2026-08-01T12:00:00Z",
        "updated_at": "2026-08-01T12:00:00Z",
        "items": [
            {
                "kind": "startup",
                "id": "startup",
                "start": {"kind": "absolute", "local_datetime": f"{NIGHT}T21:00:00"},
            },
            {
                "kind": "slew",
                "id": "slew",
                "start": {"kind": "relative", "offset_min": 0},
                "target": {
                    "kind": "catalog",
                    "name": "M42",
                    "ra_j2000_h": 5.5,
                    "dec_j2000_deg": -5.4,
                },
            },
            {
                "kind": "take_exposures",
                "id": "lights",
                "start": {"kind": "relative", "offset_min": 0},
                "count": {"kind": "fixed", "frames": frames},
                "exposure_sec": 0.01,
                "gain": 100,
                "frame_type": "light",
            },
        ],
    }


def _install_fake_run_service(
    client: TestClient, *, camera: FakeCamera | None = None
) -> tuple[Services, FakeDeviceCommands]:
    """Swap ``services.runs`` for one wired the way ``ssc.main`` wires the real
    one — a device port, an events port, and ``capture_factory`` — except the
    device/events ports are the in-memory fakes ``tests.scheduler``'s own
    crash-injection suite uses, since exercising the mount side over a real
    driver is not this test's concern (``ssc.api.device_commands.AlpacaCapturePort``
    is)."""
    services = services_of(client)
    device_commands = FakeDeviceCommands()
    events = FakeDeviceEvents()
    repo = CaptureRepository(services.database)
    fake_camera = camera if camera is not None else _camera(frame=_image_frame())

    def capture_factory(run_id: str) -> AlpacaCapturePort:
        return AlpacaCapturePort(
            run_id=run_id,
            device_commands=device_commands,
            camera=fake_camera,
            telescope=_telescope(),
            filterwheel=_filterwheel(),
            repo=repo,
            settings=services.settings,
            state=services.state,
        )

    services.runs = RunService(
        runs=RunRepository(services.database),
        plans=services.plans,
        state=services.state,
        tasks=services.tasks,
        astro=services.astro,
        weather=services.weather,
        settings=services.settings,
        device=device_commands,
        events=events,
        capture_factory=capture_factory,
    )
    return services, device_commands


def _create(client: TestClient, document: dict[str, object]) -> None:
    response = client.post("/api/plans", json={"document": document})
    assert response.status_code == 201, response.text


def _start(client: TestClient, plan_id: str) -> dict[str, Any]:
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
    client: TestClient, predicate: Callable[[dict[str, Any]], bool], *, timeout_s: float = 15.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = _current(client)
        if last is not None and predicate(last):
            return last
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout_s}s; last section: {last}")


def _items(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["item_id"]: item for item in run["items"]}


def test_take_exposures_block_shoots_saves_and_completes(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera(frame=_image_frame())
    _install_fake_run_service(paired, camera=camera)
    _create(paired, _exposure_plan(frames=3))

    started = _start(paired, "exposures")
    assert started["status"] == "running"

    done = _wait_for(paired, lambda run: run["status"] == "completed")
    assert _items(done)["lights"]["status"] == "done"
    assert done["ended_reason"] == "all items settled"

    # Startup shoots one disposable confirmation frame before this block's
    # three retained lights, all in order and one ``Camera.StartExposure`` each.
    assert camera.start_exposure_calls[0] == (10.0, True)
    assert camera.start_exposure_calls[1:] == [(0.01, True)] * 3
    assert all(light for _duration_s, light in camera.start_exposure_calls)


def test_take_exposures_block_catalogues_every_frame_durably(
    paired: TestClient, tmp_path: Path
) -> None:
    """The catalogue route reads through the app's own ``CaptureService``, a
    second ``CaptureRepository`` wrapper over the same sqlite handle
    ``AlpacaCapturePort`` wrote through — proof the rows are durable in the
    shared store, not held anywhere private to the port."""
    _configure_folder(paired, tmp_path)
    _install_fake_run_service(paired)
    _create(paired, _exposure_plan(frames=3))
    _start(paired, "exposures")
    _wait_for(paired, lambda run: run["status"] == "completed")

    response = paired.get("/api/capture/frames")
    assert response.status_code == 200, response.text
    frames = response.json()["frames"]
    assert len(frames) == 3
    assert {frame["frame_type"] for frame in frames} == {"light"}


class _CrashingCamera(FakeCamera):
    """Simulates a process killed mid-block: the ``crash_after``-th
    ``StartExposure`` raises ``CancelledError`` before it is even recorded —
    exactly what a hard kill leaves behind (no row, because
    ``CaptureRepository.begin_frame`` for that frame is never reached
    either), as opposed to the two frames before it, which completed and
    were committed."""

    def __init__(self, *, crash_after: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._crash_after = crash_after

    async def start_exposure(self, duration_s: float, *, light: bool = True) -> None:
        if len(self.start_exposure_calls) >= self._crash_after:
            raise asyncio.CancelledError("simulated process kill mid-block")
        await super().start_exposure(duration_s, light=light)


def _crashing_camera(*, crash_after: int) -> _CrashingCamera:
    """A ``_CrashingCamera`` with the same measured properties ``_camera()``
    hands out, since ``_CrashingCamera.__init__`` cannot go through that
    factory directly (it builds a plain ``FakeCamera``)."""
    base = _camera()
    return _CrashingCamera(
        properties=dict(base.properties),
        frame=base.frame,
        ready_after_polls=base.ready_after_polls,
        crash_after=crash_after,
    )


def test_take_exposures_block_resumes_from_the_catalogue_not_a_recount(
    paired: TestClient, tmp_path: Path
) -> None:
    """Kill the process two frames into a five-frame block, "restart" it (a
    fresh ``RunService`` over the same database — no memory of anything the
    crashed one did), and resume: the resumed process must shoot frame
    indices 2, 3, 4 — the catalogue's own count — never re-shoot 0 or 1, and
    never invent its own recount."""
    _configure_folder(paired, tmp_path)
    services = services_of(paired)
    # Startup consumes one disposable confirmation exposure before the two
    # retained block frames whose durable recovery is under test.
    crashing_camera = _crashing_camera(crash_after=3)
    _install_fake_run_service(paired, camera=crashing_camera)
    _create(paired, _exposure_plan(frames=5))
    started = _start(paired, "exposures")
    run_id = started["run_id"]

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if len(paired.get("/api/capture/frames").json()["frames"]) >= 2:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("the first two frames never landed")
    # The task raises on its next attempt (frame index 2) and unwinds —
    # give it a moment to actually finish (``RunService.active`` requires
    # ``self._task.done()``).
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and services.runs.active is not None:
        time.sleep(0.02)
    assert services.runs.active is None, "the crashed run's task never finished unwinding"
    assert len(crashing_camera.start_exposure_calls) == 3

    # A brand-new ``RunService`` — no in-memory ``_active``, nothing but the
    # database — is what a restarted process looks like. Before its own
    # ``startup()`` runs, the DB row it inherits is still 'running': exactly
    # what a killed process (never cleanly stopped) leaves behind.
    resumed_camera = _camera(frame=_image_frame())
    _install_fake_run_service(paired, camera=resumed_camera)
    left_behind = paired.get(f"/api/runs/{run_id}")
    assert left_behind.status_code == 200, left_behind.text
    assert left_behind.json()["run"]["status"] == "running"

    assert paired.portal is not None
    paired.portal.call(services.runs.startup)
    surfaced = paired.get(f"/api/runs/{run_id}")
    assert surfaced.json()["run"]["status"] == "interrupted"

    resumed = paired.post(f"/api/runs/{run_id}/resume-interrupted")
    assert resumed.status_code == 200, resumed.text

    done = _wait_for(paired, lambda run: run["status"] == "completed")
    assert _items(done)["lights"]["status"] == "done"

    # Exactly three more frames — indices 2, 3 and 4 — never a re-shoot of
    # the two the catalogue already held.
    assert len(resumed_camera.start_exposure_calls) == 3

    response = paired.get("/api/capture/frames")
    assert response.status_code == 200, response.text
    assert len(response.json()["frames"]) == 5
