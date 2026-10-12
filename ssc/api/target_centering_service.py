# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Closed-loop catalogue pointing: slew, solve, correct, and verify."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from ssc.api.camera_fov import invalidate_solved_camera_fov, publish_solved_camera_fov
from ssc.api.centering_defaults import (
    DEFAULT_CENTERING_TOLERANCE_ARCSEC,
    DEFAULT_SCHEDULED_CENTERING_TOLERANCE_ARCMIN,
)
from ssc.astro.frames import MountFrame, frame_offset_arcsec, j2000_coord
from ssc.astro.units import Deg, Hours, hours_to_deg, wrap_hours
from ssc.platesolve import PlateSolveResult
from ssc.state.models import MountState, MountTrackingRate
from ssc.state.ports import MountNotAvailableError, MountRefusedError
from ssc.state.store import StateStore


class CenteringDevice(Protocol):
    async def mount_slew(self, *, ra_hours: float, dec_deg: float) -> None: ...

    async def mount_sync(self, *, ra_hours: float, dec_deg: float) -> None: ...

    async def mount_set_tracking(
        self, *, enabled: bool, rate: MountTrackingRate | None
    ) -> MountState: ...


class CenteringSolver(Protocol):
    async def solve_here(
        self, *, exposure_s: float, gain: int | None = None
    ) -> PlateSolveResult: ...


class TargetCenteringError(Exception):
    """The closed-loop pointing operation could not be completed safely."""


class TargetCenteringBusyError(TargetCenteringError):
    """Another closed-loop pointing operation is already active."""


@dataclass(frozen=True, slots=True)
class TargetCenteringAttempt:
    attempt: int
    commanded_ra_j2000_h: float
    commanded_dec_j2000_deg: float
    solved_ra_j2000_h: float
    solved_dec_j2000_deg: float
    error_arcmin: float


@dataclass(frozen=True, slots=True)
class TargetCenteringResult:
    converged: bool
    target_ra_j2000_h: float
    target_dec_j2000_deg: float
    tolerance_arcmin: float
    attempts: tuple[TargetCenteringAttempt, ...]


CenteringStatus = Literal["idle", "running", "succeeded", "failed"]
CenteringStage = Literal[
    "idle",
    "slewing",
    "settling",
    "tracking",
    "capturing_solving",
    "evaluating",
    "correcting",
    "complete",
    "failed",
]


@dataclass(frozen=True, slots=True)
class TargetCenteringProgress:
    """Live, read-only view of one closed-loop pointing transaction."""

    status: CenteringStatus = "idle"
    stage: CenteringStage = "idle"
    attempt: int = 0
    max_attempts: int = 0
    target_ra_j2000_h: float | None = None
    target_dec_j2000_deg: float | None = None
    tolerance_arcmin: float | None = None
    attempts: tuple[TargetCenteringAttempt, ...] = ()
    message: str = "No centering operation has run yet."
    error: str | None = None
    updated_at: datetime | None = None


