"""Scheduled calibration continues only verified frames from its own durable item."""
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ssc.api.capture_orchestrator import CalibrationConfigurationError, CaptureService
from ssc.api.tasks import TaskRegistry
from ssc.settings.store import SettingsStore
from ssc.state.models import CaptureState, StateSnapshot
from ssc.state.store import StateStore
from ssc.store.capture import CaptureRepository
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from tests.api.test_capture_routes import _camera


async def make_service(db, folder: Path, *, camera=None):
    settings = SettingsStore(db)
    await settings.load()
    await settings.set_capture_folder(folder)
    await settings.set_calibration_path_templates(bias_folder_template='bias',
        dark_folder_template='dark', flat_folder_template='flat',
        filename_template='%SEQUENCE%')
    service = CaptureService(camera=camera or _camera(properties={'camerastate': 0}),
        telescope=None, filterwheel=None, repo=CaptureRepository(db), settings=settings,
        state=StateStore(StateSnapshot(server_started_at=datetime.now(UTC))),
        tasks=TaskRegistry(), section=CaptureState(), sleep=AsyncMock())
    return service


async def start(service, *, count=3, run_id='night', block_id='final-dark'):
    await service.start_calibration(kind='dark', count=count, duration_s=1., gain=100,
        filter_positions=[], optic_id=None, flat_source=None, generate_master=False,
        keep_only_master=False, run_id=run_id, block_id=block_id)
    await service.wait_for_calibration()


async def test_saved_dark_batch_continues_after_fault_and_sqlite_reopen(tmp_path):
    db_path = tmp_path / 'capture.sqlite'
    db = await open_database(db_path)
    await migrate(db)
    camera = _camera(properties={'camerastate': 0})
    service = await make_service(db, tmp_path, camera=camera)
    original = camera.start_exposure

    async def fail_second(duration_s, *, light=True):
        if len(camera.start_exposure_calls) == 1:
            raise OSError('camera link dropped before second exposure')
        await original(duration_s, light=light)

    camera.start_exposure = fail_second
    await start(service)
    assert service.section.calibration.status == 'failed'
    before = await service._repo.list_block_frames(run_id='night', block_id='final-dark')
    assert len(before) == 1 and before[0].path.exists()
    retained_bytes = before[0].path.read_bytes()
    await service._tasks.cancel_all()
    await db.aclose()
    db = await open_database(db_path)
    fresh_camera = _camera(properties={'camerastate': 0})
    fresh = await make_service(db, tmp_path, camera=fresh_camera)
    try:
        await start(fresh)
        assert fresh.section.calibration.status == 'completed'
        assert fresh.section.calibration.completed_frames == 3
        after = await fresh._repo.list_block_frames(run_id='night', block_id='final-dark')
        assert len(after) == 3
        assert before[0].id == after[0].id
        assert before[0].path.read_bytes() == retained_bytes
        assert len(fresh_camera.start_exposure_calls) == 2
        # Exact identity excludes another item's saved darks.
        await start(fresh, count=1, block_id='other-dark')
        assert len(fresh_camera.start_exposure_calls) == 3
    finally:
        await fresh._tasks.cancel_all()
        await db.aclose()


async def test_resume_rejects_changed_gain_without_new_exposure(tmp_path):
    db = await open_database(tmp_path / 'capture.sqlite')
    await migrate(db)
    service = await make_service(db, tmp_path)
    try:
        await start(service, count=1)
        with pytest.raises(CalibrationConfigurationError, match='parameters differ'):
            await service.start_calibration(kind='dark', count=3, duration_s=1., gain=200,
                filter_positions=[], optic_id=None, flat_source=None, generate_master=False,
                keep_only_master=False, run_id='night', block_id='final-dark')
        assert len(service._camera.start_exposure_calls) == 1
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


