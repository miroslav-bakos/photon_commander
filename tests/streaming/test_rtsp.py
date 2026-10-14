import asyncio
import base64
import shutil
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import numpy as np
import pytest

from ssc.streaming.rtsp import RtspConfig, RtspFrame, RtspStreamer


def _frame() -> RtspFrame:
    y, x = np.indices((90, 160))
    rgb = np.stack((x % 256, y % 256, (x + y) % 256), axis=2).astype(np.uint8)
    return RtspFrame(
        pixels=rgb,
        bayer_pattern=None,
        overlay_lines=("LIVE M31 | integration 300s", "accepted 10 | gain 100"),
        version="10",
        header_text="M31 · next: M45 (starting 22:00)",
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_encoder_produces_annex_b_parameter_sets_and_idr() -> None:
    payload, encoder = RtspStreamer._encode(
        _frame(), RtspConfig(width=320, height=180, bitrate_kbps=500)
    )
    types = [nal[0] & 0x1F for nal in RtspStreamer._split_annex_b(payload)]
    assert encoder in {"h264_v4l2m2m", "libx264-ultrafast"}
    assert 7 in types  # SPS
    assert 8 in types  # PPS
    assert 5 in types  # IDR


def test_stream_text_is_centred_and_wide_camera_can_be_inset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def encode(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.append(command[command.index("-vf") + 1])
        return subprocess.CompletedProcess(command, 0, b"\x00\x00\x00\x01\x65x", b"")

    monkeypatch.setattr(subprocess, "run", encode)
    frame = _frame()
    frame = RtspFrame(
        pixels=frame.pixels,
        bayer_pattern=frame.bayer_pattern,
        overlay_lines=frame.overlay_lines,
        version=frame.version,
        header_text=frame.header_text,
        picture_in_picture_pixels=np.full((30, 60, 3), 255, dtype=np.uint8),
    )
    RtspStreamer._encode(frame, RtspConfig(width=320, height=180, bitrate_kbps=500))

    drawtexts = [item for item in captured[0].split(",") if item.startswith("drawtext=")]
    assert len(drawtexts) == 3
    assert all(":x=(w-text_w)/2:" in item for item in drawtexts)
    assert "drawbox=x=0:y=ih-" in captured[0]
    assert captured[0].count("color=black@0.85:t=fill") == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_preview_decodes_the_exact_cached_access_unit() -> None:
    streamer = RtspStreamer(_frame)
    payload, _encoder = streamer._encode(
        _frame(), RtspConfig(width=320, height=180, bitrate_kbps=500)
    )
    streamer._nals = streamer._split_annex_b(payload)  # type: ignore[attr-defined]
    assert streamer.preview_png().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
@pytest.mark.parametrize(("width", "height", "bitrate"), [(1280, 720, 2500), (3840, 2160, 12000)])
def test_encoder_honours_selected_output_resolution(width: int, height: int, bitrate: int) -> None:
    payload, _encoder = RtspStreamer._encode(
        _frame(), RtspConfig(width=width, height=height, bitrate_kbps=bitrate)
    )
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-f",
            "h264",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            "pipe:0",
        ],
        input=payload,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert probe.returncode == 0, probe.stderr.decode(errors="replace")
    assert probe.stdout.strip() == f"{width},{height}".encode()


def test_large_nal_is_fragmented_and_marked_only_at_the_end() -> None:
    streamer = RtspStreamer(lambda: None)
    nal = bytes((0x65,)) + bytes(range(256)) * 12
    packets = streamer._packetize(nal, marker=True)
    assert len(packets) > 1
    assert packets[0][12] & 0x1F == 28
    assert packets[0][13] & 0x80
    assert not packets[0][1] & 0x80
    assert packets[-1][13] & 0x40
    assert packets[-1][1] & 0x80


def test_sdp_advertises_cached_parameter_sets_and_frame_rate() -> None:
    streamer = RtspStreamer(_frame)
    streamer._config = RtspConfig(fps=1.0)  # type: ignore[attr-defined]
    sps = bytes.fromhex("6742c01f") + b"sps"
    pps = bytes.fromhex("68ce") + b"pps"
    streamer._nals = (b"\x09\xf0", sps, pps, b"\x65frame")  # type: ignore[attr-defined]

    sdp = streamer._sdp("rtsp://127.0.0.1:8554/live")  # type: ignore[attr-defined]

    assert "a=framerate:1\r\n" in sdp
    assert "profile-level-id=42c01f" in sdp
    assert (
        "sprop-parameter-sets="
        f"{base64.b64encode(sps).decode()},{base64.b64encode(pps).decode()}"
    ) in sdp


@pytest.mark.asyncio
async def test_frame_refresh_does_not_pause_cached_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def slow_encode(
        _cls: type[RtspStreamer], _frame_value: RtspFrame, _config: RtspConfig, **_kwargs: object
    ) -> tuple[bytes, str]:
        time.sleep(0.4)
        return b"\x00\x00\x00\x01\x65new", "test"

    async def record_broadcast(_self: RtspStreamer, _nals: tuple[bytes, ...]) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(RtspStreamer, "_encode", classmethod(slow_encode))
    monkeypatch.setattr(RtspStreamer, "_broadcast_access_unit", record_broadcast)
    streamer = RtspStreamer(_frame)
    streamer._config = RtspConfig(fps=5, update_interval_s=0)  # type: ignore[attr-defined]
    streamer._nals = (b"\x65cached",)  # type: ignore[attr-defined]

    task = asyncio.create_task(streamer._broadcast_loop())  # type: ignore[attr-defined]
    try:
        await asyncio.sleep(0.3)
        assert calls >= 2
        assert streamer._encoded_version is None  # type: ignore[attr-defined]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_rtsp_tcp_handshake_and_interleaved_rtp(unused_tcp_port: int) -> None:
    streamer = RtspStreamer(_frame)
    await streamer.start(
        RtspConfig(
            host="127.0.0.1",
            port=unused_tcp_port,
            width=320,
            height=180,
            fps=5,
            bitrate_kbps=500,
            update_interval_s=0,
        )
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)

    async def request(method: str, suffix: str = "", extra: str = "") -> bytes:
        writer.write(
            f"{method} rtsp://127.0.0.1:{unused_tcp_port}/live{suffix} RTSP/1.0\r\n"
            f"CSeq: 1\r\n{extra}\r\n".encode()
        )
        await writer.drain()
        return await reader.readuntil(b"\r\n\r\n")

    assert b"200 OK" in await request("OPTIONS")
    describe = await request("DESCRIBE")
    length = int(describe.split(b"Content-Length: ", 1)[1].split(b"\r\n", 1)[0])
    assert b"H264/90000" in await reader.readexactly(length)
    assert b"200 OK" in await request(
        "SETUP", "/trackID=0", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"
    )
    assert b"200 OK" in await request("PLAY")
    prefix = await asyncio.wait_for(reader.readexactly(4), timeout=5)
    assert prefix[0] == ord("$") and prefix[1] == 0
    packet = await reader.readexactly(struct.unpack(">H", prefix[2:])[0])
    assert packet[0] >> 6 == 2
    assert packet[1] & 0x7F == 96

    writer.close()
    await writer.wait_closed()
    await streamer.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
async def test_ffmpeg_client_can_decode_the_rtsp_stream(unused_tcp_port: int) -> None:
    streamer = RtspStreamer(_frame)
    await streamer.start(
        RtspConfig(
            host="127.0.0.1",
            port=unused_tcp_port,
            width=320,
            height=180,
            fps=5,
            bitrate_kbps=500,
            update_interval_s=0,
        )
    )
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            f"rtsp://127.0.0.1:{unused_tcp_port}/live",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0, stderr.decode(errors="replace")
    finally:
        await streamer.stop()


def test_prepared_rgb_reuses_content_but_refreshes_inset_and_image(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    original = RtspStreamer._display_rgb
    def display(pixels, pattern):
        calls.append((pixels.shape, pattern))
        return original(pixels, pattern)
    monkeypatch.setattr(RtspStreamer, "_display_rgb", staticmethod(display))
    raw_inputs = []
    def encode(command, **kwargs):
        raw_inputs.append(kwargs["input"])
        return subprocess.CompletedProcess(command, 0, b"encoded", b"")
    monkeypatch.setattr(subprocess, "run", encode)
    streamer = RtspStreamer(_frame)
    worker = getattr(streamer, "_encode_cached", streamer._encode)
    frame = replace(_frame(), image_version="stack:1", picture_in_picture_pixels=np.full((10, 20, 3), 80, dtype=np.uint8), picture_in_picture_version="wide:1")
    config = RtspConfig(width=320, height=180)
    worker(frame, config)
    worker(replace(frame, version="clock:2"), config)
    assert len(calls) == 2
    assert raw_inputs[0] == raw_inputs[1]
    worker(replace(frame, version="clock:3", picture_in_picture_version="wide:2"), config)
    assert len(calls) == 3
    worker(replace(frame, version="clock:4", image_version="stack:2"), config)
    assert len(calls) == 5  # base changed; inset returns to wide:1.
    reference, _ = streamer._encode(frame, config)
    assert reference == b"encoded" and raw_inputs[-1] == raw_inputs[0]


def test_prepared_rgb_fallback_uses_frame_version_and_render_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(RtspStreamer, "_display_rgb", staticmethod(lambda pixels, pattern: calls.append(pattern) or np.zeros((*pixels.shape[:2], 3), dtype=np.uint8)))
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"encoded", b""))
    streamer = RtspStreamer(_frame)
    worker = getattr(streamer, "_encode_cached", streamer._encode)
    frame = _frame()
    config = RtspConfig()
    worker(frame, config)
    worker(replace(frame, version="changed"), config)
    assert len(calls) == 2
    stable = replace(frame, image_version="same", pixels=np.zeros((10, 10), dtype=np.uint16), bayer_pattern="RGGB")
    worker(stable, config)
    worker(replace(stable, bayer_pattern="BGGR"), config)
    worker(replace(stable, pixels=np.zeros((20, 10), dtype=np.uint16)), config)
    assert len(calls) == 5


