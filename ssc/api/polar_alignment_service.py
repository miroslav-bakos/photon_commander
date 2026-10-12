# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Driving one 3(+)-point polar-alignment measurement run end to end.

The maths lives in ``ssc.astro.polar`` (pure, no I/O, CLAUDE.md invariant 3)
and is not touched here. This module is everything around it: slew to each
target, wait for the mount to actually arrive (not merely stop reporting
``Slewing``), expose and plate-solve, retry what fails, and publish progress
into ``ssc.state`` at every step so a desktop and a phone watching a live run
never disagree about which point is in flight (CLAUDE.md invariant 4) — a
user standing at a tripod in the dark needs to see it working, not only the
final number. Modelled directly on ``ssc.api.capture_orchestrator.CaptureService``:
validate synchronously, publish "running" before returning, hand the rest to
one owned background task (``ssc.api.tasks.TaskRegistry``).

**Three field findings, each a requirement here, not speculation:**

* ``Telescope.Slewing == False`` is not a safe primitive on this device: it
  has been observed to read ``False`` while the mount was still physically
  moving, and the mount telemetry block has lagged a full stop by several
  minutes. :func:`PolarAlignmentService._settle` therefore requires the
  mount's own reported position to be stable across two consecutive polls,
  not merely that ``Slewing`` reads ``False`` once.
* A slew commanded into a mount that has not finished its previous move
  refuses with ``MountRefusedError`` (a 409 over HTTP) — mapped from ASCOM's
  own "invalid operation" reading. That must not abort a five-minute
  unattended sequence, so :meth:`PolarAlignmentService._slew_with_retry`
  retries it a bounded number of times before giving up on the point.
* A plate solve legitimately fails in the field (thin cloud, low altitude,
  too few stars) — three of seventeen frames did, on the run that produced
  this module. A failed solve retries the point and, if it is still
  unusable, is skipped: the run continues with whatever points remain,
  provided enough do (``ssc.astro.polar``'s own three-point minimum).

**Refuses rather than guesses**, at ``start()``, before anything is
commanded (CLAUDE.md invariant 7): no camera/telescope hosted, the mount is
not in polar alignment mode (``Telescope.AlignmentMode`` — SSC did not read
this member before this module), the mount's own site or the ephemeris
kernel is unknown, the starting position is unknown, or no plate solver is
available. None of these degrade to a confident-looking wrong answer.

**Independent recheck is authoritative.** A measurement retains its fitted
axis, but after the user physically adjusts the wedge the service makes a new
sweep in the same direction and compares the two
independently fitted axes. One low-error sweep asks for another recheck; only
two consecutive low-error, low-uncertainty five-point sweeps whose axes agree
may report ``CONFIRMED``.
Disagreement is exposed and never averaged. The older single-field refinement
endpoint remains for backwards compatibility and diagnostics, but ``start``
does not launch it and its result cannot confirm alignment.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import logging
import math
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

from ssc.api.capture_service import (
    CameraDevice,
    Device,
    ExposureContext,
    ExposureNotAvailableError,
    ExposureTimeoutError,
    drive_exposure,
)
from ssc.api.external_tools import ConfiguredAstap
from ssc.api.tasks import TaskRegistry
from ssc.astro.events import Ephemeris, Site
from ssc.astro.frames import EquatorialCoord, MountFrame, mount_site
from ssc.astro.framing import altitude_deg, azimuth_deg
from ssc.astro.polar import (
    DegeneracyReason,
    DegenerateGeometryError,
    MountTracking,
    PolarAlignmentError,
    PolarAlignmentModel,
    PolarPoint,
    PolarRefinement,
    best_conditioned_field,
    j2000_coord_at_hour_angle,
    refine_polar_alignment,
    solve_polar_alignment,
)
from ssc.astro.units import Deg, Hours, hours_to_deg, wrap_hours
from ssc.capture import FrameType, RawFrame, bin_2x2_for_solving, write_fits_file
from ssc.client.errors import AlpacaClientError
from ssc.platesolve import (
    PlateSolveError,
    PlateSolveResult,
    SolveIssue,
    SolverAvailability,
    SolveTimedOutError,
    astap_availability,
    astap_solve,
)
from ssc.settings.store import SettingsStore
from ssc.state.events import NoticeCleared, NoticeRaised, PolarAlignmentChanged
from ssc.state.models import (
    MountState,
    MountTrackingRate,
    Notice,
    NoticeLevel,
    PolarAlignmentAssessment,
    PolarAlignmentHistoryEntry,
    PolarAlignmentPointState,
    PolarAlignmentPointStatus,
    PolarAlignmentResult,
    PolarAlignmentRunKind,
    PolarAlignmentStartInfo,
    PolarAlignmentStartMode,
    PolarAlignmentState,
    PolarAlignmentStatus,
    PolarAlignmentVerdict,
    PolarRefinementResult,
    PolarRefinementStage,
    PolarRefinementState,
    PolarRefinementStatus,
    mount_stale,
)
from ssc.state.ports import MountNotAvailableError, MountRefusedError
from ssc.state.store import StateStore

_log = logging.getLogger(__name__)

#: ASCOM ``AlignmentModes``. Restated here rather than imported from
#: ``ssc.alpaca`` — this module is a client of the Alpaca seam
#: (architecture policy invariant 2), not a party to the driver's own internals, the
#: same trade ``ssc.client.errors`` already makes for the ASCOM error numbers.
_ALIGNMENT_MODE_POLAR: Final = 1

_DEFAULT_MAX_SLEW_ATTEMPTS: Final = 5
_DEFAULT_SLEW_RETRY_DELAY_S: Final = 2.0
_DEFAULT_PREFLIGHT_TIMEOUT_S: Final = 30.0
_DEFAULT_PREFLIGHT_POLL_INTERVAL_S: Final = 0.25
_SEESTAR_FIELD_HOUR_ANGLE_DEG: Final = 15.0
_DEFAULT_SETTLE_TIMEOUT_S: Final = 120.0
_DEFAULT_SETTLE_POLL_INTERVAL_S: Final = 0.25
# Coarse mount telemetry cannot prove that mechanical motion has damped.
# Keep a short physical settling interval after the coordinate gate.
_DEFAULT_POST_SLEW_SETTLE_S: Final = 3.0
#: How close two consecutive mount readings must be to call the mount
#: settled — coarse on purpose (a stability check, not a pointing check):
#: the fit itself only needs the *solved* position, never the mount's own.
_DEFAULT_SETTLE_STABLE_TOLERANCE_ARCSEC: Final = 30.0
_DEFAULT_SETTLE_CONSECUTIVE_STABLE: Final = 2
_DEFAULT_SETTLE_TARGET_TOLERANCE_ARCSEC: Final = 3600.0
_DEFAULT_TRACKING_ATTEMPTS: Final = 3
_DEFAULT_TRACKING_RETRY_DELAY_S: Final = 0.5
_DEFAULT_MAX_SOLVE_ATTEMPTS: Final = 3
_DEFAULT_SOLVE_RETRY_DELAY_S: Final = 2.0
_DEFAULT_REFINEMENT_EXPOSURE_S: Final = 2.0
_DEFAULT_REFINEMENT_SOLVE_TIMEOUT_S: Final = 15.0
_DEFAULT_REFINEMENT_MAX_SOLVE_ATTEMPTS: Final = 3
_DEFAULT_INTERACTIVE_SOLVE_TIMEOUT_S: Final = 30.0
_RECOVERABLE_REFINEMENT_SOLVE_ISSUES: Final = frozenset(
    {SolveIssue.NOT_SOLVED, SolveIssue.TIMED_OUT}
)
_CONFIRM_POINT_COUNT: Final = 5
_MIN_ADAPTIVE_TOTAL_ARC_DEG: Final = 30.0
_ADAPTIVE_ARC_STEP_DEG: Final = 5.0
_CONFIRM_MAX_ERROR_ARCMIN: Final = 2.0
_CONFIRM_MAX_AXIS_SEPARATION_ARCMIN: Final = 2.0
_CONFIRM_MAX_AXIS_UNCERTAINTY_ARCSEC: Final = 120.0


class PolarAlignmentAlreadyRunningError(Exception):
    """A polar-alignment run is already in progress; only one runs at a time."""


class PolarAlignmentDeviceUnavailableError(Exception):
    """This process hosts no camera/telescope to drive."""


class PolarSolveAlreadyRunningError(Exception):
    """Another interactive/centering solve already owns the camera."""


class PolarAlignmentFilterUnavailableError(Exception):
    """The UV/IR-cut position needed for PA could not be selected."""


class PolarAlignmentNotPolarModeError(Exception):
    """``Telescope.AlignmentMode`` is not ``algPolar`` — polar alignment is meaningless
    on an alt-az mount. Carries the raw value SSC actually saw.

    **Known gap, not a guarantee**: ``docs/DESIGN.md`` §4.4b records that on
    the owner's own unit ``AlignmentMode`` read ``1`` (``algPolar``) *while
    the scope was physically alt-az on a table tripod*, and says outright
    that the member "must not be used to decide whether polar alignment is
    applicable". This gate still refuses the one case it *can* catch (a
    device that honestly reports ``0``) and is required input, not invented
    here — but per that finding it will not catch a mount mounted alt-az
    that reports ``1`` anyway. §4.5 names the fallback: ask the user. That
    is a Polar-align-tab concern (``web/``, out of this dispatch's scope);
    this refusal is necessary but, per DESIGN.md's own measurement, not
    sufficient on its own."""

    def __init__(self, mode: int | None) -> None:
        super().__init__(
            f"the mount's AlignmentMode is {mode!r}, not 1 (algPolar): a polar-alignment "
            "measurement is only meaningful on a mount in polar/EQ alignment mode"
        )
        self.mode = mode


class PolarAlignmentSiteUnknownError(Exception):
    """The mount has not yet reported its own ``SiteLatitude``/``SiteLongitude``."""


class PolarAlignmentPreflightError(Exception):
    """Fresh stationary mount telemetry did not arrive before the run timeout."""


class PolarAlignmentEphemerisUnavailableError(Exception):
    """The ephemeris kernel is not loaded, so no frame conversion is possible."""


class PolarAlignmentPositionUnknownError(Exception):
    """``start_point`` is ``current`` and the mount's own J2000 position is
    not yet known (``MountState.ra_j2000_h``/``dec_j2000_deg``)."""


class PolarAlignmentStartAmbiguousError(Exception):
    """A starting coordinate was given while ``start_point`` was not
    ``explicit``, or ``start_point`` was ``explicit`` with a coordinate
    missing. Which one wins is ambiguous either way, so this refuses rather
    than silently picking a meaning (CLAUDE.md invariant 7). The API layer
    (``ssc.api.schemas.PolarAlignmentStartRequest``) already refuses the
    same shape at the request boundary; this is the same rule enforced
    again for any caller that reaches :meth:`PolarAlignmentService.start`
    directly."""


class PolarAlignmentNoClearArcError(Exception):
    """``ssc.astro.polar.best_conditioned_field`` found no declination that
    clears the requested minimum altitude, or none that does so without
    breaching the meridian ceiling —
    :attr:`~ssc.astro.polar.DegeneracyReason.NO_CLEAR_ARC`. Carries the same
    message, which names both bounds, rather than falling back to
    ``current`` (CLAUDE.md invariant 7: no silent degradation)."""


class PolarAlignmentSolverUnavailableError(Exception):
    """No plate solver can be used right now. Carries the same
    :class:`~ssc.platesolve.SolverAvailability` the Settings tab would show,
    so the caller gets the precise, actionable reason rather than "unavailable"."""

    def __init__(self, availability: SolverAvailability) -> None:
        super().__init__(availability.reason or "plate solving is unavailable")
        self.availability = availability


class PolarAlignmentNotRunningError(Exception):
    """``cancel()`` was called with no run in progress."""


class PolarAlignmentTooFewPointsError(Exception):
    """Fewer than three points solved; there is nothing left to fit."""


class PolarAlignmentNoMeasurementError(Exception):
    """An independent recheck was requested before this process completed a sweep."""


class PolarRefinementNoSweepError(Exception):
    """No completed sweep exists in this session to refine from.

    Refinement is differential from the sweep's own retained model
    (:attr:`PolarAlignmentService._sweep_model`), never restored across a
    restart: persisted presentation is retained, but a physical differential
    baseline needs a fresh sweep — so a process that has not itself
    completed a sweep has nothing to refine from, however good the section's
    displayed ``result`` looks.
    """


class PolarRefinementTrackingError(Exception):
    """The mount was asked to stop tracking but does not confirm it stopped —
    :func:`ssc.astro.polar.refine_polar_alignment`'s own precondition
    (CLAUDE.md's "SSC must make it so rather than hope"), checked here before
    a single frame is captured."""


class PolarRefinementAlreadyRunningError(Exception):
    """A refinement loop is already running, or the sweep it would refine
    is itself mid-run; only one polar-alignment operation runs at a time."""


class PolarRefinementNotRunningError(Exception):
    """``stop_refinement()`` was called with no refinement loop in progress."""


class _PointFailedError(Exception):
    """Internal: this one point could not be completed after every retry —
    the run continues without it, provided enough others remain."""


class _RefinementSolveExhaustedError(Exception):
    """Repeated live frames could not be solved; restore tracking and stop."""


class MountControl(Protocol):
    """The mount operations this module needs, structurally matched
    against ``ssc.api.services.DeviceService`` — not the full service, so a
    test drives this against a small fake with no gateway or HTTP app behind
    it, the same reasoning ``ssc.api.capture_service.Device`` gives for its
    own narrow protocol."""

    async def mount_slew(self, *, ra_hours: float, dec_deg: float) -> None: ...

    async def mount_slew_polar(
        self, *, ra_hours: float, dec_deg: float, fixed_mount_dec_deg: float | None
    ) -> float: ...

    async def mount_stop(self) -> None: ...

    async def mount_alignment_mode(self) -> int | None: ...

    async def mount_set_tracking(
        self, *, enabled: bool, rate: MountTrackingRate | None
    ) -> MountState:
        """Sets ``Tracking`` and returns a fresh read-back — the refinement
        loop's own way of making the drive halted rather than hoping it is
        (``ssc.state.ports.DeviceGateway.mount_set_tracking``)."""
        ...


class GainSettableCamera(CameraDevice, Protocol):
    """``CameraDevice`` plus the one write this module needs of its own —
    ``Camera.Gain`` — mirroring ``ssc.api.capture_orchestrator.GainSettableCamera``
    exactly (its own docstring explains why setting it is the caller's
    concern, not the pinned driver's), duplicated rather than imported so
    this module's device-facing protocols all live in one place. DESIGN.md
    §4.5's own settings table names gain as central here specifically —
    "high gain and a short sub is usually the right trade... the frame is
    thrown away after solving, so noise costs nothing that matters"."""

    async def put(self, member: str, **params: object) -> object: ...


class SettableDevice(Device, Protocol):
    """A generic Alpaca device with the write used by the filter wheel."""

    async def put(self, member: str, **params: object) -> object: ...


class AstroSource(Protocol):
    """The one thing this module needs of ``ssc.api.astro_service.AstroService``."""

    @property
    def ephemeris(self) -> Ephemeris | None: ...

    @property
    def ephemeris_error(self) -> str | None: ...


SolveFn = Callable[[Path], Awaitable[PlateSolveResult]]
AvailabilityFn = Callable[[], Awaitable[SolverAvailability]]


def _ssc_version() -> str:
    try:
        return importlib.metadata.version("ssc")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def _build_targets(
    start_ra_h: float, start_dec_deg: float, count: int, step_deg: float
) -> list[tuple[float, float]]:
    """``count`` targets, RA-only rotation from ``start_ra_h`` in steps of
    ``step_deg``, declination held fixed — exactly the geometry
    ``ssc.astro.polar`` requires (its own module docstring: "rotate the
    mount about its RA axis only")."""
    step_h = step_deg / 15.0
    return [(float(wrap_hours(start_ra_h + i * step_h)), start_dec_deg) for i in range(count)]


def _separation_arcsec(a: tuple[float, float], b: tuple[float, float]) -> float:
    """A coarse RA/Dec separation, arcsec — good enough for a stability
    check (no cos(dec) term), never used for the fit itself."""
    ra_hours = (a[0] - b[0] + 12.0) % 24.0 - 12.0
    ra_arcsec = ra_hours * 15.0 * 3600.0
    dec_arcsec = (a[1] - b[1]) * 3600.0
    return math.hypot(ra_arcsec, dec_arcsec)


def _result_from_model(model: PolarAlignmentModel) -> PolarAlignmentResult:
    return PolarAlignmentResult(
        axis_altitude_deg=float(model.axis_altitude_deg),
        axis_azimuth_deg=float(model.axis_azimuth_deg),
        altitude_error_deg=float(model.altitude_error_deg),
        azimuth_error_deg=float(model.azimuth_error_deg),
        azimuth_error_sky_deg=float(model.azimuth_error_sky_deg),
        total_error_deg=float(model.total_error_deg),
        altitude_correction_deg=float(model.altitude_correction_deg),
        azimuth_correction_deg=float(model.azimuth_correction_deg),
        circle_radius_deg=float(model.circle_radius_deg),
        arc_span_deg=float(model.arc_span_deg),
        rms_residual_arcsec=(
            None if model.rms_residual_arcsec is None else float(model.rms_residual_arcsec)
        ),
        max_residual_arcsec=(
            None if model.max_residual_arcsec is None else float(model.max_residual_arcsec)
        ),
        residual_dof=model.residual_dof,
        noise_amplification=model.noise_amplification,
        axis_uncertainty_arcsec=float(model.axis_uncertainty_arcsec),
        point_count=model.point_count,
        epoch=model.epoch,
        refraction_applied=model.refraction_applied,
        has_residual_check=model.residual_dof > 0,
    )


def _axis_separation_arcmin(a: PolarAlignmentModel, b: PolarAlignmentModel) -> float:
    """Great-circle separation of two Earth-fixed fitted axes."""
    alt_a = math.radians(float(a.axis_altitude_deg))
    alt_b = math.radians(float(b.axis_altitude_deg))
    delta_az = math.radians(float(a.axis_azimuth_deg) - float(b.axis_azimuth_deg))
    cosine = math.sin(alt_a) * math.sin(alt_b) + math.cos(alt_a) * math.cos(alt_b) * math.cos(
        delta_az
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine)))) * 60.0


