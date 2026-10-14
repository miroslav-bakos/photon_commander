# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``ssc.main``'s own startup reconciliation for ``ssc.capture`` (CLAUDE.md
invariant 6). Mirrors ``tests/api/test_runs.py``'s unfinished-run test: seed
what a killed process leaves behind directly against the database file the
app is about to open, then assert the *running app's own lifespan* resolves
it — not merely that ``ssc.capture.reconcile_incomplete`` works in isolation
(``tests/capture/test_pipeline.py`` already covers the function itself).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ssc.capture.pipeline import CaptureRecoveryError
from ssc.main import create_app
from ssc.settings.config import AppConfig
from ssc.store.capture import CaptureRepository
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from tests.api.conftest import CLIENT_ADDRESS, FakeGateway, TestClient, pairing_token

DATE_OBS = datetime(2026, 9, 10, 3, 0, 0, tzinfo=UTC)


async def _seed_writing_frame(config: AppConfig) -> Path:
    """What a process killed between ``begin_frame`` and ``complete_frame``
    leaves behind: a ``'writing'`` row and a partial file at its path."""
    database = await open_database(config.database_path)
    try:
        await migrate(database)
        repo = CaptureRepository(database)
        folder = config.data_dir / "capture"
        folder.mkdir(parents=True, exist_ok=True)
        partial_path = folder / "orphan.fits"
        partial_path.write_bytes(b"not a complete fits file")
        await repo.begin_frame(
            path=partial_path,
            date_obs=DATE_OBS,
            object_name="M42",
            filter_name="LP",
            frame_type="light",
            exposure_s=30.0,
            gain=200,
            ccd_temp_c=20.0,
            width=4,
            height=4,
        )
    finally:
        await database.aclose()
    return partial_path


async def test_an_unverified_final_is_preserved_and_blocks_startup(
    config: AppConfig, gateway: FakeGateway
) -> None:
    partial_path = await _seed_writing_frame(config)
    original = partial_path.read_bytes()
    app = create_app(config, gateway=gateway)
    with pytest.raises(CaptureRecoveryError), TestClient(app, client=CLIENT_ADDRESS):
        pass
    assert partial_path.read_bytes() == original
    database = await open_database(config.database_path)
    try:
        writing = await CaptureRepository(database).list_writing()
    finally:
        await database.aclose()
    assert len(writing) == 1
    assert writing[0].path == partial_path


async def test_a_durable_final_is_recovered_before_startup_completes(
    config: AppConfig, gateway: FakeGateway
) -> None:
    from dataclasses import replace

    from ssc.capture.fits import write_fits_file
    from tests.capture.test_pipeline import _raw_frame

    final_path = await _seed_writing_frame(config)
    frame = _raw_frame()
    write_fits_file(final_path, frame.pixels, replace(frame.metadata, date_obs=DATE_OBS))
    original = final_path.read_bytes()
    app = create_app(config, gateway=gateway)
    with TestClient(app, client=CLIENT_ADDRESS) as client:
        assert client.post("/api/auth/pair", json={"token": pairing_token(client)}).status_code == 200
        snapshot = client.get("/api/state").json()["snapshot"]
    assert final_path.read_bytes() == original
    database = await open_database(config.database_path)
    try:
        repo = CaptureRepository(database)
        complete = await repo.list_complete()
        assert await repo.list_writing() == []
    finally:
        await database.aclose()
    assert len(complete) == 1
    assert snapshot["capture"]["last_frame_id"] is None  # preview remains session-local


