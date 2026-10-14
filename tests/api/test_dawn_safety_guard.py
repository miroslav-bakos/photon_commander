import asyncio
from datetime import timedelta

import pytest

from ssc.api.run_service import RunNightExpiredError, dawn_safety_deadline
from ssc.scheduler.executor import ControlKind, EffectName, RecordKind
from ssc.state.models import RunLifecycle
from tests.api.test_run_supervisor_recovery import service_fixture as base_service_fixture
from tests.scheduler.schedules import BASE_UTC


async def service_fixture(tmp_path):
    from dataclasses import replace

    db, service, record, journal = await base_service_fixture(tmp_path)
    from ssc.scheduler.compiler import ResolvedEvent

    night = record.schedule.night.model_copy(update={
        'dark_end_utc': record.schedule.night.window_end_utc,
        'solar_events': (ResolvedEvent(event='sunrise',
            when_utc=record.schedule.night.window_end_utc,
            outcome='occurs', threshold_deg=-0.833),)})
    schedule = record.schedule.model_copy(update={'night': night})
    await service._runs.update_plan_snapshot(record.run_id, plan=record.plan,
        plan_revision=64, schedule=schedule)
    record = replace(record, schedule=schedule)
    service._active = record
    return db, service, record, journal


def test_sunrise_deadline_ignores_twilight_and_reserves_ten_minutes():
    from ssc.scheduler.compiler import ResolvedEvent
    from tests.scheduler.schedules import build_plan, build_schedule

    schedule = build_schedule(build_plan())
    events = tuple(ResolvedEvent(event=name, when_utc=BASE_UTC + timedelta(hours=hours),
        outcome='occurs', threshold_deg=threshold) for name, hours, threshold in (
            ('astronomical_dawn', 3, -18), ('nautical_dawn', 4, -12),
            ('civil_dawn', 5, -6), ('dawn', 5, -6), ('sunrise', 6, -0.833)))
    night = schedule.night.model_copy(update={
        'dark_end_utc': BASE_UTC + timedelta(hours=3), 'solar_events': events})
    schedule = schedule.model_copy(update={'night': night})
    assert dawn_safety_deadline(schedule) == BASE_UTC + timedelta(hours=5, minutes=50)


def test_sunrise_deadline_selects_earliest_resolved_sunrise():
    from ssc.scheduler.compiler import ResolvedEvent
    from tests.scheduler.schedules import build_plan, build_schedule

    schedule = build_schedule(build_plan())
    events = tuple(ResolvedEvent(event='sunrise', when_utc=when,
        outcome='occurs' if when else 'always_below', threshold_deg=-0.833)
        for when in (BASE_UTC + timedelta(hours=7), None, BASE_UTC + timedelta(hours=6)))
    schedule = schedule.model_copy(update={'night': schedule.night.model_copy(
        update={'solar_events': events})})
    assert dawn_safety_deadline(schedule) == BASE_UTC + timedelta(hours=5, minutes=50)


def test_missing_sunrise_fails_closed_despite_resolved_twilight():
    from ssc.scheduler.compiler import ResolvedEvent
    from tests.scheduler.schedules import build_plan, build_schedule

    schedule = build_schedule(build_plan())
    schedule = schedule.model_copy(update={'night': schedule.night.model_copy(update={
        'dark_end_utc': BASE_UTC + timedelta(hours=3),
        'solar_events': (ResolvedEvent(event='civil_dawn',
            when_utc=BASE_UTC + timedelta(hours=5), outcome='occurs', threshold_deg=-6),
            ResolvedEvent(event='sunrise', when_utc=None,
                outcome='always_below', threshold_deg=-0.833))})})
    assert dawn_safety_deadline(schedule) == schedule.night.window_start_utc

