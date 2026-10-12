# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""SSC-driven autofocus over standard ASCOM Camera and Focuser members.

ASCOM deliberately has no autofocus command.  On firmware exposing the Seestar's
native Alpaca server the focuser supplies ``Move``/``Position``/``IsMoving`` and
the camera supplies exposures, but neither device supplies a vendor autofocus
action.  SSC therefore measures a focus curve itself: seven two-second frames at
10-step coarse intervals, then a second curve at 2-step fine intervals.

The current filter is left in place.  That is intentional: silently focusing
through IR and then imaging through LP is an optical error (DESIGN.md 4.3.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

import numpy as np

from ssc.api.capture_service import (
    CameraDevice,
    ExposureContext,
    ExposureTimeoutError,
    drive_exposure,
)
from ssc.api.tasks import TaskRegistry
from ssc.capture import FrameType, measure_frame_quality
from ssc.capture.fits import bayer_pattern, debayer
from ssc.client.errors import AlpacaErrorNumber, AlpacaRemoteError, AlpacaTransportError
from ssc.state.events import AutofocusChanged, NoticeCleared, NoticeRaised
from ssc.state.models import (
    AutofocusCurvePoint,
    AutofocusSampleState,
    AutofocusStage,
    AutofocusState,
    Notice,
    NoticeLevel,
)
from ssc.state.store import StateStore

_log = logging.getLogger(__name__)

DEFAULT_EXPOSURE_S: Final = 2.0
DEFAULT_STEP: Final = 10
DEFAULT_FINE_STEP: Final = 2
DEFAULT_HALF_SPAN: Final = 3
DEFAULT_FINE_HALF_SPAN: Final = 2
DEFAULT_MOVE_TIMEOUT_S: Final = 10.0
DEFAULT_MOVE_POLL_S: Final = 0.25
MIN_SETTLE_S: Final = 2.0
DEFAULT_SETTLE_S: Final = MIN_SETTLE_S
_MIN_CONTROL_BEAT_FRACTION: Final = 0.005
_SAMPLE_TRANSPORT_RETRIES: Final = 2
_SAMPLE_RETRY_DELAY_S: Final = 2.0


class AutofocusError(RuntimeError):
    """The focus curve could not be measured or applied."""


class AutofocusAlreadyRunningError(AutofocusError):
    """A second autofocus request arrived while the first still owned the camera."""


class AutofocusConditionsError(AutofocusError):
    """Stars or the measured focus curve are temporarily insufficient."""


class AutofocusRestoreError(AutofocusError):
    """The starting focus could not be restored after a failed measurement."""


class FocuserDevice(Protocol):
    async def is_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def get(self, member: str, **params: object) -> object: ...

    async def put(self, member: str, **params: object) -> object: ...


@dataclass(frozen=True, slots=True)
class FocusSample:
    position: int
    hfr_px: float
    star_count: int


@dataclass(frozen=True, slots=True)
class AutofocusResult:
    start_position: int
    final_position: int
    hfr_px: float
    samples: tuple[FocusSample, ...]


FocusSampler = Callable[[], Awaitable[tuple[float | None, int]]]
Sleeper = Callable[[float], Awaitable[None]]

_MIN_CREDIBLE_STARS = 3
_STAR_COUNT_FRACTION = 0.25


def _integer(value: object, *, member: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AutofocusError(f"Focuser.{member} returned {value!r}, not an integer")
    if isinstance(value, float) and not value.is_integer():
        raise AutofocusError(f"Focuser.{member} returned non-integral {value!r}")
    return int(value)


def _credible_samples(samples: list[FocusSample]) -> list[FocusSample]:
    """Discard measurements whose star support is plainly weaker than the run.

    A tiny accidental detection can report an attractive HFR and pull an
    otherwise obvious V-curve away from its well-supported minimum. Keep at
    least three points; on genuinely sparse fields that means falling back to
    all measurements rather than inventing confidence we do not have.
    """
    if len(samples) < 3:
        return samples
    median_stars = float(np.median([sample.star_count for sample in samples]))
    threshold = max(_MIN_CREDIBLE_STARS, int(np.floor(median_stars * _STAR_COUNT_FRACTION)))
    credible = [sample for sample in samples if sample.star_count >= threshold]
    return credible if len(credible) >= 3 else samples


def _fit_coefficients(samples: list[FocusSample]) -> tuple[float, float, float, float] | None:
    if len(samples) < 3:
        return None
    positions = np.asarray([sample.position for sample in samples], dtype=np.float64)
    hfr = np.asarray([sample.hfr_px for sample in samples], dtype=np.float64)
    weights = np.sqrt(
        np.asarray([max(1, sample.star_count) for sample in samples], dtype=np.float64)
    )
    origin = float(positions.mean())
    quadratic, linear, constant = np.polyfit(positions - origin, hfr, 2, w=weights)
    if not np.all(np.isfinite((quadratic, linear, constant))) or quadratic <= 0.0:
        return None
    return origin, float(quadratic), float(linear), float(constant)


def _quadratic_fit(samples: list[FocusSample]) -> tuple[float, float, float, float] | None:
    """Return ``origin, a, b, c`` for a finite convex quadratic fit.

    A noisy live curve must fail safe rather than turning its lowest individual
    sample into a focus command.
    """
    credible = _credible_samples(samples)
    return _fit_coefficients(credible)


def _fitted_minimum(samples: list[FocusSample]) -> int | None:
    fit = _quadratic_fit(samples)
    if fit is None:
        return None
    origin, quadratic, linear, _constant = fit
    positions = np.asarray([sample.position for sample in samples], dtype=np.float64)
    vertex = origin - linear / (2.0 * quadratic)
    return round(float(np.clip(vertex, positions.min(), positions.max())))


def _fitted_curve(samples: list[FocusSample]) -> tuple[AutofocusCurvePoint, ...]:
    fit = _quadratic_fit(samples)
    if fit is None:
        return ()
    origin, quadratic, linear, constant = fit
    positions = np.linspace(
        min(sample.position for sample in samples),
        max(sample.position for sample in samples),
        41,
    )
    offsets = positions - origin
    hfr = quadratic * offsets**2 + linear * offsets + constant
    return tuple(
        AutofocusCurvePoint(position=float(position), hfr_px=max(0.0, float(value)))
        for position, value in zip(positions, hfr, strict=True)
    )


class AutofocusCamera(CameraDevice, Protocol):
    """AF also needs the standard AbortExposure setter for cancellation."""

    async def put(self, member: str, **params: object) -> object: ...


class AutofocusService:
    """Measure and apply one focus curve; only one run may own the camera."""

    def __init__(
        self,
        *,
        focuser: FocuserDevice,
        camera: AutofocusCamera | None,
        sample_quality: FocusSampler | None = None,
        exposure_s: float = DEFAULT_EXPOSURE_S,
        half_span: int = DEFAULT_HALF_SPAN,
        fine_half_span: int = DEFAULT_FINE_HALF_SPAN,
        move_timeout_s: float = DEFAULT_MOVE_TIMEOUT_S,
        move_poll_s: float = DEFAULT_MOVE_POLL_S,
        before_acquisition: Callable[[], Awaitable[None]] | None = None,
        sleep: Sleeper = asyncio.sleep,
        state_store: StateStore | None = None,
        tasks: TaskRegistry | None = None,
        persist: Callable[[AutofocusState], Awaitable[None]] | None = None,
    ) -> None:
        if camera is None and sample_quality is None:
            raise ValueError("camera or sample_quality is required")
        self.before_acquisition = before_acquisition
        self._focuser = focuser
        self._camera = camera
        self._sample_quality_override = sample_quality
        self._exposure_s = exposure_s
        self._half_span = half_span
        self._fine_half_span = fine_half_span
        self._move_timeout_s = move_timeout_s
        self._move_poll_s = move_poll_s
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._state_store = state_store
        self._tasks = tasks
        self._persist = persist
        self._persist_pending: AutofocusState | None = None
        self._persist_task: asyncio.Task[None] | None = None
        self._persist_lock = asyncio.Lock()
        self._section = AutofocusState()
        self._unsupported_metadata_members: set[str] = set()

    @property
    def state(self) -> AutofocusState:
        return self._section if self._state_store is None else self._state_store.snapshot.autofocus

    def _publish(self, **changes: object) -> None:
        self._set_section(self.state.model_copy(update=changes))

    def _set_section(self, section: AutofocusState) -> None:
        self._section = section
        if self._state_store is not None:
            self._state_store.apply(AutofocusChanged(autofocus=section))
        if self._persist is not None and section.started_at is not None:
            self._persist_pending = section
            if self._persist_task is None or self._persist_task.done():
                self._persist_task = (
                    asyncio.create_task(self._persist_updates())
                    if self._tasks is None
                    else self._tasks.spawn(self._persist_updates(), name="autofocus-retention")
                )

    async def _persist_updates(self) -> None:
        if self._persist is None:
            return
        try:
            async with self._persist_lock:
                while self._persist_pending is not None:
                    section = self._persist_pending
                    await self._persist(section)
                    if self._persist_pending is section:
                        self._persist_pending = None
            if self._state_store is not None:
                self._state_store.apply(NoticeCleared(notice_id="autofocus-retention"))
        except Exception as exc:
            _log.error("autofocus retention failed (%s)", type(exc).__name__)
            if self._state_store is not None:
                self._state_store.apply(
                    NoticeRaised(
                        notice=Notice(
                            id="autofocus-retention",
                            code="autofocus_retention_failed",
                            level=NoticeLevel.WARNING,
                            raised_at=datetime.now(UTC),
                            message="Autofocus measurements could not be saved. They may be lost after a restart.",
                        )
                    )
                )

    async def flush_persistence(self) -> None:
        task = self._persist_task
        if task is not None and task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        if self._persist_pending is not None:
            await self._persist_updates()

    def _record_sample(
        self,
        sample: FocusSample,
        *,
        stage: AutofocusStage,
        fit_samples: list[FocusSample] | None = None,
    ) -> None:
        published = AutofocusSampleState(
            position=sample.position,
            hfr_px=sample.hfr_px,
            star_count=sample.star_count,
            stage=stage,
        )
        changes: dict[str, object] = {
            "stage": stage,
            "samples": (*self.state.samples, published),
        }
        if fit_samples is not None:
            changes["curve"] = _fitted_curve(fit_samples)
        self._publish(**changes)

    async def run(
        self,
        *,
        step: int = DEFAULT_STEP,
        fine_step: int = DEFAULT_FINE_STEP,
        exposure_s: float | None = None,
        settle_s: float = DEFAULT_SETTLE_S,
    ) -> AutofocusResult:
        if step < 1:
            raise AutofocusError("autofocus step must be at least 1 focuser step")
        if fine_step < 1 or fine_step >= step:
            raise AutofocusError("autofocus fine step must be at least 1 and smaller than AF step")
        if settle_s < MIN_SETTLE_S:
            raise AutofocusError(f"autofocus settle time must be at least {MIN_SETTLE_S:g}s")
        if exposure_s is not None and exposure_s <= 0:
            raise AutofocusError("autofocus exposure time must be greater than zero")
        if self._lock.locked():
            raise AutofocusAlreadyRunningError("an autofocus run is already in progress")
        async with self._lock:
            try:
                return await self._run(
                    step=step,
                    fine_step=fine_step,
                    exposure_s=self._exposure_s if exposure_s is None else exposure_s,
                    settle_s=settle_s,
                )
            finally:
                await self.flush_persistence()

    async def _run(
        self, *, step: int, fine_step: int, exposure_s: float, settle_s: float
    ) -> AutofocusResult:
        self._set_section(
            AutofocusState(
                status="running",
                stage="baseline",
                started_at=datetime.now(UTC),
            )
        )
        if self.before_acquisition is not None:
            try:
                await self.before_acquisition()
            except BaseException as exc:
                self._publish(
                    status="failed", stage=None, error=str(exc), completed_at=datetime.now(UTC)
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise AutofocusError(str(exc)) from exc
        try:
            if not await self._focuser.is_connected():
                await self._focuser.connect()
            start = _integer(await self._focuser.get("position"), member="Position")
            maximum = _integer(await self._focuser.get("maxstep"), member="MaxStep")
            if maximum <= 0:
                raise AutofocusError(f"Focuser.MaxStep returned {maximum}; travel is unavailable")
        except BaseException as exc:
            # No motion/exposure has started: retain a truthful terminal
            # diagnostic without guessing a restoration position.
            self._publish(
                status="failed", stage=None, error=str(exc), completed_at=datetime.now(UTC)
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AutofocusError(str(exc)) from exc

        samples: list[FocusSample] = []
        try:
            baseline_hfr, baseline_stars = await self._sample_quality(exposure_s)
            if baseline_hfr is None or baseline_stars < _MIN_CREDIBLE_STARS:
                raise AutofocusConditionsError(
                    "autofocus found no measurable stars or fewer than three credible stars at the starting position"
                )
            baseline = FocusSample(start, baseline_hfr, baseline_stars)
            samples.append(baseline)
            self._record_sample(baseline, stage="baseline")

            coarse = await self._measure_bracketed_curve(
                start,
                step,
                self._half_span,
                maximum,
                settle_s=settle_s,
                exposure_s=exposure_s,
                label="coarse",
            )
            coarse_candidate = self._bracketed_minimum(coarse, label="coarse")
            self._publish(candidate_position=coarse_candidate)

            fine = await self._measure_fine_curve(
                coarse_candidate,
                fine_step,
                maximum,
                coarse,
                settle_s=settle_s,
                exposure_s=exposure_s,
            )
            candidate = self._fine_minimum(fine, coarse_candidate=coarse_candidate)
            self._publish(candidate_position=candidate)
            samples.extend(coarse)
            samples.extend(fine)

            await self._move_and_wait(candidate, settle_s=settle_s)
            validation_hfr, validation_stars = await self._sample_quality(exposure_s)
            if validation_hfr is None or validation_stars < _MIN_CREDIBLE_STARS:
                raise AutofocusConditionsError(
                    "autofocus candidate validation found insufficient measurable stars"
                )
            validation = FocusSample(candidate, validation_hfr, validation_stars)
            samples.append(validation)
            self._record_sample(validation, stage="validation")

            # Compare two fresh frames close together in time. The initial
            # baseline is useful telemetry, but changing transparency or seeing
            # over the full sweep must not make a worse candidate look better.
            await self._move_and_wait(start, settle_s=settle_s)
            control_hfr, control_stars = await self._sample_quality(exposure_s)
            if control_hfr is None or control_stars < _MIN_CREDIBLE_STARS:
                raise AutofocusConditionsError(
                    "autofocus control validation found insufficient measurable stars"
                )
            control = FocusSample(start, control_hfr, control_stars)
            samples.append(control)
            self._record_sample(control, stage="control")

            if validation_hfr < control_hfr * (1.0 - _MIN_CONTROL_BEAT_FRACTION):
                await self._move_and_wait(candidate, settle_s=settle_s)
                final_position = candidate
                final_hfr = validation_hfr
            else:
                final_position = start
                final_hfr = control_hfr
                _log.warning(
                    "autofocus: candidate %d HFR %.3f did not improve the fresh "
                    "start-position control %.3f by at least %.1f%%; keeping %d",
                    candidate,
                    validation_hfr,
                    control_hfr,
                    _MIN_CONTROL_BEAT_FRACTION * 100.0,
                    start,
                )
            _log.info(
                "autofocus complete: %d -> %d, HFR %.3f px",
                start,
                final_position,
                final_hfr,
            )
            self._publish(
                status="completed",
                stage=None,
                final_position=final_position,
                completed_at=datetime.now(UTC),
                error=None,
            )
            return AutofocusResult(start, final_position, final_hfr, tuple(samples))
        except BaseException as exc:
            self._publish(
                status="failed",
                stage=None,
                completed_at=datetime.now(UTC),
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                if isinstance(exc, asyncio.CancelledError):
                    if self._camera is not None:
                        await asyncio.wait_for(self._camera.put("abortexposure"), timeout=5)
                    await asyncio.wait_for(self._focuser.put("halt"), timeout=5)
                await self._move_and_wait(start, settle_s=settle_s)
            except Exception as restore_exc:
                _log.exception(
                    "autofocus failed and the starting focus position %d could not be restored",
                    start,
                )
                raise AutofocusRestoreError(
                    f"starting focus {start} could not be restored: {restore_exc}"
                ) from exc
            raise

    @staticmethod
    def _positions(center: int, step: int, half_span: int, maximum: int) -> list[int]:
        # Sweep high-to-low, matching the native Seestar autofocus direction
        # observed on hardware and keeping approach direction consistent.
        return sorted(
            {
                min(maximum, max(0, center + offset * step))
                for offset in range(-half_span, half_span + 1)
            },
            reverse=True,
        )

    async def _measure_curve(
        self,
        positions: list[int],
        *,
        settle_s: float,
        exposure_s: float,
        label: AutofocusStage,
    ) -> list[FocusSample]:
        if len(positions) < 3:
            raise AutofocusError(
                f"focuser travel leaves fewer than three distinct {label} sample points"
            )
        measured: list[FocusSample] = []
        for position in positions:
            await self._move_and_wait(position, settle_s=settle_s)
            hfr_px, star_count = await self._sample_quality(exposure_s)
            if hfr_px is None or star_count < _MIN_CREDIBLE_STARS:
                _log.warning(
                    "autofocus %s: position %d found no measurable stars; skipping",
                    label,
                    position,
                )
                continue
            measured.append(FocusSample(position, hfr_px, star_count))
            self._record_sample(measured[-1], stage=label, fit_samples=measured)
            _log.info(
                "autofocus %s: position %d HFR %.3f px (%d stars)",
                label,
                position,
                hfr_px,
                star_count,
            )
        if len(measured) < 3:
            raise AutofocusConditionsError(
                f"autofocus found measurable stars at only {len(measured)} of "
                f"{len(positions)} {label} focus positions"
            )
        return measured

    async def _measure_fine_curve(
        self,
        center: int,
        step: int,
        maximum: int,
        coarse: list[FocusSample],
        *,
        settle_s: float,
        exposure_s: float,
    ) -> list[FocusSample]:
        # Three points suffice only for a supported convex interior minimum.
        # Seeing/weak stars/edge minima retain the full fine range; the fresh
        # validation/control comparison remains mandatory in either case.
        if self._fine_half_span != DEFAULT_FINE_HALF_SPAN:
            return await self._measure_curve(
                self._positions(center, step, self._fine_half_span, maximum),
                settle_s=settle_s,
                exposure_s=exposure_s,
                label="fine",
            )
        initial = self._positions(center, step, 1, maximum)
        measured: list[FocusSample] = []
        for position in initial:
            sample = await self._measure_position(
                position,
                settle_s=settle_s,
                exposure_s=exposure_s,
                label="fine",
                fit_samples=measured,
            )
            if sample is not None:
                measured.append(sample)
        floor = max(
            _MIN_CREDIBLE_STARS,
            int(float(np.median([sample.star_count for sample in coarse])) * _STAR_COUNT_FRACTION),
        )
        fit = _fitted_minimum(measured)
        if (
            len(initial) == 3
            and len(measured) == 3
            and all(sample.star_count >= floor for sample in measured)
            and min(measured, key=lambda sample: sample.hfr_px).position == center
            and all(
                sample.hfr_px > next(point.hfr_px for point in measured if point.position == center)
                for sample in measured
                if sample.position != center
            )
            and fit is not None
            and min(initial) < fit < max(initial)
        ):
            return measured
        # Do not combine points across an approach-direction reversal: backlash
        # could distort their relative positions. Repeat the complete original
        # high-to-low sweep when the short sweep is not trustworthy.
        return await self._measure_curve(
            self._positions(center, step, self._fine_half_span, maximum),
            settle_s=settle_s,
            exposure_s=exposure_s,
            label="fine",
        )

    async def _measure_bracketed_curve(
        self,
        center: int,
        step: int,
        half_span: int,
        maximum: int,
        *,
        settle_s: float,
        exposure_s: float,
        label: AutofocusStage,
    ) -> list[FocusSample]:
        measured = await self._measure_curve(
            self._positions(center, step, half_span, maximum),
            settle_s=settle_s,
            exposure_s=exposure_s,
            label=label,
        )
        measured_positions = {sample.position for sample in measured}
        for _ in range(half_span):
            lower = min(sample.position for sample in measured)
            upper = max(sample.position for sample in measured)
            observed_best = min(measured, key=lambda sample: sample.hfr_px)
            candidate = _fitted_minimum(measured)
            if (
                lower < observed_best.position < upper
                and candidate is not None
                and lower < candidate < upper
            ):
                return measured
            if observed_best.position == lower:
                next_position = lower - step
            elif observed_best.position == upper:
                next_position = upper + step
            else:
                return measured
            if next_position < 0 or next_position > maximum or next_position in measured_positions:
                return measured
            sample = await self._measure_position(
                next_position,
                settle_s=settle_s,
                exposure_s=exposure_s,
                label=label,
                fit_samples=measured,
            )
            if sample is None:
                return measured
            measured.append(sample)
            measured_positions.add(next_position)
        return measured

    async def _measure_position(
        self,
        position: int,
        *,
        settle_s: float,
        exposure_s: float,
        label: AutofocusStage,
        fit_samples: list[FocusSample],
    ) -> FocusSample | None:
        await self._move_and_wait(position, settle_s=settle_s)
        hfr_px, star_count = await self._sample_quality(exposure_s)
        if hfr_px is None or star_count < _MIN_CREDIBLE_STARS:
            _log.warning(
                "autofocus %s: position %d found no measurable stars; skipping",
                label,
                position,
            )
            return None
        sample = FocusSample(position, hfr_px, star_count)
        self._record_sample(sample, stage=label, fit_samples=[*fit_samples, sample])
        _log.info(
            "autofocus %s: position %d HFR %.3f px (%d stars)",
            label,
            position,
            hfr_px,
            star_count,
        )
        return sample

    @staticmethod
    def _bracketed_minimum(
        samples: list[FocusSample],
        *,
        label: str,
    ) -> int:
        lower = min(sample.position for sample in samples)
        upper = max(sample.position for sample in samples)
        credible = _credible_samples(samples)
        observed_best = min(credible, key=lambda sample: sample.hfr_px)
        candidate = _fitted_minimum(samples)
        if not lower < observed_best.position < upper:
            raise AutofocusConditionsError(
                f"autofocus {label} minimum lies at the edge of the sampled range"
            )
        if candidate is None or not lower < candidate < upper:
            raise AutofocusConditionsError(
                f"autofocus {label} curve has no reliable interior minimum"
            )
        return candidate

    @staticmethod
    def _fine_minimum(samples: list[FocusSample], *, coarse_candidate: int) -> int:
        """Refine a proven coarse minimum without making fine-scale seeing fatal.

        The coarse sweep has already established a convex, bracketed minimum.
        A narrow five-point sweep can be flatter than frame-to-frame seeing:
        its fitted vertex may clip to an edge, or one edge measurement may be
        the lowest even when the fit is unusable. Prefer an interior fine fit,
        then an interior measured point, and finally the already-safe coarse
        candidate. Every returned position is still compared with a fresh
        start-position control before it can become the final focus.
        """
        lower = min(sample.position for sample in samples)
        upper = max(sample.position for sample in samples)
        fitted = _fitted_minimum(samples)
        if fitted is not None and lower < fitted < upper:
            return fitted
        credible = _credible_samples(samples)
        observed_best = min(credible, key=lambda sample: sample.hfr_px)
        if lower < observed_best.position < upper:
            return observed_best.position
        if lower < coarse_candidate < upper:
            return coarse_candidate
        raise AutofocusError("autofocus fine sweep has no safe candidate to validate")

    async def _move_and_wait(self, position: int, *, settle_s: float) -> None:
        await self._focuser.put("move", Position=position)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._move_timeout_s
        while bool(await self._focuser.get("ismoving")):
            if loop.time() >= deadline:
                raise AutofocusError(
                    f"focuser did not settle at position {position} within "
                    f"{self._move_timeout_s:g}s"
                )
            await self._sleep(self._move_poll_s)
        # IsMoving=false means the motor command ended, not that vibration and
        # backlash have settled optically. Never expose sooner than two seconds
        # after a move; the request may choose a longer value, not a shorter one.
        await self._sleep(settle_s)

    async def _sample_quality(self, exposure_s: float) -> tuple[float | None, int]:
        if self._sample_quality_override is not None:
            return await self._sample_quality_override()
        assert self._camera is not None
        for attempt in range(_SAMPLE_TRANSPORT_RETRIES + 1):
            try:
                frame, _started_at = await drive_exposure(
                    camera=self._camera,
                    telescope=None,
                    filterwheel=None,
                    duration_s=exposure_s,
                    context=ExposureContext(
                        object_name="autofocus",
                        focal_length_mm=None,
                        telescope_name=None,
                        ssc_version="autofocus",
                        device_firmware=None,
                    ),
                    frame_type=FrameType.LIGHT,
                    unsupported_members=self._unsupported_metadata_members,
                )
                break
            except (ExposureTimeoutError, AlpacaTransportError, AlpacaRemoteError) as exc:
                remote_is_transient = not isinstance(exc, AlpacaRemoteError) or exc.number == int(
                    AlpacaErrorNumber.NOT_CONNECTED
                )
                if not remote_is_transient or attempt >= _SAMPLE_TRANSPORT_RETRIES:
                    raise
                _log.warning(
                    "autofocus frame acquisition failed transiently; reconnecting and retrying "
                    "sample %d/%d: %s",
                    attempt + 1,
                    _SAMPLE_TRANSPORT_RETRIES,
                    exc,
                )
                await self._sleep(_SAMPLE_RETRY_DELAY_S)
                await self._camera.connect()
        return await asyncio.to_thread(
            self._measure,
            frame.pixels,
            frame.metadata.bayer_offset_x,
            frame.metadata.bayer_offset_y,
        )

    @staticmethod
    def _measure(
        pixels: np.ndarray, offset_x: int | None, offset_y: int | None
    ) -> tuple[float | None, int]:
        plane = pixels.astype(np.float64)
        pattern = bayer_pattern(offset_x, offset_y)
        if pattern is not None:
            plane = debayer(plane, pattern)[..., 1]
        quality = measure_frame_quality(plane, pixel_size_um=None, focal_length_mm=None)
        return quality.hfr_px, quality.star_count


__all__ = [
    "DEFAULT_SETTLE_S",
    "DEFAULT_STEP",
    "MIN_SETTLE_S",
    "AutofocusAlreadyRunningError",
    "AutofocusError",
    "AutofocusResult",
    "AutofocusService",
    "FocusSample",
]
