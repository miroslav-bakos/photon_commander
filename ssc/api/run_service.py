# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""One run at a time: start, control, resume, and publish (DESIGN.md 4.2).

The executor (``ssc/scheduler/executor.py``) is a pure driver over ports. This
service owns the *process* around it:

* **Starting** compiles the plan (``ssc/api/plans_service.py``), writes the
  ``runs`` row, and launches ``executor.run()`` as a task the
  :class:`~ssc.api.tasks.TaskRegistry` owns. The journal's first record is the
  executor's own ``RUN_STARTED``; nothing here writes journal records itself.
* **Publishing.** Every journal append — and therefore every state transition —
  is followed by a ``run.changed`` event carrying the whole ``run`` section,
  computed by folding the journal (``executor.reduce``). Clients render that;
  they never own it (CLAUDE.md invariant 4). The wrapper that does this,
  :class:`PublishingJournal`, publishes *after* the inner append returns, so a
  client can never see a transition the disk does not yet have.
* **Controls** are queued (:class:`~ssc.api.device_commands.ControlQueue`) and
  noticed by the executor at its next decision boundary — which the queue wakes
  it for. This service never reaches into the executor's state.
* **Interruption.** A run a previous process left ``running``/``paused`` is
  marked ``interrupted`` at startup and published with a
  ``resume_interrupted`` action. Resuming is the executor's ordinary start
  path over the existing journal — there is no separate recovery code — and it
  happens only when a user asks (the scope may have been moved by hand since).
* **Headless.** Nothing here depends on a client being attached: the task runs
  until the executor returns, and a client that reconnects gets the section
  from the snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from ssc.api.astro_service import AstroService
from ssc.api.device_commands import ControlQueue, ServiceGateInputs, WallClock
from ssc.api.plans_service import compile_plan_document, site_of
from ssc.api.schemas import (
    HistoricalExecutionReport,
    HistoricalFrameSummary,
    HistoricalItemReport,
    HistoricalMeasurement,
    HistoricalMeasurements,
    HistoricalNightReportResponse,
    HistoricalObjectReport,
)
from ssc.api.tasks import TaskRegistry
from ssc.api.weather_service import WeatherService
from ssc.astro.comets import load_comet_catalog
from ssc.plan.models import Severity
from ssc.plan.validate import validate_plan
from ssc.scheduler import CompiledSchedule
from ssc.scheduler.executor import (
    DEFAULT_POLL_INTERVAL_S,
    TERMINAL_ITEM_STATUSES,
    CapturePort,
    ControlCommand,
    ControlKind,
    DeviceCommands,
    DeviceEvents,
    EffectCall,
    EffectName,
    EffectOutcome,
    EphemerisTargetResolver,
    ExecutorRecoveryRestartError,
    ItemRunState,
    ItemStatus,
    JournalRecord,
    RecordKind,
    RunStatus,
    apply_control_command,
    reduce,
    run,
)
from ssc.scheduler.executor import RunState as ExecutorRunState
from ssc.settings.store import SettingsStore
from ssc.state.events import NoticeRaised, RunChanged
from ssc.state.models import (
    CaptureStage,
    LocationState,
    Notice,
    NoticeLevel,
    RunAction,
    RunItemState,
    RunItemStatus,
    RunLifecycle,
    RunState,
)
from ssc.state.store import StateStore
from ssc.store.capture import CaptureRepository, HistoricalCaptureFrame
from ssc.store.plans import PlanRepository
from ssc.store.runs import JournalEntry, RunRecord, RunRepository

_log = logging.getLogger(__name__)


class RunActiveError(Exception):
    """A run is already executing; there is one scope and one run at a time."""


class RunNightExpiredError(Exception):
    """The selected night has already ended; no hardware action is allowed."""


class RunNotActiveError(Exception):
    """The named run is not the one currently executing."""


class RunNotResumableError(Exception):
    """Only an ``interrupted`` run can be resumed."""


class RunDeviceUnavailableError(Exception):
    """This process hosts no device driver to execute against."""


class RunLocationUnsetError(Exception):
    """No observing location — the plan cannot be compiled (DESIGN.md 5)."""


@dataclass(frozen=True, slots=True)
class RunDetail:
    """``GET /api/runs/{id}``: the section plus the journal behind it."""

    run: RunState
    journal: list[JournalEntry]


_ENDED_LIFECYCLES: frozenset[RunLifecycle] = frozenset(
    {
        RunLifecycle.STOPPED,
        RunLifecycle.COMPLETED,
        RunLifecycle.COMPLETED_WITH_WARNINGS,
        RunLifecycle.FAILED,
    }
)

_ACTIONS_BY_LIFECYCLE: dict[RunLifecycle, tuple[RunAction, ...]] = {
    RunLifecycle.RUNNING: (RunAction.PAUSE, RunAction.SKIP, RunAction.STOP, RunAction.RETRY),
    RunLifecycle.PAUSED: (RunAction.RESUME, RunAction.SKIP, RunAction.STOP, RunAction.RETRY),
    RunLifecycle.STOPPED: (),
    RunLifecycle.COMPLETED: (),
    RunLifecycle.COMPLETED_WITH_WARNINGS: (),
    RunLifecycle.INTERRUPTED: (RunAction.RESUME_INTERRUPTED,),
    RunLifecycle.FAILED: (),
}


def _current_item_id(schedule: CompiledSchedule, state: ExecutorRunState) -> str | None:
    """Mirrors the executor's own choice: the first enabled item not yet settled."""
    for item in schedule.items:
        if not item.enabled:
            continue
        status = state.items.get(item.item_id, ItemRunState()).status
        if status not in TERMINAL_ITEM_STATUSES:
            return item.item_id
    return None


