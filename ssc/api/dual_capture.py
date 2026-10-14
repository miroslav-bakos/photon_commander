# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš
"""Durable, opt-in paired raw light acquisition through standard Alpaca cameras."""
from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from ssc.api.capture_service import (
    CameraDevice,
    Device,
    ExposureContext,
    ExposureNotAvailableError,
    begin_exposure,
    capture_one_exposure,
)
from ssc.capture.frame import FrameType
from ssc.capture.pipeline import reconcile_incomplete
from ssc.client.errors import AlpacaRemoteError
from ssc.store.capture import CaptureRepository


class DualCaptureError(RuntimeError):
    """A pair is incomplete or camera cleanup could not be verified."""


async def _camera_state(camera: CameraDevice) -> int:
    value = await camera.get("camerastate")
    if type(value) is not int:
        raise DualCaptureError(f"CameraState is unverified: {value!r}")
    return value


async def _idle(camera: CameraDevice) -> None:
    async with asyncio.timeout(5):
        while await _camera_state(camera) != 0:  # noqa: ASYNC110 - physical device polling
            await asyncio.sleep(0.1)


async def _abort_idle(camera: CameraDevice) -> None:
    put = getattr(camera, "put", None)
    if put is None:
        raise DualCaptureError("paired camera abort is unavailable")
    async with asyncio.timeout(5):
        await put("abortexposure")
        await _idle(camera)


async def _finish_cleanup(camera: CameraDevice) -> None:
    task = asyncio.create_task(_abort_idle(camera))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # HTTP/executor cancellation cannot release physical ownership.
            continue
    try:
        task.result()
    except Exception as exc:
        raise DualCaptureError(f"paired camera cleanup unverified: {exc}") from exc


async def recover_dual_ownership(*, repo: CaptureRepository, run_id: str, tele: CameraDevice | None, wide: CameraDevice | None) -> None:
    """Drain journalled ambiguous starts before ordinary admission checks."""
    async with repo.dual_capture_lock:
        await _recover_dual_locked(repo=repo, run_id=run_id, tele=tele, wide=wide)


async def _recover_dual_locked(*, repo: CaptureRepository, run_id: str, tele: CameraDevice | None, wide: CameraDevice | None) -> None:
    for role, camera in (("tele", tele), ("wide", wide)):
        pending = await repo.pending_dual_owned(run_id, role)
        if not pending:
            continue
        if camera is None:
            raise DualCaptureError(f"cannot recover owned {role} exposure: camera unavailable")
        if not await camera.is_connected():
            await camera.connect()
        await _finish_cleanup(camera)
        for block_id, frame_index in pending:
            await repo.settle_dual_member(run_id, block_id, frame_index, role)


@asynccontextmanager
async def dual_motion_guard(*, repo: CaptureRepository, tele: CameraDevice | None, wide: CameraDevice | None) -> AsyncIterator[None]:
    """Keep cleanup, ownership checks and motion dispatch atomic with pair starts."""
    async with repo.dual_capture_lock:
        for run_id in await repo.pending_dual_runs():
            await _recover_dual_locked(repo=repo, run_id=run_id, tele=tele, wide=wide)
        yield


async def capture_pair(
    *, tele: CameraDevice, wide: CameraDevice, telescope: Device | None,
    filterwheel: Device | None, focuser: Device | None, repo: CaptureRepository,
    run_id: str, block_id: str, frame_index: int, duration_s: float,
    gain: int, context: ExposureContext, folder: Path, filename_template: str,
    tz: ZoneInfo,
) -> int:
    async with repo.dual_capture_lock:
        return await _capture_pair_locked(tele=tele, wide=wide, telescope=telescope,
            filterwheel=filterwheel, focuser=focuser, repo=repo, run_id=run_id,
            block_id=block_id, frame_index=frame_index, duration_s=duration_s,
            gain=gain, context=context, folder=folder, filename_template=filename_template, tz=tz)