async def test_a_complete_capture_frame_survives_startup(
    config: AppConfig, gateway: FakeGateway
) -> None:
    """The reconciliation sweep must discard only 'writing' rows — a
    completed frame from a previous, clean run is not touched."""
    database = await open_database(config.database_path)
    try:
        await migrate(database)
        repo = CaptureRepository(database)
        folder = config.data_dir / "capture"
        folder.mkdir(parents=True, exist_ok=True)
        good_path = folder / "good.fits"
        good_path.write_bytes(b"pretend fits bytes")
        frame_id = await repo.begin_frame(
            path=good_path,
            date_obs=DATE_OBS,
            object_name="M31",
            filter_name="LP",
            frame_type="light",
            exposure_s=30.0,
            gain=200,
            ccd_temp_c=20.0,
            width=4,
            height=4,
        )
        await repo.complete_frame(frame_id)
    finally:
        await database.aclose()

    app = create_app(config, gateway=gateway)
    with TestClient(app, client=CLIENT_ADDRESS) as client:
        assert client.post("/api/auth/pair", json={"token": pairing_token(client)}).status_code == 200
        assert client.get("/api/state").status_code == 200

    assert good_path.exists()
    database = await open_database(config.database_path)
    try:
        complete = await CaptureRepository(database).list_complete()
    finally:
        await database.aclose()
    assert [record.object_name for record in complete] == ["M31"]


async def test_wide_preview_does_not_touch_camera_during_main_capture() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ssc.api.capture_orchestrator import CaptureService
    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._section = SimpleNamespace(in_progress=True)
    capture._wide_camera = SimpleNamespace(start_exposure=AsyncMock(), get=AsyncMock())
    await capture._capture_wide_rtsp_frame()
    capture._wide_camera.start_exposure.assert_not_awaited()
    capture._wide_camera.get.assert_not_awaited()


async def test_wide_preview_rechecks_main_ownership_after_waiting_for_camera() -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ssc.api.capture_orchestrator import CaptureService

    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._section = SimpleNamespace(in_progress=False)
    capture._wide_capture_lock = asyncio.Lock()
    capture._capture_wide_rtsp_frame_locked = AsyncMock()
    await capture._wide_capture_lock.acquire()
    preview = asyncio.create_task(capture._capture_wide_rtsp_frame())
    await asyncio.sleep(0)
    capture._section.in_progress = True
    capture._wide_capture_lock.release()
    await preview
    capture._capture_wide_rtsp_frame_locked.assert_not_awaited()


async def test_wide_preview_does_not_touch_camera_during_auxiliary_acquisition() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ssc.api.capture_orchestrator import CaptureService

    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._section = SimpleNamespace(in_progress=False)
    import asyncio

    capture._auxiliary_camera_busy = lambda: True
    capture._wide_capture_lock = asyncio.Lock()
    capture._capture_wide_rtsp_frame_locked = AsyncMock()
    capture._wide_camera = SimpleNamespace(start_exposure=AsyncMock(), get=AsyncMock())
    await capture._capture_wide_rtsp_frame()
    capture._wide_camera.get.assert_not_awaited()
    capture._wide_camera.start_exposure.assert_not_awaited()
    capture._capture_wide_rtsp_frame_locked.assert_not_awaited()


