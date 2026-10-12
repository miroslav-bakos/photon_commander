"""Real FITS and SQLite recovery of an exact scheduled panel's RAM stack."""
import asyncio
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import numpy as np
import pytest

from ssc.capture.fits import write_fits_file
from ssc.stacking.live import LiveStackError
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from tests.api.test_calibration_restart_recovery import make_service, start
from tests.capture.test_motion import star_field
from tests.stacking.test_live import _metadata


async def seed(service, folder):
    folder.mkdir()
    ids = []
    for index in range(4):
        path = folder / f"science-{index}.fits"
        write_fits_file(path, star_field().astype(np.uint16), _metadata())
        frame_id = await service._repo.begin_frame(
            path=path, date_obs=datetime(2026, 10, 8, tzinfo=UTC) + timedelta(seconds=index),
            frame_type="light", width=512, height=512,
            object_name="M31", filter_name=None, exposure_s=1., gain=100, ccd_temp_c=None,
            run_id="night" if index < 3 else "other-night", block_id="panel",
        )
        await service._repo.complete_frame(frame_id)
        ids.append(frame_id)
    return ids


async def test_science_stack_sqlite_restart_then_counted_calibration(tmp_path):
    database = tmp_path / "night.sqlite"
    db = await open_database(database)
    await migrate(db)
    service = await make_service(db, tmp_path)
    ids = await seed(service, tmp_path / "lights")
    assert await service.restore_scheduled_live_stack("night", "panel")
    original_paths = service.live_stack_run().frame_paths
    assert len(original_paths) == 3
    original_pixels = service._live_stack.pixels.copy()
    original_binning = service.live_stack_run().binning
    await service._tasks.cancel_all()
    await db.aclose()
    db = await open_database(database)
    fresh = await make_service(db, tmp_path)
    try:
        assert fresh.live_stack_run() is None
        assert await fresh.restore_scheduled_live_stack("night", "panel")
        assert fresh.live_stack_run().frame_paths == original_paths
        assert fresh.live_stack_run().binning == original_binning
        np.testing.assert_array_equal(fresh._live_stack.pixels, original_pixels)
        # Idempotent recovery and already counted frames never double-add.
        assert await fresh.restore_scheduled_live_stack("night", "panel")
        await fresh.add_live_stack_frame(ids[0])
        assert len(fresh.live_stack_run().frame_paths) == 3
        saved = await fresh.stop_scheduled_live_stack(require_retained=True)
        assert saved.output_path.is_file()
        await start(fresh, count=2)
        assert fresh.section.calibration.status == "completed"
        assert len(await fresh._repo.list_block_frames("night", "final-dark")) == 2
        assert all(path.is_file() for path in original_paths)
    finally:
        await fresh._tasks.cancel_all()
        await db.aclose()


async def test_missing_raw_does_not_publish_rebuilt_integration(tmp_path):
    db = await open_database(tmp_path / "night.sqlite")
    await migrate(db)
    service = await make_service(db, tmp_path)
    try:
        ids = await seed(service, tmp_path / "lights")
        (await service._repo.get_frame(ids[1])).path.unlink()
        with pytest.raises(LiveStackError, match="saved science frame is missing"):
            await service.restore_scheduled_live_stack("night", "panel")
        assert service.live_stack_run() is None
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


async def test_mosaic_panel_boundary_survives_service_restart(tmp_path):
    database = tmp_path / "night.sqlite"
    db = await open_database(database)
    await migrate(db)
    service = await make_service(db, tmp_path)
    ids = await seed(service, tmp_path / "lights")
    assert await service.restore_scheduled_live_stack("night", "panel", 1, 2)
    expected = (await service._repo.get_frame(ids[2])).path
    assert service.live_stack_run().frame_paths == (expected,)
    await service._tasks.cancel_all()
    await db.aclose()
    db = await open_database(database)
    fresh = await make_service(db, tmp_path)
    try:
        assert await fresh.restore_scheduled_live_stack("night", "panel", 1, None)
        assert fresh.live_stack_run().frame_paths == (expected,)
        with pytest.raises(LiveStackError, match="checkpoint missing"):
            await fresh.restore_scheduled_live_stack("night", "panel", 0, None)
    finally:
        await fresh._tasks.cancel_all()
        await db.aclose()


async def test_crash_after_first_saved_frame_before_stack_processing(tmp_path):
    database = tmp_path / "night.sqlite"
    db = await open_database(database)
    await migrate(db)
    service = await make_service(db, tmp_path)
    # Panel admission persists the boundary before the first camera dispatch.
    await service.restore_scheduled_live_stack("night", "panel", 0, 0, prepare_only=True)
    await seed(service, tmp_path / "lights")
    await service._tasks.cancel_all()
    await db.aclose()
    db = await open_database(database)
    fresh = await make_service(db, tmp_path)
    try:
        assert await fresh.restore_scheduled_live_stack("night", "panel", 0, None)
        assert len(fresh.live_stack_run().frame_paths) == 3
    finally:
        await fresh._tasks.cancel_all()
        await db.aclose()


async def test_cancelled_native_stack_worker_keeps_lock_until_finished(tmp_path):
    db = await open_database(tmp_path / "night.sqlite")
    await migrate(db)
    service = await make_service(db, tmp_path)
    entered, release = threading.Event(), threading.Event()
    try:
        ids = await seed(service, tmp_path / "lights")
        path = (await service._repo.get_frame(ids[0])).path
        service.reset_scheduled_live_stack(path, seed_folder_baseline=False)
        original = service._live_stack.add_frame
        def delayed(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        service._live_stack.add_frame = delayed
        pending = asyncio.create_task(service.add_live_stack_frame(ids[0]))
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert service._live_stack_lock.locked()
        assert not pending.done()
        with pytest.raises(LiveStackError, match="processing is still owned"):
            service.reset_scheduled_live_stack(path)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not service._live_stack_lock.locked()
        assert service._live_stack.run.frame_paths == (path,)
    finally:
        release.set()
        await service._tasks.cancel_all()
        await db.aclose()


async def test_reconstruction_never_primes_quality_from_folder_neighbours(tmp_path, monkeypatch):
    db = await open_database(tmp_path / "night.sqlite")
    await migrate(db)
    service = await make_service(db, tmp_path)
    baseline = Mock()
    monkeypatch.setattr("ssc.stacking.live.LiveStackEngine.prime_quality_baseline", baseline)
    try:
        await seed(service, tmp_path / "lights")
        await service.restore_scheduled_live_stack("night", "panel", 1, 2)
        baseline.assert_not_called()
    finally:
        await service._tasks.cancel_all()
        await db.aclose()
