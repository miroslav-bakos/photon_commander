from types import SimpleNamespace

import pytest

from ssc.scheduler.executor import (
    ControlKind,
    DeviceStackSaveError,
    EffectCall,
    EffectName,
    EffectOutcome,
    JournalRecord,
    RecordKind,
    RunStatus,
    _resolve_pending_run_effect,
)
from ssc.settings.store import SettingsStore
from tests.scheduler.fakes import FakeClock, FakeDeviceCommands, FakeJournal
from tests.scheduler.schedules import BASE_UTC


async def test_stop_parks_and_journals_failed_save_without_claiming_saved():
    journal = FakeJournal('save-fault')
    effect = EffectCall(name=EffectName.GATE_PARK)
    await journal.append(JournalRecord(at=BASE_UTC, run_id='save-fault',
        kind=RecordKind.CONTROL, control=ControlKind.STOP,
        run_status=RunStatus.STOPPED, effect=effect))
    device = FakeDeviceCommands()

    async def fail_save():
        raise DeviceStackSaveError('ENOSPC; integration retained')

    await _resolve_pending_run_effect(journal, device, FakeClock(BASE_UTC), 'save-fault',
        effect, capture=SimpleNamespace(stop_live_stack=fail_save))
    assert [name for name, _args in device.calls] == [EffectName.STOP_STACK, EffectName.PARK]
    last = journal.records[-1]
    assert last.effect_outcome is EffectOutcome.FAILED
    assert 'safe park completed' in last.reason
    assert 'ENOSPC' in last.reason


