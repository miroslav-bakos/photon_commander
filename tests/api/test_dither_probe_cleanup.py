# SPDX-License-Identifier: GPL-3.0-only
from unittest.mock import AsyncMock

import pytest

from ssc.client.errors import AlpacaRemoteError
from ssc.main import _stop_dither_probe_camera


@pytest.mark.parametrize("unsupported", [False, True])
async def test_probe_cleanup_waits_for_idle_and_only_falls_back_when_unsupported(unsupported):
    camera = AsyncMock()
    if unsupported:
        camera.put.side_effect = [AlpacaRemoteError(0x400, "unsupported", member="abortexposure"), None]
    camera.get.side_effect = [2, 0]
    await _stop_dither_probe_camera(camera)
    assert [call.args[0] for call in camera.put.await_args_list] == (
        ["abortexposure", "stopexposure"] if unsupported else ["abortexposure"]
    )
    assert camera.get.await_count == 2


async def test_probe_cleanup_transport_failure_cannot_claim_camera_idle():
    camera = AsyncMock()
    camera.put.side_effect = AlpacaRemoteError(0x407, "disconnected", member="abortexposure")
    with pytest.raises(AlpacaRemoteError):
        await _stop_dither_probe_camera(camera)
    camera.get.assert_not_awaited()
    assert camera.put.await_count == 1


async def test_idle_camera_does_not_need_supported_abort_or_stop():
    camera = AsyncMock()
    camera.put.side_effect = AlpacaRemoteError(0x400, "unsupported", member="abortexposure")
    camera.get.return_value = 0
    await _stop_dither_probe_camera(camera)
    camera.put.assert_awaited_once_with("abortexposure")


@pytest.mark.parametrize("camera_state", [False, None, "0", 0.0])
async def test_probe_cleanup_rejects_invalid_idle_values(camera_state):
    camera = AsyncMock()
    camera.get.return_value = camera_state
    with pytest.raises(RuntimeError, match="invalid CameraState"):
        await _stop_dither_probe_camera(camera)