def build_run_state(
    record: RunRecord,
    records: Sequence[JournalRecord],
    *,
    lifecycle: RunLifecycle | None,
    now: datetime,
) -> RunState:
    """Project the journal onto the snapshot's ``run`` section. Pure.

    ``lifecycle`` overrides what the journal implies (``interrupted``/``failed``
    are process facts, not journal facts); ``None`` derives it from the fold.
    """
    state = reduce(records)
    schedule = record.schedule
    current_id = _current_item_id(schedule, state)
    # The executor's four statuses are the first four lifecycles, by value.
    status = lifecycle if lifecycle is not None else RunLifecycle(state.run_status.value)

    items: list[RunItemState] = []
    for compiled in schedule.items:
        item_state = state.items.get(compiled.item_id, ItemRunState())
        item_status = RunItemStatus(item_state.status.value)
        if (
            item_status is RunItemStatus.PENDING
            and compiled.item_id == current_id
            and compiled.start_utc is not None
            and now < compiled.start_utc
        ):
            item_status = RunItemStatus.WAITING
        if status is RunLifecycle.FAILED and item_status is RunItemStatus.RUNNING:
            item_status = RunItemStatus.FAILED
        items.append(
            RunItemState(
                item_id=compiled.item_id,
                kind=compiled.kind,
                enabled=compiled.enabled,
                status=item_status,
                attempt=item_state.attempt,
                panel_index=item_state.panel_index,
                held=item_state.held,
                reason=(record.ended_reason or item_state.reason)
                if status is RunLifecycle.FAILED and item_status is RunItemStatus.FAILED
                else item_state.reason,
                start_utc=compiled.start_utc,
                end_utc=compiled.end_utc,
            )
        )

    held_item = next((item for item in items if item.held), None)
    last = records[-1] if records else None
    ended_at = record.ended_at
    if ended_at is None and last is not None and state.ended:
        ended_at = last.at
    return RunState(
        run_id=record.run_id,
        plan_id=record.plan.id,
        plan_name=record.plan.name,
        plan_revision=record.plan_revision,
        night_date=date.fromisoformat(schedule.night.date),
        status=status,
        started_at=record.started_at,
        ended_at=ended_at,
        ended_reason=record.ended_reason or state.ended_reason,
        current_item_id=current_id,
        items=items,
        gate_held=held_item is not None,
        gate_reason=held_item.reason if held_item is not None else None,
        last_decision=last.reason if last is not None else None,
        last_decision_at=last.at if last is not None else None,
        journal_length=len(records),
        available_actions=list(_ACTIONS_BY_LIFECYCLE[status]),
    )


class PublishingJournal:
    """The executor's ``Journal``, publishing the folded section after each
    durable append. Delegates every read/write to the SQLite journal."""

    def __init__(self, service: RunService, record: RunRecord) -> None:
        self._service = service
        self._record = record
        self._inner = service._runs.journal

    async def append(self, record: JournalRecord) -> None:
        await self._inner.append(record)
        if record.kind in {RecordKind.ITEM_TRANSITION, RecordKind.CONTROL} and (
            record.effect is not None or record.retry_at is not None
            or "recovery" in record.reason.casefold()
        ):
            _log.info("durable run transition utc=%s run=%s item=%s panel=%s step=%s "
                "attempt=%s effect=%s outcome=%s status=%s recovery_attempts=%s retry_at=%s reason=%s",
                record.at.isoformat(), record.run_id, record.item_id, record.panel_index,
                record.step, record.attempt,
                None if record.effect is None else record.effect.name.value,
                None if record.effect_outcome is None else record.effect_outcome.value,
                None if record.status is None else record.status.value,
                record.recovery_attempts,
                None if record.retry_at is None else record.retry_at.isoformat(), record.reason)
        await self._service._publish_from_journal(self._record)

    async def read_all(self, run_id: str) -> Sequence[JournalRecord]:
        return await self._inner.read_all(run_id)

    def replace_record(self, record: RunRecord) -> None:
        self._record = record


#: Builds this run's own ``CapturePort``, given its ``run_id`` — a factory
#: rather than one shared instance because ``CapturePort.expose_frame`` has no
#: ``run_id`` parameter of its own (``ssc.scheduler.executor``'s contract: only
#: ``block_id``, the plan item's id); the run id has to be closed over at
#: construction, the same way :class:`~ssc.api.device_commands.AlpacaCapturePort`
#: is built. ``None`` when this process hosts no device driver, mirroring
#: ``device``/``events`` below — an exposure block then fails loudly with the
#: executor's own "no capture port was supplied" reason (architecture policy invariant 7),
#: never silently.
CapturePortFactory = Callable[[str], CapturePort]


def dawn_safety_deadline(schedule: CompiledSchedule) -> datetime:
    """Prepare safe shutdown ten minutes before actual sunrise, failing closed.

    Twilight and the observing dark window are science-planning boundaries;
    only sunrise defines the owner's hard Park + Dark safety limit.
    """
    night = schedule.night
    boundaries = [event.when_utc for event in night.solar_events
        if event.when_utc is not None and event.event == "sunrise"]
    return min(boundaries) - timedelta(minutes=10) if boundaries else night.window_start_utc


def _historical_summary(frames: Sequence[HistoricalCaptureFrame]) -> HistoricalFrameSummary:
    # Frame IDs are authoritative even if a caller passes overlapping groups.
    unique = {frame.id: frame for frame in frames}
    complete = [frame for frame in unique.values() if frame.status == "complete"]
    measured = [frame for frame in complete if frame.outcome is not None]

    def integration(outcome: str) -> float | None:
        if not measured:
            return None
        selected = [frame for frame in measured if frame.outcome == outcome]
        known = [frame.exposure_s for frame in selected if frame.exposure_s is not None]
        return None if selected and not known else float(sum(known))

    return HistoricalFrameSummary(
        saved_frames=len(complete),
        pending_frames=sum(frame.status == "writing" for frame in unique.values()),
        captured_integration_s=sum(
            frame.exposure_s for frame in complete if frame.exposure_s is not None
        ),
        unknown_exposure_frames=sum(frame.exposure_s is None for frame in complete),
        outcome_measured_frames=len(measured),
        stack_unmeasured_frames=len(complete) - len(measured),
        accepted_frames=None
        if not measured
        else sum(frame.outcome == "accepted" for frame in measured),
        rejected_frames=None
        if not measured
        else sum(frame.outcome == "rejected" for frame in measured),
        stack_error_frames=None
        if not measured
        else sum(frame.outcome == "error" for frame in measured),
        accepted_integration_s=integration("accepted"),
        rejected_integration_s=integration("rejected"),
        stack_error_integration_s=integration("error"),
        unknown_outcome_exposure_frames=sum(frame.exposure_s is None for frame in measured),
    )


