# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Read and cancel backend-owned commands after any frontend reconnect."""

from fastapi import APIRouter, HTTPException, status

from ssc.api.deps import ControlDep, ServicesDep, SessionDep
from ssc.api.operation_service import OperationNotCancellableError, OperationNotFoundError
from ssc.state.models import OperationState

router = APIRouter(prefix="/api/operations", tags=["operations"])


@router.get("", response_model=list[OperationState])
async def list_operations(services: ServicesDep, session: SessionDep) -> list[OperationState]:
    del session
    return services.operations.items


@router.get("/{operation_id}", response_model=OperationState)
async def get_operation(
    operation_id: str, services: ServicesDep, session: SessionDep
) -> OperationState:
    del session
    try:
        return services.operations.get(operation_id)
    except OperationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{operation_id}/cancel", response_model=OperationState)
async def cancel_operation(
    operation_id: str, services: ServicesDep, session: ControlDep
) -> OperationState:
    del session
    try:
        return await services.operations.cancel(operation_id)
    except OperationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except OperationNotCancellableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


__all__ = ["router"]