async def test_save_retry_is_durable_and_survives_more_than_three_failures(tmp_path):
    from ssc.scheduler.executor import IssueEffect, ItemStatus, _issue_effect
    from ssc.store.database import open_database
    from ssc.store.migrations import migrate
    from ssc.store.runs import RunRepository, SqliteJournal
    from tests.scheduler.fakes import FakeDeviceEvents
    from tests.scheduler.schedules import build_plan, build_schedule

    path = tmp_path / 'save-recovery.sqlite'
    db = await open_database(str(path))
    await migrate(db)
    settings = SettingsStore(db)
    await settings.load()
    plan = build_plan()
    await RunRepository(db).create(run_id='retry-save', plan=plan, plan_revision=1,
        schedule=build_schedule(plan), started_at=BASE_UTC)
    journal = SqliteJournal(db)
    await journal.append(JournalRecord(at=BASE_UTC, run_id='retry-save',
        kind=RecordKind.RUN_STARTED))
    attempts = 0

    async def save(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 4:
            raise DeviceStackSaveError('ENOSPC; retained integration')

    effect = EffectCall(name=EffectName.STOP_LIVE_STACK)
    for attempt in range(1, 6):
        decision = IssueEffect(item_id='retained', step=0, panel_index=None,
            attempt=attempt, effect=effect, resumed=False, new_status=ItemStatus.RUNNING)
        state = await _issue_effect(journal, FakeDeviceCommands(), FakeDeviceEvents(),
            FakeClock(BASE_UTC), 'retry-save', decision,
            capture=SimpleNamespace(stop_live_stack=save), recovery_deadline=BASE_UTC)
        await db.aclose()
        db = await open_database(str(path))
        journal = SqliteJournal(db)
        records = await journal.read_all('retry-save')
        if attempt <= 4:
            assert state.items['retained'].status is ItemStatus.RUNNING
            assert records[-1].retry_at is not None
            assert records[-1].recovery_attempts == attempt
            assert records[-1].effect_outcome is EffectOutcome.FAILED
            assert 'ENOSPC' in records[-1].reason
        else:
            assert records[-1].effect_outcome is EffectOutcome.COMPLETED
    await db.aclose()


@pytest.mark.parametrize('stop_requested,lose_buffer,crash_before_outcome',
    [(False, False, False), (True, False, False), (False, True, False), (False, True, True)])
async def test_full_run_save_failure_after_window_recompiles_and_retries_or_stops(
    tmp_path, ephemeris, profile, stop_requested, lose_buffer, crash_before_outcome
):
    from datetime import UTC, date, datetime
    from zoneinfo import ZoneInfo

    from ssc.astro.events import Site
    from ssc.astro.units import Deg, Metres
    from ssc.plan.models import AbsoluteTimeSpec, DurationEndSpec, GotoAndStackItem, Plan
    from ssc.scheduler.compiler import compile_plan
    from ssc.scheduler.executor import ControlCommand, ItemStatus, reduce, run
    from ssc.store.database import open_database
    from ssc.store.migrations import migrate
    from ssc.store.runs import RunRepository
    from tests.scheduler.fakes import FakeCapturePort, FakeControls, FakeDeviceEvents

    start = datetime(2026, 10, 7, 20, tzinfo=UTC)
    item = GotoAndStackItem(id='real-panel',
        start=AbsoluteTimeSpec(local_datetime=start.replace(tzinfo=None)),
        end=DurationEndSpec(minutes=1), target_name='M45',
        ra_j2000_h=3.79, dec_j2000_deg=24.2)
    plan = Plan(id='save-night', name='Save retry night', created_at=start,
        updated_at=start, items=(item,))

    def compile_saved(value):
        return compile_plan(value,
            site=Site(latitude_deg=Deg(51.4934), longitude_deg=Deg(-.0098), elevation_m=Metres(45)),
            tz=ZoneInfo('UTC'), night_date=date(2026, 10, 7), ephemeris=ephemeris,
            now=start, profile=profile)

    class CrashAfterSaveFailure(BaseException):
        pass

    controls = FakeControls()
    device = FakeDeviceCommands()
    clock = FakeClock(start)

    class SaveFaultCapture(FakeCapturePort):
        active = False
        failures = 0
        lost = False
        async def expose_frame(self, **kwargs):
            await super().expose_frame(**kwargs)
            self.active = True
        async def stop_live_stack(self, *, require_retained=False):
            if require_retained and self.lost:
                controls.push(ControlCommand(kind=ControlKind.STOP, reason='owner Stop missing integration'))
                raise DeviceStackSaveError('retained integration is missing after restart')
            if not self.active:
                return
            if self.failures == 0:
                self.failures += 1
                if crash_before_outcome:
                    raise CrashAfterSaveFailure()
                if stop_requested:
                    controls.push(ControlCommand(kind=ControlKind.STOP, reason='owner Stop'))
                raise DeviceStackSaveError('ENOSPC; retained integration')
            self.active = False
            await super().stop_live_stack(require_retained=require_retained)

    capture = SaveFaultCapture(device, clock=clock, cycle_s=60)
    original_sleep = clock.sleep_until
    async def crash_sleep(until_utc):
        if capture.failures and not stop_requested:
            raise CrashAfterSaveFailure()
        await original_sleep(until_utc)
    clock.sleep_until = crash_sleep
    db_path = tmp_path / 'full-save-night.sqlite'
    db = await open_database(db_path)
    await migrate(db)
    repo = RunRepository(db)
    schedule = compile_saved(plan)
    await repo.create(run_id='save-night', plan=plan, plan_revision=1,
        schedule=schedule, started_at=start)
    if stop_requested:
        state = await run(run_id='save-night', plan=plan, schedule=schedule,
            journal=repo.journal, device=device, events=FakeDeviceEvents(), clock=clock,
            controls=controls, capture=capture)
        assert state.run_status is RunStatus.STOPPED
        assert EffectName.PARK in [name for name, _args in device.calls]
    else:
        with pytest.raises(CrashAfterSaveFailure):
            await run(run_id='save-night', plan=plan, schedule=schedule,
                journal=repo.journal, device=device, events=FakeDeviceEvents(), clock=clock,
                controls=controls, capture=capture)
        retained = reduce(await repo.journal.read_all('save-night')).items[item.id]
        if crash_before_outcome:
            assert retained.pending_effect is not None
            assert retained.pending_effect.args['require_retained'] is True
        else:
            assert retained.stack_save_pending and retained.retry_at > schedule.items[0].end_utc
        await db.aclose()
        db = await open_database(db_path)
        repo = RunRepository(db)
        saved = await repo.get('save-night')
        clock.sleep_until = original_sleep
        if lose_buffer:
            capture.active = False
            capture.lost = True
        if crash_before_outcome:
            from ssc.api.capture_orchestrator import CaptureService
            from ssc.api.tasks import TaskRegistry
            from ssc.state.models import CaptureState, StateSnapshot
            from ssc.state.store import StateStore
            from ssc.store.capture import CaptureRepository
            fresh_settings = SettingsStore(db)
            await fresh_settings.load()
            fresh_service = CaptureService(camera=None, telescope=None, filterwheel=None,
                repo=CaptureRepository(db), settings=fresh_settings,
                state=StateStore(StateSnapshot(server_started_at=start)), tasks=TaskRegistry(),
                section=CaptureState())
            async def fresh_stop(*, require_retained=False):
                try:
                    return await fresh_service.stop_scheduled_live_stack(require_retained=require_retained)
                except Exception as exc:
                    controls.push(ControlCommand(kind=ControlKind.STOP, reason='owner Stop missing RAM'))
                    raise DeviceStackSaveError(str(exc)) from exc
            capture.stop_live_stack = fresh_stop
        state = await run(run_id='save-night', plan=saved.plan, schedule=compile_saved(saved.plan),
            journal=repo.journal, device=device, events=FakeDeviceEvents(), clock=clock,
            controls=controls, capture=capture)
        if lose_buffer:
            assert state.run_status is RunStatus.STOPPED
            assert EffectName.PARK in [name for name, _args in device.calls]
        else:
            assert state.items[item.id].status is ItemStatus.DONE
            assert not state.items[item.id].stack_save_pending
        records = await repo.journal.read_all('save-night')
        saved_effects = [record for record in records if record.effect is not None
            and record.effect.name is EffectName.STOP_LIVE_STACK]
        assert any(record.effect_outcome is EffectOutcome.FAILED for record in saved_effects)
        assert saved_effects[-1].effect.args.get('require_retained') is True
        if lose_buffer:
            assert saved_effects[-1].effect_outcome is EffectOutcome.FAILED
            assert 'missing after restart' in saved_effects[-1].reason
        else:
            assert saved_effects[-1].effect_outcome is EffectOutcome.COMPLETED
    await db.aclose()
