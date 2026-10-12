# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Plan CRUD, structural validation, and compiling a plan for one night
(DESIGN.md 4.2). Thin: parse, delegate to ``ssc.store.plans``/``ssc.plan``/
``ssc.scheduler.compiler`` (via ``ssc/api/plans_service.py``), serialise.

Execution — actually stepping a ``CompiledSchedule`` against the device — is a
later step (``ssc/scheduler/executor.py``); nothing here runs anything.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status

from ssc.api.astro_service import AstroUnavailableError
from ssc.api.deps import ControlDep, ServicesDep, SessionDep, require_location
from ssc.api.plans_service import PlanTimezoneUnknownError, compile_plan_document
from ssc.api.schemas import (
    FromIntentRequest,
    FromIntentResponse,
    PlanApplyNightRequest,
    PlanCompileRequest,
    PlanCompileResponse,
    PlanCreateRequest,
    PlanListResponse,
    PlanNightRequest,
    PlanRecordResponse,
    PlanUpdateRequest,
    PlanValidateRequest,
    PlanValidateResponse,
    ScheduleIntentRequest,
)
from ssc.astro.events import SolarEvent
from ssc.plan import Plan, validate_plan
from ssc.plan.intent import intent_to_items
from ssc.plan.models import AsapTimeSpec, EventEndSpec, EventTimeSpec
from ssc.plan.reuse import apply_night
from ssc.state.events import PlansChanged
from ssc.store.plans import (
    PlanAlreadyExistsError,
    PlanNotFoundError,
    PlanRecord,
    PlanRevisionConflictError,
)

router = APIRouter(prefix="/api/plans", tags=["plans"])


def _record_response(record: PlanRecord) -> PlanRecordResponse:
    return PlanRecordResponse(
        document=record.document,
        revision=record.revision,
        created_at=record.created_at,
        updated_at=record.updated_at,
        night_date=record.night_date,
    )


async def _publish(services: ServicesDep, record: PlanRecord | None = None) -> None:
    # Saved editor revisions never mutate the persisted executing snapshot.
    del record
    services.state.apply(PlansChanged(plans=await services.plans.list_summaries()))


def _append_intent(
    document: Plan, body: FromIntentRequest, *, auto_lp_filter: bool = False,
    planning_defaults: bool = False,
) -> tuple[Plan, str]:
    item_ids = [uuid.uuid4().hex for _ in range(max(1, len(body.intent.panels)))]
    start = body.start
    if planning_defaults and start is None:
        start = EventTimeSpec(event=SolarEvent.ASTRONOMICAL_DUSK) if not document.items else AsapTimeSpec()
    items = intent_to_items(
        body.intent,
        item_ids=item_ids,
        start=start,
        duration_min=body.duration_min,
        auto_lp_filter=auto_lp_filter,
    )
    if planning_defaults and (body.duration_min is None or (body.intent.panels and not document.items)):
        # For equal-duration split panels on a fresh night, the final end is
        # exactly dawn even if fractional-minute arithmetic rounds a little.
        # An existing plan keeps the explicit durations, since its first new
        # panel starts after the previous item rather than at dusk.
        items = (
            *items[:-1],
            items[-1].model_copy(update={"end": EventEndSpec(event=SolarEvent.ASTRONOMICAL_DAWN)}),
        )
    updated = document.model_copy(
        update={"items": (*document.items, *items), "updated_at": datetime.now(tz=UTC)}
    )
    return updated, item_ids[0]


@router.post("/actions/schedule-intent", response_model=FromIntentResponse)
async def schedule_intent(
    body: ScheduleIntentRequest, services: ServicesDep, session: ControlDep
) -> FromIntentResponse:
    """Create-if-needed and append as one persisted plan mutation."""
    del session
    if body.plan_id is None:
        assert body.new_plan is not None
        updated, item_id = _append_intent(
            body.new_plan,
            body,
            auto_lp_filter=services.settings.capture.auto_lp_filter_for_emission_targets,
            planning_defaults=True,
        )
        try:
            record = await services.plans.create(updated, night_date=body.night_date)
        except PlanAlreadyExistsError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    else:
        try:
            current = await services.plans.get(body.plan_id)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        updated, item_id = _append_intent(
            current.document,
            body,
            auto_lp_filter=services.settings.capture.auto_lp_filter_for_emission_targets,
            planning_defaults=True,
        )
        try:
            record = await services.plans.update(
                body.plan_id, updated, expected_revision=current.revision
            )
        except PlanRevisionConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if body.night_date is not None and record.night_date != body.night_date:
            record = await services.plans.schedule(record.document.id, night_date=body.night_date)
    await _publish(services, record)
    return FromIntentResponse(document=record.document, revision=record.revision, item_id=item_id)


