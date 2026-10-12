# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``ssc.api.device_commands.AlpacaCapturePort`` — the ``CapturePort``
(``ssc.scheduler.executor``) a scheduled ``take_exposures`` block drives
(dispatch: wiring a real capture port into ``ssc.api.run_service.run``).

Drives the adapter directly against the same fake camera/telescope/
filterwheel ``tests.api.test_capture_routes`` uses for the manual capture
route, plus a real ``CaptureRepository`` over an isolated sqlite handle — the
durability the executor's resume depends on (DESIGN.md 4.3.0: "resume counts
frames, not items") is a property of that table, never of anything this
adapter keeps in memory, which is exactly what
``test_frames_captured_survives_a_fresh_instance`` (a second, independent
``AlpacaCapturePort`` — a process restart, without actually restarting a
process) checks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ssc.api.device_commands import FIRMWARE_8_46_SIGNALS, AlpacaCapturePort, OnReturn
from ssc.astro.frames import j2000_coord
from ssc.astro.units import Deg
from ssc.capture import FrameType
from ssc.client.errors import AlpacaErrorNumber, AlpacaRemoteError
from ssc.platesolve import PlateSolveResult, SolverAvailability
from ssc.scheduler.executor import (
    DeviceCommandError,
    DeviceCommandNotImplementedError,
    FrameCountError,
)
from ssc.settings.store import SettingsStore
from ssc.state.models import CaptureStage, StateSnapshot
from ssc.state.store import StateStore
from ssc.store.capture import CaptureRepository
from ssc.store.database import Database, open_database
from ssc.store.migrations import migrate
from tests.api.test_capture_routes import FakeCamera, FakeDevice, _camera, _filterwheel, _telescope
from tests.scheduler.fakes import FakeDeviceCommands

FIXED_NOW = datetime(2026, 9, 12, 22, 0, 0, tzinfo=UTC)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    handle = await open_database(tmp_path / "ssc.sqlite3")
    try:
        yield handle
    finally:
        await handle.aclose()


@pytest.fixture
async def migrated(database: Database) -> Database:
    await migrate(database)
    return database


@pytest.fixture
def repo(migrated: Database) -> CaptureRepository:
    return CaptureRepository(migrated)


@pytest.fixture
async def settings(migrated: Database, tmp_path: Path) -> SettingsStore:
    store = SettingsStore(migrated)
    await store.load()
    # No ``%DATEOFCAPTURE%`` tag: no observing location has to be configured
    # just to resolve a folder-template timezone this test does not exercise
    # (``ssc.api.capture_orchestrator.CaptureService._resolve_template_timezone``'s
    # own precedent).
    await store.set_capture_folder_template("%TARGET%/%IMAGETYPE%")
    folder = tmp_path / "captures"
    folder.mkdir()
    await store.set_capture_folder(folder)
    return store


@pytest.fixture
def state() -> StateStore:
    return StateStore(StateSnapshot(server_started_at=FIXED_NOW))


def _filterwheel_named(name: str) -> FakeDevice:
    return FakeDevice(properties={"position": 0, "names": [name]})