@pytest.mark.parametrize('failed_park', [False, True])
async def test_dawn_stops_blocked_executor_and_dark_even_if_park_fails(tmp_path, failed_park):
    db, service, record, journal = await service_fixture(tmp_path)
    calls = []
    try:
        service._dawn_safety_enabled = True
        service._clock.advance(8 * 3600)
        service._publishing_journal = journal

        class Device:
            async def ensure_safety_parked(self):
                calls.append('park')
                if failed_park:
                    raise OSError('park link failed')

            async def set_filter(self, position):
                calls.append(position)

        service._device = Device()
        blocked = asyncio.Event()
        service._task = service._tasks.spawn(blocked.wait(), name='blocked-science')
        await service._arm_dawn_safety(record)
        if failed_park:
            for _ in range(100):
                records = await journal.read_all(record.run_id)
                if any(entry.kind is RecordKind.DAWN_SAFETY and entry.effect.args['stage'] == 'dark_verified'
                       for entry in records):
                    break
                await asyncio.sleep(.01)
            assert calls == ['park', 'Dark']
            assert service._task.cancelled()
            assert any(entry.effect and entry.effect.args.get('stage') == 'park_failed' for entry in records)
        else:
            await asyncio.wait_for(service._dawn_task, timeout=2)
            records = await journal.read_all(record.run_id)
            assert calls == ['park', 'Dark']
            assert any(entry.effect and entry.effect.args.get('stage') == 'completed' for entry in records)
            assert (await service._runs.get(record.run_id)).ended_reason == "dawn safety cutoff"
        assert any(entry.control is ControlKind.STOP for entry in records)
        assert not any(entry.effect and entry.effect.name is EffectName.EXPOSE_FRAME for entry in records)
        with pytest.raises(RunNightExpiredError):
            service._check_dawn_cutoff(record)
        stored = await service._runs.get(record.run_id)
        assert stored.status is RunLifecycle.STOPPED
        assert stored.ended_at == service._clock.now()
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


async def test_startup_arms_dawn_safety_for_completed_sqlite_run(tmp_path):
    db, service, record, journal = await service_fixture(tmp_path)
    calls = []
    try:
        await service._runs.set_status(record.run_id, RunLifecycle.COMPLETED)
        service._dawn_safety_enabled = True
        service._clock.advance(8 * 3600)

        class Device:
            async def ensure_safety_parked(self):
                calls.append('park')

            async def set_filter(self, position):
                calls.append(position)

        service._device = Device()
        assert await service.startup() is None
        await asyncio.wait_for(service._dawn_task, timeout=2)
        assert calls == ['park', 'Dark']
        records = await journal.read_all(record.run_id)
        assert any(entry.kind is RecordKind.DAWN_SAFETY for entry in records)
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


async def test_unresolved_dawn_intent_survives_sqlite_reopen(tmp_path):
    from ssc.api.run_service import RunService
    from ssc.api.tasks import TaskRegistry
    from ssc.scheduler.executor import EffectCall, JournalRecord
    from ssc.store.database import open_database
    from ssc.store.runs import RunRepository

    db, service, record, journal = await service_fixture(tmp_path)
    await journal.append(JournalRecord(at=BASE_UTC, run_id=record.run_id,
        kind=RecordKind.DAWN_SAFETY, effect=EffectCall(name=EffectName.GATE_PARK,
            args={'stage': 'park_intent'}), reason='process ended before verification'))
    await service._runs.set_status(record.run_id, RunLifecycle.COMPLETED)
    await db.aclose()
    db = await open_database(tmp_path / 'supervisor.sqlite')
    calls = []

    class Device:
        async def ensure_safety_parked(self):
            calls.append('park')

        async def set_filter(self, position):
            calls.append(position)

    fresh = RunService(runs=RunRepository(db), plans=service._plans, state=service._state,
        tasks=TaskRegistry(), astro=service._astro, weather=service._weather,
        settings=service._settings, device=Device(), events=None, dawn_safety_enabled=True)
    fresh._clock = service._clock
    fresh._clock.advance(8 * 3600)
    try:
        await fresh.startup()
        await asyncio.wait_for(fresh._dawn_task, timeout=2)
        records = await fresh._runs.journal.read_all(record.run_id)
        assert calls == ['park', 'Dark']
        assert records[-1].effect.args['stage'] == 'completed'
        assert sum(entry.effect is not None and entry.effect.args.get('stage') == 'park_intent'
                   for entry in records) == 2
    finally:
        await fresh._tasks.cancel_all()
        await db.aclose()


