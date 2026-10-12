# SPDX-License-Identifier: GPL-3.0-only

"""Low-rate broadcast outputs derived from SSC's in-memory live stack."""

from ssc.streaming.rtsp import RtspConfig, RtspError, RtspFrame, RtspStatus, RtspStreamer
from ssc.streaming.youtube import (
    YOUTUBE_STREAM_KEY_NAME,
    YoutubePublisher,
    YoutubeStreamError,
    YoutubeStreamStatus,
)

__all__ = [
    "YOUTUBE_STREAM_KEY_NAME",
    "RtspConfig",
    "RtspError",
    "RtspFrame",
    "RtspStatus",
    "RtspStreamer",
    "YoutubePublisher",
    "YoutubeStreamError",
    "YoutubeStreamStatus",
]
