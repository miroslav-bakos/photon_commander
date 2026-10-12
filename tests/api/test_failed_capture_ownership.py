# SPDX-License-Identifier: GPL-3.0-only
"""Failed acquisition retains ownership until actual camera idle is confirmed."""
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from ssc.api.capture_orchestrator import CaptureAlreadyRunningError, CaptureService
from ssc.api.capture_service import ExposureContext, ExposureTimeoutError
from ssc.api.tasks import TaskRegistry
from ssc.capture import FrameType
from ssc.state.models import CaptureState, StateSnapshot
from ssc.state.store import StateStore


@pytest.mark.asyncio
@pytest.mark.parametrize('idle', [2, None, '0', False])
async def test_failed_exposure_blocks_auxiliary_until_integer_idle(monkeypatch, idle):
    camera = SimpleNamespace(get=AsyncMock(return_value=idle), put=AsyncMock())
    service = CaptureService(camera=camera, telescope=None, filterwheel=None,
                             repo=SimpleNamespace(), settings=SimpleNamespace(),
                             state=StateStore(StateSnapshot(server_started_at=datetime.now(UTC))),
                             tasks=TaskRegistry(), section=CaptureState(in_progress=True),
                             sleep=AsyncMock())
    monkeypatch.setattr('ssc.api.capture_orchestrator.capture_one_exposure',
                        AsyncMock(side_effect=ExposureTimeoutError('not ready')))
    await service._finish(camera=camera, wide=False, started_at=datetime.now(UTC),
                          duration_s=1, context=ExposureContext(object_name=None,
                          focal_length_mm=None, telescope_name=None, ssc_version='test',
                          device_firmware=None), frame_type=FrameType.LIGHT,
                          folder_template='%TARGET%', filename_template='x', tz=ZoneInfo('UTC'),
                          sequence=1, folder=Path('/tmp'), dither_applied=False,
                          center_after_drift=False, loop_automation=False)
    assert service.section.in_progress
    with pytest.raises(CaptureAlreadyRunningError):
        await service.prepare_auxiliary_capture()
    assert service.section.in_progress
    camera.put.assert_awaited_with('abortexposure')
    camera.get.return_value = 0
    await service.prepare_auxiliary_capture()
    assert not service.section.in_progress
    assert service._failed_exposure_camera is None


@pytest.mark.asyncio
async def test_scheduled_failure_retains_owner_and_main_request_can_recover():
    camera = SimpleNamespace(get=AsyncMock(return_value=2), put=AsyncMock())
    service = CaptureService(camera=camera, telescope=None, filterwheel=None,
                             repo=SimpleNamespace(), settings=SimpleNamespace(),
                             state=StateStore(StateSnapshot(server_started_at=datetime.now(UTC))),
                             tasks=TaskRegistry(), section=CaptureState(in_progress=True),
                             sleep=AsyncMock())
    service.fail_scheduled_exposure(ExposureTimeoutError('not ready'))
    assert service.section.in_progress
    with pytest.raises(CaptureAlreadyRunningError):
        await service.prepare_main_capture()
    camera.get.return_value = 0
    await service.prepare_main_capture()
    assert not service.section.in_progress