def _assess_sweep(
    current: PolarAlignmentModel, previous: PolarAlignmentModel | None
) -> PolarAlignmentAssessment:
    assessment = _assess_sweep_confirmation(current, previous)
    return _add_sweep_comparison(assessment, current, previous)


def _add_sweep_comparison(
    assessment: PolarAlignmentAssessment,
    current: PolarAlignmentModel,
    previous: PolarAlignmentModel | None,
) -> PolarAlignmentAssessment:
    if previous is None:
        return assessment
    # Always expose repeatability, even for three-point or large-error fits.
    # A changed wedge can legitimately change the axis; separation alone is
    # not a failure verdict. Confirmation still requires agreeing quality fits.
    return assessment.model_copy(
        update={
            "previous_total_error_arcmin": abs(float(previous.total_error_deg)) * 60.0,
            "previous_axis_uncertainty_arcsec": float(previous.axis_uncertainty_arcsec),
            "axis_separation_arcmin": _axis_separation_arcmin(current, previous),
        }
    )


def _assess_sweep_confirmation(
    current: PolarAlignmentModel, previous: PolarAlignmentModel | None
) -> PolarAlignmentAssessment:
    current_error = abs(float(current.total_error_deg)) * 60.0
    current_uncertainty = float(current.axis_uncertainty_arcsec)
    common = {
        "current_total_error_arcmin": current_error,
        "current_axis_uncertainty_arcsec": current_uncertainty,
        "max_error_arcmin": _CONFIRM_MAX_ERROR_ARCMIN,
        "max_axis_separation_arcmin": _CONFIRM_MAX_AXIS_SEPARATION_ARCMIN,
        "max_axis_uncertainty_arcsec": _CONFIRM_MAX_AXIS_UNCERTAINTY_ARCSEC,
    }
    if (
        current.point_count < _CONFIRM_POINT_COUNT
        or current.residual_dof < _CONFIRM_POINT_COUNT - 3
        or not math.isfinite(current_uncertainty)
        or current_uncertainty > _CONFIRM_MAX_AXIS_UNCERTAINTY_ARCSEC
    ):
        return PolarAlignmentAssessment(
            verdict=PolarAlignmentVerdict.RECHECK,
            message=(
                "This sweep does not have five solved points with a precise fitted axis. "
                "Use a new five-point measurement when you need residual checks and confirmation."
            ),
            **common,
        )
    if current_error > _CONFIRM_MAX_ERROR_ARCMIN:
        return PolarAlignmentAssessment(
            verdict=PolarAlignmentVerdict.ADJUST,
            message=(
                "Adjust altitude and azimuth, then recalculate with a fresh sweep. "
                "Use five points for final verification."
            ),
            **common,
        )
    if (
        previous is None
        or previous.point_count < _CONFIRM_POINT_COUNT
        or previous.residual_dof < _CONFIRM_POINT_COUNT - 3
        or not math.isfinite(float(previous.axis_uncertainty_arcsec))
        or float(previous.axis_uncertainty_arcsec) > _CONFIRM_MAX_AXIS_UNCERTAINTY_ARCSEC
        or abs(float(previous.total_error_deg)) * 60.0 > _CONFIRM_MAX_ERROR_ARCMIN
    ):
        return PolarAlignmentAssessment(
            verdict=PolarAlignmentVerdict.RECHECK,
            message=(
                "The measured error is low, but one sweep cannot confirm alignment. "
                "Run another five-point recheck without touching the mount."
            ),
            previous_total_error_arcmin=(
                None if previous is None else abs(float(previous.total_error_deg)) * 60.0
            ),
            previous_axis_uncertainty_arcsec=(
                None if previous is None else float(previous.axis_uncertainty_arcsec)
            ),
            **common,
        )

    separation = _axis_separation_arcmin(current, previous)
    previous_uncertainty = float(previous.axis_uncertainty_arcsec)
    consistent = (
        previous.point_count >= _CONFIRM_POINT_COUNT
        and previous.residual_dof >= _CONFIRM_POINT_COUNT - 3
        and separation <= _CONFIRM_MAX_AXIS_SEPARATION_ARCMIN
        and previous_uncertainty <= _CONFIRM_MAX_AXIS_UNCERTAINTY_ARCSEC
    )
    return PolarAlignmentAssessment(
        verdict=(
            PolarAlignmentVerdict.CONFIRMED if consistent else PolarAlignmentVerdict.INCONSISTENT
        ),
        message=(
            "Polar alignment is confirmed by two independent agreeing five-point sweeps."
            if consistent
            else "The two low-error sweeps disagree or are too uncertain. Do not average them; run another recheck."
        ),
        previous_total_error_arcmin=abs(float(previous.total_error_deg)) * 60.0,
        previous_axis_uncertainty_arcsec=previous_uncertainty,
        axis_separation_arcmin=separation,
        **common,
    )


