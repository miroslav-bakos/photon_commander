# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Running a polar-alignment measurement (M10). Thin: parse, delegate to
``ssc/api/polar_alignment_service.py``, serialise.

``POST .../start`` answers **202** — a run takes minutes, never the request
that started it (module docstring's own contract) — or one of the errors
:class:`~ssc.api.polar_alignment_service.PolarAlignmentService.start` raises,
each mapped to the status the failure means: **409** for "already running",
"not in polar mode", "site/ephemeris/position unknown" or "no solver
available" — every one of these is a refusal, not a device fault — **422**
for a ``start_point``/coordinate combination that is ambiguous or a
best-conditioned field ``ssc.astro.polar.best_conditioned_field`` cannot
place above the requested minimum altitude (``NO_CLEAR_ARC``, naming both
bounds) — these are bad request parameters, not a state conflict — and
**503** when this process hosts no camera/telescope to drive. Progress and
the final result travel in the snapshot's ``polar_alignment`` section and
the ``polar_alignment.changed`` WS event; this response mirrors the section
so a caller need not wait for the WS frame to know a run actually started.

``POST .../solve-here`` is the odd one out: it moves nothing. One exposure
where the scope already points, solved, returned next to what the mount
reported for the same instant — the difference between the two is the
measurement. It lives under this prefix because the camera, telescope and
solver are wired together on the polar-alignment service and nowhere else
above the Alpaca seam, not because it is part of a run.

``POST .../refine/start`` and ``.../refine/stop`` are the live-refinement
loop (the rest of DESIGN.md M10): same 202/409/503 mapping, one level down
in the same section — a caller watches ``polar_alignment.refinement`` for
each iteration, over the very same WS event, since refinement is a change to
the same live-mutating section rather than a second one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status

from ssc.api.camera_fov import publish_solved_camera_fov
from ssc.api.deps import ControlDep, ServicesDep, rate_limit
from ssc.api.operation_service import OperationBusyError, OperationContext
from ssc.api.polar_alignment_service import (
    PolarAlignmentAlreadyRunningError,
    PolarAlignmentDeviceUnavailableError,
    PolarAlignmentEphemerisUnavailableError,
    PolarAlignmentFilterUnavailableError,
    PolarAlignmentNoClearArcError,
    PolarAlignmentNoMeasurementError,
    PolarAlignmentNotPolarModeError,
    PolarAlignmentNotRunningError,
    PolarAlignmentPositionUnknownError,
    PolarAlignmentSiteUnknownError,
    PolarAlignmentSolverUnavailableError,
    PolarAlignmentStartAmbiguousError,
    PolarRefinementAlreadyRunningError,
    PolarRefinementNoSweepError,
    PolarRefinementNotRunningError,
    PolarRefinementTrackingError,
    PolarSolveAlreadyRunningError,
    _result_from_model,
)
from ssc.api.schemas import (
    PlateSolveHereRequest,
    PlateSolveHereResponse,
    PolarAlignmentRecheckRequest,
    PolarAlignmentStartRequest,
    PolarRefinementStartRequest,
    PolarSimulationPoint,
    PolarSimulationRequest,
    PolarSimulationResponse,
)
from ssc.astro.events import Site
from ssc.astro.polar_simulator import simulate_polar_alignment
from ssc.astro.units import Deg
from ssc.state.models import OperationState, PolarAlignmentState

router = APIRouter(prefix="/api/mount/polar-alignment", tags=["polar-alignment"])


@router.post(
    "/simulate",
    response_model=PolarSimulationResponse,
    dependencies=[Depends(rate_limit("mount"))],
)
async def simulate(
    body: PolarSimulationRequest, services: ServicesDep, session: ControlDep
) -> PolarSimulationResponse:
    """Run artificial RA captures through the real fitter; never touch devices."""
    del session
    ephemeris = services.astro.ephemeris
    if ephemeris is None:
        raise HTTPException(status_code=503, detail="ephemeris kernel unavailable")
    site = Site(latitude_deg=Deg(48.15), longitude_deg=Deg(17.11))
    points, model = simulate_polar_alignment(
        ephemeris,
        site,
        datetime.now(tz=UTC),
        altitude_adjustment_deg=body.altitude_adjustment_deg,
        azimuth_adjustment_deg=body.azimuth_adjustment_deg,
        point_count=body.point_count,
    )
    return PolarSimulationResponse(
        site_latitude_deg=float(site.latitude_deg),
        site_longitude_deg=float(site.longitude_deg),
        points=[
            PolarSimulationPoint(
                index=index,
                when=point.when,
                ra_j2000_hours=float(point.coord.ra_h),
                dec_j2000_deg=float(point.coord.dec_deg),
            )
            for index, point in enumerate(points)
        ],
        result=_result_from_model(model),
    )

_REFUSALS = (
    PolarAlignmentNotPolarModeError,
    PolarAlignmentSiteUnknownError,
    PolarAlignmentEphemerisUnavailableError,
    PolarAlignmentFilterUnavailableError,
    PolarAlignmentPositionUnknownError,
    PolarAlignmentSolverUnavailableError,
    PolarRefinementTrackingError,
)

_REFINEMENT_REFUSALS = (
    PolarRefinementNoSweepError,
    PolarAlignmentEphemerisUnavailableError,
    PolarRefinementTrackingError,
    PolarSolveAlreadyRunningError,
    PolarAlignmentSolverUnavailableError,
)


def _partial_detail(exc: Exception, *, unparked: bool, action: str) -> str:
    if not unparked:
        return str(exc)
    return f"Mount was unparked, but {action} failed; the mount remains unparked: {exc}"


@router.post(
    "/start",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PolarAlignmentState,
    dependencies=[Depends(rate_limit("mount"))],
)
async def start(
    body: PolarAlignmentStartRequest, services: ServicesDep, session: ControlDep
) -> PolarAlignmentState:
    del session
    unparked = False
    try:
        if body.unpark_if_needed:
            unparked = await services.device.mount_unpark_if_needed()
        return await services.polar_alignment.start(
            point_count=body.point_count,
            step_deg=body.step_deg,
            exposure_s=body.exposure_s,
            gain=body.gain,
            apply_refraction=body.apply_refraction,
            start_point=body.start_point,
            start_ra_j2000_h=body.start_ra_hours,
            start_dec_j2000_deg=body.start_dec_deg,
            auto_refine=body.auto_refine,
        )
    except (PolarAlignmentAlreadyRunningError, PolarSolveAlreadyRunningError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_partial_detail(exc, unparked=unparked, action="polar alignment start"),
        ) from exc
    except PolarAlignmentDeviceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_partial_detail(exc, unparked=unparked, action="polar alignment start"),
        ) from exc
    except (PolarAlignmentStartAmbiguousError, PolarAlignmentNoClearArcError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_partial_detail(exc, unparked=unparked, action="polar alignment start"),
        ) from exc
    except _REFUSALS as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_partial_detail(exc, unparked=unparked, action="polar alignment start"),
        ) from exc


