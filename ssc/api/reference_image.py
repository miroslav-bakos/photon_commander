# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš
"""Bounded private reference-image solving; never a device measurement or slew."""
from __future__ import annotations

import asyncio
import math
import struct
import tempfile
import zlib
from pathlib import Path

from ssc.api.external_tools import solver_environment
from ssc.platesolve.astap import astap_solve_reference
from ssc.platesolve.base import (
    PlateSolveError,
    ReferencePlateSolveResult,
    SolveFailedError,
    SolverUnavailableError,
    SolveTimedOutError,
)
from ssc.settings.store import SettingsStore

MAX_REFERENCE_BYTES = 64 * 1024 * 1024
MAX_REFERENCE_PIXELS = 32_000_000


def reference_image_suffix(content: bytes) -> str:
    """Validate actual image headers, not client filenames or MIME declarations."""
    width = height = 0
    suffix = ""
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 33 and content[12:16] == b"IHDR":
        width, height = struct.unpack_from(">II", content, 16)
        pos = 8
        image_data = False
        complete = False
        while pos + 12 <= len(content):
            length = int.from_bytes(content[pos:pos + 4], "big")
            kind = content[pos + 4:pos + 8]
            end = pos + 12 + length
            if end > len(content):
                break
            expected = int.from_bytes(content[end - 4:end], "big")
            if zlib.crc32(memoryview(content)[pos + 4:end - 4]) != expected:
                break
            if (pos == 8 and (kind != b"IHDR" or length != 13)) or (pos != 8 and kind == b"IHDR"):
                break
            image_data |= kind == b"IDAT" and length > 0
            if kind == b"IEND":
                complete = length == 0 and end == len(content)
                break
            pos = end
        if complete and image_data:
            suffix = ".png"
    elif content.startswith(b"\xff\xd8"):
        pos = 2
        for _ in range(4096):
            if pos + 4 > min(len(content), 1024 * 1024) or content[pos] != 255:
                break
            marker = content[pos + 1]
            if marker == 255:
                pos += 1
                continue
            if marker in {0xD9, 0xDA}:
                break
            length = int.from_bytes(content[pos + 2:pos + 4], "big")
            if length < 2 or pos + 2 + length > len(content):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 8:
                height, width = struct.unpack_from(">HH", content, pos + 5)
                if content.endswith(b"\xff\xd9"):
                    suffix = ".jpg"
                break
            pos += 2 + length
    elif content.startswith(b"SIMPLE  ="):
        cards: dict[str, str] = {}
        end = None
        for pos in range(0, min(len(content), 1024 * 1024) - 79, 80):
            card = content[pos:pos + 80]
            key = card[:8].decode("ascii", errors="replace").strip()
            if key == "END":
                end = ((pos + 80 + 2879) // 2880) * 2880
                break
            if card[8:10] == b"= ":
                if key in cards and key in {"SIMPLE", "BITPIX", "NAXIS", "NAXIS1", "NAXIS2", "NAXIS3"}:
                    raise ValueError("Upload a valid FITS, PNG or JPEG image (at most 32 megapixels).")
                cards[key] = card[10:].decode("ascii", errors="replace").split("/", 1)[0].strip()
        try:
            width, height = int(cards["NAXIS1"]), int(cards["NAXIS2"])
            axes, bitpix = int(cards["NAXIS"]), int(cards["BITPIX"])
            channels = int(cards["NAXIS3"]) if axes == 3 else 1
            if (cards.get("SIMPLE") != "T" or end is None or axes not in {2, 3}
                    or channels not in {1, 3} or bitpix not in {8, 16, 32, 64, -32, -64}
                    or end + width * height * channels * (abs(bitpix) // 8) > len(content)):
                raise ValueError
            suffix = ".fits"
        except (KeyError, ValueError):
            suffix = ""
    if (not suffix or not 1 <= width <= 16384 or not 1 <= height <= 16384
            or width * height > MAX_REFERENCE_PIXELS):
        raise ValueError("Upload a valid FITS, PNG or JPEG image (at most 32 megapixels).")
    return suffix


async def solve_reference_image(content: bytes, settings: SettingsStore,
                                data_dir: Path) -> ReferencePlateSolveResult:
    suffix = reference_image_suffix(content)
    with tempfile.TemporaryDirectory(prefix="reference-solve-", dir=data_dir) as directory:
        image = Path(directory) / ("reference" + suffix)
        write = asyncio.create_task(asyncio.to_thread(image.write_bytes, content))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise
        try:
            result = await astap_solve_reference(image, env=solver_environment(settings))
        except SolverUnavailableError as exc:
            raise ValueError("ASTAP or its star database is unavailable. Check the plate solver settings.") from exc
        except SolveTimedOutError as exc:
            raise ValueError("Reference image solving timed out.") from exc
        except SolveFailedError as exc:
            raise ValueError("No plate-solve solution was found for this image.") from exc
        except PlateSolveError as exc:
            raise ValueError("Could not solve the uploaded image.") from exc
        if (not math.isfinite(result.pixel_scale_arcsec) or result.pixel_scale_arcsec <= 0
                or not math.isfinite(result.rotation_deg)):
            raise ValueError("The plate solver returned an invalid image solution.")
        return result
