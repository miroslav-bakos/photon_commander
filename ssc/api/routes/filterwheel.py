# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Manual ASCOM filter-wheel controls for the Capture screen (ssc-bkv.6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from ssc.api.deps import ControlDep, ServicesDep, SessionDep, rate_limit
from ssc.api.schemas import FilterWheelPositionRequest, FilterWheelStateResponse
from ssc.state.ports import FilterWheelNotAvailableError, FilterWheelRefusedError

router = APIRouter(prefix="/api/filterwheel", tags=["filterwheel"])


def _refused(exc: FilterWheelRefusedError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc), "device_code": exc.device_code},
    )


def _not_available(exc: FilterWheelNotAvailableError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))


@router.get(
    "",
    response_model=FilterWheelStateResponse,
    dependencies=[Depends(rate_limit("filterwheel-read"))],
)
async def get_state(services: ServicesDep, session: SessionDep) -> FilterWheelStateResponse:
    """Read names and position independently so one failed member does not hide the other."""
    del session
    names, names_note = await services.device.filterwheel_get_names()
    position, position_note = await services.device.filterwheel_get_position()
    return FilterWheelStateResponse(
        names=names,
        position=position,
        names_note=names_note,
        position_note=position_note,
    )


@router.put(
    "/position",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
    dependencies=[Depends(rate_limit("filterwheel"))],
)
async def set_position(
    body: FilterWheelPositionRequest, services: ServicesDep, session: ControlDep
) -> JSONResponse | None:
    """Start moving to a slot; clients poll ``GET /api/filterwheel`` for read-back."""
    del session
    try:
        await services.device.filterwheel_set_position(position=body.position)
    except FilterWheelRefusedError as exc:
        return _refused(exc)
    except FilterWheelNotAvailableError as exc:
        raise _not_available(exc) from exc
    return None


__all__ = ["router"]