async def test_cancelled_wide_preview_drains_abort_before_main_can_acquire() -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, call

    from ssc.api.capture_orchestrator import CaptureService

    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._section = SimpleNamespace(in_progress=False)
    capture._wide_cleanup_error = None
    capture._wide_capture_lock = asyncio.Lock()
    capture._wide_rtsp_exposure_s = 0.1
    capture._wide_rtsp_min_exposure_s = 0.1
    capture._wide_rtsp_max_exposure_s = 1.0
    started, stop_started, allow_stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def expose(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    async def stop(*args, **kwargs):
        stop_started.set()
        await allow_stop.wait()

    capture._wide_camera = SimpleNamespace(
        is_connected=AsyncMock(return_value=True), start_exposure=expose,
        put=stop, get=AsyncMock(return_value=0),
    )
    capture._sleep = asyncio.sleep
    preview = asyncio.create_task(capture._capture_wide_rtsp_frame())
    await started.wait()
    preview.cancel()
    await stop_started.wait()
    main = asyncio.create_task(capture.drain_wide_preview())
    await asyncio.sleep(0)
    assert not main.done()
    allow_stop.set()
    with pytest.raises(asyncio.CancelledError):
        await preview
    await main
    assert capture._wide_camera.get.await_args_list == [
        call("camerastate")
    ] * 2


async def test_wide_cleanup_failure_blocks_main_acquisition() -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from ssc.api.capture_orchestrator import (
        CaptureAlreadyRunningError,
        CaptureService,
        LiveStackError,
    )

    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._wide_capture_lock = asyncio.Lock()
    capture._wide_cleanup_error = None
    capture._publish = Mock()
    capture._wide_camera = SimpleNamespace(put=AsyncMock(side_effect=RuntimeError("offline")))
    with pytest.raises(LiveStackError, match="cleanup failed"):
        await capture._stop_wide_exposure()
    with pytest.raises(CaptureAlreadyRunningError, match="cleanup failed"):
        await capture.drain_wide_preview()
    capture._publish.assert_called_once()


async def test_wide_timeout_aborts_exposure_before_releasing_lock() -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, call

    from ssc.api.capture_orchestrator import CaptureService, LiveStackError

    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._section = SimpleNamespace(in_progress=False)
    capture._wide_capture_lock = asyncio.Lock()
    capture._wide_cleanup_error = None
    capture._wide_rtsp_exposure_s = 0.1
    capture._wide_rtsp_min_exposure_s = 0.1
    capture._wide_rtsp_max_exposure_s = 1.0
    capture._sleep = AsyncMock()
    capture._wide_camera = SimpleNamespace(
        is_connected=AsyncMock(return_value=True), start_exposure=AsyncMock(),
        image_ready=AsyncMock(return_value=False),
        put=AsyncMock(), get=AsyncMock(return_value=0),
    )
    with pytest.raises(LiveStackError, match="did not produce a frame"):
        await capture._capture_wide_rtsp_frame()
    capture._wide_camera.put.assert_awaited_once_with("abortexposure")
    assert capture._wide_camera.get.await_args_list == [
        call("camerastate")
    ] * 2
    assert not capture._wide_capture_lock.locked()


async def test_autofocus_guard_refuses_before_focuser_is_touched() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ssc.api.autofocus_service import AutofocusError, AutofocusService

    guard = AsyncMock(side_effect=RuntimeError("main capture active"))
    focuser = SimpleNamespace(is_connected=AsyncMock(), get=AsyncMock(), put=AsyncMock())
    service = AutofocusService(focuser=focuser, camera=None,
                               sample_quality=AsyncMock(), before_acquisition=guard)
    with pytest.raises(AutofocusError, match="main capture active"):
        await service.run()
    focuser.is_connected.assert_not_awaited()
    focuser.get.assert_not_awaited()
    focuser.put.assert_not_awaited()
    assert service.state.status == "failed"


async def test_pa_guard_refuses_before_mount_preflight() -> None:
    from unittest.mock import AsyncMock, Mock

    from ssc.api.polar_alignment_service import PolarAlignmentService
    from ssc.state.models import PolarAlignmentStatus

    service = object.__new__(PolarAlignmentService)
    service.before_acquisition = AsyncMock(side_effect=RuntimeError("camera cleanup failed"))
    service._await_mount_preflight = AsyncMock()
    service.flush_persistence = AsyncMock()
    service._publish = Mock()
    service._now = lambda: datetime.now(UTC)
    await service._run(targets=[], site=None, ephemeris=None, exposure_s=1,
                       apply_refraction=False, auto_refine=False,
                       comparison_model=None, step_deg=10)
    service._await_mount_preflight.assert_not_awaited()
    assert service._publish.call_args.kwargs["status"] == PolarAlignmentStatus.FAILED
    assert "camera cleanup failed" in service._publish.call_args.kwargs["error"]


async def test_scheduled_capture_rejected_cleanup_does_not_claim_or_touch_device(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from ssc.api.device_commands import AlpacaCapturePort

    events = []

    async def drain():
        events.append("drain")
        raise RuntimeError("cleanup failed")

    camera = SimpleNamespace(start_exposure=AsyncMock(), get=AsyncMock())
    port = AlpacaCapturePort(
        run_id="guard-test", device_commands=Mock(), camera=camera,
        telescope=None, filterwheel=None, repo=SimpleNamespace(
            freeze_run_companion=AsyncMock(return_value=False),
            pending_dual_owned=AsyncMock(return_value=[]), dual_capture_lock=asyncio.Lock(),
        ),
        settings=SimpleNamespace(
            capture=SimpleNamespace(folder=tmp_path, folder_template="", filename_template="test"),
            device=SimpleNamespace(label=None), active_location=lambda: None,
        ),
        state=SimpleNamespace(snapshot=SimpleNamespace(device=None, mount=None)),
        begin_capture=lambda **kwargs: events.append("claim"), before_capture=drain,
    )
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await port.expose_frame(exposure_sec=1, gain=100, frame_type="light",
                               filter_name=None, block_id="block", frame_index=0)
    assert events == ["drain"]
    # Admission must finish before claiming; failed cleanup cannot steal
    # another acquisition owner's camera.
    camera.get.assert_not_awaited()
    camera.start_exposure.assert_not_awaited()


async def test_wide_rejected_exposure_does_not_abort_another_capture() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ssc.api.capture_orchestrator import CaptureService
    from ssc.client.errors import AlpacaRemoteError

    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._section = SimpleNamespace(in_progress=False)
    capture._wide_rtsp_exposure_s = .1
    capture._wide_rtsp_min_exposure_s = .1
    capture._wide_rtsp_max_exposure_s = 1
    capture._wide_camera = SimpleNamespace(is_connected=AsyncMock(return_value=True), get=AsyncMock(return_value=0),
        start_exposure=AsyncMock(side_effect=AlpacaRemoteError(member="startexposure", number=0x4ff, message="capture is active")))
    capture._stop_wide_exposure = AsyncMock()
    with pytest.raises(AlpacaRemoteError):
        await capture._capture_wide_rtsp_frame_locked()
    capture._stop_wide_exposure.assert_not_awaited()


async def test_wide_preview_skips_physical_main_capture_after_reconnect() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ssc.api.capture_orchestrator import CaptureService

    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._camera = SimpleNamespace(get=AsyncMock(return_value=2))
    capture._wide_camera = SimpleNamespace(is_connected=AsyncMock(return_value=True),
        get=AsyncMock(return_value=0), start_exposure=AsyncMock())
    await capture._capture_wide_rtsp_frame_locked()
    capture._wide_camera.start_exposure.assert_not_awaited()


async def test_wide_cleanup_recovers_only_after_both_cameras_confirm_idle() -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from ssc.api.capture_orchestrator import CaptureAlreadyRunningError, CaptureService

    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._wide_capture_lock = asyncio.Lock()
    capture._wide_cleanup_error = "cleanup failed"
    capture._section = SimpleNamespace(last_error="unrelated error")
    capture._publish = Mock()
    capture._camera = SimpleNamespace(get=AsyncMock(return_value=0))
    capture._wide_camera = SimpleNamespace(get=AsyncMock(return_value=2))
    with pytest.raises(CaptureAlreadyRunningError):
        await capture.drain_wide_preview()
    assert capture._wide_cleanup_error == "cleanup failed"
    capture._wide_camera.get.return_value = 0
    await capture.drain_wide_preview()
    assert capture._wide_cleanup_error is None
    capture._publish.assert_not_called()


def test_metadata_cache_is_shared_until_the_authoritative_connection_changes() -> None:
    from datetime import timedelta

    from ssc.api.capture_orchestrator import CaptureService
    from ssc.state.events import LinkChanged
    from ssc.state.models import LinkState, LinkStatus, StateSnapshot
    from ssc.state.store import StateStore

    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._state = StateStore(StateSnapshot(server_started_at=DATE_OBS))
    capture._unsupported_metadata_members = set()
    capture._wide_unsupported_metadata_members = set()
    capture._metadata_connection_key = None
    tele = capture.metadata_capabilities()
    tele.add("Camera.LastExposureStartTime")
    wide = capture.metadata_capabilities(wide=True)
    wide.add("Camera.LastExposureDuration")
    assert capture.metadata_capabilities() is tele
    assert tele and wide
    capture._state.apply(LinkChanged(link=LinkState(status=LinkStatus.READY,
        endpoint="10.0.0.7:32323", changed_at=DATE_OBS + timedelta(seconds=1))))
    assert capture.metadata_capabilities() == set()
    assert wide == set()


def _wide_poll_fixture():
    from types import SimpleNamespace

    from ssc.api.capture_orchestrator import CaptureService

    capture = object.__new__(CaptureService)
    capture._manual_both_active = False
    capture._capture_loop = None
    capture._settings = SimpleNamespace(capture=SimpleNamespace(wide_companion_enabled=False))
    capture._rtsp = SimpleNamespace(status=SimpleNamespace(active=True,
        config=SimpleNamespace(wide_camera_until_stacked=True)))
    capture._state = SimpleNamespace(snapshot=SimpleNamespace(mount=SimpleNamespace(at_park=False)))
    capture._live_stack_active = False
    capture._live_stack = None
    capture._wide_cleanup_error = None
    capture._wide_rtsp_pixels = None
    return capture


async def test_wide_poll_recovers_from_empty_message_timeout_without_reconciliation(caplog):
    from unittest.mock import AsyncMock

    capture = _wide_poll_fixture()
    attempts = 0
    waits = []

    async def frame():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError()
        capture._wide_rtsp_pixels = 'recovered-frame'
        capture._rtsp.status.active = False

    capture._capture_wide_rtsp_frame = AsyncMock(side_effect=frame)
    capture._sleep = AsyncMock(side_effect=lambda seconds: waits.append(seconds))
    await capture._poll_wide_rtsp()
    assert attempts == 3
    assert waits == [5, 15, 1]
    assert capture._wide_rtsp_pixels == 'recovered-frame'
    assert 'TimeoutError' in caplog.text


async def test_wide_poll_rechecks_park_after_retry_delay():
    from unittest.mock import AsyncMock

    capture = _wide_poll_fixture()
    capture._capture_wide_rtsp_frame = AsyncMock(side_effect=TimeoutError())

    async def park_during_delay(seconds):
        assert seconds == 5
        capture._state.snapshot.mount.at_park = True

    capture._sleep = AsyncMock(side_effect=park_during_delay)
    await capture._poll_wide_rtsp()
    capture._capture_wide_rtsp_frame.assert_awaited_once()
    capture._sleep.assert_awaited_once_with(5)


@pytest.mark.parametrize('cleanup_blocked', [False, True])
async def test_wide_poll_does_not_retry_unsupported_camera_or_unverified_cleanup(cleanup_blocked):
    from unittest.mock import AsyncMock

    from ssc.client.errors import AlpacaRemoteError

    capture = _wide_poll_fixture()
    capture._wide_cleanup_error = 'unverified cleanup' if cleanup_blocked else None
    error = TimeoutError() if cleanup_blocked else AlpacaRemoteError(
        0x400, 'unsupported camera', member='CameraState')
    capture._capture_wide_rtsp_frame = AsyncMock(side_effect=error)
    capture._sleep = AsyncMock()
    await capture._poll_wide_rtsp()
    capture._capture_wide_rtsp_frame.assert_awaited_once()
    capture._sleep.assert_not_awaited()
