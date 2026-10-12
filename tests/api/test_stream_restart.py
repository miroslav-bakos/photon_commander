# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ssc.api.capture_orchestrator import CaptureService
from ssc.stacking.live import LiveStackError
from ssc.streaming import RtspConfig, RtspStatus, YoutubeStreamStatus


def _service(*, youtube_active: bool = False) -> CaptureService:
    service = object.__new__(CaptureService)
    service._rtsp = SimpleNamespace(  # type: ignore[attr-defined]
        status=RtspStatus(
            active=True,
            url="rtsp://HOST:8554/live",
            clients=0,
            encoded_version="wide:1",
            last_error=None,
            config=RtspConfig(wide_camera_until_stacked=True),
            encoder="test",
        )
    )
    service._youtube = SimpleNamespace(  # type: ignore[attr-defined]
        status=YoutubeStreamStatus(active=youtube_active, last_error=None)
    )
    service._rtsp_automatic = True  # type: ignore[attr-defined]
    return service


async def test_restart_rebuilds_active_stream_from_latest_settings() -> None:
    service = _service()
    config = RtspConfig(width=1920, height=1080, bitrate_kbps=6000)
    restarted = RtspStatus(
        active=True,
        url="rtsp://HOST:8554/live",
        clients=0,
        encoded_version=None,
        last_error=None,
        config=config,
        encoder=None,
    )
    service._configured_rtsp_config = Mock(return_value=config)  # type: ignore[method-assign]
    service.stop_live_stack_rtsp = AsyncMock()  # type: ignore[method-assign]
    service.start_live_stack_rtsp = AsyncMock(return_value=restarted)  # type: ignore[method-assign]

    result = await service.restart_live_stack_rtsp()

    service.stop_live_stack_rtsp.assert_awaited_once()  # type: ignore[attr-defined]
    service.start_live_stack_rtsp.assert_awaited_once_with(config, automatic=True)  # type: ignore[attr-defined]
    assert result is restarted


async def test_restart_refuses_while_youtube_owns_the_stream() -> None:
    service = _service(youtube_active=True)
    service.stop_live_stack_rtsp = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(LiveStackError, match="stop the YouTube relay"):
        await service.restart_live_stack_rtsp()

    service.stop_live_stack_rtsp.assert_not_awaited()  # type: ignore[attr-defined]


async def test_automatic_stream_timeout_is_recoverable_on_next_mount_update() -> None:
    from ssc.state.models import MountState
    service = _service()
    service._rtsp.status = RtspStatus(active=False, url=None, clients=0, encoded_version=None, last_error=None, config=None, encoder=None)
    service._settings = SimpleNamespace(streaming=SimpleNamespace(broadcast_when_unparked=True))
    service._state = SimpleNamespace(snapshot=SimpleNamespace(mount=MountState(at_park=False)))
    service._configured_rtsp_config = Mock(return_value=RtspConfig(wide_camera_until_stacked=True))
    service.start_live_stack_rtsp = AsyncMock(side_effect=[TimeoutError("read deadline"), RtspStatus(active=True, url=None, clients=0, encoded_version=None, last_error=None, config=None, encoder=None)])
    await service.reconcile_stream_automation()
    await service.reconcile_stream_automation()
    assert service.start_live_stack_rtsp.await_count == 2
