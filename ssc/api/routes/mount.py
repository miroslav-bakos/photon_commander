# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The Mount tab's controls (DESIGN.md §4.4b).

Telemetry itself is not here — it lives in ``snapshot.mount``
(``GET /api/state``, and the ``mount.changed`` WS event), published by the
poller ``ssc.main.AlpacaDeviceGateway`` owns. This module is only the eight
controls the tab drives: slew, sync, jog, stop, tracking, park, unpark, find
home.

Every device refusal (``MountRefusedError``) and unavailable member
(``MountNotAvailableError``) is surfaced in the device's own words rather than
swallowed or retried here — recovering from one (e.g. ``FindHome`` after a
below-horizon ``Park`` refusal) is a policy decision left to whoever is
driving the tab, or eventually the scheduler (CLAUDE.md invariant 7).
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse

from ssc.api.camera_fov import invalidate_solved_camera_fov
from ssc.api.deps import ControlDep, ServicesDep, rate_limit
from ssc.api.operation_service import OperationBusyError, OperationContext
from ssc.api.schemas import (
    MOUNT_AXIS_RATE_HARD_CAP_DEG_S,
    MountCoordinatesRequest,
    MountMoveAxisRequest,
    MountTrackingRequest,
    MountZenithRequest,
    TargetCenteringAttemptModel,
    TargetCenteringRequest,
    TargetCenteringResponse,
)
from ssc.state.models import MountState, OperationState
from ssc.state.ports import MountNotAvailableError, MountRefusedError

router = APIRouter(prefix="/api/mount", tags=["mount"])


def _partial_detail(exc: Exception, *, unparked: bool, action: str) -> str:
    if not unparked:
        return str(exc)
    return f"Mount was unparked, but {action} failed; the mount remains unparked: {exc}"