def _port(
    *,
    repo: CaptureRepository,
    settings: SettingsStore,
    state: StateStore,
    run_id: str = "run-1",
    camera: FakeCamera | None = None,
    filterwheel: FakeDevice | None = None,
    device_commands: FakeDeviceCommands | None = None,
    recenter: Callable[[float, float], Awaitable[None]] | None = None,
    solve: Callable[[Path], Awaitable[PlateSolveResult]] | None = None,
    availability: Callable[[], Awaitable[SolverAvailability]] | None = None,
    reset_live_stack: Callable[[Path], object] | None = None,
    add_live_stack_frame: Callable[[int], Awaitable[object]] | None = None,
    stop_live_stack: Callable[..., Awaitable[object]] | None = None,
    dither: Callable[[float, float, str | None], Awaitable[object]] | None = None,
    begin_capture: Callable[..., object] | None = None,
    update_capture_stage: Callable[[CaptureStage], object] | None = None,
    complete_capture: Callable[[int], object] | None = None,
    fail_capture: Callable[[Exception], object] | None = None,
    set_dither_center: Callable[[float, float], None] | None = None,
) -> AlpacaCapturePort:
    kwargs: dict[str, object] = {}
    if solve is not None:
        kwargs["solve"] = solve
    if availability is not None:
        kwargs["availability"] = availability
    return AlpacaCapturePort(
        run_id=run_id,
        device_commands=device_commands or FakeDeviceCommands(),
        camera=camera if camera is not None else _camera(),
        telescope=_telescope(),
        filterwheel=filterwheel if filterwheel is not None else _filterwheel(),
        repo=repo,
        settings=settings,
        state=state,
        recenter=recenter,
        reset_live_stack=reset_live_stack,
        add_live_stack_frame=add_live_stack_frame,
        stop_live_stack=stop_live_stack,
        dither=dither,
        begin_capture=begin_capture,
        update_capture_stage=update_capture_stage,
        complete_capture=complete_capture,
        fail_capture=fail_capture,
        set_dither_center=set_dither_center,
        **kwargs,
    )


def test_frame_saved_completes_on_return_not_a_pushed_event() -> None:
    """The commit boundary: by the time ``expose_frame`` returns, the
    catalogue row is already 'complete' (see the class docstring), so there
    is nothing left to wait for — mapped as ``OnReturn``, not a pushed event
    firmware 8.46 never sends."""
    assert isinstance(FIRMWARE_8_46_SIGNALS["frame_saved"], OnReturn)