def test_preview_single_flight_keys_exact_access_unit_without_version(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    entered, release = Event(), Event()
    def decode(command, **kwargs):
        calls.append(kwargs["input"])
        entered.set()
        assert release.wait(2)
        return subprocess.CompletedProcess(command, 0, b"png:" + kwargs["input"], b"")
    monkeypatch.setattr(subprocess, "run", decode)
    streamer = RtspStreamer(_frame)
    streamer._nals = (b"one",)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(streamer.preview_png)
        assert entered.wait(2)
        second = pool.submit(streamer.preview_png)
        release.set()
        assert first.result() == second.result() == b"png:\x00\x00\x00\x01one"
    assert len(calls) == 1
    streamer._nals = (b"two",)
    assert streamer.preview_png() == b"png:\x00\x00\x00\x01two"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_stop_drains_encode_before_clearing_prepared_arrays(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = Event(), Event()
    calls = []
    original = RtspStreamer._display_rgb
    monkeypatch.setattr(RtspStreamer, "_display_rgb", staticmethod(lambda pixels, pattern: calls.append(1) or original(pixels, pattern)))
    def encode(_cls, _frame, _config, **kwargs):
        entered.set()
        assert release.wait(2)
        return b"\x00\x00\x00\x01\x65new", "test"
    monkeypatch.setattr(RtspStreamer, "_encode", classmethod(encode))
    frame = replace(_frame(), image_version="stable")
    streamer = RtspStreamer(lambda: frame)
    streamer._config = RtspConfig(fps=5, update_interval_s=0)
    streamer._task = asyncio.create_task(streamer._broadcast_loop())
    assert await asyncio.to_thread(entered.wait, 2)
    stopping = asyncio.create_task(streamer.stop())
    await asyncio.sleep(0.02)
    assert not stopping.done()
    assert streamer._display_cache is not None
    release.set()
    await stopping
    assert streamer._display_cache is None and streamer._inset_cache is None
    assert streamer._nals == ()
    streamer._encode_cached(frame, RtspConfig())
    assert len(calls) == 2  # identical content is prepared again after stop.


@pytest.mark.asyncio
async def test_preview_cannot_publish_after_stop_and_rebuilds_same_access_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = Event(), Event()
    calls = []
    def decode(command, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return subprocess.CompletedProcess(command, 0, b"png", b"")
    monkeypatch.setattr(subprocess, "run", decode)
    streamer = RtspStreamer(_frame)
    streamer._nals = (b"same",)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(streamer.preview_png)
        assert entered.wait(2)
        await streamer.stop()
        # A restarted stream may emit identical bytes; generation still differs.
        streamer._nals = (b"same",)
        release.set()
        with pytest.raises(RuntimeError, match="changed"):
            pending.result()
    assert streamer._preview_cache is None
    streamer._nals = (b"same",)
    assert streamer.preview_png() == b"png"
    assert len(calls) == 2


def test_cached_encoding_matches_uncached_pixels_for_changed_base_and_inset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = []
    def encode(command, **kwargs):
        captured.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, b"encoded", b"")
    monkeypatch.setattr(subprocess, "run", encode)
    streamer = RtspStreamer(_frame)
    config = RtspConfig(width=320, height=180)
    inset = _frame().pixels[:30, :60].copy()
    first = replace(_frame(), image_version="stack:1", picture_in_picture_pixels=inset, picture_in_picture_version="wide:1")
    frames = [first, replace(first, version="clock:2"),
              replace(first, version="clock:3", picture_in_picture_pixels=inset[:, ::-1], picture_in_picture_version="wide:2"),
              replace(first, version="clock:4", pixels=first.pixels[:, ::-1], image_version="stack:2")]
    for frame in frames:
        streamer._encode_cached(frame, config)
        cached = captured[-1]
        streamer._encode(frame, config)
        assert captured[-1] == cached  # both raw RGB bytes and exact FFmpeg options.


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_restart_prepares_same_version_again(monkeypatch: pytest.MonkeyPatch, unused_tcp_port: int) -> None:
    calls = []
    original = RtspStreamer._display_rgb
    monkeypatch.setattr(RtspStreamer, "_display_rgb", staticmethod(lambda pixels, pattern: calls.append(1) or original(pixels, pattern)))
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"encoded", b""))
    streamer = RtspStreamer(lambda: None)
    config = RtspConfig(host="127.0.0.1", port=unused_tcp_port)
    frame = replace(_frame(), image_version="unchanged")
    try:
        await streamer.start(config)
        streamer._encode_cached(frame, config)
        await streamer.stop()
        await streamer.start(config)
        streamer._encode_cached(frame, config)
        assert len(calls) == 2
    finally:
        await streamer.stop()


def test_normal_refresh_during_preview_returns_valid_old_frame_without_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    streamer = RtspStreamer(_frame)
    old_nals, new_nals = (b"old",), (b"new",)
    streamer._nals = old_nals
    def decode(nals):
        assert nals == old_nals
        with streamer._preview_state_lock:
            streamer._nals = new_nals
        return b"valid old PNG"
    monkeypatch.setattr(streamer, "_decode_preview", decode)
    assert streamer.preview_png() == b"valid old PNG"
    assert streamer._preview_cache is None
    monkeypatch.setattr(streamer, "_decode_preview", lambda nals: b"new PNG")
    assert streamer.preview_png() == b"new PNG"
    assert streamer._preview_cache == (new_nals, b"new PNG")
