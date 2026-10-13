# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Executing a plan (DESIGN.md 4.2). Thin: parse, delegate to
``ssc/api/run_service.py``, serialise.

Every control is ``ControlDep`` — a view-only session can watch a run, not
steer it (DESIGN.md 9) — and rate-limited, so a stuck client retry loop cannot
flood the executor's control queue. The run section itself travels in the
snapshot and ``run.changed``; these endpoints answer with the same section so
a caller need not wait for the WS frame to know what it just did.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status

from ssc.api.astro_service import AstroUnavailableError
from ssc.api.deps import ControlDep, ServicesDep, SessionDep, rate_limit
from ssc.api.plans_service import PlanTimezoneUnknownError
from ssc.api.run_service import (
    RunActiveError,
    RunDeviceUnavailableError,
    RunLocationUnsetError,
    RunNightExpiredError,
    RunNotActiveError,
    RunNotResumableError,
    historical_night_report,
)
from ssc.api.schemas import (
    HistoricalNightReportResponse,
    RunCaptureSummaryModel,
    RunControlRequest,
    RunDetailResponse,
    RunEffectModel,
    RunFinalMountModel,
    RunHistoryModel,
    RunHistoryResponse,
    RunItemControlRequest,
    RunJournalRecordModel,
    RunResponse,
    RunStartRequest,
)
from ssc.scheduler.executor import ControlKind
from ssc.store.capture import CaptureRepository
from ssc.store.plans import PlanNotFoundError
from ssc.store.runs import JournalEntry, RunNotFoundError, RunRepository

router = APIRouter(prefix="/api/runs", tags=["runs"])

_CONTROL_DEPS = [Depends(rate_limit("runs"))]


def _journal_model(entry: JournalEntry) -> RunJournalRecordModel:
    record = entry.record
    effect = None
    if record.effect is not None:
        effect = RunEffectModel(
            name=record.effect.name.value,
            args=dict(record.effect.args),
            completion=record.effect.completion.value,
            event_kind=record.effect.event_kind,
            deadline_s=record.effect.deadline_s,
        )
    return RunJournalRecordModel(
        seq=entry.seq,
        at=record.at,
        kind=record.kind.value,
        reason=record.reason,
        item_id=record.item_id,
        panel_index=record.panel_index,
        attempt=record.attempt,
        step=record.step,
        status=None if record.status is None else record.status.value,
        held=record.held,
        effect=effect,
        effect_outcome=None if record.effect_outcome is None else record.effect_outcome.value,
        control=None if record.control is None else record.control.value,
        run_status=None if record.run_status is None else record.run_status.value,
    )