def _refused(
    exc: MountRefusedError, *, unparked: bool = False, action: str = "the command"
) -> JSONResponse:
    """409, with the device's own message and, when it has one, its Alpaca
    ``ErrorNumber`` — e.g. the below-horizon ``Park`` refusal, ``[1279]``."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": _partial_detail(exc, unparked=unparked, action=action),
            "device_code": exc.device_code,
        },
    )


def _not_available(
    exc: MountNotAvailableError, *, unparked: bool = False, action: str = "the command"
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=_partial_detail(exc, unparked=unparked, action=action),
    )


def _check_axis_rate_cap(services: ServicesDep, rate_deg_s: float) -> None:
    """The request schema already bounds ``rate_deg_s`` to the mount's
    structural ceiling; this additionally checks the *live* advertised cap,
    which could in principle read lower (DESIGN.md §4.4b: genuinely enforced
    by the device, not merely documented)."""
    mount = services.state.snapshot.mount
    cap = MOUNT_AXIS_RATE_HARD_CAP_DEG_S
    if mount is not None and mount.capabilities is not None:
        live_cap = mount.capabilities.axis_rate_max_deg_s
        if live_cap is not None:
            cap = live_cap
    if abs(rate_deg_s) > cap:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"rate_deg_s {rate_deg_s!r} exceeds the mount's advertised cap of {cap} deg/s",
        )


@router.post(
    "/slew",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
    dependencies=[Depends(rate_limit("mount"))],
)
async def slew(
    body: MountCoordinatesRequest, services: ServicesDep, session: ControlDep
) -> JSONResponse | None:
    """Accept a J2000 target for ``SlewToCoordinatesAsync``. Accepted, not
    arrived — :class:`DeviceService` converts it into the mount's live frame,
    and the Mount tab watches ``snapshot.mount.slewing`` for completion."""
    del session
    unparked = False
    try:
        if body.unpark_if_needed:
            unparked = await services.device.mount_unpark_if_needed()
        await services.device.mount_slew(ra_hours=body.ra_hours, dec_deg=body.dec_deg)
    except MountRefusedError as exc:
        return _refused(exc, unparked=unparked, action="slewing")
    except MountNotAvailableError as exc:
        raise _not_available(exc, unparked=unparked, action="slewing") from exc
    invalidate_solved_camera_fov(services.state)
    return None


@router.post(
    "/center",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=[Depends(rate_limit("mount"))],
)
async def center_target(
    body: TargetCenteringRequest,
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    """Slew to a J2000 target and iteratively verify/correct by plate solve."""
    del session
    if services.capture.section.in_progress:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a capture is already running; target centering needs exclusive camera access",
        )
    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message="Centering target", current=0, total=body.max_attempts)
        unparked = False
        if body.unpark_if_needed:
            unparked = await services.device.mount_unpark_if_needed()
        try:
            result = await services.target_centering.center(
                ra_j2000_h=body.ra_hours,
                dec_j2000_deg=body.dec_deg,
                tolerance_arcmin=body.tolerance_arcmin,
                exposure_s=body.exposure_s,
                gain=body.gain,
                max_attempts=body.max_attempts,
            )
        except Exception as exc:
            if unparked:
                raise RuntimeError(
                    _partial_detail(exc, unparked=True, action="target centering")
                ) from exc
            raise
        response = TargetCenteringResponse(
            converged=result.converged,
            target_ra_j2000_h=result.target_ra_j2000_h,
            target_dec_j2000_deg=result.target_dec_j2000_deg,
            tolerance_arcmin=result.tolerance_arcmin,
            attempts=[
                TargetCenteringAttemptModel(
                    attempt=attempt.attempt,
                    commanded_ra_j2000_h=attempt.commanded_ra_j2000_h,
                    commanded_dec_j2000_deg=attempt.commanded_dec_j2000_deg,
                    solved_ra_j2000_h=attempt.solved_ra_j2000_h,
                    solved_dec_j2000_deg=attempt.solved_dec_j2000_deg,
                    error_arcmin=attempt.error_arcmin,
                )
                for attempt in result.attempts
            ],
        )
        return response.model_dump(mode="json")

    try:
        return services.operations.start(
            kind="target_centering",
            work=work,
            message="Starting target centering",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/center/status")
async def center_target_status(services: ServicesDep) -> dict[str, object]:
    """Return the centering loop's latest real stage for progress UIs."""
    return asdict(services.target_centering.progress)


@router.post(
    "/slew-zenith",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
    dependencies=[Depends(rate_limit("mount"))],
)
async def slew_zenith(
    services: ServicesDep, session: ControlDep, body: MountZenithRequest | None = None
) -> JSONResponse | None:
    """Slew straight up and verify tracking is off for flat-panel positioning."""
    del session
    unparked = False
    try:
        if body is not None and body.unpark_if_needed:
            unparked = await services.device.mount_unpark_if_needed()
        await services.device.mount_slew_zenith()
    except MountRefusedError as exc:
        return _refused(exc, unparked=unparked, action="flat-panel positioning")
    except MountNotAvailableError as exc:
        raise _not_available(
            exc, unparked=unparked, action="flat-panel positioning"
        ) from exc
    invalidate_solved_camera_fov(services.state)
    return None


@router.post(
    "/sync",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(rate_limit("mount"))],
)
async def sync(
    body: MountCoordinatesRequest, services: ServicesDep, session: ControlDep
) -> JSONResponse | None:
    """Accept a J2000 target for ``SyncToCoordinates``; the device service
    converts it into the mount's live frame before changing its model."""
    del session
    unparked = False
    try:
        if body.unpark_if_needed:
            unparked = await services.device.mount_unpark_if_needed()
        await services.device.mount_sync(ra_hours=body.ra_hours, dec_deg=body.dec_deg)
    except MountRefusedError as exc:
        return _refused(exc, unparked=unparked, action="syncing")
    except MountNotAvailableError as exc:
        raise _not_available(exc, unparked=unparked, action="syncing") from exc
    return None