async def _capture_pair_locked(
    *, tele: CameraDevice, wide: CameraDevice, telescope: Device | None,
    filterwheel: Device | None, focuser: Device | None, repo: CaptureRepository,
    run_id: str, block_id: str, frame_index: int, duration_s: float,
    gain: int, context: ExposureContext, folder: Path, filename_template: str,
    tz: ZoneInfo,
) -> int:
    """Save missing pair members only; never return before both cameras are Idle."""
    if telescope is None:
        raise DualCaptureError("paired acquisition requires verified mount motion state")
    if not await telescope.is_connected():
        await telescope.connect()
    slewing = await telescope.get("slewing")
    if slewing is not False:
        raise DualCaptureError(f"paired acquisition blocked: Telescope.Slewing={slewing!r}")
    for camera in (tele, wide):
        if not await camera.is_connected():
            await camera.connect()
    minimum = float(cast(float, await wide.get("exposuremin")))
    maximum = min(60.0, float(cast(float, await wide.get("exposuremax"))))
    if not all(math.isfinite(value) for value in (minimum, maximum, duration_s)) or not minimum <= duration_s <= maximum:
        raise DualCaptureError(f"Wide companion requires the same exposure within {minimum:g}-{maximum:g} seconds; requested {duration_s:g}")
    intent = json.dumps({"exposure_s": duration_s, "gain": gain, "target": context.object_name}, sort_keys=True)
    await repo.begin_dual_pair(run_id, block_id, frame_index, intent)
    existing = {}
    for role, camera in (("tele", tele), ("wide", wide)):
        prior = await repo.dual_issued(run_id, block_id, frame_index, role)
        if await _camera_state(camera) != 0:
            if not prior:
                raise DualCaptureError(f"{role} camera is busy without owned exposure intent")
            await _finish_cleanup(camera)
            await repo.settle_dual_member(run_id, block_id, frame_index, role)
        record = await repo.dual_member(run_id, block_id, frame_index, role)
        if record is not None and record.status != "complete":
            await reconcile_incomplete(repo, records=[record])
            record = await repo.dual_member(run_id, block_id, frame_index, role)
        if record is not None:
            if not await asyncio.to_thread(record.path.is_file):
                raise DualCaptureError(f"saved {role} companion frame is missing; preserving its identity")
            existing[role] = record.id

    async def member(role: str, camera: CameraDevice) -> int:
        if role in existing:
            return existing[role]
        if role == "wide":
            put = getattr(camera, "put", None)
            if put is None:
                raise DualCaptureError("Wide gain control is unavailable")
            await put("gain", Gain=gain)
        await repo.dual_issued(run_id, block_id, frame_index, role, mark=True)
        owned = True  # A transport failure after dispatch leaves ambiguous ownership.
        try:
            try:
                started_at = await begin_exposure(camera=camera, telescope=telescope if role == "tele" else None,
                    filterwheel=filterwheel if role == "tele" else None, duration_s=duration_s)
            except (AlpacaRemoteError, ExposureNotAvailableError):
                owned = False  # Explicit rejection never grants camera ownership.
                await repo.settle_dual_member(run_id, block_id, frame_index, role)
                raise
            frame_id = await capture_one_exposure(
                camera=camera, telescope=telescope if role == "tele" else None,
                filterwheel=filterwheel if role == "tele" else None,
                focuser=focuser if role == "tele" else None, repo=repo,
                folder=folder, duration_s=duration_s,
                context=replace(context, optic_id=role, wide_camera=role == "wide", focal_length_mm=None),
                frame_type=FrameType.LIGHT, folder_template=f"%TARGET%/%DATEOFCAPTURE%/{role}",
                filename_template=filename_template, tz=tz, sequence=frame_index,
                run_id=run_id, block_id=block_id, frame_index=frame_index, started_at=started_at,
            )
            await _idle(camera)
            await repo.settle_dual_member(run_id, block_id, frame_index, role)
            owned = False
            return frame_id
        finally:
            if owned:
                await _finish_cleanup(camera)
                await repo.settle_dual_member(run_id, block_id, frame_index, role)

    tasks = [asyncio.create_task(member(role, camera), name=f"companion-{role}") for role, camera in (("tele", tele), ("wide", wide))]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise DualCaptureError(f"paired light incomplete: {type(errors[0]).__name__}: {errors[0]}") from errors[0]
    return cast(int, results[0])
