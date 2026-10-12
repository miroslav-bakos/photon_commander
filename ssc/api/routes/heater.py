# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Manual dew-heater control backed by standard Alpaca ``Switch/0``."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from ssc.api.deps import ControlDep, ServicesDep, SessionDep, rate_limit
from ssc.api.schemas import (
    HeaterAutomationRequest,
    HeaterAutomationResponse,
    HeaterStateRequest,
    HeaterStateResponse,
)
from ssc.state.ports import HeaterNotAvailableError, HeaterRefusedError

router = APIRouter(prefix="/api/heater", tags=["heater"])


def _automation_response(services: ServicesDep) -> HeaterAutomationResponse:
    config = services.settings.heater
    evaluation = services.heater_automation.evaluation
    return HeaterAutomationResponse(
        enabled=config.enabled,
        on_margin_c=config.on_margin_c,
        off_margin_c=config.off_margin_c,
        evaluated_at=evaluation.evaluated_at,
        dew_margin_c=evaluation.dew_margin_c,
        action=evaluation.action,
        note=evaluation.note,
    )


@router.get("/automation", response_model=HeaterAutomationResponse)
async def get_automation(
    services: ServicesDep, session: SessionDep
) -> HeaterAutomationResponse:
    del session
    return _automation_response(services)


@router.put("/automation", response_model=HeaterAutomationResponse)
async def set_automation(
    body: HeaterAutomationRequest, services: ServicesDep, session: ControlDep
) -> HeaterAutomationResponse:
    del session
    await services.settings.set_heater_automation(
        enabled=body.enabled,
        on_margin_c=body.on_margin_c,
        off_margin_c=body.off_margin_c,
    )
    services.heater_automation.wake()
    return _automation_response(services)


@router.get(
    "", response_model=HeaterStateResponse, dependencies=[Depends(rate_limit("heater-read"))]
)
async def get_state(services: ServicesDep, session: SessionDep) -> HeaterStateResponse:
    del session
    enabled, note = await services.device.heater_get_state()
    return HeaterStateResponse(enabled=enabled, note=note)


@router.put(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
    dependencies=[Depends(rate_limit("heater"))],
)
async def set_state(
    body: HeaterStateRequest, services: ServicesDep, session: ControlDep
) -> JSONResponse | None:
    del session
    try:
        await services.device.heater_set_state(enabled=body.enabled)
    except HeaterRefusedError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "device_code": exc.device_code},
        )
    except HeaterNotAvailableError as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
    return None


__all__ = ["router"]
