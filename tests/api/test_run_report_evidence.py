from datetime import timedelta

import pytest

from ssc.scheduler.executor import (
    ControlKind,
    EffectCall,
    EffectName,
    EffectOutcome,
    JournalRecord,
    RecordKind,
    RunStatus,
)
from ssc.state.events import LinkChanged, MountChanged
from ssc.state.models import LinkState, MountState
from tests.api.test_run_supervisor_recovery import service_fixture
from tests.scheduler.schedules import BASE_UTC


@pytest.mark.parametrize('fresh_after_park', [True, False])
async def test_final_mount_waits_for_stop_park_completion(tmp_path, fresh_after_park):
    db, service, record, journal = await service_fixture(tmp_path)
    try:
        service._state.apply(LinkChanged(link=LinkState(control_connected=True)))
        service._state.apply(MountChanged(mount=MountState(
            polled_at=BASE_UTC, stale=False, at_park=False, tracking=True, slewing=False)))
        effect = EffectCall(name=EffectName.GATE_PARK)
        await journal.append(JournalRecord(at=BASE_UTC, run_id=record.run_id,
            kind=RecordKind.CONTROL, control=ControlKind.STOP,
            run_status=RunStatus.STOPPED, effect=effect))
        assert await service._runs.final_mount(record.run_id) is None
        service._clock.advance(1)
        if fresh_after_park:
            service._state.apply(MountChanged(mount=MountState(
                polled_at=BASE_UTC + timedelta(seconds=1), stale=False,
                at_park=True, tracking=False, slewing=False)))
        await journal.append(JournalRecord(at=service._clock.now(), run_id=record.run_id,
            kind=RecordKind.CONTROL, control=ControlKind.STOP,
            run_status=RunStatus.STOPPED, effect=effect, effect_outcome=EffectOutcome.COMPLETED))
        await journal.append(JournalRecord(at=service._clock.now(), run_id=record.run_id,
            kind=RecordKind.RUN_ENDED, run_status=RunStatus.STOPPED))
        final = await service._runs.final_mount(record.run_id)
        assert final is not None
        assert final['at_park'] is (True if fresh_after_park else None)
        # Later publication cannot overwrite the historical end reading.
        service._clock.advance(10)
        await service._publish_from_journal(record)
        assert await service._runs.final_mount(record.run_id) == final
    finally:
        await db.aclose()


async def test_latest_report_recovers_completed_run_from_reopened_database(tmp_path):
    from types import SimpleNamespace

    from ssc.api.routes.runs import latest_run
    from ssc.state.models import StateSnapshot
    from ssc.state.store import StateStore
    from ssc.store.database import open_database
    from ssc.store.runs import RunRepository

    db, service, record, journal = await service_fixture(tmp_path)
    await journal.append(JournalRecord(at=BASE_UTC, run_id=record.run_id,
        kind=RecordKind.RUN_ENDED, run_status=RunStatus.COMPLETED))
    await db.aclose()
    reopened = await open_database(tmp_path / 'supervisor.sqlite')
    try:
        service._runs = RunRepository(reopened)
        service._active = None
        service._state = StateStore(StateSnapshot(server_started_at=BASE_UTC))
        assert service.current is None
        response = await latest_run(SimpleNamespace(database=reopened, runs=service), None)
        assert response.run.run_id == record.run_id
        assert response.run.status.value == 'completed'
        assert (await service._runs.final_mount(record.run_id)) is not None
    finally:
        await reopened.aclose()
