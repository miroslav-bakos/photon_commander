# SPDX-License-Identifier: GPL-3.0-only
"""Bottom-up recovery verifies ownership/link/camera/tracking without moving."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ssc.api.capture_orchestrator import CaptureAlreadyRunningError, CaptureService
from ssc.api.device_commands import AlpacaCapturePort
from ssc.api.tasks import TaskRegistry
from ssc.scheduler.executor import DeviceCommandError
from ssc.state.models import CaptureState, StateSnapshot
from ssc.state.ports import DeviceGatewayError
from ssc.state.store import StateStore


def service_for(camera):
    return CaptureService(camera=camera, telescope=None, filterwheel=None,
                          repo=SimpleNamespace(), settings=SimpleNamespace(),
                          state=StateStore(StateSnapshot(server_started_at=datetime.now(UTC))),
                          tasks=TaskRegistry(), section=CaptureState(), sleep=AsyncMock())


async def test_recovery_connects_then_reconciles_owned_camera_and_tracking(caplog):
    steps = []
    state = 2

    async def get(member):
        steps.append(member)
        return state

    async def put(member):
        nonlocal state
        steps.append(member)
        state = 0

    camera = SimpleNamespace(get=AsyncMock(side_effect=get), put=AsyncMock(side_effect=put))
    service = service_for(camera)
    service._failed_exposure_camera = camera
    stack = object()
    service._live_stack = stack

    async def link():
        steps.append('link')

    async def tracking(**kwargs):
        steps.append('tracking')

    service._restore_dither_tracking = AsyncMock(side_effect=tracking)
    with caplog.at_level('INFO'):
        await service.recover_scheduled_operation(level=2, operation='expose_frame', ensure_link=link)
    assert steps[0] == 'link'
    assert steps.index('abortexposure') < steps.index('tracking')
    assert service._failed_exposure_camera is None
    assert service._live_stack is stack
    assert 'stage=camera_idle' in caplog.text and 'stage=tracking' in caplog.text
    assert 'stage=complete' in caplog.text


async def test_unknown_camera_owner_is_never_aborted():
    camera = SimpleNamespace(get=AsyncMock(return_value=2), put=AsyncMock())
    service = service_for(camera)
    with pytest.raises(CaptureAlreadyRunningError, match='unknown acquisition'):
        await service.recover_scheduled_operation(level=1, operation='expose_frame')
    camera.put.assert_not_awaited()
    assert service._section.last_error.startswith('Recovery level 1 camera_idle:')


async def test_center_recovery_does_not_enable_tracking_at_unknown_reconnected_pointing():
    camera = SimpleNamespace(get=AsyncMock(return_value=0), put=AsyncMock())
    service = service_for(camera)
    service._restore_dither_tracking = AsyncMock(side_effect=DeviceGatewayError('below horizon'))
    ensure = AsyncMock()
    await service.recover_scheduled_operation(level=3, operation='center_target', ensure_link=ensure)
    ensure.assert_awaited_once()
    camera.get.assert_awaited_with('camerastate')
    service._restore_dither_tracking.assert_not_awaited()


async def test_other_owner_blocks_reconnect_and_cleanup():
    camera = SimpleNamespace(get=AsyncMock(return_value=2), put=AsyncMock())
    service = service_for(camera)
    service._auxiliary_camera_busy = lambda: True
    ensure = AsyncMock()
    with pytest.raises(CaptureAlreadyRunningError, match='Another camera owner'):
        await service.recover_scheduled_operation(level=2, operation='expose_frame', ensure_link=ensure)
    ensure.assert_not_awaited()
    camera.put.assert_not_awaited()
    camera.get.assert_not_awaited()


async def test_explicit_disconnect_remains_authoritative():
    camera = SimpleNamespace(get=AsyncMock(return_value=0), put=AsyncMock())
    service = service_for(camera)
    ensure = AsyncMock(side_effect=DeviceGatewayError('disconnected by request'))
    with pytest.raises(DeviceGatewayError, match='disconnected by request'):
        await service.recover_scheduled_operation(level=2, operation='expose_frame', ensure_link=ensure)
    camera.get.assert_not_awaited()
    camera.put.assert_not_awaited()
    assert 'link:' in service._section.last_error


@pytest.mark.parametrize('error', [TimeoutError('camera timed out'), DeviceGatewayError('offline')])
async def test_adapter_translates_operational_recovery_failures(error):
    port = object.__new__(AlpacaCapturePort)
    port._recover_operation = AsyncMock(side_effect=error)
    with pytest.raises(DeviceCommandError, match=str(error)):
        await port.recover_operation(level=2, operation='expose_frame')


async def test_recovery_reserves_camera_while_reconnect_waits():
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    camera = SimpleNamespace(get=AsyncMock(return_value=0), put=AsyncMock())
    service = service_for(camera)
    service._restore_dither_tracking = AsyncMock()

    async def ensure():
        started.set()
        await release.wait()

    repair = asyncio.create_task(service.recover_scheduled_operation(
        level=2, operation='expose_frame', ensure_link=ensure))
    await started.wait()
    assert service.section.in_progress
    with pytest.raises(CaptureAlreadyRunningError):
        await service.prepare_scheduled_capture()
    with pytest.raises(CaptureAlreadyRunningError):
        await service.recover_scheduled_operation(level=1, operation='expose_frame')
    release.set()
    await repair
    assert not service.section.in_progress


async def test_durable_stop_interrupts_tracking_repair_without_extra_commands():
    from ssc.api.capture_orchestrator import CaptureDitherStoppedError
    from ssc.state.events import RunChanged
    from ssc.state.models import RunLifecycle, RunState

    at = datetime.now(UTC)
    active = RunState(run_id='live', plan_id='plan', plan_name='night', plan_revision=64,
                      night_date=at.date(), status=RunLifecycle.RUNNING, started_at=at)
    camera = SimpleNamespace(get=AsyncMock(return_value=0), put=AsyncMock())
    service = service_for(camera)
    service._state.apply(RunChanged(run=active))
    telescope = SimpleNamespace(get=AsyncMock(), put=AsyncMock())
    service._telescope = telescope

    async def stop_during_wait(delay):
        assert service.scheduled_recovery_active
        service._state.apply(RunChanged(run=active.model_copy(update={'status': RunLifecycle.STOPPED})))

    service._sleep = AsyncMock(side_effect=stop_during_wait)
    with pytest.raises(CaptureDitherStoppedError, match='durable run Stop'):
        await service.recover_scheduled_operation(
            level=2, operation='expose_frame', ensure_link=AsyncMock())
    assert not service.scheduled_recovery_active
    camera.put.assert_not_awaited()
    telescope.put.assert_not_awaited()
    telescope.get.assert_not_awaited()