@router.get("", response_model=PlanListResponse)
async def list_plans(services: ServicesDep, session: SessionDep) -> PlanListResponse:
    del session
    return PlanListResponse(plans=await services.plans.list_summaries())


@router.post("", response_model=PlanRecordResponse, status_code=status.HTTP_201_CREATED)
async def create_plan(
    body: PlanCreateRequest, services: ServicesDep, session: ControlDep
) -> PlanRecordResponse:
    del session
    try:
        record = await services.plans.create(body.document, night_date=body.night_date)
    except PlanAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await _publish(services, record)
    return _record_response(record)


@router.put("/{plan_id}/night", response_model=PlanRecordResponse)
async def schedule_plan_night(
    plan_id: str, body: PlanNightRequest, services: ServicesDep, session: ControlDep
) -> PlanRecordResponse:
    del session
    try:
        record = await services.plans.schedule(plan_id, night_date=body.night_date)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await _publish(services, record)
    return _record_response(record)


@router.post("/{plan_id}/apply-night", response_model=PlanRecordResponse)
async def apply_plan_night(
    plan_id: str, body: PlanApplyNightRequest, services: ServicesDep, session: ControlDep
) -> PlanRecordResponse:
    del session
    try:
        current = await services.plans.get(plan_id)
        document = apply_night(current.document, body.night_date).model_copy(
            update={"updated_at": datetime.now(tz=UTC)}
        )
        record = await services.plans.replace_for_night(
            plan_id, document, expected_revision=body.expected_revision, night_date=body.night_date
        )
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PlanRevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _publish(services, record)
    return _record_response(record)


@router.get("/{plan_id}", response_model=PlanRecordResponse)
async def get_plan(plan_id: str, services: ServicesDep, session: SessionDep) -> PlanRecordResponse:
    del session
    try:
        record = await services.plans.get(plan_id)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _record_response(record)


@router.put("/{plan_id}", response_model=PlanRecordResponse)
async def update_plan(
    plan_id: str, body: PlanUpdateRequest, services: ServicesDep, session: ControlDep
) -> PlanRecordResponse:
    del session
    if body.document.id != plan_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"the document's id {body.document.id!r} does not match the URL's "
                f"plan id {plan_id!r}"
            ),
        )
    try:
        record = await services.plans.update(
            plan_id, body.document, expected_revision=body.expected_revision
        )
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PlanRevisionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await _publish(services, record)
    return _record_response(record)


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(plan_id: str, services: ServicesDep, session: ControlDep) -> None:
    del session
    try:
        await services.plans.delete(plan_id)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await _publish(services)


@router.post("/validate", response_model=PlanValidateResponse)
async def validate(
    body: PlanValidateRequest, services: ServicesDep, session: SessionDep
) -> PlanValidateResponse:
    """Structural lint only — no location, no ephemeris — so the editor can
    call this on every keystroke (``ssc.plan.validate_plan``'s own docstring)."""
    del services, session
    return PlanValidateResponse(diagnostics=validate_plan(body.document))


@router.post("/{plan_id}/compile", response_model=PlanCompileResponse)
async def compile_plan_endpoint(
    plan_id: str, body: PlanCompileRequest, services: ServicesDep, session: SessionDep
) -> PlanCompileResponse:
    del session
    try:
        record = await services.plans.get(plan_id)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    location = require_location(services)
    try:
        schedule = await asyncio.to_thread(
            compile_plan_document,
            astro=services.astro,
            document=record.document,
            location=location,
            night_date=body.night_date,
            profile_id=services.settings.device.hardware_profile_id,
        )
    except PlanTimezoneUnknownError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AstroUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return PlanCompileResponse(schedule=schedule, diagnostics=validate_plan(record.document))


@router.post("/{plan_id}/items/from-intent", response_model=FromIntentResponse)
async def add_item_from_intent(
    plan_id: str, body: FromIntentRequest, services: ServicesDep, session: ControlDep
) -> FromIntentResponse:
    """DESIGN.md 3.4's "Send to Scheduling": append one item built from a
    ``TargetIntent`` to the stored plan, as a normal revision-bumping edit."""
    del session
    try:
        record = await services.plans.get(plan_id)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    updated_document, item_id = _append_intent(
        record.document,
        body,
        auto_lp_filter=services.settings.capture.auto_lp_filter_for_emission_targets,
    )
    try:
        new_record = await services.plans.update(
            plan_id, updated_document, expected_revision=record.revision
        )
    except PlanRevisionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await _publish(services, new_record)
    return FromIntentResponse(
        document=new_record.document, revision=new_record.revision, item_id=item_id
    )


__all__ = ["router"]
