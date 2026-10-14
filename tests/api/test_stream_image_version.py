from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from ssc.api.capture_orchestrator import CaptureService
from ssc.stacking.live import LiveStackRun
from ssc.streaming.rtsp import RtspConfig


def test_stream_content_revision_ignores_clock_but_tracks_new_pixels(tmp_path: Path) -> None:
    capture = object.__new__(CaptureService)
    now = datetime(2026, 10, 14, tzinfo=UTC)
    pixels = np.zeros((20, 30, 3), dtype=np.float32)
    run = LiveStackRun(1, tmp_path / 'live_001.fits', tmp_path / 'live_001.json',
                       (tmp_path / 'light1.fits',), None, None, None, 1)
    engine = SimpleNamespace(run=run, pixels=pixels, bayer_pattern=None,
                             presentation_pixels=lambda **_: pixels)
    capture._live_stack = engine
    capture._live_stack_active = True
    capture._live_stack_rejected_count = 0
    capture._live_stack_pending_count = 0
    capture._live_stack_error = None
    capture._wide_rtsp_version = 3
    capture._wide_rtsp_pixels = None
    capture._wide_rtsp_bayer_pattern = None
    capture._settings = SimpleNamespace(streaming=SimpleNamespace(wide_camera_picture_in_picture=False))
    capture._rtsp = SimpleNamespace(status=SimpleNamespace(config=RtspConfig()))
    capture._now = lambda: now
    capture._stream_local_time = lambda moment=None: now if moment is None else moment
    capture.live_stack_target_header_text = lambda: 'M31'

    first = capture._live_stack_rtsp_frame()
    assert first is not None
    now += timedelta(seconds=10)
    clock = capture._live_stack_rtsp_frame()
    assert clock is not None and clock.version != first.version
    assert clock.image_version == first.image_version

    engine.run = replace(run, frame_paths=(*run.frame_paths, tmp_path / 'light2.fits'))
    new_light = capture._live_stack_rtsp_frame()
    assert new_light is not None and new_light.image_version != first.image_version
    engine.run = replace(engine.run, dither_group_boundaries=(('dither-1', 2),))
    dither = capture._live_stack_rtsp_frame()
    assert dither is not None and dither.image_version != new_light.image_version
    capture._rtsp.status.config = replace(capture._rtsp.status.config, auto_crop=False)
    crop = capture._live_stack_rtsp_frame()
    assert crop is not None and crop.image_version != dither.image_version
    capture._settings.streaming.wide_camera_picture_in_picture = True
    inset = capture._live_stack_rtsp_frame()
    capture._wide_rtsp_version += 1
    refreshed = capture._live_stack_rtsp_frame()
    assert inset is not None and refreshed is not None
    assert refreshed.image_version == inset.image_version
    assert refreshed.picture_in_picture_version != inset.picture_in_picture_version