class TargetCenteringService:
    """One-at-a-time, bounded pointing correction above the device seam."""

    def __init__(
        self,
        *,
        device: CenteringDevice,
        solver: CenteringSolver,
        state: StateStore,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        settle_timeout_s: float = 120.0,
        settle_poll_s: float = 0.25,
        target_tolerance_arcsec: float = DEFAULT_CENTERING_TOLERANCE_ARCSEC,
    ) -> None:
        self._device = device
        self._solver = solver
        self._state = state
        self._sleep = sleep
        self._now = now
        self._settle_timeout_s = settle_timeout_s
        self._settle_poll_s = settle_poll_s
        self._target_tolerance_arcsec = target_tolerance_arcsec
        self._lock = asyncio.Lock()
        self._progress = TargetCenteringProgress()

    @property
    def progress(self) -> TargetCenteringProgress:
        return self._progress

    async def center_default(self, ra_j2000_h: float, dec_j2000_deg: float) -> None:
        """Center an automatic correction to the scheduled-imaging 60″ policy."""
        result = await self.center(
            ra_j2000_h=ra_j2000_h,
            dec_j2000_deg=dec_j2000_deg,
            tolerance_arcmin=DEFAULT_SCHEDULED_CENTERING_TOLERANCE_ARCMIN,
            exposure_s=1.0,
            gain=200,
            max_attempts=3,
            sync_before_correction=True,
        )
        if not result.converged:
            final_error = result.attempts[-1].error_arcmin if result.attempts else None
            raise TargetCenteringError(
                f"automatic centering did not converge within 60 arcseconds; "
                f"final measured error {final_error} arcmin; retry required"
            )

    def _publish_progress(self, **updates: object) -> None:
        values = {
            field: getattr(self._progress, field) for field in self._progress.__dataclass_fields__
        }
        values.update(updates)
        values["updated_at"] = self._now()
        self._progress = TargetCenteringProgress(**values)

    async def center(
        self,
        *,
        ra_j2000_h: float,
        dec_j2000_deg: float,
        tolerance_arcmin: float,
        exposure_s: float,
        gain: int | None,
        max_attempts: int,
        sync_before_correction: bool = False,
    ) -> TargetCenteringResult:
        if self._lock.locked():
            raise TargetCenteringBusyError("target centering is already in progress")
        async with self._lock:
            self._progress = TargetCenteringProgress(
                status="running",
                stage="slewing",
                attempt=1,
                max_attempts=max_attempts,
                target_ra_j2000_h=ra_j2000_h,
                target_dec_j2000_deg=dec_j2000_deg,
                tolerance_arcmin=tolerance_arcmin,
                message="Sending the first slew to the requested frame center.",
                updated_at=self._now(),
            )
            try:
                return await self._center_locked(
                    ra_j2000_h=ra_j2000_h,
                    dec_j2000_deg=dec_j2000_deg,
                    tolerance_arcmin=tolerance_arcmin,
                    exposure_s=exposure_s,
                    gain=gain,
                    max_attempts=max_attempts,
                    sync_before_correction=sync_before_correction,
                )
            except asyncio.CancelledError:
                self._publish_progress(
                    status="failed",
                    stage="failed",
                    message="Centering was cancelled.",
                    error="cancelled",
                )
                raise
            except Exception as exc:
                self._publish_progress(
                    status="failed",
                    stage="failed",
                    message="Centering stopped before the requested location was confirmed.",
                    error=str(exc),
                )
                raise

    async def _center_locked(
        self,
        *,
        ra_j2000_h: float,
        dec_j2000_deg: float,
        tolerance_arcmin: float,
        exposure_s: float,
        gain: int | None,
        max_attempts: int,
        sync_before_correction: bool = False,
    ) -> TargetCenteringResult:
        target = j2000_coord(hours_to_deg(Hours(ra_j2000_h)), Deg(dec_j2000_deg))
        command_ra = ra_j2000_h
        command_dec = dec_j2000_deg
        attempts: list[TargetCenteringAttempt] = []
        for attempt_number in range(1, max_attempts + 1):
            correction = attempt_number > 1
            self._publish_progress(
                stage="slewing",
                attempt=attempt_number,
                message=(
                    f"Applying correction slew {attempt_number} of {max_attempts}."
                    if correction
                    else f"Slewing to the requested frame center (attempt 1 of {max_attempts})."
                ),
            )
            await self._device.mount_slew(ra_hours=command_ra, dec_deg=command_dec)
            invalidate_solved_camera_fov(self._state)
            self._publish_progress(
                stage="settling",
                message="Waiting for the mount to stop and report the commanded position twice.",
            )
            await self._wait_until_reached(command_ra, command_dec)
            self._publish_progress(
                stage="tracking", message="Confirming sidereal tracking before exposure."
            )
            await self._ensure_tracking()
            self._publish_progress(
                stage="capturing_solving",
                message=f"Capturing a {exposure_s:g}-second frame and plate solving it.",
            )
            try:
                solved = await self._solver.solve_here(exposure_s=exposure_s, gain=gain)
            except Exception as exc:
                raise TargetCenteringError(
                    f"plate solve failed on centering attempt {attempt_number}: {exc}"
                ) from exc
            publish_solved_camera_fov(self._state, solved)
            self._publish_progress(
                stage="evaluating", message="Comparing the solved center with the requested frame."
            )
            error_arcmin = float(frame_offset_arcsec(target, solved.coord)) / 60.0
            attempts.append(
                TargetCenteringAttempt(
                    attempt=attempt_number,
                    commanded_ra_j2000_h=command_ra,
                    commanded_dec_j2000_deg=command_dec,
                    solved_ra_j2000_h=float(solved.coord.ra_h),
                    solved_dec_j2000_deg=float(solved.coord.dec_deg),
                    error_arcmin=error_arcmin,
                )
            )
            self._publish_progress(attempts=tuple(attempts))
            if error_arcmin <= tolerance_arcmin:
                self._publish_progress(
                    stage="evaluating",
                    message="Location confirmed; synchronizing the mount model to the solved field.",
                )
                await self._device.mount_sync(
                    ra_hours=float(solved.coord.ra_h), dec_deg=float(solved.coord.dec_deg)
                )
                self._publish_progress(
                    status="succeeded",
                    stage="complete",
                    message=f"Location confirmed within {error_arcmin:.2f} arcmin.",
                )
                return TargetCenteringResult(
                    converged=True,
                    target_ra_j2000_h=ra_j2000_h,
                    target_dec_j2000_deg=dec_j2000_deg,
                    tolerance_arcmin=tolerance_arcmin,
                    attempts=tuple(attempts),
                )

            self._publish_progress(
                stage="correcting",
                message=(
                    f"Measured error {error_arcmin:.2f} arcmin; calculating the next correction."
                ),
            )
            if sync_before_correction and attempt_number < max_attempts:
                # Rebase the pointing model on measured sky coordinates before
                # asking for the original mosaic center again. Telemetry alone
                # can report success while small unsynchronized corrections
                # leave the actual field unchanged.
                await self._device.mount_sync(
                    ra_hours=float(solved.coord.ra_h), dec_deg=float(solved.coord.dec_deg)
                )
                command_ra = ra_j2000_h
                command_dec = dec_j2000_deg
                continue

            # Apply the measured residual to the previous command. RA is
            # circular; declination is bounded. For the small corrections
            # this loop accepts, this is the mount-model error in the same
            # J2000 coordinate basis as both command and solve.
            ra_error_h = (ra_j2000_h - float(solved.coord.ra_h) + 12.0) % 24.0 - 12.0
            command_ra = float(wrap_hours(command_ra + ra_error_h))
            command_dec = max(
                -90.0,
                min(90.0, command_dec + dec_j2000_deg - float(solved.coord.dec_deg)),
            )

        self._publish_progress(
            status="failed",
            stage="failed",
            message=f"The final solve is still outside {tolerance_arcmin:.2f} arcmin.",
            error="maximum correction attempts reached",
        )
        return TargetCenteringResult(
            converged=False,
            target_ra_j2000_h=ra_j2000_h,
            target_dec_j2000_deg=dec_j2000_deg,
            tolerance_arcmin=tolerance_arcmin,
            attempts=tuple(attempts),
        )

    async def _wait_until_reached(self, ra_h: float, dec_deg: float) -> None:
        target = j2000_coord(hours_to_deg(Hours(ra_h)), Deg(dec_deg))
        deadline = self._now() + timedelta(seconds=self._settle_timeout_s)
        stable_samples = 0
        while True:
            mount = self._state.snapshot.mount
            position = self._mount_j2000(mount)
            if mount is not None and mount.slewing is False and position is not None:
                reported = j2000_coord(hours_to_deg(Hours(position[0])), Deg(position[1]))
                if float(frame_offset_arcsec(target, reported)) <= self._target_tolerance_arcsec:
                    stable_samples += 1
                    if stable_samples >= 2:
                        return
                else:
                    stable_samples = 0
            else:
                stable_samples = 0
            if self._now() >= deadline:
                raise TargetCenteringError(
                    f"the mount did not reach the correction target within {self._settle_timeout_s:.0f}s"
                )
            await self._sleep(self._settle_poll_s)

    async def _ensure_tracking(self) -> None:
        # A snapshot can still describe tracking from before the async slew.
        # Command and read back sidereal tracking after the stopped move rather
        # than treating cached True as confirmation for the upcoming exposure.
        last = "mount did not report tracking"
        for attempt in range(3):
            try:
                result = await self._device.mount_set_tracking(
                    enabled=True, rate="sidereal"
                )
            except (MountNotAvailableError, MountRefusedError) as exc:
                last = str(exc)
            else:
                if result.tracking is True:
                    return
                last = f"mount reports tracking={result.tracking!r}"
            if attempt < 2:
                await self._sleep(0.5)
        raise TargetCenteringError(f"could not start sidereal tracking: {last}")

    @staticmethod
    def _mount_j2000(mount: MountState | None) -> tuple[float, float] | None:
        if mount is None:
            return None
        if mount.ra_j2000_h is not None and mount.dec_j2000_deg is not None:
            return mount.ra_j2000_h, mount.dec_j2000_deg
        if (
            mount.equatorial_system is MountFrame.J2000
            and mount.ra_hours is not None
            and mount.dec_deg is not None
        ):
            return mount.ra_hours, mount.dec_deg
        return None