async def test_dawn_retry_reverifies_both_operations_and_deduplicates_arm(tmp_path):
    db, service, record, _journal = await service_fixture(tmp_path)
    calls = []
    try:
        service._dawn_safety_enabled = True
        service._clock.advance(8 * 3600)

        class Device:
            async def ensure_safety_parked(self):
                calls.append('park')
                if calls.count('park') == 1:
                    raise OSError('temporary park outage')

            async def set_filter(self, position):
                calls.append(position)

        service._device = Device()
        await service._arm_dawn_safety(record)
        original = service._dawn_task
        await service._arm_dawn_safety(record)
        assert service._dawn_task is original
        await asyncio.wait_for(original, timeout=7)
        assert calls == ['park', 'Dark', 'park', 'Dark']
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


async def test_pending_older_guard_is_not_hidden_by_newer_run_and_blocks_start(tmp_path):
    from ssc.api.run_service import RunActiveError
    from ssc.scheduler.executor import EffectCall, JournalRecord

    db, service, record, journal = await service_fixture(tmp_path)
    try:
        await journal.append(JournalRecord(at=BASE_UTC, run_id=record.run_id,
            kind=RecordKind.DAWN_SAFETY, effect=EffectCall(name=EffectName.GATE_PARK,
                args={'stage': 'park_failed'}), reason='unverified old park'))
        await service._runs.set_status(record.run_id, RunLifecycle.COMPLETED)
        newer = await service._runs.create(run_id='newer-completed', plan=record.plan,
            plan_revision=64, schedule=record.schedule,
            started_at=BASE_UTC + timedelta(minutes=1))
        await service._runs.set_status(newer.run_id, RunLifecycle.COMPLETED)
        service._dawn_safety_enabled = True
        service._clock.advance(8 * 3600)
        await service.startup()
        assert service._dawn_run_id == record.run_id
        with pytest.raises(RunActiveError, match='dawn safety verification'):
            await service.start('anything')
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


async def test_completion_rearms_next_persisted_safety_obligation(tmp_path):
    from ssc.scheduler.executor import EffectCall, JournalRecord

    db, service, record, _journal = await service_fixture(tmp_path)
    calls = []
    try:
        second = await service._runs.create(run_id='second-safety', plan=record.plan,
            plan_revision=64, schedule=record.schedule, started_at=BASE_UTC + timedelta(minutes=1))
        for run_record in (record, second):
            await service._runs.journal.append(JournalRecord(at=BASE_UTC, run_id=run_record.run_id,
                kind=RecordKind.DAWN_SAFETY, effect=EffectCall(name=EffectName.GATE_PARK,
                    args={'stage': 'armed'})))
            await service._runs.set_status(run_record.run_id, RunLifecycle.COMPLETED)

        class Device:
            async def ensure_safety_parked(self):
                calls.append('park')

            async def set_filter(self, position):
                calls.append(position)

        service._device = Device()
        service._dawn_safety_enabled = True
        service._clock.advance(8 * 3600)
        await service.startup()
        await asyncio.wait_for(service._dawn_task, timeout=2)
        await asyncio.wait_for(service._dawn_task, timeout=2)
        assert calls == ['park', 'Dark', 'park', 'Dark']
        assert await service._runs.list_dawn_safety_pending() == []
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


def test_unknown_dawn_fails_closed_at_window_start():
    from tests.scheduler.schedules import build_plan, build_schedule

    schedule = build_schedule(build_plan())
    assert dawn_safety_deadline(schedule) == schedule.night.window_start_utc