async def test_real_executor_retries_partial_calibration_from_catalogue(tmp_path):
    from ssc.plan.models import AbsoluteTimeSpec, CaptureCalibrationItem
    from ssc.scheduler.executor import (
        DeviceCommandError,
        EffectName,
        EffectOutcome,
        ItemStatus,
        reduce,
        run,
    )
    from ssc.store.runs import RunRepository
    from tests.scheduler.fakes import FakeClock, FakeControls, FakeDeviceCommands, FakeDeviceEvents
    from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule

    db = await open_database(tmp_path / 'capture.sqlite')
    await migrate(db)
    camera = _camera(properties={'camerastate': 0})
    service = await make_service(db, tmp_path, camera=camera)
    failed = False
    original = camera.start_exposure

    async def fail_once(duration_s, *, light=True):
        nonlocal failed
        if len(camera.start_exposure_calls) == 1 and not failed:
            failed = True
            raise OSError('scheduled calibration link failure')
        await original(duration_s, light=light)

    camera.start_exposure = fail_once
    item = CaptureCalibrationItem(id='final-dark',
        start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
        duration_min=10, frame_type='dark', count=3, exposure_sec=1., gain=100,
        generate_master=False)
    plan = build_plan().model_copy(update={'items': (item,)})
    schedule = build_schedule(plan)
    runs = RunRepository(db)
    await runs.create(run_id='night', plan=plan, plan_revision=64,
        schedule=schedule, started_at=BASE_UTC)
    class Device(FakeDeviceCommands):
        async def capture_calibration(self, **options):
            await service.start_calibration(kind=options['frame_type'], count=options['count'],
                duration_s=options['exposure_sec'], gain=options['gain'], filter_positions=[],
                optic_id=None, flat_source=None, generate_master=False, keep_only_master=False,
                run_id=options['run_id'], block_id=options['block_id'])
            await service.wait_for_calibration()
            if service.section.calibration.status != 'completed':
                raise DeviceCommandError(service.section.calibration.error)
    try:
        await run(run_id='night', plan=plan, schedule=schedule, journal=runs.journal,
            device=Device(), events=FakeDeviceEvents(), clock=FakeClock(BASE_UTC), controls=FakeControls())
        records = await runs.journal.read_all('night')
        outcomes = [entry.effect_outcome for entry in records if entry.effect
                    and entry.effect.name is EffectName.CAPTURE_CALIBRATION]
        assert EffectOutcome.FAILED in outcomes and EffectOutcome.COMPLETED in outcomes
        assert reduce(records).items[item.id].status is ItemStatus.DONE
        assert len(await service._repo.list_block_frames(run_id='night', block_id=item.id)) == 3
        assert len(camera.start_exposure_calls) == 3
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


async def test_corrupt_saved_fits_does_not_advance_or_duplicate_calibration(tmp_path):
    db = await open_database(tmp_path / 'capture.sqlite')
    await migrate(db)
    service = await make_service(db, tmp_path)
    try:
        await start(service, count=1)
        frame = (await service._repo.list_block_frames(run_id='night', block_id='final-dark'))[0]
        frame.path.write_bytes(b'not a FITS image')
        with pytest.raises(CalibrationConfigurationError, match='cannot verify retained'):
            await start(service)
        assert len(service._camera.start_exposure_calls) == 1
        assert len(await service._repo.list_block_frames(run_id='night', block_id='final-dark')) == 1
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


async def test_authoritative_stop_after_idle_check_prevents_calibration_exposure(tmp_path):
    from ssc.api.capture_orchestrator import _CalibrationCancelledError
    db = await open_database(tmp_path / 'capture.sqlite')
    await migrate(db)
    service = await make_service(db, tmp_path)
    checks = 0
    async def guard():
        nonlocal checks
        checks += 1
        return checks == 1
    try:
        with pytest.raises(_CalibrationCancelledError):
            await service.start_calibration(kind='dark', count=3, duration_s=1., gain=100,
                filter_positions=[], optic_id=None, flat_source=None, generate_master=False,
                keep_only_master=False, run_id='night', block_id='final-dark', continue_guard=guard)
        assert checks == 2
        assert service._camera.start_exposure_calls == []
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


async def test_ambiguous_camera_and_writing_row_reconcile_before_remaining_dark(tmp_path):
    db = await open_database(tmp_path / 'capture.sqlite')
    await migrate(db)
    camera = _camera(properties={'camerastate': 0})
    service = await make_service(db, tmp_path, camera=camera)
    try:
        await start(service, count=1)
        saved = (await service._repo.list_block_frames(run_id='night', block_id='final-dark'))[0]
        partial = tmp_path / 'unfinished.fits'
        partial.write_bytes(b'incomplete write')
        writing_id = await service._repo.begin_frame(path=partial, date_obs=datetime.now(UTC),
            object_name=None, filter_name=None, frame_type='dark', exposure_s=1., gain=100,
            ccd_temp_c=20., width=4, height=4, run_id='night', block_id='final-dark')
        camera.properties['camerastate'] = 2
        original = camera.put
        async def abort_then_idle(member, **kwargs):
            await original(member, **kwargs)
            if member == 'abortexposure':
                camera.properties['camerastate'] = 0
        camera.put = abort_then_idle
        await start(service, count=2)
        assert service.section.calibration.status == 'completed'
        frames = await service._repo.list_block_frames(run_id='night', block_id='final-dark')
        assert len(frames) == 2 and frames[0].id == saved.id
        assert writing_id not in [frame.id for frame in frames]
        assert not partial.exists()
        assert ('abortexposure', {}) in camera.put_calls
        assert len(camera.start_exposure_calls) == 2
    finally:
        await service._tasks.cancel_all()
        await db.aclose()