def _refinement_result_from(refinement: PolarRefinement) -> PolarRefinementResult:
    model = refinement.model
    return PolarRefinementResult(
        axis_altitude_deg=float(model.axis_altitude_deg),
        axis_azimuth_deg=float(model.axis_azimuth_deg),
        altitude_error_deg=float(model.altitude_error_deg),
        azimuth_error_deg=float(model.azimuth_error_deg),
        azimuth_error_sky_deg=float(model.azimuth_error_sky_deg),
        total_error_deg=float(model.total_error_deg),
        altitude_correction_deg=float(model.altitude_correction_deg),
        azimuth_correction_deg=float(model.azimuth_correction_deg),
        axis_uncertainty_arcsec=float(model.axis_uncertainty_arcsec),
        epoch=model.epoch,
        altitude_step_deg=float(refinement.altitude_step_deg),
        azimuth_step_deg=float(refinement.azimuth_step_deg),
        axis_shift_deg=float(refinement.axis_shift_deg),
        boresight_shift_deg=float(refinement.boresight_shift_deg),
        elapsed_seconds=refinement.elapsed_seconds,
        sweep_age_seconds=refinement.sweep_age_seconds,
        refinement_uncertainty_arcsec=float(refinement.refinement_uncertainty_arcsec),
        noise_amplification=refinement.noise_amplification,
        trusted=refinement.trusted,
        warnings=list(refinement.warnings),
    )


def _mount_tracking(mount: MountState | None) -> MountTracking:
    """``MountState.tracking`` (a bool the poll loop reads back) collapsed
    into :class:`~ssc.astro.polar.MountTracking` — read fresh every
    iteration, never assumed, so a drive that resumes tracking mid-loop is
    caught rather than hoped away (CLAUDE.md invariant 7)."""
    if mount is None or mount.tracking is None:
        return MountTracking.UNKNOWN
    return MountTracking.STOPPED if mount.tracking is False else MountTracking.SIDEREAL


async def _write_solver_frame(path: Path, frame: RawFrame) -> None:
    writer = asyncio.create_task(asyncio.to_thread(write_fits_file, path, frame.pixels, frame.metadata))
    try:
        await asyncio.shield(writer)
    except asyncio.CancelledError:
        # A thread cannot be cancelled. Drain it before TemporaryDirectory exits.
        await writer
        raise


def restored_night_state(section: PolarAlignmentState, now: datetime) -> PolarAlignmentState:
    """Retain measurements after restart without resurrecting physical work."""
    if section.status is PolarAlignmentStatus.RUNNING:
        section = section.model_copy(update={
            "status": PolarAlignmentStatus.FAILED, "ended_at": now,
            "exposure_started_at": None, "exposure_duration_s": None,
            "preflight_reason": None,
            "error": "Polar alignment interrupted by server restart; retained details are available",
            "points": [p.model_copy(update={"status": PolarAlignmentPointStatus.ABORTED})
                       if p.status in {PolarAlignmentPointStatus.SLEWING, PolarAlignmentPointStatus.SETTLING,
                                       PolarAlignmentPointStatus.CAPTURING, PolarAlignmentPointStatus.SOLVING}
                       else p for p in section.points],
        })
    if section.refinement.status is PolarRefinementStatus.RUNNING:
        section = section.model_copy(update={"refinement": section.refinement.model_copy(update={
            "status": PolarRefinementStatus.STOPPED, "ended_at": now, "stage": None,
            "exposure_started_at": None, "exposure_duration_s": None,
            "error": "Live refinement interrupted by server restart; retained details are available",
        })})
    return section


