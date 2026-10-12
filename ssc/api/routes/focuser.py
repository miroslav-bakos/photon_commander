# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The focuser control panel (bead ssc-bkv.7).

Mirrors ``ssc/api/routes/mount.py``'s shape: four controls (read position,
move, halt, start autofocus), each translating
:class:`~ssc.state.ports.FocuserRefusedError`/
:class:`~ssc.state.ports.FocuserNotAvailableError` into a 409 (with the
device's own message) or a 501, never swallowed or retried here — recovering
from a refusal is a policy decision left to whoever is driving the panel
(CLAUDE.md invariant 7).

A real ``Move`` needs firmware 9.16+ (``ssc.state.ports.DeviceGateway.focuser_move``'s
own docstring); this module does not detect firmware, it only issues the
standard ASCOM member and surfaces whatever the device says back.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse

from ssc.api.deps import ControlDep, ServicesDep, SessionDep, rate_limit
from ssc.api.operation_service import OperationBusyError, OperationContext
from ssc.api.schemas import AutofocusRequest, FocuserMoveRequest, FocuserPositionResponse
from ssc.state.models import AutofocusState, OperationState
from ssc.state.ports import FocuserNotAvailableError, FocuserRefusedError

router = APIRouter(prefix="/api/focuser", tags=["focuser"])
_log = logging.getLogger(__name__)


def _refused(exc: FocuserRefusedError) -> JSONResponse:
    """409, with the device's own message and, when it has one, its Alpaca
    ``ErrorNumber`` — mirrors ``routes/mount.py``'s ``_refused``."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc), "device_code": exc.device_code},
    )


def _not_available(exc: FocuserNotAvailableError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))


@router.get("/autofocus", response_model=AutofocusState)
async def autofocus_state(services: ServicesDep, session: SessionDep) -> AutofocusState:
    """Current or most recent autofocus progress; no device I/O is performed."""
    del session
    return services.state.snapshot.autofocus


@router.get(
    "/position",
    response_model=FocuserPositionResponse,
    dependencies=[Depends(rate_limit("focuser-read"))],
)
async def position(services: ServicesDep, session: SessionDep) -> FocuserPositionResponse:
    """Live ``Focuser.Position`` and ``Focuser.StepSize``. Both ``None`` when
    not connected or the device did not answer — never guessed (CLAUDE.md
    invariant 7), so this never raises on an unknown reading the way the
    control endpoints below do. Read independently (bead ssc-bkv.10): a
    device that answers ``position`` but declares ``stepsize`` unimplemented
    (this one — ``ssc/alpaca/devices/focuser.py``) still gets an honest
    position rather than both collapsing to ``None`` together. Each carries
    its own ``note``, the same shape as ``AlpacaDeviceItem.note``."""
    del session
    value, note = await services.device.focuser_position()
    is_moving, is_moving_note = await services.device.focuser_is_moving()
    step_size, step_size_note = await services.device.focuser_step_size()
    return FocuserPositionResponse(
        position=value,
        note=note,
        is_moving=is_moving,
        is_moving_note=is_moving_note,
        step_size=step_size,
        step_size_note=step_size_note,
    )


@router.post(
    "/move",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
    dependencies=[Depends(rate_limit("focuser"))],
)
async def move(
    body: FocuserMoveRequest, services: ServicesDep, session: ControlDep
) -> JSONResponse | None:
    """``Focuser.Move``. Accepted, not arrived — poll ``/position`` (or
    ``Focuser.IsMoving``, once exposed) for completion, the same shape as the
    Mount tab's ``/slew``."""
    del session
    try:
        await services.device.focuser_move(position=body.position)
    except FocuserRefusedError as exc:
        return _refused(exc)
    except FocuserNotAvailableError as exc:
        raise _not_available(exc) from exc
    return None


@router.post("/halt", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def halt(services: ServicesDep, session: ControlDep) -> JSONResponse | None:
    """``Focuser.Halt``. Never rate-limited — mirrors the Mount tab's
    ``/stop``, which must work whatever else is in flight."""
    del session
    try:
        await services.device.focuser_halt()
    except FocuserRefusedError as exc:
        return _refused(exc)
    except FocuserNotAvailableError as exc:
        raise _not_available(exc) from exc
    return None


@router.post(
    "/start-autofocus",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=[Depends(rate_limit("focuser"))],
)
async def start_autofocus(
    body: AutofocusRequest,
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    """Measure and apply an SSC-driven autofocus curve.

    The accepted backend operation owns the curve even when the client leaves.
    """
    del session
    async def work(context: OperationContext) -> None:
        context.progress(message="Running autofocus curve")
        await services.device.focuser_start_autofocus(
            step=body.step,
            fine_step=body.fine_step,
            exposure_s=body.exposure_seconds,
            settle_s=body.settle_seconds,
        )

    try:
        return services.operations.start(
            kind="autofocus",
            work=work,
            message="Starting autofocus",
            idempotency_key=idempotency_key,
            cancellable=True,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
