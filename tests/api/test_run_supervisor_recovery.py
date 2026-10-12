from datetime import timedelta
from types import SimpleNamespace

import pytest

from ssc.api import run_service as module
from ssc.api.run_service import PublishingJournal, RunService
from ssc.api.tasks import TaskRegistry
from ssc.scheduler.executor import (
    ControlCommand,
    ControlKind,
    EffectCall,
    EffectName,
    ExecutorRecoveryRestartError,
    ItemStatus,
    JournalRecord,
    RecordKind,
    RunStatus,
    reduce,
)
from ssc.settings.store import SettingsStore
from ssc.state.models import StateSnapshot
from ssc.state.store import StateStore
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from ssc.store.plans import PlanRepository
from ssc.store.runs import RunRepository
from tests.scheduler.conftest import ephemeris, profile  # noqa: F401 - imported pytest fixtures
from tests.scheduler.fakes import FakeClock, FakeDeviceCommands, FakeDeviceEvents
from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule


async def service_fixture(tmp_path):
    db = await open_database(tmp_path / 'supervisor.sqlite')
    await migrate(db)
    settings = SettingsStore(db)
    await settings.load()
    repo = RunRepository(db)
    plan = build_plan()
    schedule = build_schedule(plan)
    record = await repo.create(run_id='same-night', plan=plan, plan_revision=64,
        schedule=schedule, started_at=BASE_UTC)
    service = RunService(runs=repo, plans=PlanRepository(db),
        state=StateStore(StateSnapshot(server_started_at=BASE_UTC)), tasks=TaskRegistry(),
        astro=SimpleNamespace(), weather=SimpleNamespace(), settings=settings,
        device=None, events=None)
    service._clock = FakeClock(BASE_UTC)
    service._active = record
    journal = PublishingJournal(service, record)
    await journal.append(JournalRecord(at=BASE_UTC, run_id=record.run_id,
        kind=RecordKind.RUN_STARTED))
    return db, service, record, journal


