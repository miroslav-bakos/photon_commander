import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from ssc.api.capture_orchestrator import CaptureService


@pytest.mark.parametrize('save_fails', [False, True])
async def test_dawn_preparation_stops_automatic_frames_even_when_stack_save_fails(save_fails):
    capture = object.__new__(CaptureService)
    capture._capture_loop = object()
    capture._calibration_cancel_requested = False
    capture._live_stack_active = True
    capture._publish = Mock()
    capture._calibration_task = asyncio.create_task(asyncio.Event().wait())
    capture._live_stack_task = asyncio.create_task(asyncio.Event().wait())
    capture.stop_scheduled_live_stack = AsyncMock(side_effect=OSError('disk full') if save_fails else None)
    try:
        if save_fails:
            with pytest.raises(OSError, match='disk full'):
                await capture.prepare_dawn_safety()
        else:
            await capture.prepare_dawn_safety()
        await asyncio.gather(capture._calibration_task, capture._live_stack_task, return_exceptions=True)
        assert capture._capture_loop is None
        assert capture._calibration_cancel_requested is True
        assert capture._live_stack_active is False
        assert capture._calibration_task.cancelled()
        assert capture._live_stack_task.cancelled()
        capture.stop_scheduled_live_stack.assert_awaited_once()
        capture._publish.assert_called_once_with(loop_active=False)
    finally:
        capture._calibration_task.cancel()
        capture._live_stack_task.cancel()