async def historical_night_report(
    runs: RunRepository,
    captures: CaptureRepository,
    night_date: date,
    *,
    now: datetime,
) -> HistoricalNightReportResponse:
    """Read persisted execution snapshots and retained acquisitions only."""
    records = await runs.for_night(night_date)
    frames = await captures.historical_frames([record.run_id for record in records])

    def summary(group: Sequence[HistoricalCaptureFrame], *, light: bool) -> HistoricalFrameSummary:
        return _historical_summary(
            [frame for frame in group if (frame.frame_type == "light") == light]
        )

    executions = []
    for record in records:
        own = [frame for frame in frames if frame.run_id == record.run_id]
        entries = await runs.journal.read_all(record.run_id)
        items = []
        compiled_ids = {item.item_id for item in record.schedule.items}
        for item in record.schedule.items:
            item_frames = [frame for frame in own if frame.block_id == item.item_id]
            single_panel = len(item.panels) == 1 or (
                len(item.panels) == 0 and item.kind in {"goto_and_stack", "take_exposures"}
            )
            items.append(
                HistoricalItemReport(
                    item_id=item.item_id,
                    kind=item.kind,
                    object_name=None if item.target is None else item.target.name,
                    planned_duration_s=None
                    if item.start_utc is None or item.end_utc is None
                    else max(0.0, (item.end_utc - item.start_utc).total_seconds()),
                    panel_index=0 if single_panel else None,
                    panel_attribution_available=single_panel,
                    lights=summary(item_frames, light=True),
                    calibration=summary(item_frames, light=False),
                )
            )
        unmatched = [frame for frame in own if frame.block_id not in compiled_ids]
        if unmatched:
            items.append(
                HistoricalItemReport(
                    item_id=None,
                    kind=None,
                    object_name=None,
                    planned_duration_s=None,
                    panel_index=None,
                    panel_attribution_available=False,
                    lights=summary(unmatched, light=True),
                    calibration=summary(unmatched, light=False),
                )
            )
        executions.append(
            HistoricalExecutionReport(
                run=build_run_state(record, entries, lifecycle=record.status, now=now),
                plan=record.plan,
                schedule=record.schedule,
                lights=summary(own, light=True),
                calibration=summary(own, light=False),
                items=items,
            )
        )
    objects = [
        HistoricalObjectReport(
            object_name=name,
            lights=summary([frame for frame in frames if frame.object_name == name], light=True),
            calibration=summary(
                [frame for frame in frames if frame.object_name == name], light=False
            ),
        )
        for name in sorted({frame.object_name for frame in frames}, key=lambda name: (name is not None, name or ""))
    ]
    light_count = summary(frames, light=True).saved_frames
    return HistoricalNightReportResponse(
        night_date=night_date,
        executions=executions,
        lights=summary(frames, light=True),
        calibration=summary(frames, light=False),
        objects=objects,
        measurements=HistoricalMeasurements(
            sky_quality=HistoricalMeasurement(total_samples=light_count),
            weather=HistoricalMeasurement(total_samples=light_count),
            hfr=HistoricalMeasurement(unit="px", total_samples=light_count),
            fwhm=HistoricalMeasurement(unit="px", total_samples=light_count),
        ),
    )