@router.post(
    "",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_CONTROL_DEPS,
)
async def start_run(
    body: RunStartRequest, services: ServicesDep, session: ControlDep
) -> RunResponse:
    del session
    try:
        run = await services.runs.start(body.plan_id, night_date=body.night_date)
    except (RunActiveError, RunNightExpiredError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (RunLocationUnsetError, PlanTimezoneUnknownError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (AstroUnavailableError, RunDeviceUnavailableError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return RunResponse(run=run)


@router.get("/current", response_model=RunResponse)
async def current_run(services: ServicesDep, session: SessionDep) -> RunResponse:
    del session
    return RunResponse(run=services.runs.current)


@router.get("/latest", response_model=RunResponse)
async def latest_run(services: ServicesDep, session: SessionDep) -> RunResponse:
    """Recover the most recent persisted report after a server restart."""
    del session
    record = await RunRepository(services.database).latest()
    return RunResponse(run=None if record is None else (await services.runs.detail(record.run_id)).run)


@router.get("/history", response_model=RunHistoryResponse)
async def run_history(start_date: date, end_date: date, services: ServicesDep,
                      session: SessionDep) -> RunHistoryResponse:
    del session
    if not 0 <= (end_date - start_date).days <= 62:
        raise HTTPException(status_code=422, detail="Choose a calendar range of at most 63 days.")
    records = await RunRepository(services.database).history(start_date, end_date)
    return RunHistoryResponse(runs=[RunHistoryModel(**asdict(record)) for record in records])


@router.get("/nights/{night_date}", response_model=HistoricalNightReportResponse)
async def get_night_report(
    night_date: date,
    services: ServicesDep,
    session: SessionDep,
) -> HistoricalNightReportResponse:
    """Executed revisions and measured retained acquisitions across a night."""
    del session
    return await historical_night_report(
        RunRepository(services.database),
        CaptureRepository(services.database),
        night_date,
        now=datetime.now(UTC),
    )


@router.get("/{run_id}", response_model=RunDetailResponse)
async def get_run(run_id: str, services: ServicesDep, session: SessionDep) -> RunDetailResponse:
    """The section plus the whole journal — the morning post-mortem."""
    del session
    try:
        detail = await services.runs.detail(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    final_mount = await RunRepository(services.database).final_mount(run_id)
    return RunDetailResponse(
        final_mount=None if final_mount is None else RunFinalMountModel.model_validate(final_mount),
        run=detail.run, journal=[_journal_model(entry) for entry in detail.journal],
        capture_summary=RunCaptureSummaryModel(**asdict(await CaptureRepository(services.database).run_summary(run_id)))
    )


async def _control(
    services: ServicesDep,
    run_id: str,
    kind: ControlKind,
    *,
    item_id: str | None = None,
    reason: str = "",
) -> RunResponse:
    try:
        run = await services.runs.control(run_id, kind, item_id=item_id, reason=reason)
    except (RunNotActiveError, RunNotResumableError, RunNightExpiredError,
            RunLocationUnsetError, PlanTimezoneUnknownError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (AstroUnavailableError, RunDeviceUnavailableError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return RunResponse(run=run)


@router.post("/{run_id}/pause", response_model=RunResponse, dependencies=_CONTROL_DEPS)
async def pause_run(
    run_id: str, services: ServicesDep, session: ControlDep, body: RunControlRequest | None = None
) -> RunResponse:
    del session
    return await _control(services, run_id, ControlKind.PAUSE, reason=body.reason if body else "")


@router.post("/{run_id}/resume", response_model=RunResponse, dependencies=_CONTROL_DEPS)
async def resume_run(
    run_id: str, services: ServicesDep, session: ControlDep, body: RunControlRequest | None = None
) -> RunResponse:
    del session
    return await _control(services, run_id, ControlKind.RESUME, reason=body.reason if body else "")


@router.post("/{run_id}/stop", response_model=RunResponse, dependencies=_CONTROL_DEPS)
async def stop_run(
    run_id: str, services: ServicesDep, session: ControlDep, body: RunControlRequest | None = None
) -> RunResponse:
    del session
    return await _control(services, run_id, ControlKind.STOP, reason=body.reason if body else "")


@router.post("/{run_id}/skip", response_model=RunResponse, dependencies=_CONTROL_DEPS)
async def skip_item(
    run_id: str, body: RunItemControlRequest, services: ServicesDep, session: ControlDep
) -> RunResponse:
    del session
    return await _control(
        services, run_id, ControlKind.SKIP, item_id=body.item_id, reason=body.reason
    )


@router.post("/{run_id}/retry", response_model=RunResponse, dependencies=_CONTROL_DEPS)
async def retry_item(
    run_id: str, body: RunItemControlRequest, services: ServicesDep, session: ControlDep
) -> RunResponse:
    del session
    return await _control(
        services, run_id, ControlKind.RETRY, item_id=body.item_id, reason=body.reason
    )


@router.post("/{run_id}/resume-interrupted", response_model=RunResponse, dependencies=_CONTROL_DEPS)
async def resume_interrupted_run(
    run_id: str, services: ServicesDep, session: ControlDep
) -> RunResponse:
    """Carry an interrupted run on from its journal — the user's decision,
    never the process's (DESIGN.md 4.2)."""
    del session
    try:
        run = await services.runs.resume_interrupted(run_id)
    except (RunNotFoundError, PlanNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (RunActiveError, RunNotResumableError, RunLocationUnsetError,
            RunNightExpiredError, PlanTimezoneUnknownError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (RunDeviceUnavailableError, AstroUnavailableError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return RunResponse(run=run)


__all__ = ["router"]
