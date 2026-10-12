# SPDX-License-Identifier: GPL-3.0-only

import asyncio

import pytest

from ssc.streaming.youtube import YoutubePublisher


class _Process:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self._finished = asyncio.Event()
        self.stderr = asyncio.StreamReader()

    async def communicate(self) -> tuple[bytes, bytes]:
        await self._finished.wait()
        return b"", b""

    def terminate(self) -> None:
        self.returncode = -15
        self._finished.set()
        self.stderr.feed_eof()

    def kill(self) -> None:
        self.returncode = -9
        self._finished.set()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.mark.asyncio
async def test_youtube_publisher_uses_compatible_rtmps_command_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process()
    commands: list[tuple[str, ...]] = []

    async def spawn(*command: str, **_kwargs: object) -> _Process:
        commands.append(command)
        return process

    monkeypatch.setattr("ssc.streaming.youtube.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("ssc.streaming.youtube.asyncio.create_subprocess_exec", spawn)
    publisher = YoutubePublisher()

    started = await publisher.start(
        rtsp_url="rtsp://HOST:8554/live", stream_key="secret-stream-key"
    )
    assert started.active is True
    assert started.publishing is False
    command = commands[0]
    assert "rtsp://127.0.0.1:8554/live" in command
    assert command[command.index("-analyzeduration") + 1] == "0"
    assert command[command.index("-probesize") + 1] == "32"
    assert command[command.index("-fpsprobesize") + 1] == "0"
    assert command[command.index("-threads") + 1] == "1"
    video_filter = command[command.index("-vf") + 1]
    assert video_filter.index("scale=1280:720") < video_filter.index("fps=30")
    assert video_filter.endswith("fps=30,realtime=speed=1")
    assert command[command.index("-tune") + 1] == "zerolatency"
    assert command[command.index("-g") + 1] == "60"
    assert command[command.index("-x264-params") + 1] == "nal-hrd=cbr:force-cfr=1"
    assert command[command.index("-fps_mode") + 1] == "cfr"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-flush_packets") + 1] == "1"
    assert command[command.index("-f") + 2] == "-i"
    lavfi = command.index("lavfi")
    assert command[lavfi - 2 : lavfi] == ("-re", "-f")
    assert command[-1] == "rtmps://a.rtmps.youtube.com:443/live2/secret-stream-key"

    stopped = await publisher.stop()
    assert stopped.active is False


@pytest.mark.asyncio
async def test_publisher_requires_video_progress_and_retains_bounded_redacted_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process()

    async def spawn(*_command: str, **_kwargs: object) -> _Process:
        return process

    monkeypatch.setattr("ssc.streaming.youtube.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("ssc.streaming.youtube.asyncio.create_subprocess_exec", spawn)
    now = [0.0]
    publisher = YoutubePublisher(clock=lambda: now[0])
    await publisher.start(rtsp_url="rtsp://HOST:8554/live", stream_key="secret-stream-key")
    process.stderr.feed_data(b"frame=0\nprogress=continue\n")
    await asyncio.sleep(0)
    assert not publisher.status.publishing
    process.stderr.feed_data(b"frame=3\nprogress=continue\n")
    await asyncio.sleep(0)
    assert publisher.status.publishing
    now[0] = 11.0
    assert publisher.status.active
    assert not publisher.status.publishing
    process.stderr.feed_data(b"frame=3\nprogress=continue\n")
    await asyncio.sleep(0)
    assert not publisher.status.publishing
    process.stderr.feed_data(b"frame=4\nprogress=continue\n")
    await asyncio.sleep(0)
    assert publisher.status.publishing
    process.stderr.feed_data(
        b"x" * 70_000 + b"secret-stream-key\nServer rejected secret-stream-key\n"
    )
    process.returncode = 1
    process._finished.set()
    process.stderr.feed_eof()
    assert publisher._monitor is not None
    await publisher._monitor
    assert not publisher.status.active
    assert not publisher.status.publishing
    assert publisher.status.last_error is not None
    assert "Server rejected [redacted]" in publisher.status.last_error
    assert "secret-stream-key" not in publisher.status.last_error
    assert len(publisher.status.last_error) < 1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolution", "size", "bitrate", "preset"),
    [
        ("720p", "1280:720", "4000k", "veryfast"),
        ("1080p", "1920:1080", "10000k", "superfast"),
        ("2160p", "3840:2160", "30000k", "ultrafast"),
    ],
)
async def test_youtube_quality_presets_use_resolution_appropriate_encoder_settings(
    monkeypatch: pytest.MonkeyPatch,
    resolution: str,
    size: str,
    bitrate: str,
    preset: str,
) -> None:
    process = _Process()
    commands: list[tuple[str, ...]] = []

    async def spawn(*command: str, **_kwargs: object) -> _Process:
        commands.append(command)
        return process

    monkeypatch.setattr("ssc.streaming.youtube.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("ssc.streaming.youtube.asyncio.create_subprocess_exec", spawn)
    publisher = YoutubePublisher()

    await publisher.start(
        rtsp_url="rtsp://HOST:8554/live",
        stream_key="secret-stream-key",
        resolution=resolution,  # type: ignore[arg-type]
    )
    command = commands[0]

    assert f"scale={size}" in command[command.index("-vf") + 1]
    assert command[command.index("-b:v") + 1] == bitrate
    assert command[command.index("-minrate") + 1] == bitrate
    assert command[command.index("-maxrate") + 1] == bitrate
    assert command[command.index("-preset") + 1] == preset
    await publisher.stop()


def test_youtube_diagnostic_keeps_root_cause_and_redacts_stream_key() -> None:
    key = "secret-stream-key"
    stderr = (
        f"[rtmps] Server rejected {key}: invalid stream key\n".encode()
        + b"x" * 1_000
        + b"\nError writing trailer: Broken pipe"
    )
    message = YoutubePublisher._diagnostic(stderr, stream_key=key, returncode=1)
    assert "Server rejected [redacted]: invalid stream key" in message
    assert key not in message
    assert "YouTube closed the RTMPS connection" in message