class RunService:
    """See the module docstring."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        plans: PlanRepository,
        state: StateStore,
        tasks: TaskRegistry,
        astro: AstroService,
        weather: WeatherService,
        settings: SettingsStore,
        device: DeviceCommands | None,
        events: DeviceEvents | None,
        capture_factory: CapturePortFactory | None = None,
        calibration_count_updater: Callable[[int], Awaitable[object]] | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        operation_recovery_active: Callable[[], bool] = lambda: False,
        dawn_safety_enabled: bool = False,
        safety_prepare: Callable[[], Awaitable[object]] | None = None,
        safety_reconnect: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._dawn_safety_enabled = dawn_safety_enabled
        self._safety_prepare = safety_prepare
        self._safety_reconnect = safety_reconnect
        self._dawn_task: asyncio.Task[None] | None = None
        self._dawn_run_id: str | None = None
        self._dawn_stopped: set[str] = set()
        self._runs = runs
        self._plans = plans
        self._state = state
        self._tasks = tasks
        self._astro = astro
        self._weather = weather
        self._settings = settings
        self._device = device
        self._events = events
        self._capture_factory = capture_factory
        self._calibration_count_updater = calibration_count_updater
        self._calibration_canceller: Callable[[], Awaitable[object]] | None = None
        self._poll_interval_s = poll_interval_s
        self._operation_recovery_active = operation_recovery_active
        self._supervisor_recovering = False
        self._controls = ControlQueue()
        self._clock = WallClock(wake=self._controls.wait)
        self._task: asyncio.Task[None] | None = None
        self._active: RunRecord | None = None
        self._last_lifecycle: RunLifecycle | None = None
        self._publishing_journal: PublishingJournal | None = None
        self._resume_lock = asyncio.Lock()
        self._stop_generation = 0
        self._stop_requested_run_id: str | None = None

    def set_calibration_count_updater(self, updater: Callable[[int], Awaitable[object]]) -> None:
        self._calibration_count_updater = updater

    def set_calibration_canceller(self, canceller: Callable[[], Awaitable[object]]) -> None:
        self._calibration_canceller = canceller

    async def scheduled_calibration_permitted(self, run_id: str) -> bool:
        """Priority Stop/dawn is authoritative before every calibration frame."""
        if run_id in self._dawn_stopped:
            return False
        state = reduce(await self._runs.journal.read_all(run_id))
        return state.run_status is not RunStatus.STOPPED

    # -- lifecycle ------------------------------------------------------------- #

    async def record_dither_tracking_recovery(self) -> bool:
        """Journal one tracking reset before its commands, never twice on replay."""
        run_state = self._state.snapshot.run
        journal = self._publishing_journal
        if run_state is None or journal is None or run_state.current_item_id is None:
            return False
        records = await journal.read_all(run_state.run_id)
        state = reduce(records)
        item = state.items.get(run_state.current_item_id)
        if (item is None or state.run_status.value != "running"
                or item.pending_effect is None or item.pending_effect.name is not EffectName.DITHER):
            return False
        reason = "dither tracking reset requested after unstable stopped telemetry"
        if any(r.item_id == run_state.current_item_id and r.attempt == item.attempt
               and r.reason == reason for r in records):
            return False
        await journal.append(JournalRecord(
            at=self._clock.now(), run_id=run_state.run_id, kind=RecordKind.ITEM_TRANSITION,
            item_id=run_state.current_item_id, step=item.step, attempt=item.attempt,
            panel_index=item.panel_index, reason=reason,
        ))
        return True

    async def startup(self) -> RunState | None:
        """Surface whatever a previous process left unfinished as ``interrupted``.

        Returns the section published (also applied to the state store), or
        ``None`` when there is nothing to show.
        """
        unfinished = await self._runs.list_unfinished()
        now = self._clock.now()
        for record in unfinished:
            _log.warning(
                "run %s (plan %r) was %s when the previous process ended; marked interrupted",
                record.run_id,
                record.plan.name,
                record.status.value,
            )
            await self._runs.set_status(
                record.run_id,
                RunLifecycle.INTERRUPTED,
                ended_reason="ssc-core stopped while this run was in progress",
            )
        if self._dawn_safety_enabled:
            pending_safety = await self._runs.list_dawn_safety_pending()
            latest_safety = pending_safety[0] if pending_safety else await self._runs.latest()
            if latest_safety is not None:
                await self._arm_dawn_safety(latest_safety)
        if not unfinished:
            return None
        latest = await self._runs.get(unfinished[0].run_id)
        section = build_run_state(
            latest,
            await self._runs.journal.read_all(latest.run_id),
            lifecycle=RunLifecycle.INTERRUPTED,
            now=now,
        )
        self._state.apply(RunChanged(run=section))
        return section

    @property
    def active(self) -> RunRecord | None:
        """The run whose executor task is alive, if any."""
        if self._task is None or self._task.done():
            return None
        return self._active

    def _require_device(self) -> tuple[DeviceCommands, DeviceEvents]:
        if self._device is None or self._events is None:
            raise RunDeviceUnavailableError(
                "this ssc-core hosts no device driver, so a plan cannot be executed"
            )
        return self._device, self._events

    def _require_location(self) -> LocationState:
        location = self._settings.active_location()
        if location is None:
            raise RunLocationUnsetError(
                "no observing location is set: add one in Settings > Location before starting a run"
            )
        return location

    async def start(self, plan_id: str, *, night_date: date | None = None) -> RunState:
        """Compile, persist, launch. Raises :class:`RunActiveError` if a run is
        executing, plus everything ``compile_plan_document`` raises."""
        async with self._resume_lock:
            return await self._start(plan_id, night_date=night_date)

    async def _start(self, plan_id: str, *, night_date: date | None = None) -> RunState:
        if (self._dawn_safety_enabled and self._dawn_task is not None
                and not self._dawn_task.done() and (self._dawn_run_id in self._dawn_stopped
                    or any(self._clock.now() >= dawn_safety_deadline(pending.schedule)
                           for pending in await self._runs.list_dawn_safety_pending()))):
            raise RunActiveError("dawn safety verification is pending; science is disabled")
        if self.active is not None:
            raise RunActiveError(
                f"run {self.active.run_id} is still executing; stop it before starting another"
            )
        device, events = self._require_device()
        location = self._require_location()
        plan_record = await self._plans.get(plan_id)
        now = self._clock.now()
        schedule = compile_plan_document(
            astro=self._astro,
            document=plan_record.document,
            location=location,
            night_date=night_date,
            now=now,
            profile_id=self._settings.device.hardware_profile_id,
        )
        if now >= (dawn_safety_deadline(schedule) if self._dawn_safety_enabled else schedule.night.window_end_utc):
            raise RunNightExpiredError(
                f"night {schedule.night.date} has already ended; select a current or future "
                "night and recompile the plan before starting"
            )
        record = await self._runs.create(
            run_id=uuid.uuid4().hex,
            plan=plan_record.document,
            plan_revision=plan_record.revision,
            schedule=schedule,
            started_at=now,
        )
        return await self._launch(record, device, events, location)

    async def resume_interrupted(self, run_id: str) -> RunState:
        """Carry an ``interrupted`` run on from its journal, at the user's request."""
        async with self._resume_lock:
            return await self._resume_interrupted(run_id)

    async def _resume_interrupted(self, run_id: str) -> RunState:
        if (self._dawn_safety_enabled and self._dawn_task is not None
                and not self._dawn_task.done() and (self._dawn_run_id in self._dawn_stopped
                    or any(self._clock.now() >= dawn_safety_deadline(pending.schedule)
                           for pending in await self._runs.list_dawn_safety_pending()))):
            raise RunActiveError("dawn safety verification is pending; science is disabled")
        if self.active is not None:
            raise RunActiveError(
                f"run {self.active.run_id} is still executing; stop it before resuming another"
            )
        device, events = self._require_device()
        location = self._require_location()
        record = await self._runs.get(run_id)
        self._check_dawn_cutoff(record)
        if record.status is not RunLifecycle.INTERRUPTED:
            raise RunNotResumableError(
                f"run {run_id} is {record.status.value}, not interrupted; only an interrupted "
                "run can be resumed"
            )
        record = await self._refresh_resume_snapshot(record, location)
        self._check_resume_deadline(record)
        current = reduce(await self._runs.journal.read_all(run_id))
        if current.run_status is RunStatus.PAUSED:
            # Explicit interruption recovery also lifts a durable pre-crash Pause.
            await apply_control_command(self._runs.journal, self._clock, run_id,
                current, record.schedule, ControlCommand(kind=ControlKind.RESUME,
                    reason="interrupted run resumed by operator"))
        await self._runs.set_status(run_id, RunLifecycle.RUNNING)
        record = await self._runs.get(run_id)
        return await self._launch(record, device, events, location)

    async def _refresh_resume_snapshot(
        self, record: RunRecord, location: LocationState, *, stop_generation: int | None = None,
    ) -> RunRecord:
        """Adopt only untouched work, preserving the durable execution prefix.

        The original night anchors ASAP/relative starts. The selected editor
        night cannot move an existing run to another observing night.
        """
        self._check_resume_deadline(record)
        latest = await self._plans.get(record.plan.id)
        schedule = await asyncio.to_thread(compile_plan_document,
            astro=self._astro, document=latest.document, location=location,
            night_date=date.fromisoformat(str(record.schedule.night.date)),
            now=self._clock.now(),
            profile_id=self._settings.device.hardware_profile_id,
        )
        if (await self._plans.get(record.plan.id)).revision != latest.revision:
            raise RunNotResumableError("saved plan changed while preparing resume; retry Resume")
        if stop_generation is not None and self._stop_generation != stop_generation:
            raise RunNotResumableError("Stop took priority while preparing resume")
        records = await self._runs.journal.read_all(record.run_id)
        state = reduce(records)
        if state.ended or state.run_status is RunStatus.STOPPED:
            raise RunNotResumableError("stopped or ended runs require a new start")
        touched = {entry.item_id for entry in records if entry.item_id is not None}
        prefix = max((index + 1 for index, item in enumerate(record.plan.items)
                      if item.id in touched), default=0)
        if (latest.document.items[:prefix] != record.plan.items[:prefix]
                or schedule.items[:prefix] != record.schedule.items[:prefix]
                or schedule.night != record.schedule.night):
            raise RunNotResumableError(
                "saved plan changes completed or started work, its order, or observing conditions; "
                "restore that work before resuming, or start a new run"
            )
        protected_ids = {item.id for item in record.plan.items[:prefix]}
        errors = [diagnostic for diagnostic in validate_plan(latest.document)
                  if diagnostic.severity is Severity.ERROR and (
                      diagnostic.item_id not in protected_ids or diagnostic.code == "duplicate_id")]
        errors.extend(diagnostic for item in schedule.items[prefix:] if item.enabled
                      for diagnostic in item.diagnostics if diagnostic.severity is Severity.ERROR)
        if errors:
            raise RunNotResumableError(
                "saved remaining work cannot be compiled: " + "; ".join(
                    diagnostic.message for diagnostic in errors))
        if stop_generation is not None and self._stop_generation != stop_generation:
            raise RunNotResumableError("Stop took priority while preparing resume")
        self._check_resume_deadline(record)
        return await self._runs.update_plan_snapshot(record.run_id,
            plan=latest.document, plan_revision=latest.revision, schedule=schedule)

    async def _launch(
        self,
        record: RunRecord,
        device: DeviceCommands,
        events: DeviceEvents,
        location: LocationState,
    ) -> RunState:
        self._check_dawn_cutoff(record)
        self._controls = ControlQueue()
        self._stop_requested_run_id = None
        self._clock = WallClock(wake=self._controls.wait)
        self._active = record
        self._last_lifecycle = RunLifecycle.RUNNING
        gate_inputs = ServiceGateInputs(
            weather=self._weather, astro=self._astro, location=location, now=self._clock.now
        )
        # A body item's position is only known at the instant it is issued
        # (DESIGN.md 3.6): without a resolver here, a body item fails loudly
        # rather than slewing to a guessed or stale place (the executor's own
        # "no target resolver was supplied" reason). ``self._astro.ephemeris``
        # is loaded once at process startup and never changes afterwards, so
        # ``None`` here means the same thing for the life of this process.
        resolver = (
            None
            if self._astro.ephemeris is None
            else EphemerisTargetResolver(
                ephemeris=self._astro.ephemeris,
                site=site_of(location),
                comet_catalog=load_comet_catalog(),
            )
        )
        # Built fresh per launch, exactly like ``resolver`` above: this run's
        # own ``CapturePort``, closing over ``record.run_id`` (see
        # ``CapturePortFactory``'s own docstring for why that cannot instead
        # be a parameter of the call the executor makes).
        capture = None if self._capture_factory is None else self._capture_factory(record.run_id)
        journal = PublishingJournal(self, record)
        self._publishing_journal = journal
        self._task = self._tasks.spawn(
            self._drive(record, journal, device, events, gate_inputs, resolver, capture),
            name=f"run-{record.run_id}",
        )
        await self._arm_dawn_safety(record)
        return await self._publish_from_journal(record)

    def _check_dawn_cutoff(self, record: RunRecord) -> None:
        if self._dawn_safety_enabled and (record.run_id in self._dawn_stopped
                or self._clock.now() >= dawn_safety_deadline(record.schedule)):
            raise RunNightExpiredError("dawn safety cutoff reached or dawn boundary unknown; science cannot resume")

    def _check_resume_deadline(self, record: RunRecord) -> None:
        self._check_dawn_cutoff(record)
        if self._clock.now() >= record.schedule.night.window_end_utc:
            raise RunNightExpiredError("the original observing night has ended; start a new run")

    async def _safety_record(self, record: RunRecord, stage: str, reason: str) -> None:
        await self._runs.journal.append(JournalRecord(
            at=self._clock.now(), run_id=record.run_id, kind=RecordKind.DAWN_SAFETY,
            effect=EffectCall(name=EffectName.GATE_PARK, args={
                "stage": stage, "deadline_utc": dawn_safety_deadline(record.schedule).isoformat()}),
            reason=reason))
        _log.info("dawn safety run=%s stage=%s reason=%s", record.run_id, stage, reason)

    async def _arm_dawn_safety(self, record: RunRecord) -> None:
        if not self._dawn_safety_enabled:
            return
        if self._dawn_task is not None and not self._dawn_task.done():
            if self._dawn_run_id == record.run_id:
                return
            # An earlier night guard must remain alive until hardware is verified.
            return
        # A terminal safety verification is durable: startup's legacy fallback
        # must not turn an already satisfied obligation back into a pending one.
        safety_records = [entry for entry in await self._runs.journal.read_all(record.run_id)
                          if entry.kind is RecordKind.DAWN_SAFETY]
        if (safety_records and safety_records[-1].effect is not None
                and safety_records[-1].effect.args.get("stage") == "completed"):
            return
        await self._safety_record(record, "armed", "independent dawn safety watchdog armed")
        self._dawn_run_id = record.run_id
        self._dawn_task = self._tasks.spawn(self._dawn_watchdog(record),
            name=f"dawn-safety-{record.run_id}")

    async def _bounded_safety_operation(self, operation: Callable[[], Awaitable[object]]) -> None:
        async def invoke() -> Exception | None:
            # Operational failures belong to the watchdog's persisted retry
            # path. Return them to its caller rather than reporting a handled
            # failure as an escaped background-task crash as well.
            try:
                await operation()
            except Exception as exc:
                return exc
            return None
        task = self._tasks.spawn(invoke(), name="dawn-safety-operation")
        try:
            done, _pending = await asyncio.wait({task}, timeout=60)
            if not done:
                task.cancel()
                raise TimeoutError("dawn safety operation exceeded 60 seconds")
            failure = task.result()
            if failure is not None:
                raise failure
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _dawn_watchdog(self, record: RunRecord) -> None:
        deadline = dawn_safety_deadline(record.schedule)
        while self._clock.now() < deadline:  # noqa: ASYNC110 - independent wall-clock deadline
            await asyncio.sleep(min(1.0, (deadline - self._clock.now()).total_seconds()))
        await self._safety_record(record, "intent", "dawn cutoff overrides all panel recovery")
        guarded_ids = {record.run_id}
        if self._active is not None:
            guarded_ids.add(self._active.run_id)
        self._dawn_stopped.update(guarded_ids)
        for run_id in guarded_ids:
            current = await self._runs.get(run_id)
            if current.status not in _ENDED_LIFECYCLES:
                await self._runs.journal.append(JournalRecord(at=self._clock.now(),
                    run_id=run_id, kind=RecordKind.CONTROL,
                    control=ControlKind.STOP, reason="dawn safety cutoff"))
        if self._task is not None and not self._task.done():
            self._task.cancel()
            done, _pending = await asyncio.wait({self._task}, timeout=60)
            for task in done:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
        # Safety obligations outlive science execution. Never rewrite an
        # already terminal run's actual outcome with a later hardware retry.
        # Re-read after cancellation: executor shutdown may have finalized it.
        for run_id in guarded_ids:
            current = await self._runs.get(run_id)
            if current.status not in _ENDED_LIFECYCLES:
                await self._runs.set_status(run_id, RunLifecycle.STOPPED,
                    ended_at=self._clock.now(), ended_reason="dawn safety cutoff")
                visible = self._state.snapshot.run
                if visible is not None and visible.run_id == run_id:
                    await self._publish_from_journal(await self._runs.get(run_id),
                        lifecycle=RunLifecycle.STOPPED)
        if self._safety_prepare is not None:
            await self._safety_record(record, "prepare_intent", "bounded capture cleanup before park")
            try:
                await self._bounded_safety_operation(self._safety_prepare)
                await self._safety_record(record, "prepare_verified", "capture cleanup returned")
            except Exception as exc:
                await self._safety_record(record, "prepare_failed", str(exc))
        attempt = 0
        while True:
            attempt += 1
            errors = []
            for stage, operation in (
                ("reconnect", self._safety_reconnect),
                ("park", None if self._device is None else getattr(self._device, "ensure_safety_parked", None)),
                ("dark", None if self._device is None else lambda: self._device.set_filter("Dark")),
            ):
                if stage == "reconnect" and operation is None:
                    continue
                await self._safety_record(record, f"{stage}_intent", f"attempt {attempt}")
                try:
                    if operation is None:
                        raise RunDeviceUnavailableError("dawn safety device unavailable")
                    await self._bounded_safety_operation(operation)
                    await self._safety_record(record, f"{stage}_verified", f"attempt {attempt}")
                except Exception as exc:
                    errors.append(f"{stage}: {type(exc).__name__}: {exc}")
                    await self._safety_record(record, f"{stage}_failed", errors[-1])
            message = "; ".join(errors) if errors else "dawn safety: park and Dark verified"
            self._state.apply(NoticeRaised(notice=Notice(id="dawn-safety", code="dawn_safety",
                level=NoticeLevel.WARNING if errors else NoticeLevel.INFO,
                message=message, raised_at=self._clock.now())))
            if not errors:
                await self._safety_record(record, "completed", message)
                pending = await self._runs.list_dawn_safety_pending()
                if pending:
                    self._dawn_task = None
                    await self._arm_dawn_safety(pending[0])
                return
            await asyncio.sleep((5, 15, 30)[min(attempt - 1, 2)])

    async def _drive(
        self,
        record: RunRecord,
        journal: PublishingJournal,
        device: DeviceCommands,
        events: DeviceEvents,
        gate_inputs: ServiceGateInputs,
        resolver: EphemerisTargetResolver | None,
        capture: CapturePort | None,
    ) -> None:
        attempt = 0
        while True:
            if record.run_id in self._dawn_stopped:
                return
            try:
                await run(
                    run_id=record.run_id, schedule=record.schedule, plan=record.plan,
                    journal=journal, device=device, events=events, clock=self._clock,
                    controls=self._controls, resolver=resolver, capture=capture,
                    gate_inputs=gate_inputs, poll_interval_s=self._poll_interval_s,
                    supervisor_restarts=True,
                    plan_snapshot=lambda: (
                        self._active.schedule if self._active is not None else record.schedule,
                        self._active.plan if self._active is not None else record.plan,
                    ),
                )
                return
            except asyncio.CancelledError:
                # Process shutdown retains the exact durable checkpoint.
                raise
            except Exception as exc:
                attempt += 1
                current = reduce(await journal.read_all(record.run_id))
                active = self._active or record
                item = next((item for item in active.schedule.items
                    if item.enabled and current.items.get(item.item_id, ItemRunState()).status
                    not in TERMINAL_ITEM_STATUSES), None)
                progress = None if item is None else current.items.get(item.item_id, ItemRunState())
                deadline = active.schedule.night.window_end_utc
                if item is not None:
                    deadline = item.end_utc or deadline
                    if progress.panel_index is not None:
                        panel = (item.panels[progress.panel_index]
                            if 0 <= progress.panel_index < len(item.panels) else None)
                        if panel is not None:
                            deadline = panel.end_utc or deadline
                stage = (progress.pending_effect.name.value
                    if progress is not None and progress.pending_effect is not None
                    else f"step {progress.step}" if progress is not None else "run dispatch")
                delay = (5, 15, 30, 60)[min(attempt - 1, 3)]
                retry_at = min(self._clock.now() + timedelta(seconds=delay), deadline)
                reason = (f"supervisor recovery stage={stage} attempt={attempt} "
                    f"deadline={deadline.isoformat()} exception={type(exc).__name__}: {exc}; "
                    f"retry_at={retry_at.isoformat()}; same run checkpoint retained")
                _log.exception("run %s: %s", record.run_id, reason)
                if item is not None:
                    await journal.append(JournalRecord(
                        at=self._clock.now(), run_id=record.run_id,
                        kind=RecordKind.ITEM_TRANSITION, item_id=item.item_id,
                        step=progress.step, attempt=progress.attempt,
                        panel_index=progress.panel_index, status=ItemStatus.RUNNING,
                        effect=(replace(progress.pending_effect,
                            args={**progress.pending_effect.args, "recovery_hold": True})
                            if progress.pending_effect is not None
                            and progress.pending_effect.name in {EffectName.CAPTURE_CALIBRATION,
                                EffectName.POLAR_ALIGNMENT, EffectName.SYNC_TO}
                            and not (progress.pending_effect.name is EffectName.CAPTURE_CALIBRATION
                                and progress.pending_effect.args.get("catalogue_resumable"))
                            else progress.pending_effect),
                        effect_outcome=(EffectOutcome.FAILED
                            if progress.pending_effect is not None
                            and not isinstance(exc, ExecutorRecoveryRestartError)
                            and progress.pending_effect.name not in {EffectName.DITHER,
                                EffectName.CAPTURE_CALIBRATION, EffectName.POLAR_ALIGNMENT,
                                EffectName.SYNC_TO} else None),
                        retry_at=retry_at, recovery_attempts=max(attempt, progress.recovery_attempts), reason=reason,
                    ))
                if current.run_status is RunStatus.STOPPED:
                    # A failed park is retried with the same pending run effect.
                    # Do not turn an operator Stop into another observing run.
                    retry_at = self._clock.now() + timedelta(seconds=delay)
                while True:
                    await self._wait_for_supervisor_retry(record, journal, retry_at)
                    if record.run_id in self._dawn_stopped:
                        return
                    latest = reduce(await journal.read_all(record.run_id))
                    recovery = getattr(capture, "recover_operation", None)
                    if (latest.run_status is RunStatus.STOPPED or recovery is None
                            or (item is not None and latest.items.get(
                                item.item_id, ItemRunState()).status in TERMINAL_ITEM_STATUSES)
                            or self._clock.now() >= deadline):
                        break
                    self._supervisor_recovering = True
                    try:
                        await recovery(level=3, operation=stage)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as recovery_exc:
                        attempt += 1
                        delay = (5, 15, 30, 60)[min(attempt - 1, 3)]
                        retry_at = min(self._clock.now() + timedelta(seconds=delay), deadline)
                        _log.exception("run %s supervisor recovery stage=%s attempt=%d "
                            "deadline=%s recovery_exception=%s: %s; exact checkpoint retained",
                            record.run_id, stage, attempt, deadline.isoformat(),
                            type(recovery_exc).__name__, recovery_exc)
                        repair_state = reduce(await journal.read_all(record.run_id))
                        if item is not None:
                            repair_progress = repair_state.items.get(item.item_id, ItemRunState())
                            await journal.append(JournalRecord(
                                at=self._clock.now(), run_id=record.run_id,
                                kind=RecordKind.ITEM_TRANSITION, item_id=item.item_id,
                                step=repair_progress.step, attempt=repair_progress.attempt,
                                panel_index=repair_progress.panel_index, status=repair_progress.status,
                                retry_at=retry_at,
                                recovery_attempts=max(attempt, repair_progress.recovery_attempts),
                                reason=f"supervisor recovery repair failed level=3 stage={stage} "
                                    f"attempt={attempt} deadline={deadline.isoformat()} "
                                    f"exception={type(recovery_exc).__name__}: {recovery_exc}; "
                                    f"retry_at={retry_at.isoformat()}; checkpoint retained; "
                                    "waiting for repair before operation replay",
                            ))
                    finally:
                        self._supervisor_recovering = False
                # Re-enter the same executor/journal. At deadline its pure
                # decision closes this panel and hands off, never restarts it.

    async def _wait_for_supervisor_retry(
        self, record: RunRecord, journal: PublishingJournal, retry_at: datetime
    ) -> None:
        """Controls remain durable and wake recovery backoff immediately."""
        while True:
            current = reduce(await journal.read_all(record.run_id))
            command = await self._controls.poll()
            if command is not None:
                await apply_control_command(journal, self._clock, record.run_id,
                    current, (self._active or record).schedule, command)
                current = reduce(await journal.read_all(record.run_id))
            if current.run_status is RunStatus.STOPPED:
                return
            if current.run_status is not RunStatus.PAUSED and self._clock.now() >= retry_at:
                return
            until = (self._clock.now() + timedelta(seconds=self._poll_interval_s)
                if current.run_status is RunStatus.PAUSED else retry_at)
            await self._clock.sleep_until(until)

    # -- controls ------------------------------------------------------------------ #

    async def control(
        self, run_id: str, kind: ControlKind, *, item_id: str | None = None, reason: str = ""
    ) -> RunState:
        """Queue one control for the executing run. Returns the section as it is
        now — the control itself lands at the executor's next decision boundary."""
        active = self.active
        if active is None or active.run_id != run_id:
            raise RunNotActiveError(f"run {run_id} is not executing")
        command = ControlCommand(kind=kind, item_id=item_id, reason=reason)
        if kind is ControlKind.STOP:
            self._stop_generation += 1
            self._stop_requested_run_id = run_id
        if kind is ControlKind.RESUME:
            async with self._resume_lock:
                if self._stop_requested_run_id == run_id:
                    raise RunNotResumableError("Stop has already been requested; start a new run")
                generation = self._stop_generation
                current = reduce(await self._runs.journal.read_all(run_id))
                if current.run_status is not RunStatus.PAUSED:
                    raise RunNotResumableError("only a paused active run can resume")
                updated = await self._refresh_resume_snapshot(active, self._require_location(),
                    stop_generation=generation)
                if self.active is None or self._stop_generation != generation:
                    # Stop can arrive while SQLite commits the proposed snapshot.
                    # Restore the old checkpoint before refusing its activation.
                    await self._runs.update_plan_snapshot(run_id, plan=active.plan,
                        plan_revision=active.plan_revision, schedule=active.schedule)
                    raise RunNotResumableError("Stop took priority while preparing resume")
                self._check_resume_deadline(updated)
                self._active = active = updated
                if self._publishing_journal is not None:
                    self._publishing_journal.replace_record(updated)
                self._controls.push(command)
                return await self._section(updated)
        if (kind is ControlKind.STOP and (self._supervisor_recovering
                or self._operation_recovery_active()
                or (self._state.snapshot.capture is not None
                    and self._state.snapshot.capture.stage is CaptureStage.DITHERING))):
            # Publish durable Stop before the capture adapter interrupts its wait.
            journal = self._publishing_journal
            current = reduce(await journal.read_all(run_id))
            await apply_control_command(journal, self._clock, run_id, current, active.schedule, command)
        elif (kind is ControlKind.STOP and self._calibration_canceller is not None
              and self._state.snapshot.capture is not None
              and self._state.snapshot.capture.calibration.status == "running"
              and await self._calibration_effect_pending(run_id)):
            # Stop is durable before the server-owned batch sees cancellation.
            # Retain/download its current frame, then prevent another exposure.
            journal = self._publishing_journal
            current = reduce(await journal.read_all(run_id))
            await apply_control_command(journal, self._clock, run_id, current, active.schedule, command)
            await self._calibration_canceller()
        elif kind is ControlKind.STOP and getattr(self._device, "autofocus_running", False):
            # Journal the Stop and pending park before cancellation can touch hardware.
            journal = self._publishing_journal
            current = reduce(await journal.read_all(run_id))
            await apply_control_command(journal, self._clock, run_id, current, active.schedule, command)
            await self._device.stop_autofocus()
        else:
            self._controls.push(command)
        return await self._section(active)

    async def _calibration_effect_pending(self, run_id: str) -> bool:
        state = reduce(await self._publishing_journal.read_all(run_id))
        return any(item.pending_effect is not None
                   and item.pending_effect.name is EffectName.CAPTURE_CALIBRATION
                   for item in state.items.values())

    # -- reading ------------------------------------------------------------------ #

    @property
    def current(self) -> RunState | None:
        return self._state.snapshot.run

    def live_stack_targets(self) -> tuple[str | None, str | None, datetime | None]:
        """Current and next distinct target from the executing schedule."""
        record = self.active
        state = self.current
        if record is None or state is None or state.current_item_id is None:
            return None, None, None
        items = record.schedule.items
        current_index = next(
            (index for index, item in enumerate(items) if item.item_id == state.current_item_id),
            None,
        )
        if current_index is None:
            return None, None, None
        current_item = items[current_index]
        current_name = None if current_item.target is None else current_item.target.name
        for item in items[current_index + 1 :]:
            if not item.enabled or item.target is None or item.target.name is None:
                continue
            if current_name is not None and item.target.name == current_name:
                continue
            return current_name, item.target.name, item.start_utc
        return current_name, None, None

    async def detail(self, run_id: str) -> RunDetail:
        record = await self._runs.get(run_id)
        entries = await self._runs.journal.read_entries(run_id)
        active = self.active
        lifecycle = None if active is not None and active.run_id == run_id else record.status
        section = build_run_state(
            record,
            [entry.record for entry in entries],
            lifecycle=lifecycle,
            now=self._clock.now(),
        )
        return RunDetail(run=section, journal=entries)

    async def _section(
        self, record: RunRecord, *, lifecycle: RunLifecycle | None = None
    ) -> RunState:
        records = await self._runs.journal.read_all(record.run_id)
        return build_run_state(record, records, lifecycle=lifecycle, now=self._clock.now())

    async def _publish_from_journal(
        self,
        record: RunRecord,
        *,
        lifecycle: RunLifecycle | None = None,
        ended_reason: str | None = None,
    ) -> RunState:
        """Fold the journal, persist a lifecycle change to the ``runs`` row, publish."""
        section = await self._section(record, lifecycle=lifecycle)
        if section.status in _ENDED_LIFECYCLES:
            section = section.model_copy(
                update={
                    "ended_at": section.ended_at or self._clock.now(),
                    "ended_reason": ended_reason or section.ended_reason,
                }
            )
        if section.status in _ENDED_LIFECYCLES:
            records = await self._runs.journal.read_all(record.run_id)
            # STOPPED is published before its park effect runs. Only the terminal
            # journal boundary is evidence that executor shutdown has finished.
            finalized = reduce(records).ended or lifecycle is RunLifecycle.FAILED
            if finalized and await self._runs.final_mount(record.run_id) is None:
                snapshot = self._state.snapshot
                mount = snapshot.mount
                last_effect_at = max((entry.at for entry in records
                    if entry.effect is not None and entry.effect_outcome is not None),
                    default=record.started_at)
                fresh = (snapshot.link.control_connected and mount is not None and not mount.stale
                         and mount.polled_at is not None and mount.polled_at >= last_effect_at
                         and 0 <= (self._clock.now() - mount.polled_at).total_seconds() <= 5)
                await self._runs.record_final_mount(record.run_id,
                    polled_at=mount.polled_at if fresh and mount else None,
                    at_park=mount.at_park if fresh and mount else None,
                    tracking=mount.tracking if fresh and mount else None,
                    slewing=mount.slewing if fresh and mount else None)
        if section.status is not self._last_lifecycle:
            self._last_lifecycle = section.status
            await self._runs.set_status(
                record.run_id,
                section.status,
                ended_at=section.ended_at,
                ended_reason=section.ended_reason,
            )
        self._state.apply(RunChanged(run=section))
        return section


__all__ = [
    "CapturePortFactory",
    "PublishingJournal",
    "RunActiveError",
    "RunDetail",
    "RunDeviceUnavailableError",
    "RunLocationUnsetError",
    "RunNotActiveError",
    "RunNotResumableError",
    "RunService",
    "build_run_state",
]