class PolarAlignmentService:
    """Drives one polar-alignment measurement run. See the module docstring."""

    def __init__(
        self,
        *,
        before_acquisition: Callable[[], Awaitable[None]] | None = None,
        camera: GainSettableCamera | None,
        telescope: Device | None,
        filterwheel: SettableDevice | None,
        device: MountControl,
        state: StateStore,
        astro: AstroSource,
        tasks: TaskRegistry,
        section: PolarAlignmentState,
        settings: SettingsStore | None = None,
        solve: SolveFn = astap_solve,
        availability: AvailabilityFn = astap_availability,
        now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_slew_attempts: int = _DEFAULT_MAX_SLEW_ATTEMPTS,
        slew_retry_delay_s: float = _DEFAULT_SLEW_RETRY_DELAY_S,
        preflight_timeout_s: float = _DEFAULT_PREFLIGHT_TIMEOUT_S,
        preflight_poll_interval_s: float = _DEFAULT_PREFLIGHT_POLL_INTERVAL_S,
        settle_timeout_s: float = _DEFAULT_SETTLE_TIMEOUT_S,
        settle_poll_interval_s: float = _DEFAULT_SETTLE_POLL_INTERVAL_S,
        post_slew_settle_s: float = _DEFAULT_POST_SLEW_SETTLE_S,
        settle_stable_tolerance_arcsec: float = _DEFAULT_SETTLE_STABLE_TOLERANCE_ARCSEC,
        settle_consecutive_stable: int = _DEFAULT_SETTLE_CONSECUTIVE_STABLE,
        settle_target_tolerance_arcsec: float = _DEFAULT_SETTLE_TARGET_TOLERANCE_ARCSEC,
        tracking_attempts: int = _DEFAULT_TRACKING_ATTEMPTS,
        tracking_retry_delay_s: float = _DEFAULT_TRACKING_RETRY_DELAY_S,
        max_solve_attempts: int = _DEFAULT_MAX_SOLVE_ATTEMPTS,
        solve_retry_delay_s: float = _DEFAULT_SOLVE_RETRY_DELAY_S,
        refinement_solve_timeout_s: float = _DEFAULT_REFINEMENT_SOLVE_TIMEOUT_S,
        refinement_max_solve_attempts: int = _DEFAULT_REFINEMENT_MAX_SOLVE_ATTEMPTS,
        interactive_solve_timeout_s: float = _DEFAULT_INTERACTIVE_SOLVE_TIMEOUT_S,
        persist: Callable[[PolarAlignmentState], Awaitable[None]] | None = None,
        night_key: Callable[[datetime], tuple[date, str] | None] | None = None,
    ) -> None:
        self.before_acquisition = before_acquisition
        self._camera = camera
        self._telescope = telescope
        self._filterwheel = filterwheel
        self._device = device
        self._state = state
        self._astro = astro
        self._tasks = tasks
        self._section = section
        self._solve = solve
        self._configured_solver = ConfiguredAstap(settings) if settings is not None else None
        self._availability = (self._configured_solver.availability
                              if self._configured_solver is not None and availability is astap_availability
                              else availability)
        self._now = now
        self._sleep = sleep
        self._max_slew_attempts = max_slew_attempts
        self._slew_retry_delay_s = slew_retry_delay_s
        self._preflight_timeout_s = preflight_timeout_s
        self._preflight_poll_interval_s = preflight_poll_interval_s
        self._settle_timeout_s = settle_timeout_s
        self._settle_poll_interval_s = settle_poll_interval_s
        self._post_slew_settle_s = post_slew_settle_s
        self._settle_stable_tolerance_arcsec = settle_stable_tolerance_arcsec
        self._settle_consecutive_stable = settle_consecutive_stable
        self._settle_target_tolerance_arcsec = settle_target_tolerance_arcsec
        self._tracking_attempts = tracking_attempts
        self._tracking_retry_delay_s = tracking_retry_delay_s
        self._max_solve_attempts = max_solve_attempts
        self._solve_retry_delay_s = solve_retry_delay_s
        self._refinement_solve_timeout_s = refinement_solve_timeout_s
        self._refinement_max_solve_attempts = refinement_max_solve_attempts
        self._interactive_solve_timeout_s = interactive_solve_timeout_s
        self._solve_here_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        #: The sweep's own retained model, kept for refinement to be
        #: differential *from the sweep*, never from a previous refinement
        #: (module docstring). Never restored across a restart — see
        #: ``PolarAlignmentState.refinement``'s own docstring.
        self._sweep_model: PolarAlignmentModel | None = None
        self._last_step_deg: float | None = None
        self._sweep_targets: tuple[tuple[float, float], ...] | None = None
        self._sweep_start_mode: PolarAlignmentStartMode | None = None
        self._refinement_task: asyncio.Task[None] | None = None
        self._active_mount_dec_deg: float | None = None
        self._persist = persist
        self._night_key = night_key
        self._retained_night = None if night_key is None else night_key(section.started_at or self._now())
        self._persist_pending: PolarAlignmentState | None = None
        self._persist_task: asyncio.Task[None] | None = None
        self._persist_lock = asyncio.Lock()

    def _ensure_current_night(self) -> None:
        if (self._night_key is None
                or self._section.status is PolarAlignmentStatus.RUNNING
                or self._section.refinement.status is PolarRefinementStatus.RUNNING):
            return
        key = self._night_key(self._now())
        if key is not None and key != self._retained_night:
            self._retained_night = key
            self._sweep_model = None
            self._last_step_deg = None
            self._sweep_targets = None
            self._sweep_start_mode = None
            self._section = PolarAlignmentState()
            self._state.apply(PolarAlignmentChanged(polar_alignment=self._section))

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
            self._state.apply(NoticeCleared(notice_id="polar-alignment-retention"))
        except Exception as exc:
            _log.exception("polar alignment night retention failed")
            self._state.apply(NoticeRaised(notice=Notice(
                id="polar-alignment-retention", code="polar_alignment_retention_failed",
                level=NoticeLevel.WARNING, raised_at=self._now(),
                message=f"Polar alignment details could not be retained: {exc}",
            )))

    async def flush_persistence(self) -> None:
        task = self._persist_task
        if task is not None and task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        if self._persist_pending is not None:
            await self._persist_updates()

    @property
    def section(self) -> PolarAlignmentState:
        self._ensure_current_night()
        return self._section

    def _publish(self, **updates: object) -> PolarAlignmentState:
        self._section = self._section.model_copy(update=updates)
        self._state.apply(PolarAlignmentChanged(polar_alignment=self._section))
        if self._persist is not None and self._section.started_at is not None:
            self._persist_pending = self._section
            if self._persist_task is None or self._persist_task.done():
                self._persist_task = self._tasks.spawn(self._persist_updates(), name="polar-alignment-retention")
        return self._section

    def _set_point(self, index: int, **updates: object) -> None:
        points = list(self._section.points)
        points[index] = points[index].model_copy(update=updates)
        self._publish(points=points)

    def _publish_refinement(self, **updates: object) -> PolarAlignmentState:
        """Whole-section replacement of ``polar_alignment.refinement``, via
        the same ``PolarAlignmentChanged`` event as every other change to
        this section (CLAUDE.md invariant 4: a desktop and a phone must
        never disagree about a live-mutating surface)."""
        refinement = self._section.refinement.model_copy(update=updates)
        return self._publish(refinement=refinement)

    # -- starting and cancelling --------------------------------------------- #

    async def start(
        self,
        *,
        point_count: int,
        step_deg: float,
        exposure_s: float,
        gain: int | None = None,
        apply_refraction: bool = True,
        start_point: PolarAlignmentStartMode = PolarAlignmentStartMode.BEST,
        start_ra_j2000_h: float | None = None,
        start_dec_j2000_deg: float | None = None,
        auto_refine: bool = False,
        run_kind: PolarAlignmentRunKind = PolarAlignmentRunKind.MEASURE,
        comparison_model: PolarAlignmentModel | None = None,
        _planned_targets: tuple[tuple[float, float], ...] | None = None,
        _planned_start_mode: PolarAlignmentStartMode | None = None,
    ) -> PolarAlignmentState:
        """Validate everything that can be checked without moving the mount,
        publish the run as started, and hand the rest to a background task —
        the same synchronous-validate/spawn-and-return split
        ``CaptureService.request_exposure`` uses, for the same reason: a
        multi-minute operation must never be awaited by the request that
        started it.

        ``start_point`` picks one of DESIGN.md §4.5's start points:
        ``BEST`` (the default — ``ssc.astro.polar.best_conditioned_field``,
        needing no coordinate at all, only the site and ephemeris already
        required below), ``CURRENT`` (the mount's own J2000 position, held
        over from before 2026-09-11's default change), or ``EXPLICIT``
        ``SEESTAR`` (the official-app field geometry measured on 2026-09-12),
        or ``EXPLICIT`` (``start_ra_j2000_h``/``start_dec_j2000_deg``, both required
        together). Giving a coordinate outside ``EXPLICIT``, or omitting one
        inside it, refuses with :class:`PolarAlignmentStartAmbiguousError`
        rather than guessing which one the caller meant (CLAUDE.md
        invariant 7) — the API layer refuses the same shape earlier, at the
        request boundary.

        ``step_deg``'s sign *is* DESIGN.md §4.5's "direction" setting: a
        negative value rotates through decreasing RA instead of increasing,
        which on a wedge-mounted S30 is the same choice as which way the arm
        swings — there is no separate east/west field because the sign of
        the one number already says it. ``gain`` is applied once, before the
        first exposure (`Camera.Gain` — DESIGN.md §4.5: high gain and a
        short sub is the usual trade here, since every frame is discarded
        after solving)."""
        if self._section.status is PolarAlignmentStatus.RUNNING or (
            self._task is not None and not self._task.done()
        ):
            raise PolarAlignmentAlreadyRunningError(
                "a polar-alignment run is still finishing; only one runs at a time"
            )
        self._ensure_current_night()
        if self._refinement_task is not None and not self._refinement_task.done():
            raise PolarRefinementAlreadyRunningError(
                "a live refinement loop is running against the current sweep; stop it before "
                "starting a new sweep — a new sweep would slew the mount the refinement loop "
                "assumes is standing still"
            )
        if self._solve_here_lock.locked():
            raise PolarSolveAlreadyRunningError(
                "a target-centering or plate-solve exposure is already in progress; "
                "wait for it before starting a polar-alignment sweep"
            )
        if start_point is PolarAlignmentStartMode.EXPLICIT:
            if start_ra_j2000_h is None or start_dec_j2000_deg is None:
                raise PolarAlignmentStartAmbiguousError(
                    "start_point 'explicit' requires both start_ra_hours and start_dec_deg"
                )
        elif start_ra_j2000_h is not None or start_dec_j2000_deg is not None:
            raise PolarAlignmentStartAmbiguousError(
                f"start_ra_hours/start_dec_deg were given but start_point is "
                f"{start_point.value!r}, not 'explicit': which one wins is ambiguous, so this "
                "refuses rather than guessing"
            )
        if self._camera is None or self._telescope is None:
            raise PolarAlignmentDeviceUnavailableError(
                "this ssc-core hosts no camera/telescope, so a polar-alignment run cannot start"
            )
        mode = await self._device.mount_alignment_mode()
        if mode != _ALIGNMENT_MODE_POLAR:
            raise PolarAlignmentNotPolarModeError(mode)
        mount = self._state.snapshot.mount
        if mount is None or mount.site_latitude_deg is None or mount.site_longitude_deg is None:
            raise PolarAlignmentSiteUnknownError(
                "the mount has not yet reported its own SiteLatitude/SiteLongitude; connect "
                "and wait for a telemetry poll before starting a polar-alignment run"
            )
        ephemeris = self._astro.ephemeris
        if ephemeris is None:
            raise PolarAlignmentEphemerisUnavailableError(
                self._astro.ephemeris_error or "the ephemeris kernel is not loaded"
            )
        site = mount_site(Deg(mount.site_latitude_deg), Deg(mount.site_longitude_deg))

        start_info: PolarAlignmentStartInfo
        effective_step_deg = step_deg
        planned_altitudes: list[float] = []
        if start_point is PolarAlignmentStartMode.EXPLICIT:
            # Ambiguity guard above already ensured both are present.
            assert start_ra_j2000_h is not None and start_dec_j2000_deg is not None
            start_info = PolarAlignmentStartInfo(
                mode=_planned_start_mode or PolarAlignmentStartMode.EXPLICIT,
                step_deg=step_deg,
                arc_span_deg=abs(step_deg) * (point_count - 1),
            )
        elif start_point is PolarAlignmentStartMode.CURRENT:
            if mount.ra_j2000_h is None or mount.dec_j2000_deg is None:
                raise PolarAlignmentPositionUnknownError(
                    "start_point is 'current' and the mount's own J2000 position is not yet "
                    "known; wait for a telemetry poll, or use start_point 'best' or 'explicit'"
                )
            start_ra_j2000_h = mount.ra_j2000_h
            start_dec_j2000_deg = mount.dec_j2000_deg
            start_info = PolarAlignmentStartInfo(
                mode=PolarAlignmentStartMode.CURRENT,
                step_deg=step_deg,
                arc_span_deg=abs(step_deg) * (point_count - 1),
            )
        elif start_point is PolarAlignmentStartMode.SEESTAR:
            # Captured from the official app on 2026-09-12: its first field
            # is about 15 degrees west of the meridian at apparent
            # declination equal to the site latitude. Express the preset in
            # that local frame, then convert at the actual start instant so
            # it remains reusable instead of freezing the recorded clock RA.
            field_when = self._now()
            coord = j2000_coord_at_hour_angle(
                ephemeris,
                site,
                field_when,
                declination_deg=site.latitude_deg,
                hour_angle_deg=Deg(_SEESTAR_FIELD_HOUR_ANGLE_DEG),
            )
            start_ra_j2000_h = float(coord.ra_h)
            start_dec_j2000_deg = float(coord.dec_deg)
            start_info = PolarAlignmentStartInfo(
                mode=PolarAlignmentStartMode.SEESTAR,
                step_deg=step_deg,
                arc_span_deg=abs(step_deg) * (point_count - 1),
                start_altitude_deg=float(
                    altitude_deg(
                        Deg(_SEESTAR_FIELD_HOUR_ANGLE_DEG),
                        site.latitude_deg,
                        site.latitude_deg,
                    )
                ),
                start_azimuth_deg=float(
                    azimuth_deg(
                        Deg(_SEESTAR_FIELD_HOUR_ANGLE_DEG),
                        site.latitude_deg,
                        site.latitude_deg,
                    )
                ),
                circle_radius_deg=90.0 - abs(float(site.latitude_deg)),
            )
        else:
            requested_span = abs(step_deg) * (point_count - 1)
            candidate_spans = [requested_span]
            next_span = requested_span - _ADAPTIVE_ARC_STEP_DEG
            while next_span >= _MIN_ADAPTIVE_TOTAL_ARC_DEG:
                candidate_spans.append(next_span)
                next_span -= _ADAPTIVE_ARC_STEP_DEG
            last_clear_arc_error: DegenerateGeometryError | None = None
            field = None
            for span in candidate_spans:
                candidate_step = math.copysign(span / (point_count - 1), step_deg)
                try:
                    field = best_conditioned_field(
                        ephemeris,
                        site,
                        self._now(),
                        point_count=point_count,
                        step_deg=Deg(candidate_step),
                    )
                except DegenerateGeometryError as exc:
                    if exc.reason is not DegeneracyReason.NO_CLEAR_ARC:
                        raise
                    last_clear_arc_error = exc
                    continue
                effective_step_deg = candidate_step
                break
            if field is None:
                assert last_clear_arc_error is not None
                raise PolarAlignmentNoClearArcError(
                    str(last_clear_arc_error)
                ) from last_clear_arc_error
            # ``SweepField.coord`` is J2000 with RA in degrees
            # (``ssc.astro`` units rule); convert to hours here, at the
            # wiring boundary, per this dispatch's own instruction.
            start_ra_j2000_h = float(field.coord.ra_h)
            start_dec_j2000_deg = float(field.coord.dec_deg)
            planned_altitudes = [float(value) for value in field.point_altitudes_deg]
            warnings = list(field.warnings)
            if abs(effective_step_deg - step_deg) > 1e-9:
                warnings.append(
                    f"the requested {requested_span:.0f} deg arc could not stay safely above "
                    f"the altitude floor; SSC reduced it to {float(field.arc_span_deg):.0f} deg"
                )
            start_info = PolarAlignmentStartInfo(
                mode=PolarAlignmentStartMode.BEST,
                step_deg=effective_step_deg,
                arc_span_deg=float(field.arc_span_deg),
                point_altitudes_deg=planned_altitudes,
                start_altitude_deg=float(field.start_altitude_deg),
                start_azimuth_deg=float(field.start_azimuth_deg),
                meridian_altitude_deg=float(field.meridian_altitude_deg),
                worst_altitude_deg=float(field.worst_altitude_deg),
                declination_offset_deg=float(field.declination_offset_deg),
                circle_radius_deg=float(field.circle_radius_deg),
                sweep_amplification=field.sweep_amplification,
                ideal=field.ideal,
                warnings=warnings,
            )

        availability = await self._availability()
        if not availability.available:
            raise PolarAlignmentSolverUnavailableError(availability)
        if gain is not None:
            # Before anything is published as running: a rejected gain
            # (out of the camera's own range) must refuse the whole start,
            # not strand a "running" section behind a camera that never
            # actually got the gain the caller asked for.
            await self._camera.put("gain", Gain=gain)
        await self._select_uvircut_filter()

        targets = (
            list(_planned_targets)
            if _planned_targets is not None
            else _build_targets(
                start_ra_j2000_h, start_dec_j2000_deg, point_count, effective_step_deg
            )
        )
        points_state = [
            PolarAlignmentPointState(
                index=index,
                status=PolarAlignmentPointStatus.PENDING,
                target_ra_j2000_h=ra_h,
                target_dec_j2000_deg=dec_deg,
                planned_altitude_deg=(
                    planned_altitudes[index] if index < len(planned_altitudes) else None
                ),
            )
            for index, (ra_h, dec_deg) in enumerate(targets)
        ]
        # Keep the last *completed* model and geometry until this run also
        # completes. A cancelled or cloud-failed recheck must remain retryable
        # against the same independent baseline; a half-run is not evidence.
        self._ensure_current_night()
        self._publish(
            status=PolarAlignmentStatus.RUNNING,
            run_kind=run_kind,
            started_at=self._now(),
            ended_at=None,
            exposure_started_at=None,
            exposure_duration_s=None,
            current_point_index=None,
            preflight_reason=None,
            points=points_state,
            result=None,
            assessment=None,
            start=start_info,
            error=None,
            refinement=PolarRefinementState(),
        )
        self._task = self._tasks.spawn(
            self._run(
                targets=targets,
                site=site,
                ephemeris=ephemeris,
                exposure_s=exposure_s,
                apply_refraction=apply_refraction,
                auto_refine=auto_refine,
                comparison_model=comparison_model,
                step_deg=effective_step_deg,
            ),
            name="polar-alignment-run",
        )
        return self._section

    async def start_recheck(
        self,
        *,
        exposure_s: float,
        gain: int | None = None,
        apply_refraction: bool = True,
        point_count: int | None = None,
    ) -> PolarAlignmentState:
        """Run a fresh fit over the completed sweep's sky arc in the same direction.

        The prior fitted axis is retained only for an explicit consistency
        assessment after the new fit. It is never averaged into, or used to
        constrain, the new measurement. With a new point count, intermediate
        positions are interpolated across the same arc.
        """
        self._ensure_current_night()
        previous = self._sweep_model
        if previous is None:
            raise PolarAlignmentNoMeasurementError(
                "no completed polar-alignment measurement exists in this session; "
                "run Measure before requesting an independent recheck"
            )
        if self._sweep_targets is None or self._last_step_deg is None:
            raise PolarAlignmentNoMeasurementError(
                "the completed measurement has no retained sweep geometry; run Measure again"
            )
        previous_count = len(self._sweep_targets)
        if point_count is None:
            point_count = previous_count
        if not 3 <= point_count <= 12:
            raise ValueError("recheck point_count must be between 3 and 12")
        # Preserve the measured arc and its direction. Extra points
        # sample the same arc rather than extending it into untested sky.
        step = self._last_step_deg * (previous_count - 1) / (point_count - 1)
        targets = (
            self._sweep_targets
            if point_count == previous_count
            else tuple(
                _build_targets(
                    self._sweep_targets[0][0], self._sweep_targets[0][1], point_count, step
                )
            )
        )
        start_ra_j2000_h, start_dec_j2000_deg = targets[0]
        return await self.start(
            point_count=point_count,
            step_deg=step,
            exposure_s=exposure_s,
            gain=gain,
            apply_refraction=apply_refraction,
            start_point=PolarAlignmentStartMode.EXPLICIT,
            start_ra_j2000_h=start_ra_j2000_h,
            start_dec_j2000_deg=start_dec_j2000_deg,
            auto_refine=False,
            run_kind=PolarAlignmentRunKind.RECHECK,
            comparison_model=previous,
            _planned_targets=targets,
            _planned_start_mode=self._sweep_start_mode,
        )

    async def cancel(self) -> PolarAlignmentState:
        """Cancel the in-progress run. Leaves the mount stopped
        (:meth:`_run`'s ``CancelledError`` handler) and the state coherent —
        the section reaches ``CANCELLED`` before this returns."""
        if self._task is None or self._task.done():
            raise PolarAlignmentNotRunningError("no polar-alignment run is in progress")
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        return self._section

    async def run_unattended(self, **kwargs: object) -> None:
        """Start a scheduler-owned run and wait until its background procedure ends."""
        start_point = kwargs.get("start_point")
        if isinstance(start_point, str):
            kwargs["start_point"] = PolarAlignmentStartMode(start_point)
        # Scheduler item fields use the concise names exposed in the plan UI;
        # the service API keeps the explicit J2000 suffix for clarity.
        if "start_ra_hours" in kwargs:
            kwargs["start_ra_j2000_h"] = kwargs.pop("start_ra_hours")
        if "start_dec_deg" in kwargs:
            kwargs["start_dec_j2000_deg"] = kwargs.pop("start_dec_deg")
        await self.start(**kwargs)  # type: ignore[arg-type]
        if self._task is not None:
            await self._task

    # -- live refinement (module docstring's seam) ---------------------------- #

    async def solve_here(self, *, exposure_s: float, gain: int | None = None) -> PlateSolveResult:
        """One exposure where the scope already points, solved — "where is it
        *really* looking" — with nothing slewed and no run published.

        Lives on this service rather than on a mount route because the camera,
        the telescope and the solver are wired together here and nowhere else
        above the Alpaca seam; this is :meth:`_expose_and_solve`, the
        refinement loop's own primitive, minus the loop.

        Refuses while a sweep or a refinement loop is running. Both assume
        they are the only thing driving the camera, and a frame taken from
        underneath one would be attributed to whichever exposure happened to
        be in flight — a wrong measurement rather than a failed one.
        """
        if self._section.status is PolarAlignmentStatus.RUNNING:
            raise PolarAlignmentAlreadyRunningError(
                "a polar-alignment run is in progress and is already exposing; a second "
                "exposure would contend with it for the camera"
            )
        if self._refinement_task is not None and not self._refinement_task.done():
            raise PolarRefinementAlreadyRunningError(
                "a live refinement loop is running and is already exposing; a second "
                "exposure would contend with it for the camera"
            )
        if self._camera is None or self._telescope is None:
            raise PolarAlignmentDeviceUnavailableError(
                "this ssc-core hosts no camera/telescope, so there is nothing to solve from"
            )
        availability = await self._availability()
        if not availability.available:
            raise PolarAlignmentSolverUnavailableError(availability)
        if gain is not None:
            await self._camera.put("gain", Gain=gain)
        # No await between this check and acquisition: two requests that
        # passed the slower availability/gain calls cannot both queue camera
        # work and make the second button appear to hang behind the first.
        if self._solve_here_lock.locked():
            raise PolarSolveAlreadyRunningError(
                "another target-centering or plate-solve exposure is already in progress"
            )
        async with self._solve_here_lock:
            try:
                return await self._expose_and_solve(
                    exposure_s,
                    "solve-here",
                    solve_timeout_s=self._interactive_solve_timeout_s,
                )
            except (AlpacaClientError, ExposureNotAvailableError, ExposureTimeoutError) as exc:
                raise PolarAlignmentDeviceUnavailableError(
                    f"plate-solve capture failed: {exc}"
                ) from exc

    async def start_refinement(
        self, *, exposure_s: float, gain: int | None = None
    ) -> PolarAlignmentState:
        """Validate everything synchronously, stop tracking and confirm it,
        publish the loop as started, and hand the rest to a background task —
        the same split :meth:`start` uses, for the same reason. The reference
        solve itself is *not* taken here: a plate solve is a multi-second
        camera exposure, so it belongs in the background task like every
        other slow step, not in the request that started the loop. Refinement
        caps its exposure at two seconds because the required halted drive
        would trail stars during longer sweep exposures.
        """
        self._ensure_current_night()
        if self._refinement_task is not None and not self._refinement_task.done():
            raise PolarRefinementAlreadyRunningError(
                "a refinement loop is already running; only one runs at a time"
            )
        if self._section.status is PolarAlignmentStatus.RUNNING:
            raise PolarRefinementAlreadyRunningError(
                "a polar-alignment sweep is currently running; refinement measures a fixed "
                "tripod state and cannot start while the mount is being slewed for a sweep"
            )
        if self._section.refinement.status is not None:
            raise PolarRefinementNoSweepError(
                "live adjustment has already ended for this sweep; run a new measurement "
                "before starting another reference"
            )
        if self._section.status is not PolarAlignmentStatus.COMPLETED:
            raise PolarRefinementNoSweepError(
                "the latest polar-alignment sweep did not complete; run Measure again"
            )
        if self._solve_here_lock.locked():
            raise PolarSolveAlreadyRunningError(
                "a plate-solve exposure is already in progress; wait before starting live adjustment"
            )
        if self._camera is None or self._telescope is None:
            raise PolarAlignmentDeviceUnavailableError(
                "this ssc-core hosts no camera/telescope, so a refinement loop cannot start"
            )
        sweep_model = self._sweep_model
        if sweep_model is None:
            raise PolarRefinementNoSweepError(
                "no completed polar-alignment sweep exists in this session; run a 3(+)-point "
                "sweep first — refinement is differential from the sweep's own model and has "
                "nothing to measure from without one"
            )
        ephemeris = self._astro.ephemeris
        if ephemeris is None:
            raise PolarAlignmentEphemerisUnavailableError(
                self._astro.ephemeris_error or "the ephemeris kernel is not loaded"
            )
        availability = await self._availability()
        if not availability.available:
            raise PolarAlignmentSolverUnavailableError(availability)
        if gain is not None:
            await self._camera.put("gain", Gain=gain)

        prior_mount = self._state.snapshot.mount
        if prior_mount is None or prior_mount.stale or prior_mount.slewing is not False:
            raise PolarRefinementTrackingError(
                "mount motion is not confirmed stopped; wait for fresh telemetry before live adjustment"
            )
        restore_tracking = prior_mount is not None and prior_mount.tracking is True
        restore_rate = None if prior_mount is None else prior_mount.tracking_rate

        # Stop tracking and make SSC confirm it, rather than hope
        # (``ssc.astro.polar.refine_polar_alignment``'s own precondition —
        # a tracking mount turns its own boresight 15"/s, which this would
        # read as tripod motion).
        mount = await self._device.mount_set_tracking(enabled=False, rate=None)
        if mount.tracking is not False:
            raise PolarRefinementTrackingError(
                f"asked the mount to stop tracking but it now reports tracking={mount.tracking!r}: "
                "refinement cannot proceed while the drive may still be turning the boresight"
            )

        self._publish_refinement(
            status=PolarRefinementStatus.RUNNING,
            started_at=self._now(),
            ended_at=None,
            iteration=0,
            degenerate_count=0,
            last_degenerate_reason=None,
            error=None,
            result=None,
        )
        refinement_exposure_s = min(exposure_s, _DEFAULT_REFINEMENT_EXPOSURE_S)
        if refinement_exposure_s < exposure_s:
            _log.info(
                "polar refinement: shortening %.1fs sweep exposure to %.1fs while tracking "
                "is halted",
                exposure_s,
                refinement_exposure_s,
            )
        self._refinement_task = self._tasks.spawn(
            self._refine_run(
                sweep_model=sweep_model,
                ephemeris=ephemeris,
                exposure_s=refinement_exposure_s,
                restore_tracking=restore_tracking,
                restore_rate=restore_rate,
            ),
            name="polar-alignment-refine",
        )
        return self._section

    async def stop_refinement(self) -> PolarAlignmentState:
        """Stop the live refinement loop. The user decides when they are
        done (out of this dispatch's scope is deciding *for* them); this
        only ends the loop and leaves the section at ``STOPPED``."""
        if self._refinement_task is None or self._refinement_task.done():
            raise PolarRefinementNotRunningError("no refinement loop is in progress")
        self._refinement_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._refinement_task
        return self._section

    async def _refine_run(
        self,
        *,
        sweep_model: PolarAlignmentModel,
        ephemeris: Ephemeris,
        exposure_s: float,
        restore_tracking: bool,
        restore_rate: MountTrackingRate | None,
    ) -> None:
        """Reference solve once, then re-solve continuously.

        ``sweep_model`` is fixed for the whole loop — never replaced by a
        previous iteration's own refined model (module docstring, and
        ``ssc.astro.polar.PolarRefinement.model``'s own comment on why that
        would compound the reference solve's noise once per re-solve).

        A :class:`~ssc.astro.polar.DegenerateGeometryError` iteration is
        published with its reason and the loop keeps going — it is a
        per-iteration outcome a user provokes by shoving the tripod too far,
        not a broken precondition. Anything else deriving from
        :class:`~ssc.astro.polar.PolarAlignmentError` (chiefly
        :class:`~ssc.astro.polar.TrackingNotStoppedError`, if tracking
        resumes mid-loop) *is* a broken precondition and ends the run.
        """
        try:
            if self.before_acquisition is not None:
                await self.before_acquisition()
            reference_solve = await self._refinement_expose_until_solved(
                exposure_s, "polar-alignment-refinement-reference"
            )
            reference = PolarPoint(
                coord=reference_solve.coord,
                when=reference_solve.date_obs,
                reported=self._reported_coord(),
            )
            while True:
                resolved_solve = await self._refinement_expose_until_solved(
                    exposure_s, "polar-alignment-refinement"
                )
                tracking = _mount_tracking(self._state.snapshot.mount)
                resolved = PolarPoint(
                    coord=resolved_solve.coord,
                    when=resolved_solve.date_obs,
                    reported=self._reported_coord(),
                )
                try:
                    refinement = refine_polar_alignment(
                        ephemeris, sweep_model, reference, resolved, tracking=tracking
                    )
                except DegenerateGeometryError as exc:
                    self._publish_refinement(
                        degenerate_count=self._section.refinement.degenerate_count + 1,
                        last_degenerate_reason=exc.reason.value,
                        error=str(exc),
                    )
                    _log.warning(
                        "polar refinement: iteration degenerate (%s): %s", exc.reason.value, exc
                    )
                    continue
                _log.info(
                    "polar refinement: iteration=%d tracking=%s reference_when=%s "
                    "resolved_when=%s reference_ra=%.7fh "
                    "reference_dec=%.7fdeg resolved_ra=%.7fh resolved_dec=%.7fdeg "
                    "boresight_shift=%.4fdeg axis_shift=%.4fdeg total_error=%.3farcmin "
                    "trusted=%s warnings=%s",
                    self._section.refinement.iteration + 1,
                    tracking.value,
                    reference.when.isoformat(),
                    resolved.when.isoformat(),
                    float(reference.coord.ra_h),
                    float(reference.coord.dec_deg),
                    float(resolved.coord.ra_h),
                    float(resolved.coord.dec_deg),
                    float(refinement.boresight_shift_deg),
                    float(refinement.axis_shift_deg),
                    float(refinement.model.total_error_deg) * 60.0,
                    refinement.trusted,
                    refinement.warnings,
                )
                self._publish_refinement(
                    iteration=self._section.refinement.iteration + 1,
                    last_degenerate_reason=None,
                    error=None,
                    result=_refinement_result_from(refinement),
                )
        except asyncio.CancelledError:
            self._publish_refinement(status=PolarRefinementStatus.STOPPED, ended_at=self._now())
            raise
        except (PolarAlignmentError, _RefinementSolveExhaustedError) as exc:
            _log.warning("polar refinement: ending on a broken precondition: %s", exc)
            self._publish_refinement(
                status=PolarRefinementStatus.FAILED, ended_at=self._now(), error=str(exc)
            )
        except Exception as exc:
            _log.exception("polar refinement run failed")
            self._publish_refinement(
                status=PolarRefinementStatus.FAILED,
                ended_at=self._now(),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self._publish_refinement(stage=None, exposure_started_at=None, exposure_duration_s=None)
            if restore_tracking:
                try:
                    await asyncio.shield(
                        self._device.mount_set_tracking(enabled=True, rate=restore_rate)
                    )
                except Exception as exc:
                    _log.exception("polar refinement: failed to restore tracking")
                    self._publish_refinement(
                        status=PolarRefinementStatus.FAILED,
                        ended_at=self._now(),
                        error=f"failed to restore tracking: {type(exc).__name__}: {exc}",
                    )

            await self.flush_persistence()

    # -- the run --------------------------------------------------------------- #

    async def _run(
        self,
        *,
        targets: list[tuple[float, float]],
        site: Site,
        ephemeris: Ephemeris,
        exposure_s: float,
        apply_refraction: bool,
        auto_refine: bool,
        comparison_model: PolarAlignmentModel | None,
        step_deg: float,
    ) -> None:
        successful: list[PolarPoint] = []
        self._active_mount_dec_deg = None
        try:
            if self.before_acquisition is not None:
                await self.before_acquisition()
            mount = await self._await_mount_preflight()
            if mount.site_latitude_deg is None or mount.site_longitude_deg is None:
                raise PolarAlignmentSiteUnknownError("mount site is unavailable after PA preflight")
            site = mount_site(Deg(mount.site_latitude_deg), Deg(mount.site_longitude_deg))
            for index, (ra_h, dec_deg) in enumerate(targets):
                self._publish(current_point_index=index)
                try:
                    point = await self._run_one_point(index, ra_h, dec_deg, exposure_s)
                except _PointFailedError as exc:
                    self._set_point(index, status=PolarAlignmentPointStatus.FAILED, error=str(exc))
                    _log.warning("polar alignment: point %d unusable: %s", index, exc)
                    continue
                self._set_point(
                    index,
                    status=PolarAlignmentPointStatus.SOLVED,
                    solved_ra_j2000_h=float(point.coord.ra_h),
                    solved_dec_j2000_deg=float(point.coord.dec_deg),
                    error=None,
                )
                successful.append(point)
            if len(successful) < 3:
                raise PolarAlignmentTooFewPointsError(
                    f"only {len(successful)} of {len(targets)} points solved; at least 3 are "
                    "needed to fit an axis"
                )
            model = solve_polar_alignment(
                ephemeris, site, successful, apply_refraction=apply_refraction
            )
            # Retained for refinement (module docstring's seam): the JSON
            # projection below is the section's own copy for clients, this
            # is the real dataclass a later ``start_refinement`` needs —
            # its ``site`` and refraction setting, not just its numbers.
            previous_model = self._sweep_model
            self._sweep_model = model
            self._last_step_deg = step_deg
            self._sweep_targets = tuple(targets)
            self._sweep_start_mode = self._section.start.mode if self._section.start else None
            assessment = _assess_sweep(model, comparison_model)
            if comparison_model is None:
                assessment = _add_sweep_comparison(assessment, model, previous_model)
            _log.info(
                "polar alignment: %s completed; total_error=%.3f arcmin "
                "axis_uncertainty=%.1f arcsec verdict=%s axis_separation=%s arcmin "
                "altitude_error=%.6fdeg azimuth_error=%.6fdeg points=%d "
                "rms_residual_arcsec=%s max_residual_arcsec=%s",
                self._section.run_kind,
                abs(float(model.total_error_deg)) * 60.0,
                float(model.axis_uncertainty_arcsec),
                assessment.verdict,
                (
                    "n/a"
                    if assessment.axis_separation_arcmin is None
                    else f"{assessment.axis_separation_arcmin:.3f}"
                ),
                float(model.altitude_error_deg),
                float(model.azimuth_error_deg),
                model.point_count,
                model.rms_residual_arcsec,
                model.max_residual_arcsec,
            )
            completed_at = self._now()
            result = _result_from_model(model)
            run_kind = self._section.run_kind or PolarAlignmentRunKind.MEASURE
            history_entry = PolarAlignmentHistoryEntry(
                run_kind=run_kind,
                completed_at=completed_at,
                altitude_error_deg=result.altitude_error_deg,
                azimuth_error_deg=result.azimuth_error_deg,
                total_error_deg=result.total_error_deg,
            )
            self._publish(
                status=PolarAlignmentStatus.COMPLETED,
                ended_at=completed_at,
                current_point_index=None,
                result=result,
                history=[*self._section.history, history_entry],
                assessment=assessment,
                error=None,
            )
            if auto_refine:
                try:
                    await self.start_refinement(exposure_s=exposure_s)
                except Exception as exc:
                    _log.exception("polar alignment: automatic live refinement failed to start")
                    self._publish_refinement(
                        status=PolarRefinementStatus.FAILED,
                        ended_at=self._now(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
        except asyncio.CancelledError:
            try:
                await self._device.mount_stop()
            except Exception:
                # Cancellation must still leave the state coherent even if
                # the panic-stop itself fails — logged, never silently lost
                # (architecture policy invariant 7), but never re-raised in place of
                # the CancelledError already in flight.
                _log.exception("polar alignment: mount_stop failed while cancelling")
            active_index = self._section.current_point_index
            terminal = {
                PolarAlignmentPointStatus.SOLVED,
                PolarAlignmentPointStatus.FAILED,
            }
            final_points = [
                point
                if point.status in terminal
                else point.model_copy(
                    update={
                        "status": (
                            PolarAlignmentPointStatus.ABORTED
                            if active_index is not None and point.index == active_index
                            else PolarAlignmentPointStatus.SKIPPED
                        ),
                        "error": (
                            "Polar alignment cancelled during this point"
                            if active_index is not None and point.index == active_index
                            else None
                        ),
                    }
                )
                for point in self._section.points
            ]
            self._publish(
                status=PolarAlignmentStatus.CANCELLED,
                ended_at=self._now(),
                current_point_index=None,
                points=final_points,
            )
            raise
        except (
            PolarAlignmentTooFewPointsError,
            DegenerateGeometryError,
            PolarAlignmentError,
            PolarAlignmentPreflightError,
        ) as exc:
            self._publish(
                status=PolarAlignmentStatus.FAILED,
                ended_at=self._now(),
                current_point_index=None,
                error=str(exc),
            )
        except Exception as exc:
            _log.exception("polar alignment run failed")
            self._publish(
                status=PolarAlignmentStatus.FAILED,
                ended_at=self._now(),
                current_point_index=None,
                error=f"{type(exc).__name__}: {exc}",
            )

        finally:
            await self.flush_persistence()

    async def _await_mount_preflight(self) -> MountState:
        """Wait for one fresh, site-aware, stationary poll before point 0."""
        reason = "Waiting for fresh site coordinates and a stationary mount"
        self._publish(preflight_reason=reason)
        deadline = self._now() + timedelta(seconds=self._preflight_timeout_s)
        last_reason = "mount telemetry is unavailable"
        while True:
            checked_at = self._now()
            mount = self._state.snapshot.mount
            if mount is None:
                last_reason = "mount telemetry is unavailable"
            elif mount.site_latitude_deg is None or mount.site_longitude_deg is None:
                last_reason = "SiteLatitude/SiteLongitude are not available"
            elif mount.stale or mount_stale(mount.polled_at, now=checked_at):
                last_reason = "mount telemetry is stale"
            elif mount.slewing is not False:
                last_reason = "the mount is still slewing or its motion state is unknown"
            else:
                assert mount.site_latitude_deg is not None
                assert mount.site_longitude_deg is not None
                self._publish(preflight_reason=None)
                return mount
            if checked_at >= deadline:
                raise PolarAlignmentPreflightError(
                    f"polar-alignment preflight timed out after "
                    f"{self._preflight_timeout_s:.0f}s: {last_reason}"
                )
            await self._sleep(self._preflight_poll_interval_s)

    async def _run_one_point(
        self, index: int, ra_h: float, dec_deg: float, exposure_s: float
    ) -> PolarPoint:
        # ``attempt`` is cumulative across the whole point — slew retries and
        # solve retries share one counter (``PolarAlignmentPointState.attempt``'s
        # own docstring: "every slew/capture/solve attempt"), so a point that
        # took two slews and then solved first time reads ``attempt == 2``,
        # not a solve-only count that quietly erases the slew retries.
        attempt = await self._slew_with_retry(index, ra_h, dec_deg)
        await self._ensure_tracking_after_slew()

        last_error: BaseException | None = None
        for solve_attempt in range(1, self._max_solve_attempts + 1):
            attempt += 1
            self._set_point(
                index, status=PolarAlignmentPointStatus.CAPTURING, attempt=attempt, error=None
            )
            try:
                result = await self._capture_and_solve(index, exposure_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                _log.warning(
                    "polar alignment: point %d solve attempt %d failed: %s",
                    index,
                    solve_attempt,
                    exc,
                )
                self._set_point(index, error=f"{type(exc).__name__}: {exc}")
                if solve_attempt < self._max_solve_attempts:
                    await self._sleep(self._solve_retry_delay_s)
                continue
            _log.info(
                "polar alignment: point %d solved exposure_epoch=%s ra_j2000=%.9fh "
                "dec_j2000=%.9fdeg midpoint_epoch=%s",
                index,
                result.date_obs.isoformat(),
                float(result.coord.ra_h),
                float(result.coord.dec_deg),
                (result.date_obs + timedelta(seconds=exposure_s / 2)).isoformat(),
            )
            # A plate-solved integrated image represents the exposure midpoint.
            # DATE-OBS remains the start in FITS and PlateSolveResult; the
            # Earth-fixed axis fit must use the midpoint, not the start.
            return PolarPoint(
                coord=result.coord,
                when=result.date_obs + timedelta(seconds=exposure_s / 2),
                reported=self._reported_coord(),
            )
        raise _PointFailedError(
            f"could not solve after {self._max_solve_attempts} attempts: {last_error}"
        )

    async def _slew_with_retry(self, index: int, ra_h: float, dec_deg: float) -> int:
        """Slew and prove arrival, retrying accepted moves that stop off-target.

        ASCOM accepting ``SlewToCoordinatesAsync`` only proves that the command
        was queued.  The S30 Pro can occasionally stop without reaching the
        requested coordinates, especially after several back-to-back sweeps.
        Treat that exactly like a refused busy command: retain the bounded
        retry count and try the whole move again.
        """
        attempt = 0
        while True:
            attempt += 1
            self._set_point(
                index,
                status=PolarAlignmentPointStatus.SLEWING,
                attempt=attempt,
                actual_altitude_deg=None,
                actual_azimuth_deg=None,
                mount_slewing=None,
                mount_tracking=None,
                error=None,
            )
            point_state = self._section.points[index]
            mount = self._state.snapshot.mount
            _log.info(
                "polar alignment: point %d slew attempt %d target_ra=%.6fh target_dec=%.6fdeg "
                "planned_altitude=%s mount_slewing=%s mount_tracking=%s",
                index,
                attempt,
                ra_h,
                dec_deg,
                (
                    "unknown"
                    if point_state.planned_altitude_deg is None
                    else f"{point_state.planned_altitude_deg:.3f}deg"
                ),
                None if mount is None else mount.slewing,
                None if mount is None else mount.tracking,
            )
            try:
                self._active_mount_dec_deg = await self._device.mount_slew_polar(
                    ra_hours=ra_h,
                    dec_deg=dec_deg,
                    fixed_mount_dec_deg=self._active_mount_dec_deg,
                )
                _log.info(
                    "polar alignment: point %d commanded device declination=%.9fdeg",
                    index,
                    self._active_mount_dec_deg,
                )
                await self._settle(index)
                return attempt
            except MountNotAvailableError as exc:
                raise _PointFailedError(f"the mount does not support slewing: {exc}") from exc
            except (MountRefusedError, _PointFailedError) as exc:
                if attempt >= self._max_slew_attempts:
                    raise _PointFailedError(
                        f"the mount did not complete the slew after {attempt} attempts: {exc}"
                    ) from exc
                self._set_point(index, error=str(exc))
                _log.warning(
                    "polar alignment: point %d slew incomplete (attempt %d); retrying: %s",
                    index,
                    attempt,
                    exc,
                )
                await self._sleep(self._slew_retry_delay_s)

    async def _settle(self, index: int) -> None:
        """Wait until the mount's own reported position is stable across two
        distinct fresh polls with ``Slewing`` reading ``False`` — see the
        module docstring for why ``Slewing`` alone is not trusted."""
        deadline = self._now() + timedelta(seconds=self._settle_timeout_s)
        previous: tuple[float, float] | None = None
        last_polled_at: datetime | None = None
        stable = 0
        point = self._section.points[index]
        target = (point.target_ra_j2000_h, point.target_dec_j2000_deg)
        while True:
            checked_at = self._now()
            mount = self._state.snapshot.mount
            current = self._mount_j2000_position(mount)
            fresh = (
                mount is not None
                and not mount.stale
                and not mount_stale(mount.polled_at, now=checked_at)
            )
            new_sample = (
                fresh
                and mount is not None
                and (
                    mount.polled_at is not None
                    and (last_polled_at is None or mount.polled_at > last_polled_at)
                )
            )
            at_target = (
                fresh
                and mount is not None
                and mount.slewing is False
                and current is not None
                and _separation_arcsec(current, target) <= self._settle_target_tolerance_arcsec
            )
            if at_target and mount is not None:
                point_state = self._section.points[index]
                if point_state.status is not PolarAlignmentPointStatus.SETTLING:
                    self._set_point(
                        index,
                        status=PolarAlignmentPointStatus.SETTLING,
                        actual_altitude_deg=mount.alt_deg,
                        actual_azimuth_deg=mount.az_deg,
                        mount_slewing=mount.slewing,
                        mount_tracking=mount.tracking,
                    )
            if new_sample and at_target and mount is not None and current is not None:
                last_polled_at = mount.polled_at
                if (
                    previous is not None
                    and _separation_arcsec(previous, current)
                    <= self._settle_stable_tolerance_arcsec
                ):
                    stable += 1
                    if stable >= self._settle_consecutive_stable:
                        self._set_point(
                            index,
                            status=PolarAlignmentPointStatus.SETTLING,
                            actual_altitude_deg=mount.alt_deg,
                            actual_azimuth_deg=mount.az_deg,
                            mount_slewing=mount.slewing,
                            mount_tracking=mount.tracking,
                        )
                        _log.info(
                            "polar alignment: point %d settled planned_altitude=%s "
                            "actual_altitude=%s actual_azimuth=%s slewing=%s tracking=%s",
                            index,
                            (
                                "unknown"
                                if self._section.points[index].planned_altitude_deg is None
                                else f"{self._section.points[index].planned_altitude_deg:.3f}deg"
                            ),
                            "unknown" if mount.alt_deg is None else f"{mount.alt_deg:.3f}deg",
                            "unknown" if mount.az_deg is None else f"{mount.az_deg:.3f}deg",
                            mount.slewing,
                            mount.tracking,
                        )
                        if self._post_slew_settle_s > 0:
                            _log.info(
                                "polar alignment: point %d mount is stable; waiting %.1fs "
                                "before capture",
                                index,
                                self._post_slew_settle_s,
                            )
                            await self._sleep(self._post_slew_settle_s)
                        return
                else:
                    stable = 1
                previous = current
            elif not fresh or (new_sample and not at_target):
                previous = None
                stable = 0
                if mount is not None:
                    last_polled_at = mount.polled_at
            if checked_at >= deadline:
                raise _PointFailedError(
                    f"the mount did not settle within {self._settle_timeout_s:.0f}s of the "
                    "slew (it never reached the target with Slewing false and a stable position)"
                )
            await self._sleep(self._settle_poll_interval_s)

    @staticmethod
    def _mount_j2000_position(mount: MountState | None) -> tuple[float, float] | None:
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

    async def _ensure_tracking_after_slew(self) -> None:
        mount = self._state.snapshot.mount
        if mount is not None and mount.tracking is True:
            return
        last_error: str | None = None
        for attempt in range(1, self._tracking_attempts + 1):
            try:
                tracking = await self._device.mount_set_tracking(enabled=True, rate="sidereal")
            except (MountNotAvailableError, MountRefusedError) as exc:
                current = self._state.snapshot.mount
                if current is not None and current.tracking is True:
                    return
                last_error = str(exc)
            else:
                if tracking.tracking is True:
                    return
                last_error = f"mount reports tracking={tracking.tracking!r}"
            if attempt < self._tracking_attempts:
                await self._sleep(self._tracking_retry_delay_s)
        raise _PointFailedError(
            f"could not start sidereal tracking after {self._tracking_attempts} attempts: "
            f"{last_error}"
        )

    async def _capture_and_solve(self, index: int, exposure_s: float) -> PlateSolveResult:
        assert self._camera is not None
        self._publish(exposure_started_at=self._now(), exposure_duration_s=exposure_s)
        try:
            frame, _started_at = await drive_exposure(
                camera=self._camera,
                telescope=self._telescope,
                filterwheel=self._filterwheel,
                duration_s=exposure_s,
                context=self._exposure_context(),
                frame_type=FrameType.LIGHT,
                now=self._now,
                sleep=self._sleep,
                metadata_mode="solver",
            )
        finally:
            self._publish(exposure_started_at=None, exposure_duration_s=None)
        with tempfile.TemporaryDirectory(prefix="ssc-polar-") as tmp_dir:
            fits_path = Path(tmp_dir) / f"point-{index}.fits"
            solve_frame = bin_2x2_for_solving(frame)
            await _write_solver_frame(fits_path, solve_frame)
            self._set_point(index, status=PolarAlignmentPointStatus.SOLVING)
            return await self._solve_with_hints(fits_path)

    async def _solve_with_hints(self, fits_path: Path) -> PlateSolveResult:
        if self._solve is not astap_solve:
            return await self._solve(fits_path)
        mount = self._state.snapshot.mount
        position = None
        if (mount is not None and not mount.stale and mount.polled_at is not None
            and 0 <= (self._now() - mount.polled_at).total_seconds() <= 15
            and mount.ra_j2000_h is not None and mount.dec_j2000_deg is not None):
            position = (mount.ra_j2000_h, mount.dec_j2000_deg)
        field = self._state.snapshot.camera_fov
        solver = astap_solve if self._configured_solver is None else self._configured_solver.solve
        return await solver(fits_path, position_hint=position,
                                 field_height_deg=None if field is None else field.height_deg)

    def _exposure_context(self, object_name: str = "polar-alignment") -> ExposureContext:
        device = self._state.snapshot.device
        return ExposureContext(
            object_name=object_name,
            focal_length_mm=None,
            telescope_name=None,
            ssc_version=_ssc_version(),
            device_firmware=None if device is None else device.firmware,
        )

    async def _expose_and_solve(
        self,
        exposure_s: float,
        object_name: str,
        *,
        publish_refinement_stage: bool = False,
        solve_timeout_s: float | None = None,
    ) -> PlateSolveResult:
        """One exposure, solved — the refinement loop's own primitive.

        No point index, no per-point status: refinement has no points, only
        a running count of iterations (``_refine_run``'s own publishing).
        Mirrors :meth:`_capture_and_solve` minus that bookkeeping.
        """
        assert self._camera is not None
        if publish_refinement_stage:
            self._publish_refinement(
                stage=PolarRefinementStage.CAPTURING,
                exposure_started_at=self._now(),
                exposure_duration_s=exposure_s,
            )
        frame, _started_at = await drive_exposure(
            camera=self._camera,
            telescope=self._telescope,
            filterwheel=self._filterwheel,
            duration_s=exposure_s,
            context=self._exposure_context(object_name),
            frame_type=FrameType.LIGHT,
            now=self._now,
            sleep=self._sleep,
            metadata_mode="solver",
        )
        with tempfile.TemporaryDirectory(prefix="ssc-polar-refine-") as tmp_dir:
            fits_path = Path(tmp_dir) / "frame.fits"
            solve_frame = bin_2x2_for_solving(frame)
            await _write_solver_frame(fits_path, solve_frame)
            if publish_refinement_stage:
                self._publish_refinement(
                    stage=PolarRefinementStage.SOLVING,
                    exposure_started_at=None,
                    exposure_duration_s=None,
                )
            if solve_timeout_s is None:
                return await self._solve_with_hints(fits_path)
            try:
                async with asyncio.timeout(solve_timeout_s):
                    return await self._solve_with_hints(fits_path)
            except TimeoutError:
                raise SolveTimedOutError(
                    SolveIssue.TIMED_OUT,
                    f"plate solver did not finish within {solve_timeout_s:g}s solving "
                    f"{fits_path.name}",
                ) from None

    async def _refinement_expose_until_solved(
        self, exposure_s: float, object_name: str
    ) -> PlateSolveResult:
        """Keep replacing motion-blurred refinement frames until one solves.

        Turning the physical altitude/azimuth adjusters can move stars during
        the short exposure. Retry a few such frames, but never leave tracking
        stopped indefinitely when the field cannot be solved. Missing solver/
        database and malformed-output failures still end the run immediately.
        """
        for attempt in range(1, self._refinement_max_solve_attempts + 1):
            try:
                return await self._expose_and_solve(
                    exposure_s,
                    object_name,
                    publish_refinement_stage=True,
                    solve_timeout_s=self._refinement_solve_timeout_s,
                )
            except (PlateSolveError, ExposureTimeoutError) as exc:
                if (
                    isinstance(exc, PlateSolveError)
                    and exc.issue not in _RECOVERABLE_REFINEMENT_SOLVE_ISSUES
                ):
                    raise
                if isinstance(exc, ExposureTimeoutError):
                    assert self._camera is not None
                    try:
                        async with asyncio.timeout(5.0):
                            while not await self._camera.image_ready():
                                await self._sleep(0.5)
                    except TimeoutError:
                        raise _RefinementSolveExhaustedError(
                            "Camera.ImageReady stayed false after the exposure timeout; "
                            "tracking was restored without starting another exposure"
                        ) from exc
                if attempt == self._refinement_max_solve_attempts:
                    raise _RefinementSolveExhaustedError(
                        f"live adjustment could not solve or capture {attempt} consecutive frames; "
                        f"tracking was restored. Last error: {exc}"
                    ) from exc
                message = f"{type(exc).__name__}: {exc}; capturing a fresh frame"
                _log.warning("polar refinement: %s", message)
                self._publish_refinement(error=message)
        raise AssertionError("refinement solve attempts must be positive")

    async def _select_uvircut_filter(self) -> None:
        """Select the Seestar's UV/IR-cut slot (reported as ``IR`` on S30 Pro)."""
        if self._filterwheel is None:
            return
        if not await self._filterwheel.is_connected():
            await self._filterwheel.connect()
        names = await self._filterwheel.get("names")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise PolarAlignmentFilterUnavailableError(
                f"FilterWheel.Names did not return string slots: {names!r}"
            )
        normalised = ["".join(char for char in name.casefold() if char.isalnum()) for name in names]
        try:
            position = next(
                normalised.index(alias) for alias in ("uvircut", "ir") if alias in normalised
            )
        except StopIteration as exc:
            raise PolarAlignmentFilterUnavailableError(
                f"UV/IR-cut filter is not one of the wheel's slots {names}"
            ) from exc
        await self._filterwheel.put("position", Position=position)
        _log.info(
            "polar alignment: selected UV/IR-cut filter slot %d (%s)", position, names[position]
        )

    def _reported_coord(self) -> EquatorialCoord | None:
        """The mount's own believed position at (approximately) solve time,
        for ``PolarPoint.reported`` — recorded only, never fitted
        (``ssc.astro.polar``'s own docstring). ``None`` when any of the three
        fields this needs is not currently known."""
        mount = self._state.snapshot.mount
        if (
            mount is None
            or mount.ra_hours is None
            or mount.dec_deg is None
            or mount.equatorial_system is None
        ):
            return None
        return EquatorialCoord(
            ra_deg=hours_to_deg(Hours(mount.ra_hours)),
            dec_deg=Deg(mount.dec_deg),
            frame=mount.equatorial_system,
        )


__all__ = [
    "AstroSource",
    "MountControl",
    "PolarAlignmentAlreadyRunningError",
    "PolarAlignmentDeviceUnavailableError",
    "PolarAlignmentEphemerisUnavailableError",
    "PolarAlignmentFilterUnavailableError",
    "PolarAlignmentNoMeasurementError",
    "PolarAlignmentNotPolarModeError",
    "PolarAlignmentNotRunningError",
    "PolarAlignmentPositionUnknownError",
    "PolarAlignmentService",
    "PolarAlignmentSiteUnknownError",
    "PolarAlignmentSolverUnavailableError",
    "PolarAlignmentTooFewPointsError",
    "PolarRefinementAlreadyRunningError",
    "PolarRefinementNoSweepError",
    "PolarRefinementNotRunningError",
    "PolarRefinementTrackingError",
    "PolarSolveAlreadyRunningError",
]
