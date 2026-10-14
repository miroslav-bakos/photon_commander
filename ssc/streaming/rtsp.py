# SPDX-License-Identifier: GPL-3.0-only

"""Small RTSP/RTP-over-TCP server for a cached, low-rate H.264 frame."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import random
import re
import shutil
import struct
import subprocess
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ssc.capture.fits import debayer
from ssc.skymap.png import balance_rgb_for_display, nina_autostretch


@dataclass(frozen=True, slots=True)
class RtspConfig:
    host: str = "0.0.0.0"
    port: int = 8554
    path: str = "live"
    width: int = 1280
    height: int = 720
    fps: float = 1.0
    bitrate_kbps: int = 1500
    update_interval_s: float = 5.0
    auto_crop: bool = True
    wide_camera_until_stacked: bool = False


@dataclass(frozen=True, slots=True)
class RtspFrame:
    pixels: np.ndarray
    bayer_pattern: str | None
    overlay_lines: tuple[str, ...]
    version: str
    header_text: str | None = None
    picture_in_picture_pixels: np.ndarray | None = None
    picture_in_picture_bayer_pattern: str | None = None
    image_version: str | None = None
    picture_in_picture_version: str | None = None


@dataclass(frozen=True, slots=True)
class RtspStatus:
    active: bool
    url: str | None
    clients: int
    encoded_version: str | None
    last_error: str | None
    config: RtspConfig | None
    encoder: str | None


@dataclass(eq=False, slots=True)
class _Client:
    writer: asyncio.StreamWriter
    session: str
    channel: int = 0
    playing: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RtspError(RuntimeError):
    """The broadcast stream cannot be started or encoded."""


class RtspStreamer:
    """Serve a cached all-intra H.264 image through RTSP interleaved TCP."""

    def __init__(
        self,
        frame_provider: Callable[[], RtspFrame | None],
        task_factory: Callable[
            [Coroutine[object, object, None]], asyncio.Task[None]
        ] = asyncio.create_task,
    ) -> None:
        self._frame_provider = frame_provider
        self._task_factory = task_factory
        self._server: asyncio.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._config: RtspConfig | None = None
        self._clients: set[_Client] = set()
        self._nals: tuple[bytes, ...] = ()
        self._encoded_version: str | None = None
        self._last_encoded_at = 0.0
        self._last_error: str | None = None
        self._encoder: str | None = None
        self._sequence = random.randrange(0, 65536)
        self._timestamp = random.randrange(0, 2**32)
        self._ssrc = random.randrange(1, 2**32)
        self._display_cache: tuple[object, np.ndarray] | None = None
        self._inset_cache: tuple[object, np.ndarray] | None = None
        self._preview_lock = threading.Lock()
        self._preview_state_lock = threading.Lock()
        self._preview_generation = 0
        self._preview_cache: tuple[tuple[bytes, ...], bytes] | None = None

    @property
    def status(self) -> RtspStatus:
        config = self._config
        url = None if config is None else f"rtsp://HOST:{config.port}/{config.path}"
        return RtspStatus(
            active=self._server is not None,
            url=url,
            clients=sum(client.playing for client in self._clients),
            encoded_version=self._encoded_version,
            last_error=self._last_error,
            config=config,
            encoder=self._encoder,
        )

    async def start(self, config: RtspConfig) -> RtspStatus:
        if self._server is not None:
            raise RtspError("RTSP live-stack stream is already active")
        if shutil.which("ffmpeg") is None:
            raise RtspError("ffmpeg is required for RTSP H.264 encoding")
        if not (1 <= config.port <= 65535):
            raise RtspError("RTSP port must be between 1 and 65535")
        if config.width < 320 or config.height < 180 or config.width % 2 or config.height % 2:
            raise RtspError("RTSP dimensions must be even and at least 320x180")
        if not (0.2 <= config.fps <= 5.0):
            raise RtspError("RTSP frame rate must be between 0.2 and 5 fps")
        if not (100 <= config.bitrate_kbps <= 20_000):
            raise RtspError("RTSP bitrate must be between 100 and 20000 kbps")
        path = config.path.strip("/")
        if not path or not re.fullmatch(r"[A-Za-z0-9_-]+", path):
            raise RtspError("RTSP path may contain only letters, numbers, underscore, and dash")
        self._config = RtspConfig(
            host=config.host,
            port=config.port,
            path=path,
            width=config.width,
            height=config.height,
            fps=config.fps,
            bitrate_kbps=config.bitrate_kbps,
            update_interval_s=config.update_interval_s,
            auto_crop=config.auto_crop,
            wide_camera_until_stacked=config.wide_camera_until_stacked,
        )
        try:
            self._server = await asyncio.start_server(self._handle_client, config.host, config.port)
        except OSError as exc:
            self._config = None
            raise RtspError(f"could not listen on {config.host}:{config.port}: {exc}") from exc
        self._last_encoded_at = 0.0
        self._display_cache = self._inset_cache = None
        self._task = self._task_factory(self._broadcast_loop())
        return self.status

    async def stop(self) -> RtspStatus:
        with self._preview_state_lock:
            self._preview_generation += 1
            self._preview_cache = None
            self._nals = ()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        clients = list(self._clients)
        self._clients.clear()
        for client in clients:
            client.writer.close()
            with contextlib.suppress(OSError):
                await client.writer.wait_closed()
        self._config = None
        self._nals = ()
        self._encoded_version = None
        self._encoder = None
        self._display_cache = self._inset_cache = None
        return self.status

    def preview_png(self) -> bytes:
        """Decode the exact cached H.264 access unit clients are receiving.

        This deliberately previews the encoded frame rather than rendering
        the provider again, so the browser shows the same source, crop and
        overlays as VLC/OBS.
        """
        with self._preview_lock:
            with self._preview_state_lock:
                nals = self._nals
                generation = self._preview_generation
                cached = self._preview_cache
            if not nals:
                raise RtspError("the stream has not encoded a frame yet")
            if cached is not None and cached[0] == nals:
                return cached[1]
            rendered = self._decode_preview(nals)
            with self._preview_state_lock:
                if generation != self._preview_generation:
                    raise RtspError("the stream changed while decoding its preview")
                # A normal refresh may overtake this valid request. Return its
                # access unit without installing it as the new frame's cache.
                if nals == self._nals:
                    self._preview_cache = (nals, rendered)
            return rendered

    @staticmethod
    def _decode_preview(nals: tuple[bytes, ...]) -> bytes:
        payload = b"".join(b"\x00\x00\x00\x01" + nal for nal in nals)
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "h264",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            input=payload,
            capture_output=True,
            check=False,
            timeout=20,
        )
        if completed.returncode != 0 or not completed.stdout:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RtspError(f"could not decode the RTSP preview: {error}")
        return completed.stdout

    async def _broadcast_loop(self) -> None:
        assert self._config is not None
        loop = asyncio.get_running_loop()
        encoding: asyncio.Task[tuple[bytes, str]] | None = None
        encoding_version: str | None = None
        try:
            while True:
                if encoding is not None and encoding.done():
                    try:
                        encoded, encoder = encoding.result()
                        nals = self._split_annex_b(encoded)
                        if not nals:
                            raise RtspError("ffmpeg produced no H.264 NAL units")
                        with self._preview_state_lock:
                            self._nals = nals
                        self._encoded_version = encoding_version
                        self._encoder = encoder
                        self._last_error = None
                    except Exception as exc:
                        self._last_error = f"{type(exc).__name__}: {exc}"
                    encoding = None
                    encoding_version = None

                frame = self._frame_provider()
                now = loop.time()
                if (
                    encoding is None
                    and frame is not None
                    and frame.version != self._encoded_version
                    and now - self._last_encoded_at >= self._config.update_interval_s
                ):
                    encoding_version = frame.version
                    self._last_encoded_at = now
                    encoding = asyncio.create_task(
                        asyncio.to_thread(self._encode_cached, frame, self._config)
                    )

                # Encoding a refreshed 1920x1080 image can take several seconds.
                # Keep serving the previous access unit while that happens so
                # RTSP/YouTube clients never see an artificial ingest gap.
                if self._nals:
                    await self._broadcast_access_unit(self._nals)
                await asyncio.sleep(1.0 / self._config.fps)
        finally:
            if encoding is not None:
                # Cancelling to_thread cannot stop its native worker. Retain
                # ownership until it exits before stop clears cached arrays.
                while not encoding.done():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(encoding)
                if not encoding.cancelled():
                    encoding.exception()

    @staticmethod
    def _escape_drawtext(text: str) -> str:
        return text.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")

    @staticmethod
    def _display_rgb(pixels: np.ndarray, bayer_pattern: str | None) -> np.ndarray:
        if pixels.ndim == 2 and bayer_pattern is not None:
            display = nina_autostretch(
                balance_rgb_for_display(debayer(pixels.astype(np.float64), bayer_pattern))
            )
        elif pixels.ndim == 3 and pixels.shape[2] == 3:
            display = nina_autostretch(balance_rgb_for_display(pixels))
        elif pixels.ndim == 2:
            mono = nina_autostretch(pixels.astype(np.float64))
            display = np.repeat(mono[..., None], 3, axis=2)
        else:
            raise RtspError(f"RTSP source must be a 2-D image or RGB, got {pixels.shape}")
        rgb = np.ascontiguousarray(display, dtype=np.uint8)
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise RtspError(f"RTSP source must be RGB, got {rgb.shape}")
        return rgb

    @staticmethod
    def _add_picture_in_picture(rgb: np.ndarray, inset: np.ndarray) -> np.ndarray:
        """Place a small aspect-preserving inset at the source image's upper left."""
        source_height, source_width = rgb.shape[:2]
        inset_height, inset_width = inset.shape[:2]
        max_width = max(1, source_width * 3 // 10)
        max_height = max(1, source_height * 3 // 10)
        scale = min(max_width / inset_width, max_height / inset_height)
        width = max(1, round(inset_width * scale))
        height = max(1, round(inset_height * scale))
        y_indices = np.linspace(0, inset_height - 1, height).round().astype(int)
        x_indices = np.linspace(0, inset_width - 1, width).round().astype(int)
        resized = inset[y_indices[:, None], x_indices]
        margin = max(4, min(source_height, source_width) // 100)
        border = max(2, min(source_height, source_width) // 300)
        result = rgb.copy()
        y_end = min(source_height, margin + height + 2 * border)
        x_end = min(source_width, margin + width + 2 * border)
        result[margin:y_end, margin:x_end] = 255
        result[
            margin + border : margin + border + height,
            margin + border : margin + border + width,
        ] = resized
        return result

    def _encode_cached(self, frame: RtspFrame, config: RtspConfig) -> tuple[bytes, str]:
        def key(pixels: np.ndarray, pattern: str | None, version: str | None) -> object:
            content = ("frame", frame.version) if version is None else ("image", version)
            return content, pattern, pixels.shape, pixels.dtype.str

        base_key = key(frame.pixels, frame.bayer_pattern, frame.image_version)
        if self._display_cache is None or self._display_cache[0] != base_key:
            self._display_cache = (base_key, self._display_rgb(frame.pixels, frame.bayer_pattern))
        rgb = self._display_cache[1]
        if frame.picture_in_picture_pixels is not None:
            inset_key = key(frame.picture_in_picture_pixels, frame.picture_in_picture_bayer_pattern, frame.picture_in_picture_version)
            if self._inset_cache is None or self._inset_cache[0] != inset_key:
                self._inset_cache = (inset_key, self._display_rgb(frame.picture_in_picture_pixels, frame.picture_in_picture_bayer_pattern))
            rgb = self._add_picture_in_picture(rgb, self._inset_cache[1])
        else:
            self._inset_cache = None
        return self._encode(frame, config, prepared_rgb=rgb)

    @classmethod
    def _encode(cls, frame: RtspFrame, config: RtspConfig, *, prepared_rgb: np.ndarray | None = None) -> tuple[bytes, str]:
        rgb = prepared_rgb if prepared_rgb is not None else cls._display_rgb(frame.pixels, frame.bayer_pattern)
        if prepared_rgb is None and frame.picture_in_picture_pixels is not None:
            inset = cls._display_rgb(
                frame.picture_in_picture_pixels, frame.picture_in_picture_bayer_pattern
            )
            rgb = cls._add_picture_in_picture(rgb, inset)
        source_height, source_width = rgb.shape[:2]
        font_size = max(12, config.height // 60)
        line_gap = max(3, config.height // 360)
        vertical_margin = max(8, config.height // 90)
        footer = vertical_margin * 2 + 4 * font_size + 3 * line_gap
        filters = [
            f"scale={config.width}:{config.height}:force_original_aspect_ratio=decrease:flags=lanczos",
            f"pad={config.width}:{config.height}:(ow-iw)/2:(oh-ih)/2:black",
            "setsar=1",
            f"drawbox=x=0:y=ih-{footer}:w=iw:h={footer}:color=black@0.85:t=fill",
        ]
        if frame.header_text is not None:
            header_size = max(14, config.height // 50)
            header_height = header_size + 2 * vertical_margin
            filters.extend(
                (
                    f"drawbox=x=0:y=0:w=iw:h={header_height}:color=black@0.85:t=fill",
                    "drawtext="
                    f"font='DejaVu Sans':text='{cls._escape_drawtext(frame.header_text)}':"
                    f"fontcolor=white:fontsize={header_size}:x=(w-text_w)/2:y=({header_height}-text_h)/2",
                )
            )
        for index, line in enumerate(frame.overlay_lines[:4]):
            y = config.height - footer + vertical_margin + index * (font_size + line_gap)
            filters.append(
                "drawtext="
                f"font='DejaVu Sans':text='{cls._escape_drawtext(line)}':"
                f"fontcolor=white:fontsize={font_size}:x=(w-text_w)/2:y={y}"
            )
        filters.append("format=yuv420p")
        common = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{source_width}x{source_height}",
            "-framerate",
            f"{config.fps:g}",
            "-i",
            "pipe:0",
            "-vf",
            ",".join(filters),
            "-frames:v",
            "1",
            "-an",
        ]
        software = [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "baseline",
            "-b:v",
            f"{config.bitrate_kbps}k",
            "-g",
            "1",
            "-x264-params",
            "repeat-headers=1:aud=1",
            "-f",
            "h264",
            "pipe:1",
        ]
        candidates: list[tuple[str, list[str]]] = []
        model_path = Path("/proc/device-tree/model")
        model = model_path.read_text(errors="ignore") if model_path.is_file() else ""
        # Pi 4 has a dedicated H.264 encoder exposed through V4L2 M2M. Pi 5
        # deliberately does not; its recommended path is the fast libx264
        # software encoder, especially appropriate for SSC's one frame/sec.
        if "Raspberry Pi 4" in model and Path("/dev/video11").exists():
            candidates.append(
                (
                    "h264_v4l2m2m",
                    [
                        "-pix_fmt",
                        "yuv420p",
                        "-c:v",
                        "h264_v4l2m2m",
                        "-b:v",
                        f"{config.bitrate_kbps}k",
                        "-g",
                        "1",
                        "-f",
                        "h264",
                        "pipe:1",
                    ],
                )
            )
        candidates.append(("libx264-ultrafast", software))
        errors = []
        for encoder, options in candidates:
            completed = subprocess.run(
                [*common, *options],
                input=rgb.tobytes(),
                capture_output=True,
                check=False,
                timeout=20,
            )
            if completed.returncode == 0 and completed.stdout:
                return completed.stdout, encoder
            errors.append(completed.stderr.decode("utf-8", errors="replace").strip())
        raise RtspError(f"ffmpeg H.264 encode failed: {'; '.join(filter(None, errors))}")

    @staticmethod
    def _split_annex_b(payload: bytes) -> tuple[bytes, ...]:
        starts = list(re.finditer(b"\x00\x00\x00?\x01", payload))
        units = []
        for index, match in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(payload)
            unit = payload[match.end() : end]
            if unit:
                units.append(unit)
        return tuple(units)

    async def _broadcast_access_unit(self, nals: tuple[bytes, ...]) -> None:
        clients = [client for client in self._clients if client.playing]
        if not clients:
            return
        packets: list[bytes] = []
        for nal_index, nal in enumerate(nals):
            packets.extend(self._packetize(nal, marker=nal_index == len(nals) - 1))
        dead: list[_Client] = []
        for client in clients:
            try:
                async with client.lock:
                    for packet in packets:
                        client.writer.write(
                            b"$"
                            + bytes((client.channel,))
                            + struct.pack(">H", len(packet))
                            + packet
                        )
                    await asyncio.wait_for(client.writer.drain(), timeout=2.0)
            except (TimeoutError, OSError):
                dead.append(client)
        for client in dead:
            self._clients.discard(client)
            client.writer.close()
        assert self._config is not None
        self._timestamp = (self._timestamp + round(90_000 / self._config.fps)) & 0xFFFFFFFF

    def _packetize(self, nal: bytes, *, marker: bool) -> list[bytes]:
        maximum = 1200
        if len(nal) <= maximum:
            return [self._rtp_header(marker=marker) + nal]
        indicator = (nal[0] & 0xE0) | 28
        nal_type = nal[0] & 0x1F
        chunks = [nal[index : index + maximum - 2] for index in range(1, len(nal), maximum - 2)]
        packets = []
        for index, chunk in enumerate(chunks):
            fu_header = nal_type
            if index == 0:
                fu_header |= 0x80
            if index == len(chunks) - 1:
                fu_header |= 0x40
            packets.append(
                self._rtp_header(marker=marker and index == len(chunks) - 1)
                + bytes((indicator, fu_header))
                + chunk
            )
        return packets

    def _rtp_header(self, *, marker: bool) -> bytes:
        header = struct.pack(
            ">BBHII",
            0x80,
            (0x80 if marker else 0) | 96,
            self._sequence,
            self._timestamp,
            self._ssrc,
        )
        self._sequence = (self._sequence + 1) & 0xFFFF
        return header

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client = _Client(writer=writer, session=f"{random.randrange(1, 2**31):08x}")
        self._clients.add(client)
        try:
            while True:
                request = await reader.readuntil(b"\r\n\r\n")
                lines = request.decode("utf-8", errors="replace").split("\r\n")
                method, url, _protocol = lines[0].split(" ", 2)
                headers = {
                    key.lower(): value.strip()
                    for line in lines[1:]
                    if ":" in line
                    for key, value in [line.split(":", 1)]
                }
                cseq = headers.get("cseq", "1")
                if method == "OPTIONS":
                    await self._reply(
                        client,
                        cseq,
                        headers={
                            "Public": "OPTIONS, DESCRIBE, SETUP, PLAY, GET_PARAMETER, TEARDOWN"
                        },
                    )
                elif method == "DESCRIBE":
                    body = self._sdp(url).encode()
                    await self._reply(
                        client,
                        cseq,
                        headers={
                            "Content-Type": "application/sdp",
                            "Content-Base": url.rstrip("/") + "/",
                        },
                        body=body,
                    )
                elif method == "SETUP":
                    transport = headers.get("transport", "")
                    match = re.search(r"interleaved=(\d+)-(\d+)", transport)
                    if "RTP/AVP/TCP" not in transport.upper() or match is None:
                        await self._reply(client, cseq, status="461 Unsupported Transport")
                        continue
                    client.channel = int(match.group(1))
                    await self._reply(
                        client,
                        cseq,
                        headers={
                            "Transport": f"RTP/AVP/TCP;unicast;interleaved={match.group(1)}-{match.group(2)}",
                            "Session": client.session,
                        },
                    )
                elif method == "PLAY":
                    client.playing = True
                    await self._reply(
                        client, cseq, headers={"Session": client.session, "Range": "npt=0.000-"}
                    )
                elif method == "GET_PARAMETER":
                    await self._reply(client, cseq, headers={"Session": client.session})
                elif method == "TEARDOWN":
                    await self._reply(client, cseq, headers={"Session": client.session})
                    return
                else:
                    await self._reply(client, cseq, status="405 Method Not Allowed")
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            self._clients.discard(client)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _reply(
        self,
        client: _Client,
        cseq: str,
        *,
        status: str = "200 OK",
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        fields = {"CSeq": cseq, "Server": "Photon Commander", **(headers or {})}
        if body:
            fields["Content-Length"] = str(len(body))
        payload = (
            f"RTSP/1.0 {status}\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in fields.items())
            + "\r\n"
        )
        async with client.lock:
            client.writer.write(payload.encode() + body)
            await client.writer.drain()

    def _sdp(self, url: str) -> str:
        config = self._config
        frame_rate = 1.0 if config is None else config.fps
        parameter_sets = ""
        sps = next((nal for nal in self._nals if nal and nal[0] & 0x1F == 7), None)
        pps = next((nal for nal in self._nals if nal and nal[0] & 0x1F == 8), None)
        if sps is not None and pps is not None:
            profile_level_id = sps[1:4].hex() if len(sps) >= 4 else "42e01f"
            encoded_sps = base64.b64encode(sps).decode("ascii")
            encoded_pps = base64.b64encode(pps).decode("ascii")
            parameter_sets = (
                "a=fmtp:96 packetization-mode=1;"
                f"profile-level-id={profile_level_id};"
                f"sprop-parameter-sets={encoded_sps},{encoded_pps}\r\n"
            )
        else:
            parameter_sets = "a=fmtp:96 packetization-mode=1\r\n"
        return (
            "v=0\r\n"
            "o=- 0 0 IN IP4 0.0.0.0\r\n"
            "s=Photon Commander live stack\r\n"
            "t=0 0\r\n"
            "a=control:*\r\n"
            "m=video 0 RTP/AVP 96\r\n"
            "a=rtpmap:96 H264/90000\r\n"
            f"a=framerate:{frame_rate:g}\r\n"
            f"{parameter_sets}"
            f"a=control:{url.rstrip('/')}/trackID=0\r\n"
        )


__all__ = ["RtspConfig", "RtspError", "RtspFrame", "RtspStatus", "RtspStreamer"]
