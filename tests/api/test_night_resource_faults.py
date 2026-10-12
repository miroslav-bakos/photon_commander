# SPDX-License-Identifier: GPL-3.0-only
"""Bounded disk/persistence/render faults; no hardware or RAM benchmark."""
import asyncio
import errno
import os
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from ssc.api import capture_orchestrator
from ssc.api.capture_orchestrator import CaptureService
from ssc.api.tasks import TaskRegistry
from ssc.capture.fits import FitsWriteError, write_fits_file
from ssc.capture.pipeline import capture_frame, reconcile_incomplete
from ssc.scheduler.executor import JournalRecord, RecordKind, run
from ssc.state.models import CaptureState, StateSnapshot
from ssc.state.store import StateStore
from ssc.store.capture import CaptureRepository
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from ssc.store.runs import RunRepository
from tests.capture.test_pipeline import DATE_OBS, _raw_frame
from tests.scheduler.fakes import (
    FakeCapturePort,
    FakeClock,
    FakeControls,
    FakeDeviceCommands,
    FakeDeviceEvents,
)
from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule


@pytest.mark.parametrize('fault', ['enospc', 'short_write'])
async def test_partial_fits_never_completes_catalogue_and_saved_frame_survives(tmp_path, monkeypatch, fault):
    path = tmp_path / 'night.sqlite3'
    db = await open_database(path)
    await migrate(db)
    repo = CaptureRepository(db)
    frame = _raw_frame()
    args = {'repo': repo, 'folder': tmp_path / 'frames', 'frame': frame, 'date_obs': DATE_OBS,
            'template': '%TARGET%', 'tz': ZoneInfo('UTC')}
    saved_id = await capture_frame(**args, sequence=0)
    saved = (await repo.list_complete())[0]
    saved_bytes = saved.path.read_bytes()
    real_fdopen = os.fdopen

    class PartialWriter:
        def __init__(self, fd, mode):
            self.handle = real_fdopen(fd, mode)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.handle.close()
        def write(self, payload):
            written = self.handle.write(payload[:100])
            if fault == 'enospc':
                raise OSError(errno.ENOSPC, 'No space left on device')
            return written
        def flush(self):
            self.handle.flush()
        def fileno(self):
            return self.handle.fileno()

    try:
        with monkeypatch.context() as patch:
            patch.setattr('ssc.capture.fits.os.fdopen', PartialWriter)
            expected = OSError if fault == 'enospc' else FitsWriteError
            with pytest.raises(expected):
                await capture_frame(**args, sequence=1)
        assert [record.id for record in await repo.list_complete()] == [saved_id]
        incomplete = await repo.list_writing()
        assert len(incomplete) == 1 and not incomplete[0].path.exists()
        assert not list((tmp_path / 'frames').rglob('*.part'))
    finally:
        await db.aclose()
    db = await open_database(path)
    try:
        repo = CaptureRepository(db)
        await reconcile_incomplete(repo)
        assert [record.id for record in await repo.list_complete()] == [saved_id]
        assert not await repo.list_writing()
        assert saved.path.read_bytes() == saved_bytes
    finally:
        await db.aclose()


async def test_sqlite_journal_write_failure_issues_no_unjournalled_hardware_effect(tmp_path):
    path = tmp_path / 'night.sqlite3'
    db = await open_database(path)
    await migrate(db)
    repo = RunRepository(db)
    plan = build_plan()
    schedule = build_schedule(plan)
    await repo.create(run_id='night', plan=plan, plan_revision=64, schedule=schedule, started_at=BASE_UTC)
    await repo.journal.append(JournalRecord(at=BASE_UTC, run_id='night', kind=RecordKind.RUN_STARTED))
    prefix = await repo.journal.read_all('night')
    device = FakeDeviceCommands()
    clock = FakeClock(BASE_UTC)
    try:
        await db.execute('PRAGMA query_only = ON')
        with pytest.raises(aiosqlite.OperationalError, match='readonly'):
            await run(run_id='night', plan=plan, schedule=schedule, journal=repo.journal,
                      device=device, events=FakeDeviceEvents(), clock=clock, controls=FakeControls(),
                      capture=FakeCapturePort(device, clock=clock))
        assert not device.calls
    finally:
        await db.aclose()
    db = await open_database(path)
    try:
        assert await RunRepository(db).journal.read_all('night') == prefix
    finally:
        await db.aclose()


async def test_cancelled_full_resolution_preview_does_not_duplicate_worker(tmp_path, monkeypatch):
    frame = _raw_frame()
    path = tmp_path / 'saved.fits'
    write_fits_file(path, frame.pixels, frame.metadata)
    service = CaptureService(camera=None, telescope=None, filterwheel=None,
        repo=SimpleNamespace(), settings=SimpleNamespace(capture=SimpleNamespace(folder=tmp_path)),
        state=StateStore(StateSnapshot(server_started_at=datetime.now(UTC))),
        tasks=TaskRegistry(), section=CaptureState(), sleep=AsyncMock())
    entered, release = threading.Event(), threading.Event()
    calls = []

    def expensive_render(raw, *, thumbnail):
        assert not thumbnail and raw == path.read_bytes()
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return b'PNG'

    monkeypatch.setattr(capture_orchestrator, '_render_frame_png', expensive_render)
    first = asyncio.create_task(service.get_archive_image('saved.fits', thumbnail=False))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(service.get_archive_image('saved.fits', thumbnail=False))
        await asyncio.sleep(.05)
        release.set()
        assert await asyncio.wait_for(second, 1) == (b'PNG', 'image/png')
        assert len(calls) == 1, 'cancelled caller caused a second expensive full-resolution render'
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