@router.post(
    "/move-axis",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(rate_limit("mount-jog"))],
)
async def move_axis(
    body: MountMoveAxisRequest, services: ServicesDep, session: ControlDep
) -> JSONResponse | None:
    """``MoveAxis``. ``rate_deg_s: 0`` stops that one axis — the jog-button
    release. Rate-limited on its own, more generous bucket (``"mount-jog"``):
    this is the endpoint a held-down jog button drives repeatedly, and it must
    stay responsive without letting a stuck client retry loop spam the mount
    (DESIGN.md §9)."""
    del session
    _check_axis_rate_cap(services, body.rate_deg_s)
    try:
        await services.device.mount_move_axis(axis=body.axis, rate_deg_s=body.rate_deg_s)
    except MountRefusedError as exc:
        return _refused(exc)
    except MountNotAvailableError as exc:
        raise _not_available(exc) from exc
    if body.rate_deg_s != 0:
        invalidate_solved_camera_fov(services.state)
    return None


@router.post("/stop", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def stop(services: ServicesDep, session: ControlDep) -> JSONResponse | None:
    """The panic control: ``AbortSlew`` plus ``MoveAxis(rate=0)`` on both
    axes. Must work whatever else is in flight — never rate-limited."""
    del session
    try:
        await services.device.mount_stop()
    except MountRefusedError as exc:
        return _refused(exc)
    except MountNotAvailableError as exc:
        raise _not_available(exc) from exc
    invalidate_solved_camera_fov(services.state)
    return None


@router.put("/tracking", response_model=MountState, dependencies=[Depends(rate_limit("mount"))])
async def set_tracking(
    body: MountTrackingRequest, services: ServicesDep, session: ControlDep
) -> MountState | JSONResponse:
    """``Tracking`` and, if given, ``TrackingRate``. Returns the read-back
    mount state rather than the caller's own guess of the result."""
    del session
    try:
        return await services.device.mount_set_tracking(enabled=body.enabled, rate=body.rate)
    except MountRefusedError as exc:
        return _refused(exc)
    except MountNotAvailableError as exc:
        raise _not_available(exc) from exc


@router.post(
    "/park",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
    dependencies=[Depends(rate_limit("mount"))],
)
async def park(services: ServicesDep, session: ControlDep) -> JSONResponse | None:
    """``Park``. Refuses below the horizon (``[1279]``, DESIGN.md §4.4b) —
    surfaced as a 409 with the device's own message, never swallowed and
    never auto-recovered (that is the scheduler's shutdown policy, not this
    endpoint's)."""
    del session
    try:
        await services.device.mount_park()
    except MountRefusedError as exc:
        return _refused(exc)
    except MountNotAvailableError as exc:
        raise _not_available(exc) from exc
    invalidate_solved_camera_fov(services.state)
    return None


@router.post(
    "/unpark",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(rate_limit("mount"))],
)
async def unpark(services: ServicesDep, session: ControlDep) -> JSONResponse | None:
    """``Unpark``. Permits motion; moves nothing by itself (DESIGN.md §4.4b) —
    a still ``Altitude`` afterwards is correct, not a failure."""
    del session
    try:
        await services.device.mount_unpark()
    except MountRefusedError as exc:
        return _refused(exc)
    except MountNotAvailableError as exc:
        raise _not_available(exc) from exc
    return None


@router.post(
    "/find-home",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
    dependencies=[Depends(rate_limit("mount"))],
)
async def find_home(services: ServicesDep, session: ControlDep) -> JSONResponse | None:
    """``FindHome``. Also what recovers a below-horizon ``Park`` refusal —
    left to the caller to issue, never done on its behalf."""
    del session
    try:
        await services.device.mount_find_home()
    except MountRefusedError as exc:
        return _refused(exc)
    except MountNotAvailableError as exc:
        raise _not_available(exc) from exc
    invalidate_solved_camera_fov(services.state)
    return None