async def test_expose_frame_commits_a_row_frames_captured_can_count(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    port = _port(repo=repo, settings=settings, state=state, run_id="run-1")
    assert await port.frames_captured(run_id="run-1", item_id="lights") == 0

    await port.expose_frame(
        exposure_sec=0.01,
        gain=100,
        frame_type="light",
        filter_name="LP",
        block_id="lights",
        frame_index=0,
    )
    assert await port.frames_captured(run_id="run-1", item_id="lights") == 1

    frames = await repo.list_recent(limit=10)
    assert len(frames) == 1
    assert frames[0].status == "complete"
    assert frames[0].frame_type == "light"


async def test_scheduled_capture_retries_same_uncommitted_frame_after_disconnect(
    repo: CaptureRepository,
    settings: SettingsStore,
    state: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReconnectingCamera(FakeCamera):
        async def image_ready(self) -> bool:
            self.image_ready_calls += 1
            if self.image_ready_calls <= 4:
                raise AlpacaRemoteError(
                    int(AlpacaErrorNumber.NOT_CONNECTED),
                    "connect timed out after 5s with no response (ConnectTimeout)",
                    member="Camera.ImageReady",
                )
            return True

    monkeypatch.setattr("ssc.api.device_commands.SCHEDULED_CAPTURE_RETRY_DELAY_S", 0.0)
    base = _camera()
    camera = ReconnectingCamera(
        frame=base.frame,
        connected=True,
        properties=base.properties,
    )
    port = _port(repo=repo, settings=settings, state=state, camera=camera)

    with pytest.raises(DeviceCommandError, match="ConnectTimeout"):
        await port.expose_frame(
            exposure_sec=0.0,
            gain=100,
            frame_type="light",
            filter_name="LP",
            block_id="lights",
            frame_index=0,
        )
    # The adapter must not start another exposure before durable retry admission.
    assert camera.start_exposure_calls == [(0.0, True)]
    assert await port.frames_captured(run_id="run-1", item_id="lights") == 0
    await port.expose_frame(
        exposure_sec=0.0,
        gain=100,
        frame_type="light",
        filter_name="LP",
        block_id="lights",
        frame_index=0,
    )

    assert camera.start_exposure_calls == [(0.0, True), (0.0, True)]
    assert await port.frames_captured(run_id="run-1", item_id="lights") == 1
    assert len(await repo.list_complete()) == 1


async def test_each_scheduled_panel_starts_a_fresh_live_stack(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    events: list[tuple[str, int | None]] = []

    def reset_stack(_first_frame: Path) -> None:
        events.append(("reset", None))

    async def add_frame(frame_id: int) -> None:
        events.append(("add", frame_id))

    async def stop_stack() -> None:
        events.append(("stop", None))

    port = _port(
        repo=repo,
        settings=settings,
        state=state,
        reset_live_stack=reset_stack,
        add_live_stack_frame=add_frame,
        stop_live_stack=stop_stack,
    )

    async def expose(index: int) -> None:
        await port.expose_frame(
            exposure_sec=0.01,
            gain=100,
            frame_type="light",
            filter_name="LP",
            block_id="mosaic",
            frame_index=index,
            live_stack=True,
        )

    await expose(0)
    await expose(1)
    await port.stop_live_stack()
    await expose(2)

    assert [kind for kind, _ in events] == ["reset", "add", "add", "stop", "reset", "add"]


async def test_scheduled_live_stack_exposure_publishes_capture_screen_state(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    events: list[tuple[str, object]] = []

    def begin_capture(**kwargs: object) -> None:
        events.append(("begin", kwargs))

    def update_stage(stage: CaptureStage) -> None:
        events.append(("stage", stage))

    def complete_capture(frame_id: int) -> None:
        events.append(("complete", frame_id))

    async def add_frame(frame_id: int) -> None:
        events.append(("add", frame_id))

    port = _port(
        repo=repo,
        settings=settings,
        state=state,
        reset_live_stack=lambda _first_frame: events.append(("reset", None)),
        add_live_stack_frame=add_frame,
        begin_capture=begin_capture,
        update_capture_stage=update_stage,
        complete_capture=complete_capture,
    )

    await port.expose_frame(
        exposure_sec=0.01,
        gain=200,
        frame_type="light",
        filter_name="LP",
        block_id="mosaic-panel-2",
        frame_index=0,
        object_name="Sh2-131 2",
        live_stack=True,
    )

    first_kind, first_payload = events[0]
    assert first_kind == "begin"
    assert first_payload == {
        "duration_s": 0.01,
        "gain": 200,
        "object_name": "Sh2-131 2",
        "frame_type": FrameType.LIGHT,
    }
    assert ("stage", CaptureStage.SAVING) in events
    assert [kind for kind, _payload in events[-3:]] == ["complete", "reset", "add"]


async def test_stop_live_stack_clears_existing_manual_stack_before_a_scheduled_panel(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    stops = 0

    async def stop_stack() -> None:
        nonlocal stops
        stops += 1

    port = _port(repo=repo, settings=settings, state=state, stop_live_stack=stop_stack)

    await port.stop_live_stack()

    assert stops == 1


async def test_scheduled_dither_rebaselines_drift_instead_of_being_corrected_away(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    positions = [
        (10.0, 20.0),
        (10.01, 20.0),  # intentional dither, far beyond 30 arcseconds
        (10.02, 20.0),  # subsequent uncommanded drift
    ]
    recentered: list[tuple[float, float]] = []
    planned_anchors: list[tuple[float, float]] = []

    async def available() -> SolverAvailability:
        return SolverAvailability(available=True, version="test-1")

    async def solve(_path: Path) -> PlateSolveResult:
        ra_h, dec_deg = positions.pop(0)
        return PlateSolveResult(
            coord=j2000_coord(Deg(ra_h * 15.0), Deg(dec_deg)),
            date_obs=FIXED_NOW,
            pixel_scale_arcsec=3.6,
            rotation_deg=0.0,
            solver="fake",
            solver_version="1",
        )

    async def recenter(ra_h: float, dec_deg: float) -> None:
        recentered.append((ra_h, dec_deg))

    async def dither(_pixels: float, _max_pixels: float, _object_name: str | None) -> None:
        return None

    port = _port(
        repo=repo,
        settings=settings,
        state=state,
        recenter=recenter,
        solve=solve,
        availability=available,
        dither=dither,
        set_dither_center=lambda ra, dec: planned_anchors.append((ra, dec)),
    )
    await port.center_target(ra_j2000_h=10.0, dec_j2000_deg=20.0)
    recentered.clear()

    async def expose(frame_index: int) -> None:
        await port.expose_frame(
            exposure_sec=0.01,
            gain=100,
            frame_type="light",
            filter_name="LP",
            block_id="lights",
            frame_index=frame_index,
            retain=True,
            center_after_drift=True,
            drift_threshold_pixels=1.0,
        )

    await expose(0)
    await port.dither(pixels=50.0, max_pixels=200.0, object_name="M31")
    await expose(1)
    assert recentered == [], "the intentional dither must become the new drift reference"

    await expose(2)
    assert recentered == [(10.01, 20.0)]
    assert planned_anchors == [(10.0, 20.0)], "drift correction must not replace planned dither origin"


async def test_frames_captured_scopes_by_run_and_block_not_just_status(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    """Two blocks (and two runs) writing into the same catalogue must never
    be counted into each other's total — the count that decides "shoot frame
    N next" has to be this block's alone."""
    # Distinct filter wheels so the two frames land at distinct paths — two
    # runs never actually execute at once (module docstring: "one run at a
    # time"), so this is only exercising the *count*, not a real collision.
    a = _port(
        repo=repo,
        settings=settings,
        state=state,
        run_id="run-a",
        filterwheel=_filterwheel_named("LP"),
    )
    b = _port(
        repo=repo,
        settings=settings,
        state=state,
        run_id="run-b",
        filterwheel=_filterwheel_named("SII"),
    )
    await a.expose_frame(
        exposure_sec=0.01,
        gain=100,
        frame_type="light",
        filter_name=None,
        block_id="block-1",
        frame_index=0,
    )
    await b.expose_frame(
        exposure_sec=0.01,
        gain=100,
        frame_type="light",
        filter_name=None,
        block_id="block-1",
        frame_index=0,
    )
    assert await a.frames_captured(run_id="run-a", item_id="block-1") == 1
    assert await b.frames_captured(run_id="run-b", item_id="block-1") == 1
    assert await a.frames_captured(run_id="run-a", item_id="block-2") == 0


async def test_frames_captured_survives_a_fresh_instance(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    """The crash-resume property this adapter exists to provide: a *second*,
    independent ``AlpacaCapturePort`` — standing in for a resumed process,
    which starts with no memory of anything the first one did — sees exactly
    the frames the first one committed, because the count comes from the
    catalogue, never an in-memory counter (DESIGN.md 4.3.0)."""
    first = _port(repo=repo, settings=settings, state=state, run_id="run-1")
    for frame_index in range(3):
        await first.expose_frame(
            exposure_sec=0.01,
            gain=100,
            frame_type="light",
            filter_name=None,
            block_id="lights",
            frame_index=frame_index,
        )
    assert await first.frames_captured(run_id="run-1", item_id="lights") == 3

    resumed = _port(repo=repo, settings=settings, state=state, run_id="run-1")
    assert await resumed.frames_captured(run_id="run-1", item_id="lights") == 3

    # The resumed process shoots the frame index the catalogue implies (3),
    # never one it recounted itself.
    await resumed.expose_frame(
        exposure_sec=0.01,
        gain=100,
        frame_type="light",
        filter_name=None,
        block_id="lights",
        frame_index=3,
    )
    assert await resumed.frames_captured(run_id="run-1", item_id="lights") == 4
    frames = await repo.list_recent(limit=10)
    assert len(frames) == 4


async def test_no_capture_folder_configured_fails_the_item_not_the_process(
    repo: CaptureRepository, state: StateStore, tmp_path: Path
) -> None:
    handle = await open_database(tmp_path / "unconfigured.sqlite3")
    try:
        await migrate(handle)
        unconfigured_settings = SettingsStore(handle)
        await unconfigured_settings.load()
        port = _port(repo=repo, settings=unconfigured_settings, state=state)
        with pytest.raises(DeviceCommandNotImplementedError):
            await port.expose_frame(
                exposure_sec=0.01,
                gain=100,
                frame_type="light",
                filter_name=None,
                block_id="lights",
                frame_index=0,
            )
    finally:
        await handle.aclose()


async def test_dither_with_no_amount_and_no_optic_profile_fails_loudly(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    """``pixels=None`` means "the active optic profile's dither_pixels"
    (DESIGN.md 4.3.0) — no such setting exists yet anywhere in
    ``ssc.settings``, so this must say so rather than inventing a default."""
    port = _port(repo=repo, settings=settings, state=state)
    with pytest.raises(DeviceCommandNotImplementedError):
        await port.dither(pixels=None, max_pixels=None, object_name=None)


async def test_dither_with_an_amount_uses_verified_absolute_slew_callback(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    calls: list[tuple[float, float, str | None]] = []

    async def dither(pixels: float, max_pixels: float, object_name: str | None) -> None:
        calls.append((pixels, max_pixels, object_name))

    port = _port(repo=repo, settings=settings, state=state, dither=dither)
    await port.dither(pixels=8.0, max_pixels=20.0, object_name="M31")
    assert calls == [(8.0, 20.0, "M31")]


async def test_scheduled_fits_groups_follow_successful_offsets_not_frame_count(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    from ssc.api.capture_orchestrator import CaptureDitherSettleError
    from ssc.skymap.fits import read_fits_image

    fail = False

    async def dither(pixels: float, max_pixels: float, object_name: str | None) -> None:
        if fail:
            raise CaptureDitherSettleError("still settling")

    port = _port(repo=repo, settings=settings, state=state, dither=dither)

    async def expose(index: int) -> str:
        await port.expose_frame(exposure_sec=.01, gain=100, frame_type="light",
                                filter_name=None, block_id="lights", frame_index=index,
                                dither_every_n_frames=6)
        frames = await repo.list_complete()
        newest = max(frames, key=lambda frame: frame.id)
        return str(read_fits_image(newest.path.read_bytes()).header["DITHGRP"])

    first = await expose(0)
    assert await expose(1) == first
    fail = True
    with pytest.raises(DeviceCommandError, match="still settling"):
        await port.dither(pixels=8, max_pixels=10, object_name="M31")
    assert await expose(2) == first
    fail = False
    await port.dither(pixels=8, max_pixels=10, object_name="M31")
    assert await expose(3) != first


async def test_center_target_uses_closed_loop_centering_callback(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    calls: list[tuple[float, float]] = []

    async def recenter(ra_h: float, dec_deg: float) -> None:
        calls.append((ra_h, dec_deg))

    port = _port(repo=repo, settings=settings, state=state, recenter=recenter)
    await port.center_target(ra_j2000_h=2.72375, dec_j2000_deg=61.1361)
    assert calls == [(2.72375, 61.1361)]


async def test_center_target_refuses_to_continue_without_closed_loop_centering(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    port = _port(repo=repo, settings=settings, state=state)
    with pytest.raises(DeviceCommandNotImplementedError, match="closed-loop centering"):
        await port.center_target(ra_j2000_h=2.72375, dec_j2000_deg=61.1361)


async def test_center_target_reports_centering_failure_as_device_error(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore
) -> None:
    async def fail(_ra_h: float, _dec_deg: float) -> None:
        raise RuntimeError("not enough stars")

    port = _port(repo=repo, settings=settings, state=state, recenter=fail)
    with pytest.raises(DeviceCommandError, match="not enough stars"):
        await port.center_target(ra_j2000_h=2.72375, dec_j2000_deg=61.1361)


async def test_frames_captured_wraps_a_store_failure(
    settings: SettingsStore, state: StateStore
) -> None:
    """Never a guess: a count that cannot be read becomes ``FrameCountError``,
    which is what lets the executor fail the block instead of assuming zero
    and re-shooting frames already on disk."""

    class _BrokenRepo:
        async def count_committed(self, *, run_id: str, block_id: str) -> int:
            import aiosqlite

            raise aiosqlite.OperationalError("database is locked")

    port = AlpacaCapturePort(
        run_id="run-1",
        device_commands=FakeDeviceCommands(),
        camera=_camera(),
        telescope=_telescope(),
        filterwheel=_filterwheel(),
        repo=_BrokenRepo(),  # type: ignore[arg-type]
        settings=settings,
        state=state,
    )
    with pytest.raises(FrameCountError):
        await port.frames_captured(run_id="run-1", item_id="lights")


@pytest.mark.parametrize("failure_at", ["reset", "add"])
async def test_optional_stack_failure_does_not_abort_saved_capture(
    repo: CaptureRepository, settings: SettingsStore, state: StateStore, failure_at: str
) -> None:
    def reset_stack(_first_frame: Path) -> None:
        if failure_at == "reset":
            raise RuntimeError("stack reset failed")

    async def add_frame(_frame_id: int) -> None:
        raise RuntimeError("stack was stopped")

    port = _port(
        repo=repo,
        settings=settings,
        state=state,
        reset_live_stack=reset_stack,
        add_live_stack_frame=add_frame,
    )
    for index in range(2):
        await port.expose_frame(
            exposure_sec=0.01,
            gain=100,
            frame_type="light",
            filter_name="LP",
            block_id="independent",
            frame_index=index,
            live_stack=True,
        )
    assert await port.frames_captured(run_id="run-1", item_id="independent") == 2


async def test_scheduled_capture_caches_unsupported_metadata_across_frames(repo, settings, state) -> None:
    from unittest.mock import AsyncMock

    camera = _camera()
    original = camera.get
    async def getter(member):
        if member == "lastexposurestarttime":
            raise AlpacaRemoteError(member=member, number=0x400, message="not implemented")
        return await original(member)
    camera.get = AsyncMock(side_effect=getter)
    port = _port(repo=repo, settings=settings, state=state, camera=camera)
    for index in range(2):
        await port.expose_frame(exposure_sec=.01, gain=100, frame_type="light",
            filter_name="LP", block_id="lights", frame_index=index)
    assert sum(call.args == ("lastexposurestarttime",) for call in camera.get.await_args_list) == 1
    assert await port.frames_captured(run_id="run-1", item_id="lights") == 2


async def test_wide_cleanup_block_is_recoverable_and_does_not_start_main_exposure(repo, settings, state) -> None:
    from unittest.mock import AsyncMock

    from ssc.api.capture_orchestrator import CaptureAlreadyRunningError
    camera = _camera()
    camera.start_exposure = AsyncMock()
    port = _port(repo=repo, settings=settings, state=state, camera=camera)
    port._before_capture = AsyncMock(side_effect=CaptureAlreadyRunningError("cleanup not confirmed"))
    with pytest.raises(DeviceCommandError, match="cleanup not confirmed") as error:
        await port.expose_frame(exposure_sec=60, gain=200, frame_type="light",
            filter_name="LP", block_id="lights", frame_index=0)
    assert error.value.recoverable
    camera.start_exposure.assert_not_awaited()
    assert await port.frames_captured(run_id="run-1", item_id="lights") == 0


async def test_failed_stack_save_retains_owner_and_can_retry(repo, settings, state):
    attempts = 0

    async def stop_stack():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(28, 'No space left on device')

    port = _port(repo=repo, settings=settings, state=state, stop_live_stack=stop_stack)
    port._scheduled_live_stack_active = True
    with pytest.raises(DeviceCommandError, match='No space left'):
        await port.stop_live_stack()
    assert port._scheduled_live_stack_active
    await port.stop_live_stack()
    assert not port._scheduled_live_stack_active


async def test_missing_retained_stack_is_not_reported_saved(repo, settings, state):
    from ssc.stacking.live import LiveStackError

    async def empty_service_stop(*, require_retained=False):
        if require_retained:
            raise LiveStackError('retained integration is missing after restart')

    port = _port(repo=repo, settings=settings, state=state,
        stop_live_stack=empty_service_stop)
    await port.stop_live_stack()
    with pytest.raises(DeviceCommandError, match='missing after restart'):
        await port.stop_live_stack(require_retained=True)