async def test_verified_safety_is_not_rearmed_after_sqlite_reopen(tmp_path):
    from ssc.api.run_service import RunService
    from ssc.api.tasks import TaskRegistry
    from ssc.store.database import open_database
    from ssc.store.runs import RunRepository

    db, service, record, journal = await service_fixture(tmp_path)
    calls = []

    class Device:
        async def ensure_safety_parked(self):
            calls.append('park')

        async def set_filter(self, position):
            calls.append(position)

    service._device = Device()
    service._dawn_safety_enabled = True
    service._clock.advance(8 * 3600)
    await service._arm_dawn_safety(record)
    await asyncio.wait_for(service._dawn_task, timeout=2)
    before = await journal.read_all(record.run_id)
    assert before[-1].effect.args['stage'] == 'completed'
    await db.aclose()
    db = await open_database(tmp_path / 'supervisor.sqlite')
    fresh = RunService(runs=RunRepository(db), plans=service._plans, state=service._state,
        tasks=TaskRegistry(), astro=service._astro, weather=service._weather,
        settings=service._settings, device=Device(), events=None, dawn_safety_enabled=True)
    fresh._clock = service._clock
    try:
        for _ in range(2):
            assert await fresh.startup() is None
            await asyncio.sleep(0)
            assert fresh._dawn_task is None
            assert await fresh._runs.list_dawn_safety_pending() == []
            assert await fresh._runs.journal.read_all(record.run_id) == before
        assert calls == ['park', 'Dark']
    finally:
        await fresh._tasks.cancel_all()
        await db.aclose()


async def test_bounded_safety_failure_is_owned_by_watchdog_not_logged_as_crash(tmp_path, caplog):
    db, service, _record, _journal = await service_fixture(tmp_path)

    async def unavailable():
        raise OSError('temporary outage')

    try:
        with pytest.raises(OSError, match='temporary outage'):
            await service._bounded_safety_operation(unavailable)
        await asyncio.sleep(0)
        assert not any('background task dawn-safety-operation failed' in record.message
                       for record in caplog.records)
    finally:
        await service._tasks.cancel_all()
        await db.aclose()


@pytest.mark.parametrize('available', [False, True])
@pytest.mark.parametrize('lifecycle', [RunLifecycle.STOPPED, RunLifecycle.COMPLETED, RunLifecycle.FAILED])
async def test_terminal_metadata_survives_dawn_retry_and_sqlite_reopen(tmp_path, available, lifecycle):
    from ssc.store.database import open_database
    from ssc.store.runs import RunRepository

    db, service, record, journal = await service_fixture(tmp_path)
    ended_at = BASE_UTC + timedelta(minutes=15)
    reason = 'owner stopped after capture failure'
    await service._runs.set_status(record.run_id, lifecycle,
        ended_at=ended_at, ended_reason=reason)
    # A newer run owns the displayed state; an old safety obligation must not
    # replace it when its hardware verification eventually succeeds.
    newer = await service._runs.create(run_id='newer-visible', plan=record.plan,
        plan_revision=64, schedule=record.schedule, started_at=ended_at + timedelta(minutes=1))
    await service._runs.set_status(newer.run_id, RunLifecycle.COMPLETED,
        ended_at=ended_at + timedelta(minutes=2), ended_reason='newer complete')
    service._active = None
    await service._publish_from_journal(newer, lifecycle=RunLifecycle.COMPLETED)
    visible = service._state.snapshot.run

    class Device:
        async def ensure_safety_parked(self):
            pass

        async def set_filter(self, position):
            assert position == 'Dark'

    service._device = Device() if available else None
    service._dawn_safety_enabled = True
    service._clock.advance(8 * 3600)
    try:
        await service._arm_dawn_safety(record)
        if available:
            await asyncio.wait_for(service._dawn_task, timeout=2)
        else:
            for _ in range(100):
                entries = await journal.read_all(record.run_id)
                if any(entry.effect and entry.effect.args.get('stage') == 'dark_failed'
                       for entry in entries):
                    break
                await asyncio.sleep(.01)
            else:
                pytest.fail('unavailable hardware was not recorded as failed')
        stored = await service._runs.get(record.run_id)
        assert (stored.status, stored.ended_at, stored.ended_reason) == (
            lifecycle, ended_at, reason)
        entries = await journal.read_all(record.run_id)
        assert not any(entry.control is ControlKind.STOP for entry in entries)
        assert service._state.snapshot.run == visible
    finally:
        await service._tasks.cancel_all()
        await db.aclose()
    db = await open_database(tmp_path / 'supervisor.sqlite')
    try:
        repo = RunRepository(db)
        stored = await repo.get(record.run_id)
        assert (stored.status, stored.ended_at, stored.ended_reason) == (
            lifecycle, ended_at, reason)
        pending = await repo.list_dawn_safety_pending()
        assert [row.run_id for row in pending] == ([] if available else [record.run_id])
    finally:
        await db.aclose()