@router.post(
    "/recheck",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PolarAlignmentState,
    dependencies=[Depends(rate_limit("mount"))],
)
async def recheck(
    body: PolarAlignmentRecheckRequest, services: ServicesDep, session: ControlDep
) -> PolarAlignmentState:
    """Start a fresh same-direction sweep and compare only after fitting."""
    del session
    unparked = False
    try:
        if body.unpark_if_needed:
            unparked = await services.device.mount_unpark_if_needed()
        return await services.polar_alignment.start_recheck(
            exposure_s=body.exposure_s,
            gain=body.gain,
            apply_refraction=body.apply_refraction,
            point_count=body.point_count,
        )
    except (
        PolarAlignmentAlreadyRunningError,
        PolarRefinementAlreadyRunningError,
        PolarSolveAlreadyRunningError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_partial_detail(exc, unparked=unparked, action="polar alignment recheck"),
        ) from exc
    except PolarAlignmentNoMeasurementError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_partial_detail(exc, unparked=unparked, action="polar alignment recheck"),
        ) from exc
    except PolarAlignmentDeviceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_partial_detail(exc, unparked=unparked, action="polar alignment recheck"),
        ) from exc
    except PolarAlignmentNoClearArcError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_partial_detail(exc, unparked=unparked, action="polar alignment recheck"),
        ) from exc
    except _REFUSALS as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_partial_detail(exc, unparked=unparked, action="polar alignment recheck"),
        ) from exc


@router.post(
    "/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def cancel(services: ServicesDep, session: ControlDep) -> None:
    """Must work whatever else is in flight, like ``POST /api/mount/stop`` —
    deliberately not rate-limited (that route's own reasoning)."""
    del session
    try:
        await services.polar_alignment.cancel()
    except PolarAlignmentNotRunningError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/solve-here",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=[Depends(rate_limit("mount"))],
)
async def solve_here(
    body: PlateSolveHereRequest,
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    """Solve where the scope is pointing right now. Nothing slews."""
    del session
    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message="Capturing and plate solving current field")
        result = await services.polar_alignment.solve_here(
            exposure_s=body.exposure_s, gain=body.gain
        )
        mount = services.state.snapshot.mount
        publish_solved_camera_fov(services.state, result)
        response = PlateSolveHereResponse(
            ra_j2000_hours=float(result.coord.ra_h),
            dec_j2000_deg=float(result.coord.dec_deg),
            date_obs=result.date_obs,
            pixel_scale_arcsec=result.pixel_scale_arcsec,
            rotation_deg=result.rotation_deg,
            solver=result.solver,
            solver_version=result.solver_version,
            mount_ra_j2000_hours=None if mount is None else mount.ra_j2000_h,
            mount_dec_j2000_deg=None if mount is None else mount.dec_j2000_deg,
            mount_altitude_deg=None if mount is None else mount.alt_deg,
            mount_azimuth_deg=None if mount is None else mount.az_deg,
        )
        return response.model_dump(mode="json")

    try:
        return services.operations.start(
            kind="solve_here",
            work=work,
            message="Starting plate solve",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/refine/start",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PolarAlignmentState,
    dependencies=[Depends(rate_limit("mount"))],
)
async def refine_start(
    body: PolarRefinementStartRequest, services: ServicesDep, session: ControlDep
) -> PolarAlignmentState:
    del session
    try:
        return await services.polar_alignment.start_refinement(
            exposure_s=body.exposure_s, gain=body.gain
        )
    except (PolarAlignmentAlreadyRunningError, PolarRefinementAlreadyRunningError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PolarAlignmentDeviceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except _REFINEMENT_REFUSALS as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/refine/stop",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def refine_stop(services: ServicesDep, session: ControlDep) -> None:
    """Same "must work" reasoning as ``cancel`` above — not rate-limited."""
    del session
    try:
        await services.polar_alignment.stop_refinement()
    except PolarRefinementNotRunningError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


__all__ = ["router"]
