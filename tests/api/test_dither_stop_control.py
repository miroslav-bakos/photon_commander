from unittest.mock import AsyncMock, Mock

import pytest

from ssc.api.device_commands import ControlQueue
from ssc.api.run_service import RunService
from ssc.scheduler.executor import ControlKind, JournalRecord, RecordKind, RunStatus, reduce
from ssc.state.models import CaptureStage, CaptureState, StateSnapshot
from ssc.state.store import StateStore
from tests.scheduler.fakes import FakeClock, FakeJournal
from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule


@pytest.mark.asyncio
async def test_stop_is_journalled_while_dither_effect_is_waiting() -> None:
    service = object.__new__(RunService)
    service._supervisor_recovering = False
    service._operation_recovery_active = lambda: False
    service._task = Mock()
    service._task.done.return_value = False
    service._active = Mock(run_id="live", schedule=build_schedule(build_plan()))
    service._state = StateStore(StateSnapshot(
        server_started_at=BASE_UTC,
        capture=CaptureState(stage=CaptureStage.DITHERING, in_progress=True),
    ))
    service._device = Mock()
    service._clock = FakeClock(BASE_UTC)
    service._controls = ControlQueue()
    service._publishing_journal = FakeJournal("live", [
        JournalRecord(at=BASE_UTC, run_id="live", kind=RecordKind.RUN_STARTED),
    ])
    service._section = AsyncMock()
    await service.control("live", ControlKind.STOP, reason="owner recovery")
    state = reduce(await service._publishing_journal.read_all("live"))
    assert state.run_status is RunStatus.STOPPED
    assert state.pending_run_effect is not None
    assert await service._controls.poll() is None
