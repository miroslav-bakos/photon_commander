# SPDX-License-Identifier: GPL-3.0-only

"""Relay SSC's local RTSP broadcast to a preconfigured YouTube Live stream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Final, Literal

YOUTUBE_RTMPS_URL: Final = "rtmps://a.rtmps.youtube.com:443/live2"
_log = logging.getLogger(__name__)
YOUTUBE_STREAM_KEY_NAME: Final = "youtube_stream_key"
YoutubeResolution = Literal["720p", "1080p", "2160p"]
_YOUTUBE_PRESETS: Final[dict[YoutubeResolution, tuple[int, int, int, str]]] = {
    # YouTube's September 2026 H.264/30 fps live-ingest recommendations.
    "720p": (1280, 720, 4_000, "veryfast"),
    "1080p": (1920, 1080, 10_000, "superfast"),
    "2160p": (3840, 2160, 30_000, "ultrafast"),
}


class YoutubeStreamError(RuntimeError):
    """The YouTube publisher could not be started or stopped cleanly."""


@dataclass(frozen=True, slots=True)
class YoutubeStreamStatus:
    active: bool
    last_error: str | None
    publishing: bool = False


class YoutubePublisher:
    """Own one FFmpeg process publishing the local stream to YouTube RTMPS."""

    def __init__(
        self,
        task_factory: Callable[
            [Coroutine[object, object, None]], asyncio.Task[None]
        ] = asyncio.create_task,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._task_factory = task_factory
        self._process: asyncio.subprocess.Process | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._stopping = False
        self._secret_marker: str | None = None
        self._clock = clock
        self._frame_count = 0
        self._last_progress_at: float | None = None

    @property
    def status(self) -> YoutubeStreamStatus:
        process = self._process
        return YoutubeStreamStatus(
            active=process is not None and process.returncode is None,
            last_error=self._last_error,
            publishing=(
                process is not None
                and process.returncode is None
                and self._last_progress_at is not None
                and self._clock() - self._last_progress_at <= 10.0
            ),
        )

    async def start(
        self,
        *,
        rtsp_url: str,
        stream_key: str,
        resolution: YoutubeResolution = "720p",
    ) -> YoutubeStreamStatus:
        if self.status.active:
            raise YoutubeStreamError("YouTube publishing is already active")
        if shutil.which("ffmpeg") is None:
            raise YoutubeStreamError("ffmpeg is required for YouTube publishing")
        key = stream_key.strip()
        if not key:
            raise YoutubeStreamError("no YouTube stream key is configured")
        local_url = rtsp_url.replace("HOST", "127.0.0.1")
        output_url = f"{YOUTUBE_RTMPS_URL}/{key}"
        width, height, bitrate_kbps, encoder_preset = _YOUTUBE_PRESETS[resolution]
        bitrate = f"{bitrate_kbps}k"
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostats",
            "-progress",
            "pipe:2",
            # SPS/PPS and frame rate come from SSC's SDP. Skip FFmpeg's long
            # live-input probing and publish from the first complete frame.
            "-analyzeduration",
            "0",
            "-probesize",
            "32",
            "-fpsprobesize",
            "0",
            "-rtsp_transport",
            "tcp",
            # Every SSC access unit is an independent IDR frame. A large
            # frame-thread queue adds many seconds of latency at one fps and
            # starves RTMPS after its first batch; decode these frames inline.
            "-threads",
            "1",
            "-i",
            local_url,
            # A lavfi source otherwise produces silence as fast as the CPU can
            # generate it. Pace it to wall clock so audio timestamps cannot run
            # ahead of SSC's deliberately low-frame-rate RTSP video.
            "-re",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            (
                # RTP timestamps intentionally start at a random offset. Rebase
                # first, and scale the one-fps astronomy source before expanding
                # it to YouTube's 30 fps.  Expanding first made FFmpeg resize the
                # same 4K frame thirty times per second and could fall behind
                # real time even on a machine with ample upload bandwidth.
                "setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
                # Keep the expanded 30 fps frames evenly wall-clock paced.
                # The former 0.1-second reset limit turned brief encoder stalls
                # into one-second output bursts, which YouTube reported as
                # intermittent ingest starvation despite sufficient bandwidth.
                "fps=30,realtime=speed=1"
            ),
            "-c:v",
            "libx264",
            "-preset",
            encoder_preset,
            "-tune",
            "zerolatency",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            # Astronomy frames can remain nearly identical for several seconds.
            # Merely setting minrate=maxrate does not make libx264 emit filler,
            # so YouTube can mistake the tiny bursts for an ingest underflow.
            # HRD CBR keeps bytes and timestamps arriving continuously.
            "-x264-params",
            "nal-hrd=cbr:force-cfr=1",
            "-b:v",
            bitrate,
            "-minrate",
            bitrate,
            "-maxrate",
            bitrate,
            "-bufsize",
            f"{bitrate_kbps * 2}k",
            "-r",
            "30",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-fps_mode",
            "cfr",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-flvflags",
            "no_duration_filesize",
            "-flush_packets",
            "1",
            "-f",
            "flv",
            output_url,
        ]
        self._last_error = None
        self._frame_count = 0
        self._last_progress_at = None
        self._stopping = False
        self._secret_marker = key
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._secret_marker = None
            raise YoutubeStreamError(f"could not start the YouTube publisher: {exc}") from exc
        self._process = process
        _log.info("YouTube publisher process started")
        self._monitor = self._task_factory(self._watch(process))
        await asyncio.sleep(0)
        return self.status

    async def stop(self) -> YoutubeStreamStatus:
        process = self._process
        monitor = self._monitor
        if process is None:
            return self.status
        self._stopping = True
        if process.returncode is None:
            process.terminate()
        if monitor is not None:
            try:
                await asyncio.wait_for(asyncio.shield(monitor), timeout=5)
            except TimeoutError:
                monitor.cancel()
                if process.returncode is None:
                    process.kill()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
                await process.wait()
        self._process = None
        self._monitor = None
        self._secret_marker = None
        self._stopping = False
        _log.info("YouTube publisher stopped")
        return self.status

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        diagnostics = bytearray()

        def record(line: bytes) -> None:
            key, separator, value = line.partition(b"=")
            if key == b"frame" and separator:
                with contextlib.suppress(ValueError):
                    if self._process is process:
                        frames = int(value.strip())
                        if frames > self._frame_count:
                            if self._frame_count == 0:
                                _log.info("YouTube publisher emitted video frames")
                            self._frame_count = frames
                            self._last_progress_at = self._clock()
                return
            if (
                key
                in {
                    b"fps",
                    b"stream_0_0_q",
                    b"bitrate",
                    b"total_size",
                    b"out_time_us",
                    b"out_time_ms",
                    b"out_time",
                    b"dup_frames",
                    b"drop_frames",
                    b"speed",
                    b"progress",
                }
                and separator
            ):
                return
            if self._secret_marker:
                line = line.replace(self._secret_marker.encode(), b"[redacted]")
            diagnostics.extend(line + b"\n")
            if len(diagnostics) > 4096:
                diagnostics[:] = diagnostics[:2048] + b"\n...\n" + diagnostics[-2048:]

        try:
            assert process.stderr is not None
            partial = b""
            dropping = False
            while chunk := await process.stderr.read(4096):
                segments = chunk.split(b"\n")
                for index, segment in enumerate(segments):
                    complete = index < len(segments) - 1
                    if dropping:
                        if complete:
                            dropping = False
                        continue
                    if len(partial) + len(segment) > 4096:
                        record(b"Publisher diagnostic line exceeded the safety limit.")
                        partial = b""
                        dropping = not complete
                        continue
                    partial += segment
                    if complete:
                        record(partial)
                        partial = b""
            if partial and not dropping:
                record(partial)
            await process.wait()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.terminate()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=5)
            raise
        if self._process is not process:
            return
        if not self._stopping:
            _log.warning("YouTube publisher exited unexpectedly (%s)", process.returncode)
            self._last_error = self._diagnostic(
                bytes(diagnostics),
                stream_key=self._secret_marker,
                returncode=-1 if process.returncode is None else process.returncode,
            )
        self._process = None
        self._monitor = None
        self._secret_marker = None
        self._last_progress_at = None

    @staticmethod
    def _diagnostic(stderr: bytes, *, stream_key: str | None, returncode: int) -> str:
        message = stderr.decode("utf-8", errors="replace").strip()
        if stream_key:
            message = message.replace(stream_key, "[redacted]")
        if len(message) > 900:
            message = f"{message[:440]}\n…\n{message[-440:]}"
        if "Broken pipe" in message:
            guidance = (
                "YouTube closed the RTMPS connection. Verify that Live streaming is enabled, "
                "the reusable stream is open in Live Control Room, and its current stream key "
                "is saved in SSC, then retry."
            )
            message = f"{message}\n{guidance}"
        return message or f"YouTube publisher exited with status {returncode}"


__all__ = [
    "YOUTUBE_RTMPS_URL",
    "YOUTUBE_STREAM_KEY_NAME",
    "YoutubePublisher",
    "YoutubeResolution",
    "YoutubeStreamError",
    "YoutubeStreamStatus",
]