@pytest.mark.parametrize("escalation", [False, True])
async def test_executor_exception_reenters_same_sqlite_pending_checkpoint(tmp_path, monkeypatch, escalation):
    db, service, record, journal = await service_fixture(tmp_path)
    effect = EffectCall(name=EffectName.SET_FILTER if escalation else EffectName.DITHER,
        args={'position': 'LP'} if escalation else {'after_frame': 6})
    await journal.append(JournalRecord(at=BASE_UTC, run_id=record.run_id,
        kind=RecordKind.ITEM_TRANSITION, item_id='startup', step=2, attempt=7,
        status=ItemStatus.RUNNING, effect=effect))
    calls = []

    async def executor(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            if escalation:
                raise ExecutorRecoveryRestartError('lower recovery tiers exhausted')
            raise OSError('transport unexpectedly dropped')
        records = await kwargs['journal'].read_all(record.run_id)
        state = reduce(records)
        assert state.items['startup'].pending_effect == effect
        assert state.items['startup'].step == 2
        assert state.items['startup'].attempt == 7
        assert 'supervisor recovery' in records[-1].reason
        assert kwargs['run_id'] == 'same-night'
        assert kwargs['plan'] == record.plan and kwargs['schedule'] == record.schedule

    monkeypatch.setattr(module, 'run', executor)
    capture = object()
    await service._drive(record, journal, FakeDeviceCommands(), FakeDeviceEvents(), None, None, capture)
    assert len(calls) == 2
    assert calls[1]['capture'] is capture
    assert service._clock.now() == BASE_UTC + timedelta(seconds=5)
    assert (await service._runs.get(record.run_id)).plan_revision == 64
    await db.aclose()


@pytest.mark.parametrize('control', [ControlKind.STOP, ControlKind.PAUSE])
async def test_controls_during_supervisor_backoff_remain_authoritative(tmp_path, monkeypatch, control):
    db, service, record, journal = await service_fixture(tmp_path)
    calls = 0

    async def executor(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            service._controls.push(ControlCommand(kind=control))
            if control is ControlKind.PAUSE:
                service._controls.push(ControlCommand(kind=ControlKind.RESUME))
            raise RuntimeError('unexpected operational boundary')
        state = reduce(await journal.read_all(record.run_id))
        assert state.run_status is (RunStatus.STOPPED if control is ControlKind.STOP else RunStatus.RUNNING)

    monkeypatch.setattr(module, 'run', executor)
    await service._drive(record, journal, FakeDeviceCommands(), FakeDeviceEvents(), None, None, None)
    records = await journal.read_all(record.run_id)
    assert any(record.control is control for record in records)
    assert calls == 2
    await db.aclose()


async def test_real_executor_sqlite_supervisor_recovers_dither_without_repeated_offset(
    tmp_path, ephemeris, profile  # noqa: F811 - imported pytest fixtures
):
    from datetime import date
    from zoneinfo import ZoneInfo

    from ssc.astro.events import Site
    from ssc.astro.units import Deg, Metres
    from ssc.plan.models import AbsoluteTimeSpec, DitherSpec
    from ssc.scheduler.compiler import compile_plan
    from tests.scheduler.fakes import FakeCapturePort

    db, service, old_record, _journal = await service_fixture(tmp_path)
    target = old_record.plan.items[1].model_copy(update={
        'start': AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
        'duration_min': 3, 'dither': DitherSpec(pixels=3, every_n_frames=1),
    })
    plan = old_record.plan.model_copy(update={'items': (target, old_record.plan.items[2])})
    site = Site(latitude_deg=Deg(51.4934), longitude_deg=Deg(-.0098), elevation_m=Metres(45))
    schedule = compile_plan(plan, site=site, tz=ZoneInfo('UTC'),
        night_date=date(2026, 1, 1), ephemeris=ephemeris, now=BASE_UTC, profile=profile)
    record = await service._runs.create(run_id='actual-night', plan=plan,
        plan_revision=64, schedule=schedule, started_at=BASE_UTC)
    saved = await service._runs.get(record.run_id)
    record = saved
    assert record.plan == plan
    service._active = record
    journal = PublishingJournal(service, record)
    device = FakeDeviceCommands()

    class Capture(FakeCapturePort):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.physical_moves = 0
            self.recovery_moves = []
            self.hooks = []

        async def dither(self, **kwargs):
            await super().dither(**kwargs)
            self.physical_moves += 1
            if self.physical_moves == 1:
                raise OSError('link disappeared after offset')

        async def recover_dither(self, *, object_name):
            self.recovery_moves.append(self.physical_moves)
            await super().recover_dither(object_name=object_name)

        async def recover_operation(self, *, level, operation):
            self.hooks.append((level, operation))

    capture = Capture(device, clock=service._clock, cycle_s=60)
    await service._drive(record, journal, device, FakeDeviceEvents(), None, None, capture)
    records = await journal.read_all(record.run_id)
    assert capture.recovery_moves == [1]
    assert capture.hooks == [(3, 'dither')]
    assert any('supervisor recovery' in entry.reason for entry in records)
    assert reduce(records).items[target.id].status is ItemStatus.DONE
    assert len([name for name, _args in device.calls if name is EffectName.EXPOSE_FRAME]) >= 2
    assert (await service._runs.get(record.run_id)).plan_revision == 64
    await db.aclose()


async def test_supervisor_deadline_hands_off_without_stopping_whole_plan(tmp_path, monkeypatch):
    from ssc.plan.models import AbsoluteTimeSpec, DurationEndSpec
    from tests.scheduler.fakes import FakeCapturePort

    db, service, initial, _journal = await service_fixture(tmp_path)
    target = initial.plan.items[1].model_copy(update={
        'start': AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
        'end': DurationEndSpec(minutes=1),
    })
    plan = initial.plan.model_copy(update={'items': (target, initial.plan.items[2])})
    schedule = build_schedule(plan)
    record = await service._runs.create(run_id='handoff-night', plan=plan,
        plan_revision=64, schedule=schedule, started_at=BASE_UTC)
    service._active = record
    journal = PublishingJournal(service, record)
    actual_run = module.run
    attempts = 0

    async def executor(**kwargs):
        nonlocal attempts
        attempts += 1
        if service._clock.now() < schedule.items[0].end_utc:
            raise OSError('temporary executor dispatch outage')
        return await actual_run(**kwargs)

    monkeypatch.setattr(module, 'run', executor)
    device = FakeDeviceCommands()
    capture = FakeCapturePort(device, clock=service._clock, cycle_s=60)
    await service._drive(record, journal, device, FakeDeviceEvents(), None, None, capture)
    state = reduce(await journal.read_all(record.run_id))
    assert attempts >= 3
    assert state.run_status is not RunStatus.STOPPED
    assert state.items['filter'].status is ItemStatus.DONE
    assert state.items[target.id].status is not ItemStatus.FAILED
    assert EffectName.SET_FILTER in [name for name, _args in device.calls]
    await db.aclose()


async def test_recovery_reconnect_honors_operator_disconnect():
    from unittest.mock import AsyncMock

    from ssc.api.services import DeviceService
    from ssc.state.models import LinkStatus
    from ssc.state.ports import DeviceGatewayError

    device = object.__new__(DeviceService)
    device._suspended = True
    device._link_status = LinkStatus.DISCONNECTED
    device.connect_preferred = AsyncMock()
    with pytest.raises(DeviceGatewayError, match='disconnected by request'):
        await device.ensure_ready()
    device.connect_preferred.assert_not_awaited()
    device._suspended = False
    await device.ensure_ready()
    device.connect_preferred.assert_awaited_once()


async def test_stop_during_supervisor_repair_is_durable_before_hook_returns(tmp_path, monkeypatch):
    import asyncio

    db, service, record, journal = await service_fixture(tmp_path)
    service._publishing_journal = journal
    service._task = asyncio.current_task()
    device = FakeDeviceCommands()
    original_run = module.run
    calls = 0

    async def executor(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError('link repair required')
        return await original_run(**kwargs)

    from tests.scheduler.fakes import FakeCapturePort

    class Capture(FakeCapturePort):
        async def recover_operation(self, *, level, operation):
            assert service._supervisor_recovering
            await service.control(record.run_id, ControlKind.STOP, reason='owner Stop during repair')
            assert reduce(await journal.read_all(record.run_id)).run_status is RunStatus.STOPPED

        async def focus_required(self):
            raise AssertionError("Stop must not poll optional focus telemetry")

        async def stop_live_stack(self):
            pass

    monkeypatch.setattr(module, 'run', executor)
    await service._drive(record, journal, device, FakeDeviceEvents(), None, None, Capture(device, clock=service._clock))
    assert not service._supervisor_recovering
    assert EffectName.PARK in [name for name, _args in device.calls]
    assert reduce(await journal.read_all(record.run_id)).run_status is RunStatus.STOPPED
    await db.aclose()


async def test_durable_recovery_transition_is_written_to_commander_log(tmp_path, caplog):
    import logging

    db, _service, record, journal = await service_fixture(tmp_path)
    caplog.set_level(logging.INFO, logger='ssc.api.run_service')
    entry = JournalRecord(at=BASE_UTC, run_id=record.run_id,
        kind=RecordKind.ITEM_TRANSITION, item_id='startup', panel_index=1,
        step=3, attempt=7, status=ItemStatus.RUNNING,
        effect=EffectCall(name=EffectName.SLEW_TO), effect_outcome=module.EffectOutcome.FAILED,
        retry_at=BASE_UTC + timedelta(seconds=5), recovery_attempts=2,
        reason='recovering transport timeout')
    await journal.append(entry)
    assert (await journal.read_all(record.run_id))[-1] == entry
    line = next(item.message for item in caplog.records if 'durable run transition' in item.message)
    assert 'utc=2026-01-01T20:00:00+00:00 run=same-night item=startup panel=1 step=3' in line
    assert 'attempt=7 effect=slew_to outcome=failed status=running recovery_attempts=2' in line
    assert 'retry_at=2026-01-01T20:00:05+00:00 reason=recovering transport timeout' in line
    await db.aclose()


async def test_persistent_focus_poll_failure_expires_panel_and_next_panel_captures(
    tmp_path, ephemeris, profile  # noqa: F811 - imported pytest fixtures
):
    import asyncio
    from datetime import date
    from zoneinfo import ZoneInfo

    from ssc.astro.events import Site
    from ssc.astro.units import Deg, Metres
    from ssc.plan.models import AbsoluteTimeSpec, DurationEndSpec, RelativeTimeSpec
    from ssc.scheduler.compiler import compile_plan
    from tests.scheduler.fakes import FakeCapturePort

    db, service, initial, _journal = await service_fixture(tmp_path)
    try:
        first = initial.plan.items[1].model_copy(update={
            'id': 'persistent-fault-panel',
            'start': AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
            'end': DurationEndSpec(minutes=1),
        })
        second = first.model_copy(update={
            'id': 'healthy-next-panel', 'start': RelativeTimeSpec(offset_min=0),
        })
        plan = initial.plan.model_copy(update={'items': (first, second)})
        site = Site(latitude_deg=Deg(51.4934), longitude_deg=Deg(-.0098), elevation_m=Metres(45))
        schedule = compile_plan(plan, site=site, tz=ZoneInfo('UTC'),
            night_date=date(2026, 1, 1), ephemeris=ephemeris, now=BASE_UTC, profile=profile)
        record = await service._runs.create(run_id='persistent-fault-night', plan=plan,
            plan_revision=64, schedule=schedule, started_at=BASE_UTC)
        record = await service._runs.get(record.run_id)
        service._active = record
        journal = PublishingJournal(service, record)
        device = FakeDeviceCommands()

        class Capture(FakeCapturePort):
            failures = 0

            async def focus_required(self):
                if service._state.snapshot.run.current_item_id == first.id:
                    self.failures += 1
                    raise RuntimeError('persistent first-panel focus telemetry failure')
                return False

            async def recover_operation(self, *, level, operation):
                pass

        capture = Capture(device, clock=service._clock, cycle_s=30)
        await asyncio.wait_for(service._drive(record, journal, device,
            FakeDeviceEvents(), None, None, capture), timeout=3)
        records = await journal.read_all(record.run_id)
        state = reduce(records)
        assert capture.failures >= 3
        assert state.run_status is RunStatus.COMPLETED
        assert state.items[first.id].status is ItemStatus.DONE
        assert state.items[second.id].status is ItemStatus.DONE
        assert any(name is EffectName.EXPOSE_FRAME and args['block_id'] == second.id
            for name, args in device.calls)
        assert not any(entry.control is ControlKind.STOP for entry in records)
        assert any('persistent first-panel focus telemetry failure' in entry.reason for entry in records)
        assert (await service._runs.get(record.run_id)).plan_revision == 64
    finally:
        await db.aclose()
