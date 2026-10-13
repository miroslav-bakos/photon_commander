# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The durable executor: ``CompiledSchedule`` -> Alpaca actions (DESIGN.md 4.2).

Three ideas carry the whole module.

**Durable before acted on.** Every state transition — an effect about to be issued, an
effect's outcome, a control being applied, a gate starting or ending a hold, the run
starting or ending — is appended to the :class:`Journal` *before* the corresponding side
effect happens. Nothing here ever mutates a past record; the journal only grows.

**``RunState`` is a pure fold over the journal.** :func:`reduce` takes every record ever
appended for a run and produces the current position — deterministic, total, and callable
on any prefix of a valid journal (that totality is what the Hypothesis property test in
``tests/scheduler/test_crash_resume.py`` checks). Resuming a run is: read the journal,
``reduce`` it, and carry on — there is no separate "recovery" code path, which is what
makes "kill it at any instant" a property of the design rather than something bolted on
after the fact.

**Stepping is `decide` (pure) plus a thin `run` driver (I/O).** :func:`decide` looks at
the current ``RunState``, the compiled schedule, the wall clock and (when relevant) a
gate verdict, and returns one :class:`Decision` — never acts on anything itself.
:func:`run` is the only place that touches a port: for each decision it journals the
*intent*, performs the effect through a port, and journals the *outcome*. Crash-inject
between any two of those journal writes and a freshly built driver — reading nothing but
what actually landed in the journal — reaches the identical position with the identical
side effects already issued. That is the whole of the M5 gate.

**Every port is a ``Protocol``.** This module never imports ``sqlite3``, ``httpx``,
``asyncio.sleep`` against the wall clock, or ``ssc.alpaca`` — see the ``pure-core``
import-linter contract. ``backend-core`` supplies concrete adapters for
:class:`Journal`, :class:`DeviceCommands`, :class:`DeviceEvents`, :class:`Clock`,
:class:`GateInputs` and :class:`ControlSource`.

**Item-specific fields live in the ``Plan``, not the ``CompiledSchedule``.**
:class:`~ssc.scheduler.compiler.CompiledItem` deliberately carries only timing and
diagnostics (compiler.py's docstring: "absolute times, mosaics expanded, validated") —
not a goto's RA/Dec, an exposure's seconds, a filter's position. :func:`run` is handed
both the ``Plan`` and its ``CompiledSchedule`` and joins them on ``item_id`` for exactly
that reason; ``CompiledItem`` itself is never extended with payload fields.

**Why ``wait_for(event_kind, deadline)`` and not an async iterator for device
completion.** An iterator implies a background consumer that keeps running between
driver steps — something has to buffer events the driver has not asked for yet, and that
buffer is itself state the driver would need to reason about across a crash.
``wait_for`` keeps the driver's control flow lock-step with the journal: issue, await
exactly one outcome, journal it. It is also trivial to fake deterministically in a test
(a dict of ``event_kind -> None | exception``), which an iterator-based fake is not.

**Why a scheduler-owned ``DeviceCommandError`` and not ``ssc.client.errors``.**
CLAUDE.md invariant 3 keeps ``ssc.scheduler`` free of I/O; importing ``ssc.client``
would pull its HTTP-backed ``ssc.client.alpaca`` module in via ``ssc/client/__init__.py``.
``ssc/client/errors.py`` already sets the precedent for this exact situation — it
restates the ASCOM error numbers instead of importing ``ssc.alpaca``'s, "because a client
of a standard protocol should depend on the protocol, not on one particular server that
speaks it." The same reasoning applies one layer up: the ``DeviceCommands`` adapter
``backend-core`` writes is the one place that knows about ``AlpacaRemoteError`` and
``is_not_implemented``; it translates those into :class:`DeviceCommandError` /
:class:`DeviceCommandNotImplementedError` before they ever reach this module.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from ssc.astro.comets import (
    CometCatalog,
    comet_execution_rejection,
    comet_position,
)
from ssc.astro.events import Ephemeris, Site
from ssc.astro.targets import resolve_position
from ssc.astro.units import deg_to_hours
from ssc.plan.models import (
    AutofocusItem,
    BodyTargetSpec,
    CaptureCalibrationItem,
    CatalogTargetSpec,
    CometTargetSpec,
    DitherItem,
    DitherSpec,
    FilterItem,
    FindHomeItem,
    FixedCountSpec,
    GotoAndStackItem,
    HeaterItem,
    MosaicItem,
    ParkItem,
    Plan,
    PlanItem,
    PolarAlignmentItem,
    SetExposureItem,
    SetGainItem,
    ShutdownItem,
    SlewItem,
    SolveItem,
    StartupItem,
    SyncItem,
    TakeExposuresItem,
    TargetSpec,
    UnparkItem,
    WaitItem,
)
from ssc.scheduler.compiler import CompiledItem, CompiledSchedule
from ssc.scheduler.exposure import cycle_duration_s, whole_cycle_fits
from ssc.scheduler.gates import (
    GateDecision,
    GateSample,
    GateThresholds,
    TargetSnapshot,
    build_gate_sample,
    evaluate_gate,
)
from ssc.weather.base import ForecastSeries

# --- lifecycle vocabulary ----------------------------------------------------


class ItemStatus(StrEnum):
    """DESIGN.md 4.2: ``pending -> waiting -> running -> done | skipped | failed |
    aborted``. ``PENDING`` and ``WAITING`` are never written to the journal — both are
    fully recomputable from ``now`` vs. the schedule (see :func:`decide`'s docstring),
    and journalling "now waiting" would make the journal's *content* depend on
    incidental clock timing at the moment a particular process happened to observe it,
    which a resumed process cannot reproduce. A caller wanting a live "waiting" badge
    derives it: no journal record yet, and ``now < start_utc``."""

    PENDING = "pending"
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    ABORTED = "aborted"


#: Statuses that end an item's turn: the scan for "what should the executor act on
#: next" skips past every item in one of these.
TERMINAL_ITEM_STATUSES: frozenset[ItemStatus] = frozenset(
    {ItemStatus.DONE, ItemStatus.SKIPPED, ItemStatus.FAILED, ItemStatus.ABORTED}
)


class RunStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


class ControlKind(StrEnum):
    """The five controls DESIGN.md 4.2 names: "pause, resume, skip item, stop, retry a
    failed item." Pause/resume/stop are run-level; skip/retry name one ``item_id``."""

    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    SKIP = "skip"
    RETRY = "retry"


class EffectName(StrEnum):
    """The ASCOM/``ssc.*`` vocabulary a :class:`DeviceCommands` port implements
    (DESIGN.md 4.3). ``GATE_PARK`` is the one name here that is not a single ASCOM
    call — it is this module's own composite for "stop any active stack, then park",
    issued when a gate starts a hold."""

    STARTUP = "startup"
    SET_TRACKING_RATE = "set_tracking_rate"
    SET_TRACKING = "set_tracking"
    SLEW_TO = "slew_to"
    START_STACK = "start_stack"
    STOP_STACK = "stop_stack"
    AUTOFOCUS = "autofocus"
    SET_FILTER = "set_filter"
    SET_HEATER = "set_heater"
    SET_EXPOSURE = "set_exposure"
    SET_GAIN = "set_gain"
    SET_DITHER = "set_dither"
    #: One frame of an exposure block: start the exposure, and let the frame's
    #: arrival in the catalogue be its completion (DESIGN.md 4.3.0).
    EXPOSE_FRAME = "expose_frame"
    STOP_LIVE_STACK = "stop_live_stack"
    #: One dither nudge, issued *between* an exposure block's frames on the
    #: block's own cadence — not the ``set_dither`` property write.
    DITHER = "dither"
    CENTER_TARGET = "center_target"
    SOLVE = "solve"
    SYNC_TO = "sync_to"
    PARK = "park"
    UNPARK = "unpark"
    FIND_HOME = "find_home"
    POLAR_ALIGNMENT = "polar_alignment"
    CAPTURE_CALIBRATION = "capture_calibration"
    SHUTDOWN = "shutdown"
    GATE_PARK = "gate_park"


class CompletionMode(StrEnum):
    """How a device call is known to be done. ``AWAIT_RETURN``: the port method's own
    coroutine does not return until the ASCOM call is complete (a synchronous-style
    setter/action). ``DEVICE_EVENT``: the call only *starts* a long-running operation
    (an async ASCOM slew, an autofocus run); completion is a separate event, observed
    through :class:`DeviceEvents`, with ``deadline_s`` as a timeout guard only — never
    a substitute for the event (CLAUDE.md: "a timer is only a timeout guard")."""

    AWAIT_RETURN = "await_return"
    DEVICE_EVENT = "device_event"


@dataclass(frozen=True, slots=True)
class EffectCall:
    """One :class:`DeviceCommands` invocation, durable enough to reissue idempotently.

    ``args`` are JSON-plain (str/float/int/bool/None) so a :class:`Journal` adapter can
    serialise them without knowing the vocabulary.
    """

    name: EffectName
    args: Mapping[str, Any] = field(default_factory=dict)
    completion: CompletionMode = CompletionMode.AWAIT_RETURN
    event_kind: str | None = None
    deadline_s: float | None = None


class EffectOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    WINDOW_ENDED = "window_ended"


# --- the journal ---------------------------------------------------------------


class RecordKind(StrEnum):
    RUN_STARTED = "run_started"
    ITEM_TRANSITION = "item_transition"
    CONTROL = "control"
    RUN_ENDED = "run_ended"
    DAWN_SAFETY = "dawn_safety"


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """One durable transition. Every field beyond ``at``/``run_id``/``kind``/``reason``
    is optional because a single flat record shape serves every kind of transition —
    the alternative, one dataclass per kind, would need a discriminated union
    ``Journal`` adapters have no simpler a job serialising.

    ``item_id`` is ``None`` for a run-level record (``RUN_STARTED``, ``RUN_ENDED``, and
    a ``CONTROL`` record for ``pause``/``resume``/``stop``); it names the item for
    ``skip``/``retry`` and for every ``ITEM_TRANSITION``. ``attempt`` and ``item_id``
    together are the idempotency key CLAUDE.md asks for; ``step`` is the index into that
    item's own phase sequence (see ``_phases_for``), so a resume lands on the right
    mosaic panel as well as the right item.
    """

    at: datetime
    run_id: str
    kind: RecordKind
    reason: str = ""
    item_id: str | None = None
    panel_index: int | None = None
    attempt: int = 0
    step: int = 0
    status: ItemStatus | None = None
    held: bool | None = None
    effect: EffectCall | None = None
    effect_outcome: EffectOutcome | None = None
    control: ControlKind | None = None
    run_status: RunStatus | None = None
    retry_at: datetime | None = None
    recovery_attempts: int = 0
    focus_conditions: bool = False


# --- RunState: the pure fold ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemRunState:
    status: ItemStatus = ItemStatus.PENDING
    attempt: int = 0
    step: int = 0
    panel_index: int | None = None
    pending_effect: EffectCall | None = None
    held: bool = False
    reason: str = ""
    #: The frame index after which this item last dithered, folded from the
    #: dither effect's own durable args. Without it, "dither after every third
    #: frame" would re-decide the same dither on every loop tick until a frame
    #: happened to land — the cadence has to be as durable as the frames are.
    dithered_after_frame: int | None = None
    #: The frame index of the last ``EXPOSE_FRAME`` this item journalled as
    #: *completed*. Not the block's progress — the catalogue is that
    #: (:class:`FrameLedger`) — but the one number that lets the two be
    #: compared, so a frame the journal calls done and the catalogue has never
    #: heard of is a loud failure instead of an infinite re-exposure.
    last_frame_completed: int | None = None
    retry_at: datetime | None = None
    recovery_attempts: int = 0
    focus_conditions: bool = False
    window_exhausted: bool = False
    stack_save_pending: bool = False
    window_cleanup_completed: bool = False


@dataclass(frozen=True, slots=True)
class RunState:
    run_id: str = ""
    run_status: RunStatus = RunStatus.RUNNING
    items: Mapping[str, ItemRunState] = field(default_factory=dict)
    #: A ``stop`` control's own park effect, issued/resolved with the same
    #: issued-then-resolved durability as any item effect, but not scoped to one item.
    pending_run_effect: EffectCall | None = None
    ended_reason: str | None = None
    #: True once a ``RUN_ENDED`` record has been folded. Distinct from
    #: ``run_status``: a run can be ``STOPPED`` (mid park) without yet being
    #: ``ended`` — this is what lets a resume land on an *already-finished* journal
    #: (every item settled, the final record already written) without writing a
    #: second, redundant ``RUN_ENDED``.
    ended: bool = False


def reduce(records: Iterable[JournalRecord]) -> RunState:
    """Fold every record into the run's current position. Deterministic and total over
    any prefix of a valid journal — the property Hypothesis checks."""
    state = RunState()
    for record in records:
        state = _apply(state, record)
    return state


def _apply(state: RunState, record: JournalRecord) -> RunState:
    if record.kind is RecordKind.RUN_STARTED:
        return replace(state, run_id=record.run_id, run_status=RunStatus.RUNNING)
    if record.kind is RecordKind.RUN_ENDED:
        return replace(
            state,
            run_status=record.run_status or state.run_status,
            ended_reason=record.reason,
            ended=True,
        )
    if record.kind is RecordKind.DAWN_SAFETY:
        # Independent safety supervisor intents/results never advance science.
        return state
    if record.kind is RecordKind.CONTROL:
        return _apply_control(state, record)
    if record.kind is RecordKind.ITEM_TRANSITION:
        return _apply_item_transition(state, record)
    raise AssertionError(f"unhandled record kind: {record.kind!r}")  # exhaustiveness guard


def _update_item(
    state: RunState, item_id: str | None, fn: Callable[[ItemRunState], ItemRunState]
) -> RunState:
    if item_id is None:
        raise ValueError("this record kind requires item_id")
    current = state.items.get(item_id, ItemRunState())
    items = dict(state.items)
    items[item_id] = fn(current)
    return replace(state, items=items)


def _apply_item_transition(state: RunState, record: JournalRecord) -> RunState:
    def updater(item: ItemRunState) -> ItemRunState:
        status = record.status if record.status is not None else item.status
        held = record.held if record.held is not None else item.held
        pending_effect = item.pending_effect
        if record.effect is not None and record.effect_outcome is None:
            pending_effect = record.effect
        elif record.effect_outcome is not None:
            pending_effect = None
            if (record.effect is not None
                    and record.effect.name is EffectName.CENTER_TARGET
                    and record.effect.args.get("resume_autofocus_during_capture")
                    and record.effect_outcome is EffectOutcome.COMPLETED):
                # A centering detour must not consume a persisted focus intent.
                # The live quality monitor may be reset by centering/restart.
                pending_effect = EffectCall(
                    name=EffectName.AUTOFOCUS, args={"during_capture": True}
                )
        dithered_after_frame = item.dithered_after_frame
        last_frame_completed = item.last_frame_completed
        if record.effect is not None and record.effect_outcome is EffectOutcome.COMPLETED:
            # Both are read straight back out of the effect's own args, which were
            # durable before the effect was issued — so the fold stays a pure
            # function of the journal and needs no counter of its own.
            if record.effect.name is EffectName.EXPOSE_FRAME:
                last_frame_completed = int(record.effect.args["frame_index"])
            elif record.effect.name is EffectName.DITHER:
                dithered_after_frame = int(record.effect.args["after_frame"])
        return replace(
            item,
            status=status,
            held=held,
            attempt=record.attempt,
            step=record.step,
            panel_index=record.panel_index,
            pending_effect=pending_effect,
            reason=record.reason,
            window_exhausted=item.window_exhausted or record.effect_outcome is EffectOutcome.WINDOW_ENDED,
            stack_save_pending=(
                record.effect_outcome is EffectOutcome.FAILED
                and bool(record.effect.args.get("stack_save_failure", True))
                if record.effect is not None
                and record.effect.name is EffectName.STOP_LIVE_STACK
                and record.effect_outcome is not None
                else item.stack_save_pending
            ),
            window_cleanup_completed=(item.window_cleanup_completed or (
                record.effect is not None
                and record.effect.name is EffectName.STOP_LIVE_STACK
                and bool(record.effect.args.get("window_cleanup"))
                and record.effect_outcome is EffectOutcome.COMPLETED
            )),
            dithered_after_frame=dithered_after_frame,
            last_frame_completed=last_frame_completed,
            retry_at=record.retry_at,
            recovery_attempts=(
                record.recovery_attempts
                if record.effect_outcome is EffectOutcome.FAILED
                else 0
                if record.effect_outcome is EffectOutcome.COMPLETED
                and not (record.effect is not None
                         and record.effect.name is EffectName.CENTER_TARGET
                         and record.effect.args.get("resume_field_verification"))
                else item.recovery_attempts
            ),
            focus_conditions=(
                record.focus_conditions
                if record.effect_outcome is not None
                else item.focus_conditions
            ),
        )

    return _update_item(state, record.item_id, updater)


def _apply_control(state: RunState, record: JournalRecord) -> RunState:
    kind = record.control
    if kind is ControlKind.PAUSE:
        paused = state
        if record.item_id is not None:
            paused = _update_item(
                paused,
                record.item_id,
                lambda item: replace(
                    item,
                    status=record.status or ItemStatus.RUNNING,
                    step=record.step,
                    reason=record.reason,
                ),
            )
        return replace(paused, run_status=RunStatus.PAUSED)
    if kind is ControlKind.RESUME:
        return replace(state, run_status=RunStatus.RUNNING)
    if kind is ControlKind.STOP:
        pending = state.pending_run_effect
        if record.effect is not None and record.effect_outcome is None:
            pending = record.effect
        elif record.effect_outcome is not None:
            pending = None
        return replace(state, run_status=RunStatus.STOPPED, pending_run_effect=pending)
    if kind is ControlKind.SKIP:
        return _update_item(
            state,
            record.item_id,
            lambda item: replace(
                item,
                status=ItemStatus.SKIPPED,
                held=False,
                pending_effect=None,
                reason=record.reason,
            ),
        )
    if kind is ControlKind.RETRY:
        return _update_item(
            state,
            record.item_id,
            lambda item: replace(
                item,
                status=ItemStatus.PENDING,
                step=0,
                panel_index=None,
                held=False,
                pending_effect=None,
                reason=record.reason,
                dithered_after_frame=None,
                last_frame_completed=None,
                retry_at=None,
                recovery_attempts=0,
                focus_conditions=False,
            ),
        )
    raise AssertionError(f"unhandled control kind: {kind!r}")  # exhaustiveness guard


# --- ports (Protocols; backend-core supplies the adapters) ----------------------


class DeviceCommandError(Exception):
    """A :class:`DeviceCommands` call failed. See the module docstring for why this is
    scheduler-owned rather than ``ssc.client.errors.AlpacaClientError``."""

    def __init__(self, message: str, *, recoverable: bool = True) -> None:
        super().__init__(message)
        self.recoverable = recoverable


class DeviceCommandNotImplementedError(DeviceCommandError):
    """The device does not support this operation at all (ASCOM ``NOT_IMPLEMENTED``,
    0x400 — see ``ssc/client/errors.py``). The reason remains visible while
    recovery waits for correction through the panel window."""


class DeviceStackSaveError(DeviceCommandError):
    """An unsaved integration is retained; safe parking remains independent."""


class DeviceFocusConditionsError(DeviceCommandError):
    """A safe autofocus measurement needs more stars or better conditions."""


class DeviceWindowEndedError(DeviceCommandError):
    """Recovery handed control back after a safely stopped science window."""


class ExecutorRecoveryRestartError(Exception):
    """Ask the supervisor to reopen this same durable run after lower-level repair."""


class DeviceEventTimeoutError(Exception):
    """The completion deadline elapsed with no matching device event. A timeout is a
    guard, never evidence of success (CLAUDE.md) — this is raised, not swallowed, and
    the caller marks the item failed with the reason it carries."""


@dataclass(frozen=True, slots=True)
class ResolvedPointing:
    """Where a target actually is, at one instant (DESIGN.md 3.6).

    Produced for a moving target immediately before
    the effect that uses it is journalled, which is the whole feature: the plan
    document holds "the Moon", and this is the answer to "where is it *now*".
    ``at`` is recorded so a run log says which instant the numbers belonged to.
    """

    ra_j2000_h: float
    dec_j2000_deg: float
    tracking_rate: str
    name: str | None
    at: datetime


class TargetResolutionError(Exception):
    """A moving target could not be resolved (no ephemeris, stale/unsafe orbit,
    or a bad site). The item
    fails with this reason; it is never silently pointed somewhere else."""


class TargetResolver(Protocol):
    """Resolves a body reference against an instant.

    A port rather than a direct ``ssc.astro`` call for the same reason every
    other side of this module is one: :func:`decide` is pure, and the resolution
    is an input to it, evaluated by :func:`run` and handed in — exactly like the
    pre-evaluated gate verdict. It is also the seam a test uses to make a body's
    motion deterministic without an ephemeris kernel.
    """

    async def resolve(
        self, target: BodyTargetSpec | CometTargetSpec, at: datetime
    ) -> ResolvedPointing:
        """Raises :class:`TargetResolutionError` on failure; never returns a
        guessed position."""
        ...


@dataclass(frozen=True, slots=True)
class EphemerisTargetResolver:
    """The :class:`TargetResolver` for a real sky: :mod:`ssc.astro` at an instant.

    Lives here rather than in ``ssc/api`` because it is pure — a loaded
    ephemeris and a site in, coordinates out, no I/O and no clock of its own
    (the instant is a parameter). ``backend-core`` constructs one per run from
    the settings location and hands it to :func:`run`; without it, an item
    targeting a body fails with a reason saying exactly that, and never slews.
    """

    ephemeris: Ephemeris
    site: Site
    comet_catalog: CometCatalog | None = None

    async def resolve(
        self, target: BodyTargetSpec | CometTargetSpec, at: datetime
    ) -> ResolvedPointing:
        if isinstance(target, CometTargetSpec):
            if self.comet_catalog is None:
                raise TargetResolutionError("no comet catalogue was supplied to the executor")
            if target.source_url != self.comet_catalog.source_url:
                raise TargetResolutionError(
                    "the plan's comet provenance does not match the bundled JPL catalogue"
                )
            if self.comet_catalog.fetched_at < target.snapshot_fetched_at:
                raise TargetResolutionError(
                    "the bundled comet catalogue is older than the snapshot used to author the plan"
                )
            comet = self.comet_catalog.find(spk_id=target.spk_id, designation=target.designation)
            if comet is None:
                raise TargetResolutionError(
                    f"JPL comet identity {target.designation} / {target.spk_id} is unavailable "
                    "or no longer matches"
                )
            rejection = comet_execution_rejection(self.comet_catalog, comet, at)
            if rejection is not None:
                raise TargetResolutionError(rejection)
            try:
                position = comet_position(self.ephemeris, self.site, comet, at)
            except (ValueError, LookupError) as exc:
                raise TargetResolutionError(str(exc)) from exc
            return ResolvedPointing(
                ra_j2000_h=float(position.ra_j2000_h),
                dec_j2000_deg=float(position.dec_j2000_deg),
                tracking_rate=str(target.tracking_rate),
                name=comet.full_name,
                at=position.when,
            )
        try:
            resolved = resolve_position(self.ephemeris, self.site, target.to_ref(), at)
        except (ValueError, LookupError) as exc:
            raise TargetResolutionError(str(exc)) from exc
        return ResolvedPointing(
            ra_j2000_h=float(resolved.ra_j2000_h),
            dec_j2000_deg=float(resolved.dec_j2000_deg),
            tracking_rate=str(resolved.tracking_rate),
            name=resolved.name,
            at=resolved.when,
        )


class DeviceCommands(Protocol):
    """Async methods in ASCOM/``ssc.*`` vocabulary. Every method either runs to
    completion itself (``AWAIT_RETURN`` effects) or only *starts* the operation, with
    completion observed via :class:`DeviceEvents` (``DEVICE_EVENT`` effects — see
    :class:`CompletionMode`). Implementations raise :class:`DeviceCommandError` (or
    :class:`DeviceCommandNotImplementedError`) on failure, never a bare exception and
    never a silent no-op."""

    async def startup(self) -> None: ...
    async def set_tracking_rate(self, rate: str) -> None: ...
    async def set_tracking(self, enabled: bool) -> None: ...
    async def slew_to(self, ra_j2000_h: float, dec_j2000_deg: float) -> None: ...
    async def start_stack(
        self, *, target_name: str | None, ra_j2000_h: float, dec_j2000_deg: float
    ) -> None: ...
    async def stop_stack(self) -> None: ...
    async def autofocus(self) -> None: ...
    async def set_filter(self, position: str) -> None: ...
    async def set_heater(self, on: bool) -> None: ...
    async def set_exposure(self, exposure_sec: float) -> None: ...
    async def set_gain(self, gain: int) -> None: ...
    async def set_dither(self, dither_pixels: float | None) -> None: ...
    async def solve(self) -> None: ...
    async def sync_to(self, ra_j2000_h: float | None, dec_j2000_deg: float | None) -> None: ...
    async def ensure_safety_parked(self) -> None: ...
    async def park(self) -> str | None: ...
    async def unpark(self) -> None: ...
    async def find_home(self) -> None: ...
    async def capture_calibration(self, **args: Any) -> None: ...
    async def shutdown(self) -> None: ...
    async def polar_alignment(self, **args: Any) -> None: ...


class FrameCountError(Exception):
    """The catalogue's frame count could not be read. The block fails with this
    reason: an unknown count is never treated as zero (CLAUDE.md invariant 7),
    because zero would re-shoot a block that is already three-quarters done."""


class CapturePort(Protocol):
    """The exposure block's own seam (DESIGN.md 4.3.0) — shoot a frame, dither,
    and say how many frames of a block the catalogue already holds.

    Separate from :class:`DeviceCommands` on purpose. Those are ASCOM/``ssc.*``
    verbs against the mount and camera; these three are the capture *pipeline* —
    the thing that writes a FITS and a catalogue row is the same thing that can
    count them, and it is one adapter for ``backend-core`` to wire rather than
    two. Keeping them apart also means a run that schedules no exposure block
    needs no capture wiring at all.

    Without this port an exposure block fails with a reason naming it, and never
    silently does nothing (CLAUDE.md invariant 7).
    """

    async def expose_frame(
        self,
        *,
        exposure_sec: float,
        gain: int,
        frame_type: str,
        filter_name: str | None,
        block_id: str,
        frame_index: int,
        retain: bool = True,
        center_after_drift: bool = False,
        drift_threshold_pixels: float = 400.0,
        object_name: str | None = None,
        live_stack: bool = False,
        dither_every_n_frames: int | None = None,
    ) -> None:
        """Start **one** frame of an exposure block.

        Like every other ``DEVICE_EVENT`` effect this only *starts* the
        operation: completion is the ``"frame_saved"`` event, and the adapter
        must not raise it until the frame's catalogue row is committed — the
        executor compares that count against what the journal says completed,
        and treats a disagreement as a hard error rather than re-exposing.

        ``block_id`` is the plan item's own id, and it is what tags the frame as
        belonging to this block; ``frame_index`` is 0-based within it.
        """
        ...

    async def stop_live_stack(self, *, require_retained: bool = False) -> None: ...

    async def focus_required(self) -> bool: ...

    async def autofocus(self) -> None: ...

    async def center_target(self, *, ra_j2000_h: float, dec_j2000_deg: float) -> None:
        """Plate-solve and apply bounded correction slews to an imaging target."""
        ...

    async def recover_dither(self, *, object_name: str | None) -> None:
        """Verify an ambiguous prior dither without issuing another offset."""
        ...

    async def recover_operation(self, *, level: int, operation: str) -> None:
        """Reconcile camera/link ownership without resetting saved acquisition."""
        ...

    async def dither(
        self, *, pixels: float | None, max_pixels: float | None, object_name: str | None
    ) -> None:
        """One dither nudge, between frames, on the block's own cadence.

        ``pixels`` is ``None`` when the block named no amount, meaning "the
        active optic profile's ``dither_pixels``" — resolved by the adapter,
        which can read settings, never here (CLAUDE.md invariant 3). This is an
        ``AWAIT_RETURN`` effect: the firmware surfaces no dither-complete event,
        so the adapter must not return until the mount has settled.
        """
        ...

    async def frames_captured(self, *, run_id: str, item_id: str) -> int:
        """How many frames of this block are **durably catalogued**.

        The executor's answer to "where was I", and deliberately not a counter
        it keeps: DESIGN.md 4.3.0 — "resume counts frames, not items ... the
        durable record is the catalogue rows". A block of 60 interrupted after
        37 resumes at 38 because 37 rows exist, not because anything wrote 37
        down. Counted per ``run_id``/``item_id``, so a ``retry`` control
        *continues* a block rather than re-shooting frames already on disk.

        Raises :class:`FrameCountError` if the count cannot be read; never
        returns a guess.
        """
        ...


class DeviceEvents(Protocol):
    async def wait_for(self, event_kind: str, *, item_id: str, deadline: datetime) -> None:
        """Block until ``event_kind`` is observed for ``item_id``, or raise
        :class:`DeviceEventTimeoutError` at ``deadline`` (UTC). Never returns normally
        on a timeout."""
        ...


class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep_until(self, when: datetime) -> None: ...


@dataclass(frozen=True, slots=True)
class ControlCommand:
    kind: ControlKind
    item_id: str | None = None
    reason: str = ""


class ControlSource(Protocol):
    async def poll(self) -> ControlCommand | None:
        """A pending control command, or ``None``. Must not block — the driver calls
        this once per loop tick between decisions, so pause/skip/stop/retry are noticed
        at the next decision boundary (scheduled autofocus has a service-owned,
        durable Stop interruption path; there is no generic cancel() in
        :class:`DeviceCommands`)."""
        ...


class Journal(Protocol):
    async def append(self, record: JournalRecord) -> None: ...
    async def read_all(self, run_id: str) -> Sequence[JournalRecord]: ...


class GateInputs(Protocol):
    """The weather + astro data a gate check needs, each fetched independently: the
    forecast is whole-sky and slow-changing; the target snapshot is per-item and cheap
    to recompute every sample."""

    async def forecast(self) -> ForecastSeries: ...
    async def target_snapshot(
        self, *, ra_j2000_h: float, dec_j2000_deg: float, at: datetime
    ) -> TargetSnapshot: ...


# --- effect deadlines (timeout guards only) --------------------------------------


@dataclass(frozen=True, slots=True)
class EffectDeadlines:
    """Timeout guards for ``DEVICE_EVENT`` effects. Firing one is a failure, never a
    silent success (CLAUDE.md) — see :class:`DeviceEventTimeoutError`."""

    startup_s: float = 300.0
    autofocus_s: float = 180.0
    slew_s: float = 120.0
    solve_s: float = 60.0
    capture_calibration_s: float = 900.0
    #: Added to a frame's own exposure time to get its guard: a 300 s sub gets
    #: 360 s. Sized off the measured ~4 s cycle overhead with a wide margin, so
    #: firing it means something is actually wrong — and when it fires, the
    #: frame fails with that reason and the block stops. It never counts as a
    #: frame (architecture policy: "a timer is only a timeout guard").
    frame_margin_s: float = 60.0


DEFAULT_DEADLINES: EffectDeadlines = EffectDeadlines()
#: The default hysteresis band, shared so ``run()``'s own default does not construct a
#: fresh instance in the signature (ruff B008).
DEFAULT_GATE_THRESHOLDS: GateThresholds = GateThresholds()

#: Effects whose failure is *information about the device*, not a reason to
#: abandon the item: the outcome and its reason are journalled exactly as any
#: other, and then the item carries on.
#:
#: ``SET_TRACKING_RATE`` is the only member and the reason is concrete. SSC's own
#: driver reports the S30 Pro on firmware 8.46 as sidereal-only —
#: ``Telescope.TrackingRates`` is ``[DRIVE_SIDEREAL]`` and the ``TrackingRate``
#: setter is refused as not implemented (``ssc/alpaca/devices/telescope.py``).
#: Failing a Moon item because the mount cannot be *told* to track lunar would be
#: the wrong call: sidereal tracking plus the item's own re-centring segments
#: (``compiler.MAX_BODY_DRIFT_FRACTION_OF_FRAME``) still images the Moon, and the
#: alternative is a plan that cannot run at all on the hardware SSC ships for.
#: What must not happen is the drift going unexplained — so the refusal, in the
#: driver's own words, lands in the run log, which is the product (architecture policy).
ADVISORY_EFFECTS: frozenset[EffectName] = frozenset({EffectName.SET_TRACKING_RATE})

#: Effects that happen *inside* a phase rather than being one: completing one
#: leaves the item's durable ``step`` exactly where it was. ``GATE_PARK`` is a
#: detour out of a ``RunUntilPhase``; ``EXPOSE_FRAME`` and ``DITHER`` are the
#: repeating body of an :class:`ExposeLoopPhase`, whose repetitions are counted
#: by the catalogue (:class:`FrameLedger`) and never by a phase index.
NON_ADVANCING_EFFECTS: frozenset[EffectName] = frozenset(
    {EffectName.GATE_PARK, EffectName.EXPOSE_FRAME, EffectName.DITHER}
)


# --- phases: the pure per-item step sequence ------------------------------------


@dataclass(frozen=True, slots=True)
class SleepPhase:
    until_utc: datetime
    panel_index: int | None = None


@dataclass(frozen=True, slots=True)
class RunUntilPhase:
    """Run (stacking) until ``end_utc`` or a gate says stop — the item's own compiled
    duration is the completion signal here, not a device event (compiler.py:
    ``duration_min`` is "an estimate for Gantt display ... never a completion
    signal" for the *item*, but the session's *own* planned length is exactly what
    decides when to call ``stop_stack``)."""

    end_utc: datetime
    panel_index: int | None = None


@dataclass(frozen=True, slots=True)
class EffectPhase:
    effect: EffectCall
    panel_index: int | None = None


@dataclass(frozen=True, slots=True)
class ExposeLoopPhase:
    """An exposure block's frame loop (DESIGN.md 4.3.0) — one phase, however
    many frames.

    It is deliberately *not* expanded into one ``EffectPhase`` per frame. Two
    reasons, and both are the point of the item: an open-ended block has no
    count to expand, and a fixed-count block's progress is the catalogue's to
    report, not a phase index's. So the loop keeps one durable ``step`` and
    each frame is journalled at that same step, with its own frame index in the
    effect's args.

    ``window_end_utc`` is a *hard* window and ``window_required`` says whether
    the plan asked for one. A fixed count with no end of its own has neither:
    the user asked for sixty frames, not for a slot, so nothing cuts it short.
    An open-ended block has both, and the loop ends when a whole cycle no
    longer fits (never when "there is still time").
    """

    count: int | None
    exposure_sec: float
    gain: int
    frame_type: str
    filter_name: str | None
    dither_pixels: float | None
    dither_min_pixels: float | None
    dither_max_pixels: float | None
    dither_every_n_frames: int | None
    center_after_drift: bool
    drift_threshold_pixels: float
    window_end_utc: datetime | None
    window_required: bool
    frame_deadline_s: float
    object_name: str | None = None
    live_stack: bool = False
    panel_index: int | None = None


@dataclass(frozen=True, slots=True)
class OperatorCheckpointPhase:
    instructions: str


Phase = SleepPhase | RunUntilPhase | EffectPhase | ExposeLoopPhase | OperatorCheckpointPhase


def _simple_effect(
    name: EffectName, args: Mapping[str, Any], *, panel_index: int | None = None
) -> EffectPhase:
    return EffectPhase(effect=EffectCall(name=name, args=dict(args)), panel_index=panel_index)


def _slew_and_stack_phases(
    *,
    ra_j2000_h: float,
    dec_j2000_deg: float,
    target_name: str | None,
    end_utc: datetime,
    panel_index: int | None,
    deadlines: EffectDeadlines,
    autofocus_before: bool = False,
    live_stack: bool = True,
    exposure_sec: float = 10.0,
    gain: int = 200,
    filter_name: str | None = None,
    dither: DitherSpec | None = None,
) -> list[Phase]:
    phases: list[Phase] = [
        # A retry starts this phase sequence from the beginning while the
        # capture adapter can still own the live-stack reference from the
        # previous attempt.  Finalise that stack before moving the optical
        # field.  The adapter deliberately treats this as a no-op when no
        # scheduled stack is active, so the same phase is safe for a fresh
        # item and between normally completed panels.
        EffectPhase(
            effect=EffectCall(name=EffectName.STOP_LIVE_STACK, args={"if_active": True}),
            panel_index=panel_index,
        ),
        EffectPhase(
            effect=EffectCall(
                name=EffectName.SLEW_TO,
                args={"ra_j2000_h": ra_j2000_h, "dec_j2000_deg": dec_j2000_deg},
                completion=CompletionMode.DEVICE_EVENT,
                event_kind="slew_complete",
                deadline_s=deadlines.slew_s,
            ),
            panel_index=panel_index,
        ),
    ]
    if filter_name is not None:
        phases.append(
            _simple_effect(
                EffectName.SET_FILTER, {"position": filter_name}, panel_index=panel_index
            )
        )
    phases.append(
        EffectPhase(
            effect=EffectCall(
                name=EffectName.CENTER_TARGET,
                args={"ra_j2000_h": ra_j2000_h, "dec_j2000_deg": dec_j2000_deg},
            ),
            panel_index=panel_index,
        )
    )
    if autofocus_before:
        phases.append(
            EffectPhase(
                # The SSC autofocus runner returns only after its focus curve
                # completes; firmware exposes no separate completion signal.
                effect=EffectCall(name=EffectName.AUTOFOCUS),
                panel_index=panel_index,
            )
        )
    phases.append(_simple_effect(EffectName.SET_GAIN, {"gain": gain}, panel_index=panel_index))
    # Live stacks use SSC's capture pipeline, not the unverified firmware
    # ``ssc.start_stack``/``ssc.set_exposure`` actions. The exposure duration
    # travels with every Camera.StartExposure call and each committed frame is
    # then added to the same live-stack engine used by the Capture screen.
    phases.append(
        ExposeLoopPhase(
            count=None,
            exposure_sec=exposure_sec,
            gain=gain,
            frame_type="light",
            filter_name=filter_name,
            dither_pixels=dither.pixels if dither else None,
            dither_min_pixels=dither.min_pixels if dither else None,
            dither_max_pixels=dither.max_pixels if dither else None,
            dither_every_n_frames=dither.every_n_frames if dither else None,
            center_after_drift=False,
            drift_threshold_pixels=400.0,
            window_end_utc=end_utc,
            window_required=True,
            frame_deadline_s=exposure_sec + deadlines.frame_margin_s,
            object_name=target_name,
            live_stack=live_stack,
            panel_index=panel_index,
        )
    )
    return phases


@dataclass(frozen=True, slots=True)
class _Pointing:
    """An item's target reduced to what the phase builder needs."""

    ra_j2000_h: float
    dec_j2000_deg: float
    tracking_rate: str
    name: str | None


def _pointing_for(plan_item: PlanItem, resolved: ResolvedPointing | None) -> _Pointing | None:
    """The item's target as coordinates, or ``None`` if it has none to give.

    A catalogue target answers from the document. A body target answers only
    from ``resolved`` — the ephemeris evaluation :func:`run` performed for *this
    tick*, moments before the effect that will carry it is journalled. There is
    deliberately no fallback: a body with no resolution has no position, and the
    caller fails the item rather than pointing the scope somewhere plausible.
    """
    target: TargetSpec | None = getattr(plan_item, "target", None)
    if isinstance(target, CatalogTargetSpec):
        return _Pointing(
            ra_j2000_h=target.ra_j2000_h,
            dec_j2000_deg=target.dec_j2000_deg,
            tracking_rate=str(target.tracking_rate),
            name=target.name,
        )
    if isinstance(target, BodyTargetSpec | CometTargetSpec):
        if resolved is None:
            return None
        return _Pointing(
            ra_j2000_h=resolved.ra_j2000_h,
            dec_j2000_deg=resolved.dec_j2000_deg,
            tracking_rate=resolved.tracking_rate,
            name=resolved.name,
        )
    return None


def _tracking_rate_phase(pointing: _Pointing) -> EffectPhase:
    """Set the drive rate the target implies, before pointing at it.

    Issued for every target-bearing item, not only for a body one: the rate is a
    property of the target (DESIGN.md 3.6, "a body target and its tracking rate
    are one decision, not two"), so an M42 item after a Moon item has to put the
    mount back on the sidereal rate. Leaving it would make the *next* target
    drift for a reason nothing in the run log explains.
    """
    return _simple_effect(EffectName.SET_TRACKING_RATE, {"rate": pointing.tracking_rate})


def _phases_for(
    item: CompiledItem,
    plan_item: PlanItem,
    deadlines: EffectDeadlines = DEFAULT_DEADLINES,
    resolved: ResolvedPointing | None = None,
) -> tuple[Phase, ...]:
    """The item's whole execution as an ordered, pure phase sequence.

    Phase 0 is always "wait for ``start_utc``" — common to every item kind, so it is
    built here rather than per-kind. Only ``EffectPhase`` ever needs a durable
    ``step``/effect-issued record; ``SleepPhase`` and ``RunUntilPhase`` are recomputed
    live from ``now``/the gate on every call to :func:`decide` (see its docstring).

    ``resolved`` is this tick's answer for a body target. The *shape* of the
    sequence never depends on it — same length, same order, same step indices —
    only the coordinates inside an ``EffectPhase`` do, which is what makes it
    safe to rebuild this on every call while ``step`` stays a durable index.
    A body's coordinates therefore enter the journal exactly once, as the args
    of the effect that was journalled before being issued.
    """
    if item.start_utc is None:
        return ()

    def timeout(default_s: float) -> float:
        """Per-item guard; it never substitutes for a completion event."""
        return default_s if plan_item.timeout_min is None else plan_item.timeout_min * 60.0

    pointing = _pointing_for(plan_item, resolved)
    if (
        isinstance(plan_item, SlewItem)
        and plan_item.destination == "polar_best"
        and item.target is not None
        and item.target.ra_j2000_deg is not None
        and item.target.dec_j2000_deg is not None
    ):
        pointing = _Pointing(
            ra_j2000_h=float(deg_to_hours(item.target.ra_j2000_deg)),  # type: ignore[arg-type]
            dec_j2000_deg=item.target.dec_j2000_deg,
            tracking_rate=item.target.tracking_rate,
            name=item.target.name,
        )
    phases: list[Phase] = [SleepPhase(until_utc=item.start_utc)]

    if isinstance(plan_item, StartupItem):
        phases.append(
            EffectPhase(
                effect=EffectCall(
                    name=EffectName.STARTUP,
                    completion=CompletionMode.DEVICE_EVENT,
                    event_kind="startup_complete",
                    deadline_s=timeout(deadlines.startup_s),
                )
            )
        )
        phases.append(_simple_effect(EffectName.UNPARK, {}))
        if pointing is not None:
            phases.append(_tracking_rate_phase(pointing))
            phases.append(
                EffectPhase(
                    effect=EffectCall(
                        name=EffectName.SLEW_TO,
                        args={
                            "ra_j2000_h": pointing.ra_j2000_h,
                            "dec_j2000_deg": pointing.dec_j2000_deg,
                        },
                        completion=CompletionMode.DEVICE_EVENT,
                        event_kind="slew_complete",
                        deadline_s=timeout(deadlines.slew_s),
                    )
                )
            )
        phases.append(_simple_effect(EffectName.SET_FILTER, {"position": "IRCUT"}))
        phases.append(
            EffectPhase(
                effect=EffectCall(
                    name=EffectName.AUTOFOCUS,
                    completion=CompletionMode.AWAIT_RETURN,
                )
            )
        )
        polar_ra = plan_item.target_ra_hours
        polar_dec = plan_item.target_dec_deg
        polar_start_point = plan_item.polar_start_point or "best"
        if polar_start_point == "explicit" and pointing is not None:
            polar_ra = polar_ra if polar_ra is not None else pointing.ra_j2000_h
            polar_dec = polar_dec if polar_dec is not None else pointing.dec_j2000_deg
        polar_args = {
            "point_count": plan_item.polar_point_count or 5,
            "step_deg": plan_item.polar_step_deg or 15.0,
            "exposure_s": plan_item.polar_exposure_s or 2.0,
            "gain": plan_item.polar_gain or 200,
            "start_point": polar_start_point,
            "start_ra_hours": polar_ra,
            "start_dec_deg": polar_dec,
        }
        phases.append(_simple_effect(EffectName.POLAR_ALIGNMENT, polar_args))
        phases.append(_simple_effect(EffectName.SET_GAIN, {"gain": 200}))
        phases.append(
            EffectPhase(
                effect=EffectCall(
                    name=EffectName.EXPOSE_FRAME,
                    args={
                        "exposure_sec": 10.0,
                        "gain": 200,
                        "frame_type": "light",
                        "filter_name": "IRCUT",
                        "block_id": plan_item.id,
                        "frame_index": 0,
                        "retain": False,
                    },
                    completion=CompletionMode.AWAIT_RETURN,
                    event_kind="frame_saved",
                    deadline_s=deadlines.frame_margin_s + 10.0,
                )
            )
        )
        phases.append(
            EffectPhase(
                effect=EffectCall(
                    name=EffectName.SOLVE,
                    completion=CompletionMode.DEVICE_EVENT,
                    event_kind="solve_complete",
                    deadline_s=timeout(deadlines.solve_s),
                )
            )
        )
    elif isinstance(plan_item, WaitItem):
        if plan_item.requires_confirmation:
            phases.append(
                OperatorCheckpointPhase(
                    instructions=plan_item.instructions
                    or "Review the physical setup, then Resume to accept and continue."
                )
            )
        if item.end_utc is not None:
            phases.append(SleepPhase(until_utc=item.end_utc))
    elif isinstance(plan_item, AutofocusItem):
        phases.append(
            EffectPhase(
                effect=EffectCall(
                    name=EffectName.AUTOFOCUS,
                    completion=CompletionMode.AWAIT_RETURN,
                )
            )
        )
    elif isinstance(plan_item, GotoAndStackItem):
        if item.end_utc is not None and pointing is not None:
            phases.append(_tracking_rate_phase(pointing))
            if item.panels:
                # Re-centring segments (compiler.py's
                # ``MAX_BODY_DRIFT_FRACTION_OF_FRAME``): each one is a fresh
                # slew, and because ``run`` re-resolves the body every tick, the
                # segment that is *about* to be issued carries a position no
                # older than this tick. That is also the bound on a resumed
                # run's pointing error — see :func:`run`'s docstring.
                for segment_index, segment in enumerate(item.panels):
                    phases.extend(
                        _slew_and_stack_phases(
                            ra_j2000_h=pointing.ra_j2000_h,
                            dec_j2000_deg=pointing.dec_j2000_deg,
                            target_name=pointing.name,
                            end_utc=segment.end_utc,
                            panel_index=segment_index,
                            deadlines=deadlines,
                            autofocus_before=plan_item.autofocus_before,
                            exposure_sec=plan_item.exposure_sec,
                            gain=plan_item.gain,
                            filter_name=plan_item.filter_name,
                            dither=plan_item.dither,
                            live_stack=plan_item.live_stack,
                        )
                    )
            else:
                phases.extend(
                    _slew_and_stack_phases(
                        ra_j2000_h=pointing.ra_j2000_h,
                        dec_j2000_deg=pointing.dec_j2000_deg,
                        target_name=pointing.name,
                        end_utc=item.end_utc,
                        panel_index=None,
                        deadlines=deadlines,
                        autofocus_before=plan_item.autofocus_before,
                        exposure_sec=plan_item.exposure_sec,
                        gain=plan_item.gain,
                        filter_name=plan_item.filter_name,
                        dither=plan_item.dither,
                        live_stack=plan_item.live_stack,
                    )
                )
    elif isinstance(plan_item, MosaicItem):
        # ``item.panels`` is already in the row-major emission order
        # ``mosaic_panel_centers`` produces, so its own tuple position is a stable,
        # dependency-free panel index — no need to reconstruct one from row/col.
        if pointing is not None:
            phases.append(_tracking_rate_phase(pointing))
        for panel_index, panel in enumerate(item.panels):
            if panel.ra_j2000_deg is None or panel.dec_j2000_deg is None:
                # A coordinate-less panel is a body re-centring segment, and the
                # compiler refuses a mosaic on a body — reaching here means the
                # compiled schedule and the plan disagree.
                raise AssertionError("a mosaic panel has no compiled centre")
            phases.extend(
                _slew_and_stack_phases(
                    ra_j2000_h=float(deg_to_hours(panel.ra_j2000_deg)),  # type: ignore[arg-type]
                    dec_j2000_deg=panel.dec_j2000_deg,
                    target_name=plan_item.target_name,
                    end_utc=panel.end_utc,
                    panel_index=panel_index,
                    deadlines=deadlines,
                    exposure_sec=plan_item.exposure_sec,
                    gain=plan_item.gain,
                    filter_name=plan_item.filter_name,
                    dither=plan_item.dither,
                    live_stack=plan_item.live_stack,
                )
            )
    elif isinstance(plan_item, SetExposureItem):
        phases.append(
            _simple_effect(EffectName.SET_EXPOSURE, {"exposure_sec": plan_item.exposure_sec})
        )
    elif isinstance(plan_item, SetGainItem):
        phases.append(_simple_effect(EffectName.SET_GAIN, {"gain": plan_item.gain}))
    elif isinstance(plan_item, FilterItem):
        phases.append(_simple_effect(EffectName.SET_FILTER, {"position": plan_item.position}))
    elif isinstance(plan_item, HeaterItem):
        phases.append(_simple_effect(EffectName.SET_HEATER, {"on": plan_item.heater_on}))
    elif isinstance(plan_item, DitherItem):
        phases.append(
            _simple_effect(EffectName.SET_DITHER, {"dither_pixels": plan_item.dither_pixels})
        )
    elif isinstance(plan_item, SolveItem):
        phases.append(
            EffectPhase(
                effect=EffectCall(
                    name=EffectName.SOLVE,
                    completion=CompletionMode.DEVICE_EVENT,
                    event_kind="solve_complete",
                    deadline_s=timeout(deadlines.solve_s),
                )
            )
        )
    elif isinstance(plan_item, SyncItem):
        # No target at all means "sync to the last plate solve"; a body target
        # means "sync to where the body is right now", which is what ``pointing``
        # already holds.
        args: dict[str, Any] = {"ra_j2000_h": None, "dec_j2000_deg": None}
        if pointing is not None:
            args = {
                "ra_j2000_h": pointing.ra_j2000_h,
                "dec_j2000_deg": pointing.dec_j2000_deg,
            }
        phases.append(_simple_effect(EffectName.SYNC_TO, args))
    elif isinstance(plan_item, SlewItem):
        if pointing is not None:
            if plan_item.unpark_before:
                phases.append(_simple_effect(EffectName.UNPARK, {}))
            phases.append(_tracking_rate_phase(pointing))
            phases.append(
                EffectPhase(
                    effect=EffectCall(
                        name=EffectName.SLEW_TO,
                        args={
                            "ra_j2000_h": pointing.ra_j2000_h,
                            "dec_j2000_deg": pointing.dec_j2000_deg,
                        },
                        completion=CompletionMode.DEVICE_EVENT,
                        event_kind="slew_complete",
                        deadline_s=timeout(deadlines.slew_s),
                    )
                )
            )
    elif isinstance(plan_item, FindHomeItem):
        phases.append(_simple_effect(EffectName.FIND_HOME, {}))
        if plan_item.park_after:
            phases.append(_simple_effect(EffectName.PARK, {}))
    elif isinstance(plan_item, PolarAlignmentItem):
        phases.append(
            _simple_effect(
                EffectName.POLAR_ALIGNMENT,
                {
                    "point_count": plan_item.point_count,
                    "step_deg": plan_item.step_deg,
                    "exposure_s": plan_item.exposure_s,
                    "gain": plan_item.gain,
                    "start_point": plan_item.start_point,
                    "start_ra_hours": plan_item.start_ra_hours,
                    "start_dec_deg": plan_item.start_dec_deg,
                },
            )
        )
    elif isinstance(plan_item, ParkItem):
        phases.append(_simple_effect(EffectName.PARK, {}))
    elif isinstance(plan_item, UnparkItem):
        phases.append(_simple_effect(EffectName.UNPARK, {}))
    elif isinstance(plan_item, CaptureCalibrationItem):
        if plan_item.safe_state:
            if plan_item.filter_name is not None:
                phases.append(
                    _simple_effect(EffectName.SET_FILTER, {"position": plan_item.filter_name})
                )
            phases.append(_simple_effect(EffectName.SET_TRACKING, {"enabled": False}))
            phases.append(
                EffectPhase(
                    effect=EffectCall(
                        name=EffectName.FIND_HOME,
                        completion=CompletionMode.DEVICE_EVENT,
                        event_kind="home_complete",
                        deadline_s=timeout(deadlines.slew_s),
                    )
                )
            )
            phases.append(
                EffectPhase(
                    effect=EffectCall(
                        name=EffectName.PARK,
                        completion=CompletionMode.DEVICE_EVENT,
                        event_kind="park_complete",
                        deadline_s=timeout(deadlines.slew_s),
                    )
                )
            )
        phases.append(
            EffectPhase(
                effect=EffectCall(
                    name=EffectName.CAPTURE_CALIBRATION,
                    args={
                        "frame_type": plan_item.frame_type,
                        "count": plan_item.count,
                        "exposure_sec": plan_item.exposure_sec,
                        "gain": plan_item.gain,
                        "filter_name": plan_item.filter_name,
                        "optic_id": plan_item.optic_id,
                        "flat_source": plan_item.flat_source,
                        "generate_master": plan_item.generate_master,
                    },
                )
            )
        )
    elif isinstance(plan_item, TakeExposuresItem):
        # The block sets what it needs, every time (DESIGN.md 4.3.0): filter,
        # exposure and gain go to the device before this block's first frame,
        # whatever the previous block left behind. That is the state leak the
        # item exists to close, and it costs three property writes.
        if pointing is not None:
            phases.append(_simple_effect(EffectName.UNPARK, {}))
            phases.append(_tracking_rate_phase(pointing))
            phases.append(
                EffectPhase(
                    effect=EffectCall(
                        name=EffectName.SLEW_TO,
                        args={
                            "ra_j2000_h": pointing.ra_j2000_h,
                            "dec_j2000_deg": pointing.dec_j2000_deg,
                        },
                        completion=CompletionMode.DEVICE_EVENT,
                        event_kind="slew_complete",
                        deadline_s=timeout(deadlines.slew_s),
                    )
                )
            )
        if plan_item.filter_name is not None:
            phases.append(
                _simple_effect(EffectName.SET_FILTER, {"position": plan_item.filter_name})
            )
        phases.append(_simple_effect(EffectName.SET_GAIN, {"gain": plan_item.gain}))
        if plan_item.dither is not None and (
            plan_item.dither.pixels is not None or plan_item.dither.min_pixels is None
        ):
            phases.append(
                _simple_effect(EffectName.SET_DITHER, {"dither_pixels": plan_item.dither.pixels})
            )
        phases.append(
            ExposeLoopPhase(
                count=(
                    plan_item.count.frames if isinstance(plan_item.count, FixedCountSpec) else None
                ),
                exposure_sec=plan_item.exposure_sec,
                gain=plan_item.gain,
                frame_type=str(plan_item.frame_type.value),
                filter_name=plan_item.filter_name,
                dither_pixels=plan_item.dither.pixels if plan_item.dither else None,
                dither_min_pixels=plan_item.dither.min_pixels if plan_item.dither else None,
                dither_max_pixels=plan_item.dither.max_pixels if plan_item.dither else None,
                dither_every_n_frames=(
                    plan_item.dither.every_n_frames if plan_item.dither else None
                ),
                center_after_drift=plan_item.center_after_drift is not None
                and plan_item.center_after_drift.enabled,
                drift_threshold_pixels=(
                    plan_item.center_after_drift.threshold_pixels
                    if plan_item.center_after_drift
                    else 400.0
                ),
                # Only an end the *plan* asked for bounds the loop. A fixed-count
                # block with no end compiles a derived ``end_utc`` for the Gantt
                # and for chaining (compiler._exposure_block), and that estimate
                # must never cut the count short.
                window_end_utc=item.end_utc if plan_item.end is not None else None,
                window_required=plan_item.end is not None,
                frame_deadline_s=plan_item.exposure_sec + deadlines.frame_margin_s,
                object_name=None if pointing is None else pointing.name,
                live_stack=plan_item.live_stack,
            )
        )
    elif isinstance(plan_item, ShutdownItem):
        # Safe shutdown is expressed entirely through standard Alpaca state and
        # its authoritative completion members.  The firmware-specific
        # ``ssc.shutdown`` action is intentionally not trusted here.
        phases.append(_simple_effect(EffectName.SET_TRACKING, {"enabled": False}))
        phases.append(
            EffectPhase(
                effect=EffectCall(
                    name=EffectName.FIND_HOME,
                    completion=CompletionMode.DEVICE_EVENT,
                    event_kind="home_complete",
                    deadline_s=timeout(deadlines.slew_s),
                )
            )
        )
        phases.append(
            EffectPhase(
                effect=EffectCall(
                    name=EffectName.PARK,
                    completion=CompletionMode.DEVICE_EVENT,
                    event_kind="park_complete",
                    deadline_s=timeout(deadlines.slew_s),
                )
            )
        )
    else:
        raise AssertionError(
            f"unhandled plan item kind: {plan_item.kind!r}"
        )  # exhaustiveness guard

    return tuple(phases)


def _slew_step_index(phases: Sequence[Phase], from_step: int, panel_index: int | None) -> int:
    """Search backward from ``from_step`` for the ``SLEW_TO`` phase of the panel
    currently in progress — where a gate resume must restart from, since parking during
    a hold loses the mount's pointing."""
    for idx in range(from_step, -1, -1):
        phase = phases[idx]
        if (
            isinstance(phase, EffectPhase)
            and phase.effect.name is EffectName.SLEW_TO
            and phase.panel_index == panel_index
        ):
            return idx
    raise AssertionError("no preceding slew phase to resume a gate hold from")


def _item_window_end(item: CompiledItem, panel_index: int | None) -> datetime | None:
    if panel_index is not None and item.panels:
        return item.panels[panel_index].end_utc
    return item.end_utc


def _recovery_window_end(
    item: CompiledItem, plan_item: PlanItem | None, panel_index: int | None
) -> datetime | None:
    if isinstance(plan_item, GotoAndStackItem | MosaicItem) or (
        isinstance(plan_item, TakeExposuresItem) and plan_item.end is not None
    ):
        return _item_window_end(item, panel_index)
    return None


# --- decide(): the pure decision function ----------------------------------------


@dataclass(frozen=True, slots=True)
class WaitUntil:
    item_id: str
    until_utc: datetime
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class IssueEffect:
    item_id: str
    step: int
    panel_index: int | None
    attempt: int
    effect: EffectCall
    #: True when this exact effect was already journalled as issued (a prior process
    #: crashed before resolving it) — the driver must reissue it without writing a new
    #: "issued" record, since one already exists.
    resumed: bool
    new_status: ItemStatus | None


@dataclass(frozen=True, slots=True)
class HoldForGate:
    item_id: str
    step: int
    panel_index: int | None
    reason: str
    #: True the first time the hold is decided (not yet durable) — the driver must
    #: journal it and issue ``GATE_PARK`` before parking; False once already held and
    #: still holding, where there is nothing new to journal.
    starting: bool


@dataclass(frozen=True, slots=True)
class ResumeFromGate:
    item_id: str
    step: int
    panel_index: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class ConfirmFrame:
    """A frame journalled as issued, whose row the catalogue already holds.

    The crash landed between "issued" and "resolved" and the frame itself is
    safe on disk. Re-exposing would put a duplicate in the catalogue, which is
    the one thing the generic reissue rule must not do here — a duplicate slew
    is a slew, but a duplicate frame is a frame. So the driver journals the
    outcome and moves on, and the run log says why.
    """

    item_id: str
    step: int
    panel_index: int | None
    attempt: int
    effect: EffectCall
    reason: str


@dataclass(frozen=True, slots=True)
class AdvancePhase:
    """Durably finish an exposure loop without terminating later panels."""

    item_id: str
    step: int
    panel_index: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class CompleteItem:
    item_id: str
    panel_index: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class SkipItem:
    item_id: str
    panel_index: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class FailItem:
    item_id: str
    panel_index: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class AbortItem:
    item_id: str
    panel_index: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class PausedWait:
    reason: str


@dataclass(frozen=True, slots=True)
class AwaitOperator:
    item_id: str
    step: int
    reason: str


@dataclass(frozen=True, slots=True)
class RunEnded:
    reason: str
    #: A ``stop``'s park effect that was journalled as issued but never resolved
    #: (crash mid-park) — the driver must reissue it before the run is truly over.
    pending_run_effect: EffectCall | None = None


Decision = (
    WaitUntil
    | IssueEffect
    | ConfirmFrame
    | HoldForGate
    | ResumeFromGate
    | AdvancePhase
    | CompleteItem
    | SkipItem
    | FailItem
    | AbortItem
    | PausedWait
    | AwaitOperator
    | RunEnded
)


def _current_item(schedule: CompiledSchedule, state: RunState) -> CompiledItem | None:
    """The first enabled item not yet in a terminal status — the executor is
    strictly sequential (DESIGN.md 4.2: "a supervisor loop that steps the compiled
    schedule"), never concurrent across items."""
    for item in schedule.items:
        if not item.enabled:
            continue
        status = state.items.get(item.item_id, ItemRunState()).status
        if status not in TERMINAL_ITEM_STATUSES:
            return item
    return None


def decide(
    state: RunState,
    *,
    schedule: CompiledSchedule,
    plan_items: Mapping[str, PlanItem],
    now: datetime,
    gate: GateDecision | None,
    deadlines: EffectDeadlines = DEFAULT_DEADLINES,
    resolved: ResolvedPointing | None = None,
    resolution_error: str | None = None,
    frames_captured: int | None = None,
    frames_error: str | None = None,
    focus_required: bool = False,
) -> Decision:
    """The one place that decides what happens next. Pure: a function of ``state``
    (itself a pure fold of the journal), the compiled schedule, the plan, ``now``, a
    pre-evaluated ``gate`` verdict (only meaningful while the current item is
    stacking), a pre-evaluated ``resolved`` body position (only meaningful for an
    item whose target is a body) and a pre-evaluated ``frames_captured`` count from
    the catalogue (only meaningful for an exposure block) — no clock, no device, no
    ephemeris, no database and no journal write happens here.
    """
    if state.ended:
        # Already fully finished (including any pending run-level effect) in a prior
        # process — resuming from a journal whose last record is already
        # ``RUN_ENDED`` must not write a second one.
        return RunEnded(reason=state.ended_reason or "already ended", pending_run_effect=None)
    if state.run_status is RunStatus.STOPPED:
        return RunEnded(
            reason=state.ended_reason or "stopped by control",
            pending_run_effect=state.pending_run_effect,
        )
    if state.run_status is RunStatus.PAUSED:
        return PausedWait(reason="run is paused")

    current = _current_item(schedule, state)
    if current is None:
        return RunEnded(reason="all items settled")

    item_id = current.item_id
    item_state = state.items.get(item_id, ItemRunState())
    recovery_end = _recovery_window_end(current, plan_items.get(item_id), item_state.panel_index)
    if item_state.retry_at is not None and recovery_end is None:
        recovery_end = schedule.night.dark_end_utc or schedule.night.window_end_utc
    if (item_state.retry_at is not None and recovery_end is not None
        and now >= recovery_end and not item_state.stack_save_pending
        and not (item_state.pending_effect is not None
                 and item_state.pending_effect.name is EffectName.DITHER)):
        recovery_phases = _phases_for(current, plan_items[item_id], deadlines, resolved)
        if (item_state.pending_effect is not None
                and item_state.pending_effect.name is EffectName.STOP_LIVE_STACK):
            return AdvancePhase(item_id=item_id, step=item_state.step + 1,
                                panel_index=item_state.panel_index,
                                reason="window ended with stack capability unavailable; proceeding to safe cleanup")
        if (item_state.step < len(recovery_phases)
                and isinstance(recovery_phases[item_state.step], EffectPhase)
                and recovery_phases[item_state.step].effect.name is EffectName.PARK):
            # Confirmed safe state is independent of science window expiry.
            if now < item_state.retry_at:
                return WaitUntil(item_id=item_id, until_utc=item_state.retry_at)
            return IssueEffect(item_id=item_id, step=item_state.step,
                               panel_index=item_state.panel_index,
                               attempt=item_state.attempt + 1,
                               effect=recovery_phases[item_state.step].effect,
                               resumed=False, new_status=ItemStatus.RUNNING)
        exposure_step = next((index for index, phase in enumerate(recovery_phases)
                              if index >= item_state.step
                              and isinstance(phase, ExposeLoopPhase)
                              and phase.panel_index == item_state.panel_index), None)
        if exposure_step is not None:
            return AdvancePhase(item_id=item_id, step=exposure_step,
                                panel_index=item_state.panel_index,
                                reason="scheduled panel window ended during recovery; closing saved acquisition")
        return CompleteItem(item_id=item_id, panel_index=item_state.panel_index,
                            reason="scheduled window ended while operation unavailable; saved frames retained")
    if item_state.retry_at is not None and now < item_state.retry_at:
        return WaitUntil(item_id=item_id, until_utc=item_state.retry_at)
    if (
        current.start_utc is not None
        and current.end_utc is not None
        and current.end_utc > current.start_utc
        and current.end_utc < now < schedule.night.window_end_utc
        and now >= schedule.night.window_start_utc
        and item_id not in state.items
    ):
        return SkipItem(
            item_id=item_id,
            panel_index=None,
            reason=(
                "scheduled window ended before this run started; skipped to begin "
                "from the current time"
            ),
        )
    plan_item = plan_items.get(item_id)
    if plan_item is None or (isinstance(plan_item, MosaicItem) and not current.panels):
        reason = (f"item {item_id!r} is in the compiled schedule but absent from the plan"
                  if plan_item is None else "mosaic item has no expanded panels; see its compile diagnostics")
        end = current.end_utc or schedule.night.window_end_utc
        if now < end:
            return WaitUntil(item_id=item_id, until_utc=min(end, now + timedelta(seconds=30)),
                             reason=f"Configuration recovery waiting: {reason}")
        return CompleteItem(item_id=item_id, panel_index=None,
                            reason=f"window ended while configuration unavailable: {reason}")

    moving_target = getattr(plan_item, "target", None)
    if isinstance(moving_target, BodyTargetSpec | CometTargetSpec) and resolved is None:
        # A body target with no resolution has no position. Before the item is
        # due that is simply "not yet asked"; once it is due it is a hard
        # failure, never a slew to a stale or guessed place (architecture policy
        # invariant 7).
        if current.start_utc is not None and now < current.start_utc:
            return WaitUntil(item_id=item_id, until_utc=current.start_utc)
        end = current.end_utc or schedule.night.window_end_utc
        if now < end:
            return WaitUntil(item_id=item_id, until_utc=min(end, now + timedelta(seconds=30)),
                             reason=resolution_error or "Target unavailable; waiting until window end")
        if item_state.stack_save_pending and item_state.retry_at is not None and now < item_state.retry_at:
            return WaitUntil(item_id=item_id, until_utc=item_state.retry_at, reason=item_state.reason)
        if getattr(plan_item, "live_stack", False) and not item_state.window_cleanup_completed:
            return IssueEffect(item_id=item_id, step=item_state.step,
                               panel_index=item_state.panel_index,
                               attempt=item_state.attempt + 1,
                               effect=EffectCall(name=EffectName.STOP_LIVE_STACK,
                                                args={"window_cleanup": True, "if_active": True,
                                                      "require_retained": bool(frames_captured)
                                                      or item_state.last_frame_completed is not None}),
                               resumed=False, new_status=ItemStatus.RUNNING)
        return CompleteItem(item_id=item_id, panel_index=None,
                            reason=resolution_error or "target remained unavailable through scheduled window")

    phases = _phases_for(current, plan_item, deadlines, resolved)
    if not phases:
        end = current.end_utc or schedule.night.window_end_utc
        if now < end:
            return WaitUntil(item_id=item_id, until_utc=min(end, now + timedelta(seconds=30)),
                             reason="Item timing unavailable; waiting until window end")
        return CompleteItem(item_id=item_id, panel_index=None,
                            reason="item timing unavailable through scheduled window")

    step = min(item_state.step, len(phases))

    if item_state.pending_effect is not None:
        # An effect (an ordinary item effect, or a gate's own GATE_PARK) was
        # journalled as issued but never resolved — a prior process crashed between
        # those two writes. Reissue the exact same effect, same attempt: this is the
        # idempotency key architecture policy asks for, and it is checked before everything
        # else so a crash mid-gate-park resumes the park, not a fresh decision about
        # whether to hold at all.
        #
        # DITHER is reconciled without replay by _issue_effect: its physical
        # dispatch outcome is unknown. A frame is another exception, settled
        # by the catalogue: if the row for
        # this frame index already exists, the exposure completed and only the second
        # journal write was lost. Reissuing then would duplicate a *file*, not just a
        # command, so it is confirmed instead of repeated (DESIGN.md 4.3.0).
        pending = item_state.pending_effect
        if pending.name is EffectName.DITHER and item_state.retry_at is not None and now < item_state.retry_at:
            return WaitUntil(item_id=item_id, until_utc=item_state.retry_at)
        if pending.name is EffectName.EXPOSE_FRAME and frames_captured is not None:
            frame_index = int(pending.args["frame_index"])
            if frames_captured > frame_index:
                return ConfirmFrame(
                    item_id=item_id,
                    step=step,
                    panel_index=item_state.panel_index,
                    attempt=item_state.attempt,
                    effect=pending,
                    reason=(
                        f"frame {frame_index} is already in the catalogue "
                        f"({frames_captured} frames recorded for this block); its "
                        "outcome was never journalled, so it is confirmed here rather "
                        "than re-exposed"
                    ),
                )
        return IssueEffect(
            item_id=item_id,
            step=step,
            panel_index=item_state.panel_index,
            attempt=item_state.attempt,
            effect=item_state.pending_effect,
            resumed=True,
            new_status=None,
        )

    if item_state.held and item_state.retry_at is not None:
        return IssueEffect(
            item_id=item_id,
            step=step,
            panel_index=item_state.panel_index,
            attempt=item_state.attempt + 1,
            effect=EffectCall(name=EffectName.GATE_PARK),
            resumed=False,
            new_status=None,
        )

    if item_state.held:
        return _decide_held(current, item_state, phases, step, now=now, gate=gate)

    if step >= len(phases):
        return CompleteItem(
            item_id=item_id, panel_index=item_state.panel_index,
            reason="observing window ended during recovery; saved frames retained" if item_state.window_exhausted else "all phases complete"
        )

    # Skip forward through any Sleep/RunUntil phases already elapsed. This never
    # touches the journal: neither phase kind has a side effect, so recomputing "which
    # phase applies right now" from scratch on every call is always safe, including
    # right after a crash.
    idx = step
    while idx < len(phases):
        phase = phases[idx]
        if isinstance(phase, SleepPhase):
            if now < phase.until_utc:
                return WaitUntil(item_id=item_id, until_utc=phase.until_utc)
            idx += 1
            continue
        if isinstance(phase, RunUntilPhase):
            if gate is not None and gate.hold:
                return HoldForGate(
                    item_id=item_id,
                    step=idx,
                    panel_index=phase.panel_index,
                    reason=gate.reason,
                    starting=True,
                )
            if now < phase.end_utc:
                return WaitUntil(item_id=item_id, until_utc=phase.end_utc)
            idx += 1
            continue
        if isinstance(phase, ExposeLoopPhase):
            # Every exposure loop advances to following panels or segments. A
            # live-stack loop first emits its final save/stop effect.
            exposure_decision = _decide_exposure(
                item_id,
                phase,
                item_state,
                idx,
                now=now,
                frames_captured=frames_captured,
                frames_error=frames_error,
                focus_required=focus_required or item_state.focus_conditions,
            )
            if isinstance(exposure_decision, FailItem):
                # Unknown catalogue progress/configuration is not permission to
                # expose. Re-read on a bounded cadence until this window closes.
                end = phase.window_end_utc or current.end_utc or schedule.night.window_end_utc
                if now < end:
                    return WaitUntil(item_id=item_id, until_utc=min(end, now + timedelta(seconds=30)),
                                     reason=f"Acquisition blocked; retrying until window end: {exposure_decision.reason}")
                if phase.live_stack:
                    return IssueEffect(item_id=item_id, step=idx,
                                       panel_index=phase.panel_index,
                                       attempt=item_state.attempt + 1,
                                       effect=EffectCall(name=EffectName.STOP_LIVE_STACK,
                                                       args={"require_retained": bool(frames_captured)
                                                             or item_state.last_frame_completed is not None}),
                                       resumed=False, new_status=ItemStatus.RUNNING)
                exposure_decision = AdvancePhase(item_id=item_id, step=idx + 1,
                                                  panel_index=phase.panel_index,
                                                  reason="window ended while acquisition blocked; saved frames retained")
            if isinstance(exposure_decision, AdvancePhase) and idx + 1 >= len(phases):
                return CompleteItem(item_id=item_id, panel_index=phase.panel_index,
                                    reason=exposure_decision.reason)
            return exposure_decision
        if isinstance(phase, OperatorCheckpointPhase):
            return AwaitOperator(item_id=item_id, step=idx + 1, reason=phase.instructions)
        break

    if idx >= len(phases):
        return CompleteItem(
            item_id=item_id, panel_index=item_state.panel_index,
            reason="observing window ended during recovery; saved frames retained" if item_state.window_exhausted else "all phases complete"
        )

    phase = phases[idx]
    assert isinstance(phase, EffectPhase)  # the loop above only stops here
    new_status = None if item_state.status is ItemStatus.RUNNING else ItemStatus.RUNNING
    return IssueEffect(
        item_id=item_id,
        step=idx,
        panel_index=phase.panel_index,
        attempt=item_state.attempt + 1,
        effect=phase.effect,
        resumed=False,
        new_status=new_status,
    )


def _dither_pixels_for_frame(
    *,
    item_id: str,
    frame_index: int,
    fixed_pixels: float | None,
    min_pixels: float | None,
    max_pixels: float | None,
) -> float | None:
    """Choose and return one durable amount for a dither operation.

    The choice is derived from the item/frame identity rather than process
    RNG, so a crash-resume decision remains reproducible while successive
    frames still receive different values across the configured range.
    """
    if min_pixels is None or max_pixels is None:
        return fixed_pixels
    digest = hashlib.blake2b(f"{item_id}:{frame_index}".encode(), digest_size=8).digest()
    fraction = int.from_bytes(digest, "big") / float(2**64 - 1)
    return round(min_pixels + (max_pixels - min_pixels) * fraction, 3)


def _decide_exposure(
    item_id: str,
    phase: ExposeLoopPhase,
    item_state: ItemRunState,
    step: int,
    *,
    now: datetime,
    frames_captured: int | None,
    frames_error: str | None,
    focus_required: bool = False,
) -> Decision:
    """One tick of an exposure block's frame loop (DESIGN.md 4.3.0).

    Pure, like everything else :func:`decide` calls: the catalogue's frame count
    arrives as ``frames_captured``, already read by :func:`run`. Every exit is a
    decision with a reason attached, because the run log is the product — "the
    block finished", "the window closed after 42 of 60", "the catalogue does not
    show a frame the journal says completed" are three different mornings.
    """
    if phase.count is None and not phase.window_required:
        return FailItem(
            item_id=item_id,
            panel_index=phase.panel_index,
            reason=(
                "this block exposes until its window closes but has no end, so there "
                "is no number of frames to take; give it a duration or an event end"
            ),
        )
    if phase.window_required and phase.window_end_utc is None:
        return FailItem(
            item_id=item_id,
            panel_index=phase.panel_index,
            reason=(
                "this block's end did not resolve, so its window is unknown; see the "
                "item's compile diagnostics"
            ),
        )
    if frames_captured is None:
        return FailItem(
            item_id=item_id,
            panel_index=phase.panel_index,
            reason=frames_error
            or (
                "an exposure block's progress is the catalogue's frame count, but no "
                "frame ledger was supplied to the executor"
            ),
        )
    if (
        item_state.last_frame_completed is not None
        and frames_captured <= item_state.last_frame_completed
    ):
        # The journal says frame N completed; the catalogue has never heard of it.
        # Exposing again would loop on the same index forever, so this stops instead
        # and says exactly what disagrees (architecture policy: fail loudly and specifically).
        return FailItem(
            item_id=item_id,
            panel_index=phase.panel_index,
            reason=(
                f"frame {item_state.last_frame_completed} completed but the catalogue "
                f"holds only {frames_captured} frames for this block; refusing to "
                "re-expose the same frame index in a loop"
            ),
        )

    if phase.count is not None and frames_captured >= phase.count:
        if phase.live_stack:
            return IssueEffect(
                item_id=item_id,
                step=step,
                panel_index=phase.panel_index,
                attempt=item_state.attempt + 1,
                effect=EffectCall(name=EffectName.STOP_LIVE_STACK,
                    args={"require_retained": frames_captured > 0}),
                resumed=False,
                new_status=None,
            )
        return AdvancePhase(
            item_id=item_id,
            step=step + 1,
            panel_index=phase.panel_index,
            reason=f"{frames_captured} of {phase.count} frames captured",
        )

    cycle_s = cycle_duration_s(phase.exposure_sec)
    if phase.window_end_utc is not None:
        remaining_s = (phase.window_end_utc - now).total_seconds()
        if not whole_cycle_fits(remaining_s, phase.exposure_sec):
            of_count = f" of {phase.count}" if phase.count is not None else ""
            reason = (
                f"the block's window closed after {frames_captured}{of_count} frames: "
                f"{max(remaining_s, 0.0):.0f} s left and a whole cycle needs "
                f"{cycle_s:.0f} s ({phase.exposure_sec:g} s exposure + download)"
            )
            if phase.live_stack:
                return IssueEffect(
                    item_id=item_id,
                    step=step,
                    panel_index=phase.panel_index,
                    attempt=item_state.attempt + 1,
                    effect=EffectCall(name=EffectName.STOP_LIVE_STACK,
                        args={"reason": reason, "require_retained": frames_captured > 0}),
                    resumed=False,
                    new_status=None,
                )
            return AdvancePhase(
                item_id=item_id,
                step=step + 1,
                panel_index=phase.panel_index,
                reason=reason,
            )

    if phase.live_stack and focus_required:
        return IssueEffect(
            item_id=item_id,
            step=step,
            panel_index=phase.panel_index,
            attempt=item_state.attempt + 1,
            effect=EffectCall(name=EffectName.AUTOFOCUS, args={"during_capture": True}),
            resumed=False,
            new_status=None,
        )

    every = phase.dither_every_n_frames
    if (
        every is not None
        and frames_captured > 0
        and frames_captured % every == 0
        and item_state.dithered_after_frame != frames_captured
    ):
        pixels = _dither_pixels_for_frame(
            item_id=item_id,
            frame_index=frames_captured,
            fixed_pixels=phase.dither_pixels,
            min_pixels=phase.dither_min_pixels,
            max_pixels=phase.dither_max_pixels,
        )
        return IssueEffect(
            item_id=item_id,
            step=step,
            panel_index=phase.panel_index,
            attempt=item_state.attempt + 1,
            effect=EffectCall(
                name=EffectName.DITHER,
                args={
                    "pixels": pixels,
                    "max_pixels": phase.dither_max_pixels or phase.dither_pixels,
                    "object_name": phase.object_name,
                    "after_frame": frames_captured,
                },
            ),
            resumed=False,
            new_status=None,
        )

    expose_args: dict[str, Any] = {
        "block_id": item_id,
        "frame_index": frames_captured,
        "exposure_sec": phase.exposure_sec,
        "gain": phase.gain,
        "frame_type": phase.frame_type,
        "filter_name": phase.filter_name,
        "center_after_drift": phase.center_after_drift,
        "drift_threshold_pixels": phase.drift_threshold_pixels,
    }
    if phase.object_name is not None:
        expose_args["object_name"] = phase.object_name
    if phase.live_stack:
        expose_args["live_stack"] = True
    if phase.dither_every_n_frames is not None:
        expose_args["dither_every_n_frames"] = phase.dither_every_n_frames
    return IssueEffect(
        item_id=item_id,
        step=step,
        panel_index=phase.panel_index,
        attempt=item_state.attempt + 1,
        effect=EffectCall(
            name=EffectName.EXPOSE_FRAME,
            args=expose_args,
            completion=CompletionMode.DEVICE_EVENT,
            event_kind="frame_saved",
            deadline_s=phase.frame_deadline_s,
        ),
        resumed=False,
        new_status=None if item_state.status is ItemStatus.RUNNING else ItemStatus.RUNNING,
    )


def _decide_held(
    item: CompiledItem,
    item_state: ItemRunState,
    phases: Sequence[Phase],
    step: int,
    *,
    now: datetime,
    gate: GateDecision | None,
) -> Decision:
    item_id = item.item_id
    if gate is None:
        return HoldForGate(
            item_id=item_id,
            step=step,
            panel_index=item_state.panel_index,
            reason="awaiting a fresh gate sample",
            starting=False,
        )
    if gate.hold:
        return HoldForGate(
            item_id=item_id,
            step=step,
            panel_index=item_state.panel_index,
            reason=gate.reason,
            starting=False,
        )
    window_end = _item_window_end(item, item_state.panel_index)
    if window_end is not None and now < window_end:
        resume_step = _slew_step_index(phases, step, item_state.panel_index)
        return ResumeFromGate(
            item_id=item_id,
            step=resume_step,
            panel_index=item_state.panel_index,
            reason=gate.reason,
        )
    return AbortItem(
        item_id=item_id,
        panel_index=item_state.panel_index,
        reason=f"gate cleared ({gate.reason}) but the item's window had already ended",
    )


# --- the driver: journal -> act -> journal ----------------------------------------

#: How often the loop wakes to re-check controls and (while gate-relevant) sample
#: conditions, while otherwise sleeping toward a future ``WaitUntil``/``RunUntil``
#: target. Not a completion signal for anything — see the module docstring.
DEFAULT_POLL_INTERVAL_S: float = 30.0


async def _dispatch(
    device: DeviceCommands, capture: CapturePort | None, effect: EffectCall
) -> str | None:
    """Call the one :class:`DeviceCommands` method (or, for ``GATE_PARK``, the two
    calls) an :class:`EffectCall` names. Raises whatever the port raises — this
    function does not catch anything; the caller decides how a failure becomes a
    journal record."""
    name = effect.name
    args = effect.args
    if name is EffectName.STARTUP:
        await device.startup()
    elif name is EffectName.SET_TRACKING_RATE:
        await device.set_tracking_rate(args["rate"])
    elif name is EffectName.SET_TRACKING:
        await device.set_tracking(args["enabled"])
    elif name is EffectName.SLEW_TO:
        await device.slew_to(args["ra_j2000_h"], args["dec_j2000_deg"])
    elif name is EffectName.START_STACK:
        await device.start_stack(
            target_name=args.get("target_name"),
            ra_j2000_h=args["ra_j2000_h"],
            dec_j2000_deg=args["dec_j2000_deg"],
        )
    elif name is EffectName.STOP_STACK:
        await device.stop_stack()
    elif name is EffectName.AUTOFOCUS:
        if args.get("during_capture"):
            if capture is None:
                raise DeviceCommandNotImplementedError("capture autofocus monitor is unavailable")
            await capture.autofocus()
        else:
            await device.autofocus()
    elif name is EffectName.SET_FILTER:
        await device.set_filter(args["position"])
    elif name is EffectName.SET_HEATER:
        await device.set_heater(args["on"])
    elif name is EffectName.SET_EXPOSURE:
        await device.set_exposure(args["exposure_sec"])
    elif name is EffectName.SET_GAIN:
        await device.set_gain(args["gain"])
    elif name is EffectName.SET_DITHER:
        await device.set_dither(args.get("dither_pixels"))
    elif name is EffectName.EXPOSE_FRAME:
        if capture is None:
            raise DeviceCommandNotImplementedError(
                "this run has no capture port, so no frame can be exposed"
            )
        expose_args = {
            "exposure_sec": args["exposure_sec"],
            "gain": args["gain"],
            "frame_type": args["frame_type"],
            "filter_name": args.get("filter_name"),
            "block_id": args["block_id"],
            "frame_index": args["frame_index"],
            **(
                {
                    "center_after_drift": args["center_after_drift"],
                    "drift_threshold_pixels": args["drift_threshold_pixels"],
                }
                if args.get("center_after_drift")
                else {}
            ),
            "object_name": args.get("object_name"),
            "live_stack": args.get("live_stack", False),
        }
        if "retain" in args:
            expose_args["retain"] = args["retain"]
        if "dither_every_n_frames" in args:
            expose_args["dither_every_n_frames"] = args["dither_every_n_frames"]
        await capture.expose_frame(**expose_args)
    elif name is EffectName.STOP_LIVE_STACK:
        if capture is None:
            if args.get("if_active") and not args.get("require_retained"):
                return None
            raise DeviceCommandNotImplementedError(
                "this run has no capture port to stop live stacking"
            )
        if args.get("require_retained"):
            restore_stack = getattr(capture, "restore_live_stack", None)
            if restore_stack is not None and args.get("block_id") is not None:
                await restore_stack(args["block_id"], args.get("stack_panel_index"))
            await capture.stop_live_stack(require_retained=True)
        else:
            await capture.stop_live_stack()
    elif name is EffectName.CENTER_TARGET:
        if capture is None:
            raise DeviceCommandNotImplementedError(
                "this run has no capture port for closed-loop target centering"
            )
        await capture.center_target(
            ra_j2000_h=args["ra_j2000_h"], dec_j2000_deg=args["dec_j2000_deg"]
        )
    elif name is EffectName.DITHER:
        if capture is None:
            raise DeviceCommandNotImplementedError(
                "this run has no capture port, so it cannot dither between frames"
            )
        await capture.dither(
            pixels=args.get("pixels"),
            max_pixels=args.get("max_pixels"),
            object_name=args.get("object_name"),
        )
    elif name is EffectName.SOLVE:
        await device.solve()
    elif name is EffectName.SYNC_TO:
        await device.sync_to(args.get("ra_j2000_h"), args.get("dec_j2000_deg"))
    elif name is EffectName.PARK:
        return await device.park()
    elif name is EffectName.UNPARK:
        unpark = getattr(device, "unpark", None)
        if unpark is not None:
            # Older Seestar firmware has no verified unpark verb. Startup
            # already opens the observing view, so absence of this optional
            # ASCOM capability must not fail the rest of startup.
            with contextlib.suppress(DeviceCommandNotImplementedError):
                await unpark()
    elif name is EffectName.FIND_HOME:
        await device.find_home()
    elif name is EffectName.POLAR_ALIGNMENT:
        polar_alignment = getattr(device, "polar_alignment", None)
        if polar_alignment is not None:
            await polar_alignment(**args)
    elif name is EffectName.CAPTURE_CALIBRATION:
        await device.capture_calibration(**args)
    elif name is EffectName.SHUTDOWN:
        await device.shutdown()
    elif name is EffectName.GATE_PARK:
        # Best-effort "stop whatever is running, then park" — see the module
        # docstring's note on why this is not wrapped in its own idempotency games
        # beyond the ordinary issued/resolved journal pair every effect gets.
        save_error = None
        if capture is not None:
            try:
                await capture.stop_live_stack()
            except DeviceCommandNotImplementedError:
                pass
            except DeviceStackSaveError as exc:
                save_error = str(exc)
        with contextlib.suppress(DeviceCommandNotImplementedError):
            await device.stop_stack()
        parked = await device.park()
        if save_error is not None:
            raise DeviceStackSaveError(f"safe park completed; integration retained unsaved: {save_error}")
        return parked
    else:
        raise AssertionError(f"unhandled effect name: {name!r}")  # exhaustiveness guard
    return None


async def _issue_effect(
    journal: Journal,
    device: DeviceCommands,
    events: DeviceEvents,
    clock: Clock,
    run_id: str,
    decision: IssueEffect,
    *,
    capture: CapturePort | None = None,
    held: bool | None = None,
    recovery_deadline: datetime | None = None,
    supervisor_restarts: bool = False,
) -> RunState:
    """Durable-before-acted-on, for one effect: journal the issue (unless it is
    already durable from a crashed prior attempt), perform it, journal the outcome.

    A :data:`NON_ADVANCING_EFFECTS` member's successful completion does not advance
    the item's phase ``step``: ``GATE_PARK`` is a detour taken *from* a
    ``RunUntilPhase``, and a frame or a dither is one repetition of an
    ``ExposeLoopPhase``. Both leave ``step`` exactly where it was.
    """
    is_gate_park = decision.effect.name is EffectName.GATE_PARK
    # EXPOSE_FRAME normally belongs to an ExposeLoopPhase and the catalogue,
    # not the phase index, advances that loop. Startup's final confirmation
    # image is deliberately a one-off AWAIT_RETURN EffectPhase, however; it
    # must advance or the scheduler will take that disposable frame forever.
    is_one_off_frame = (
        decision.effect.name is EffectName.EXPOSE_FRAME
        and decision.effect.completion is CompletionMode.AWAIT_RETURN
    )
    is_non_advancing = (decision.effect.name in NON_ADVANCING_EFFECTS and not is_one_off_frame) or (
        decision.effect.name is EffectName.AUTOFOCUS
        and bool(decision.effect.args.get("during_capture"))
    ) or (
        decision.effect.name is EffectName.CENTER_TARGET
        and bool(decision.effect.args.get("resume_field_verification"))
    )
    previous_state = reduce(await journal.read_all(run_id)).items.get(decision.item_id)
    if decision.effect.name is EffectName.CAPTURE_CALIBRATION and not decision.resumed:
        decision = replace(decision, effect=replace(decision.effect, args={
            **decision.effect.args, "run_id": run_id, "block_id": decision.item_id,
            "catalogue_resumable": True,
        }))
    if (decision.effect.name is EffectName.STOP_LIVE_STACK
        and previous_state is not None and (
            previous_state.stack_save_pending
            or (decision.resumed and not decision.effect.args.get("if_active")
                and previous_state.last_frame_completed is not None)
        )):
        decision = replace(decision, effect=replace(decision.effect,
            args={**decision.effect.args, "require_retained": True}))
    if capture is not None and decision.effect.name is EffectName.EXPOSE_FRAME:
        stack_context = getattr(capture, "set_stack_panel_context", None)
        if stack_context is not None:
            stack_context(decision.panel_index, decision.effect.args.get("frame_index"))
    if (decision.effect.name is EffectName.STOP_LIVE_STACK
        and decision.effect.args.get("require_retained") and capture is not None):
        decision = replace(decision, effect=replace(decision.effect,
            args={**decision.effect.args, "block_id": decision.item_id,
                  "stack_panel_index": decision.panel_index}))
    is_advisory = decision.effect.name in ADVISORY_EFFECTS
    if not decision.resumed:
        await journal.append(
            JournalRecord(
                at=clock.now(),
                run_id=run_id,
                kind=RecordKind.ITEM_TRANSITION,
                item_id=decision.item_id,
                panel_index=decision.panel_index,
                attempt=decision.attempt,
                step=decision.step,
                status=decision.new_status,
                held=held,
                effect=decision.effect,
                effect_outcome=None,
                reason=(f"issuing {decision.effect.name.value} (attempt {decision.attempt}); "
                        f"recovery level {min(previous_state.recovery_attempts, 3) if previous_state else 0}; "
                        f"window end {recovery_deadline.isoformat() if recovery_deadline else 'unknown'}"),
            )
        )

    outcome: EffectOutcome | None
    reason: str
    focus_conditions = False
    stack_save_failure = False
    unsupported = False
    try:
        if (previous_state is not None and previous_state.recovery_attempts > 0
                and previous_state.retry_at is not None
                and not decision.effect.args.get("recovery_hold")
                and decision.effect.name not in {EffectName.STOP_LIVE_STACK, EffectName.PARK,
                                                EffectName.GATE_PARK, EffectName.CAPTURE_CALIBRATION,
                                                EffectName.POLAR_ALIGNMENT, EffectName.SYNC_TO,
                                                EffectName.DITHER}):
            level = min(previous_state.recovery_attempts, 3)
            if (supervisor_restarts and not decision.resumed
                    and previous_state.recovery_attempts % 3 == 0):
                raise ExecutorRecoveryRestartError(
                    f"level 3 supervisor restart after {previous_state.recovery_attempts} failures "
                    f"of {decision.effect.name.value}; same run, panel, saved frames and intent retained"
                )
            if capture is not None:
                await capture.recover_operation(level=level, operation=decision.effect.name.value)
        if decision.effect.args.get("recovery_hold"):
            raise DeviceCommandNotImplementedError("operation remains held for safe operator recovery")
        if decision.resumed and decision.effect.name is EffectName.DITHER:
            if capture is None:
                raise DeviceCommandNotImplementedError("ambiguous dither recovery requires a capture port")
            await capture.recover_dither(object_name=decision.effect.args.get("object_name"))
            completion_reason = (
                "ambiguous prior dither reconciled without another offset; "
                "fresh stopped tracking and image stability verified; requested offset "
                "completion unknown; planned field centering required before science"
            )
        else:
            completion_reason = await _dispatch(device, capture, decision.effect)
        if decision.effect.completion is CompletionMode.DEVICE_EVENT:
            deadline = clock.now() + timedelta(seconds=decision.effect.deadline_s or 0.0)
            await events.wait_for(
                decision.effect.event_kind or "", item_id=decision.item_id, deadline=deadline
            )
        outcome, reason = EffectOutcome.COMPLETED, completion_reason or f"{decision.effect.name.value} completed"
    except DeviceWindowEndedError as exc:
        outcome, reason = EffectOutcome.WINDOW_ENDED, str(exc)
    except DeviceEventTimeoutError as exc:
        outcome = EffectOutcome.FAILED
        reason = f"timed out waiting for {decision.effect.event_kind}: {exc}"
    except DeviceCommandNotImplementedError as exc:
        unsupported = True
        outcome = EffectOutcome.FAILED
        reason = f"not implemented: {exc}"
    except DeviceFocusConditionsError as exc:
        outcome = EffectOutcome.FAILED
        reason = f"waiting for autofocus stars/conditions: {exc}"
        focus_conditions = True
    except DeviceCommandError as exc:
        stack_save_failure = isinstance(exc, DeviceStackSaveError)
        outcome = EffectOutcome.FAILED
        reason = str(exc)

    failed = outcome is EffectOutcome.FAILED
    if failed and is_advisory:
        reason = (
            f"{reason} — continuing at the mount's current rate; the target will drift "
            "instead, and a body item is re-centred on its own schedule regardless"
        )
    # An advisory failure still advances the phase and leaves the item running:
    # the effect is over, and it was never the item's purpose.
    next_step = (
        decision.step if is_non_advancing or (failed and not is_advisory) else decision.step + 1
    )
    next_status = ItemStatus.FAILED if failed and not is_gate_park and not is_advisory else None
    retry_at = None
    recovery_attempts = 0
    # Relative moves, unscoped calibration and PA cannot be blindly replayed.
    # Scoped calibration instead verifies its saved catalogue and captures
    # only the missing frames under the same immutable intent.
    if (
        failed
        and not is_advisory
        and (decision.effect.name not in {
            EffectName.DITHER,
            EffectName.CAPTURE_CALIBRATION,
            EffectName.POLAR_ALIGNMENT,
            EffectName.SYNC_TO,
        } or (decision.effect.name is EffectName.CAPTURE_CALIBRATION
              and decision.effect.args.get("catalogue_resumable")))
    ):
        previous = reduce(await journal.read_all(run_id)).items[decision.item_id]
        recovery_attempts = previous.recovery_attempts + 1
        delay_s = 600 if focus_conditions else (5, 15, 30, 60)[min(recovery_attempts - 1, 3)]
        candidate = clock.now() + timedelta(seconds=delay_s)
        retry_at = (candidate if stack_save_failure
                    or recovery_deadline is None else min(candidate, recovery_deadline))
        next_status = ItemStatus.RUNNING
        reason += f"; automatic recovery {recovery_attempts} scheduled at {retry_at.isoformat()}"
    if failed and decision.effect.name is EffectName.PARK:
        # Safe-state failure is not an observing panel expiry. Never report a
        # failed park as completed merely because the night's deadline passed.
        retry_at = (clock.now() + timedelta(seconds=5)
                    if recovery_attempts <= 3 else None)
        next_status = ItemStatus.RUNNING if retry_at is not None else ItemStatus.FAILED
    if failed and decision.effect.name is EffectName.STOP_LIVE_STACK:
        decision = replace(decision, effect=replace(decision.effect,
                           args={**decision.effect.args, "stack_save_failure": stack_save_failure}))
        if unsupported:
            decision = replace(decision, effect=replace(decision.effect,
                               args={**decision.effect.args, "recovery_hold": True}))
            outcome = None
    if failed and not is_advisory and decision.effect.name in {
        EffectName.CAPTURE_CALIBRATION, EffectName.POLAR_ALIGNMENT, EffectName.SYNC_TO,
    } and not (decision.effect.name is EffectName.CAPTURE_CALIBRATION
               and decision.effect.args.get("catalogue_resumable") and not unsupported):
        # Legacy calibration lacks catalogue identity; physical PA/sync is
        # likewise uncertain. Keep these held rather than blindly replaying.
        decision = replace(decision, effect=replace(decision.effect,
                           args={**decision.effect.args, "recovery_hold": True}))
        outcome = None
        next_status = ItemStatus.RUNNING
        retry_at = clock.now() + timedelta(seconds=60)
        if recovery_deadline is not None:
            retry_at = min(retry_at, recovery_deadline)
        reason += "; unsafe replay held pending operator recovery until window end"
    if failed and decision.effect.name is EffectName.DITHER:
        previous = reduce(await journal.read_all(run_id)).items[decision.item_id]
        recovery_attempts = previous.recovery_attempts + 1
        retry_at = clock.now() + timedelta(seconds=5)
        if recovery_deadline is not None:
            retry_at = min(retry_at, recovery_deadline)
        # Preserve unresolved intent: the next dispatch uses recover_dither,
        # never the original random physical offset. Science stays blocked.
        outcome = None
        next_status = ItemStatus.RUNNING
        reason += "; ambiguous dither retained for no-offset image recovery"
    latest = reduce(await journal.read_all(run_id))
    if latest.run_status is RunStatus.STOPPED:
        # A durable Stop may interrupt an injected long-running device effect.
        # Its late completion must never resurrect the aborted item or retry it.
        next_status = ItemStatus.ABORTED
        retry_at = None
        recovery_attempts = 0

    await journal.append(
        JournalRecord(
            at=clock.now(),
            run_id=run_id,
            kind=RecordKind.ITEM_TRANSITION,
            item_id=decision.item_id,
            panel_index=decision.panel_index,
            attempt=decision.attempt,
            step=next_step,
            status=next_status,
            effect=decision.effect,
            effect_outcome=outcome,
            reason=reason,
            retry_at=retry_at,
            recovery_attempts=recovery_attempts,
            focus_conditions=focus_conditions,
        )
    )
    return reduce(await journal.read_all(run_id))


def _terminal_record(
    run_id: str,
    item_id: str,
    panel_index: int | None,
    status: ItemStatus,
    reason: str,
    state: RunState,
    at: datetime,
) -> JournalRecord:
    current = state.items.get(item_id, ItemRunState())
    return JournalRecord(
        at=at,
        run_id=run_id,
        kind=RecordKind.ITEM_TRANSITION,
        item_id=item_id,
        panel_index=panel_index,
        attempt=current.attempt,
        step=current.step,
        status=status,
        held=False,
        reason=reason,
    )


async def apply_control_command(
    journal: Journal,
    clock: Clock,
    run_id: str,
    run_state: RunState,
    schedule: CompiledSchedule,
    control: ControlCommand,
) -> RunState:
    """One control, applied durably. ``stop`` also marks the currently active item
    aborted and journals its own park effect as issued — the *device* park call
    itself happens back in the main loop, through the same ``RunEnded`` path a
    crash-then-resume takes, so "stop, then crash before parking" and "stop, then the
    process just keeps running" reach the identical journal shape.
    """
    at = clock.now()
    if control.kind is ControlKind.STOP:
        current = _current_item(schedule, run_state)
        if current is not None:
            item_state = run_state.items.get(current.item_id, ItemRunState())
            await journal.append(
                _terminal_record(
                    run_id,
                    current.item_id,
                    item_state.panel_index,
                    ItemStatus.ABORTED,
                    control.reason or "stopped by control",
                    run_state,
                    at,
                )
            )
            run_state = reduce(await journal.read_all(run_id))
        await journal.append(
            JournalRecord(
                at=clock.now(),
                run_id=run_id,
                kind=RecordKind.CONTROL,
                control=ControlKind.STOP,
                run_status=RunStatus.STOPPED,
                effect=EffectCall(name=EffectName.GATE_PARK),
                effect_outcome=None,
                reason=control.reason or "stopped by control",
            )
        )
        return reduce(await journal.read_all(run_id))

    if control.kind in (ControlKind.PAUSE, ControlKind.RESUME):
        new_run_status = (
            RunStatus.PAUSED if control.kind is ControlKind.PAUSE else RunStatus.RUNNING
        )
        await journal.append(
            JournalRecord(
                at=at,
                run_id=run_id,
                kind=RecordKind.CONTROL,
                control=control.kind,
                run_status=new_run_status,
                reason=control.reason or control.kind.value,
            )
        )
        return reduce(await journal.read_all(run_id))

    if control.kind in (ControlKind.SKIP, ControlKind.RETRY):
        await journal.append(
            JournalRecord(
                at=at,
                run_id=run_id,
                kind=RecordKind.CONTROL,
                control=control.kind,
                item_id=control.item_id,
                reason=control.reason or control.kind.value,
            )
        )
        return reduce(await journal.read_all(run_id))

    raise AssertionError(f"unhandled control kind: {control.kind!r}")  # exhaustiveness guard


async def _resolve_pending_run_effect(
    journal: Journal,
    device: DeviceCommands,
    clock: Clock,
    run_id: str,
    effect: EffectCall,
    *,
    capture: CapturePort | None = None,
) -> RunState:
    try:
        await _dispatch(device, capture, effect)
        outcome, reason = EffectOutcome.COMPLETED, f"{effect.name.value} completed"
    except DeviceCommandNotImplementedError as exc:
        outcome, reason = EffectOutcome.FAILED, f"not implemented: {exc}"
    except DeviceCommandError as exc:
        outcome, reason = EffectOutcome.FAILED, str(exc)
    await journal.append(
        JournalRecord(
            at=clock.now(),
            run_id=run_id,
            kind=RecordKind.CONTROL,
            control=ControlKind.STOP,
            run_status=RunStatus.STOPPED,
            effect=effect,
            effect_outcome=outcome,
            reason=reason,
        )
    )
    return reduce(await journal.read_all(run_id))


def _trim_gate_history(history: list[GateSample], thresholds: GateThresholds) -> None:
    """Keep only what the longest sustained-for window could still need. Twice that
    window, not exactly it, so a sample that is about to roll out of the abort window
    is not also missing from a resume check that started sampling a moment later."""
    if not history:
        return
    span_min = max(thresholds.cloud_abort_sustained_min, thresholds.cloud_resume_sustained_min) * 2
    cutoff = history[-1].at - timedelta(minutes=span_min)
    while len(history) > 1 and history[0].at < cutoff:
        history.pop(0)


async def _maybe_evaluate_gate(
    current: CompiledItem | None,
    plan_items: Mapping[str, PlanItem],
    state: RunState,
    gate_inputs: GateInputs | None,
    gate_history: list[GateSample],
    gate_thresholds: GateThresholds,
    clock: Clock,
    deadlines: EffectDeadlines,
    resolved: ResolvedPointing | None = None,
) -> GateDecision | None:
    """A gate is only relevant while the current item is actually stacking (or
    already held mid-stack) — evaluating it for, say, a filter change would just
    manufacture a verdict nobody asked for. Returns ``None`` when there is nothing to
    gate, which :func:`decide` treats as "no gate involved right now"."""
    if current is None or gate_inputs is None:
        return None
    plan_item = plan_items.get(current.item_id)
    if plan_item is None:
        return None
    item_state = state.items.get(current.item_id, ItemRunState())
    phases = _phases_for(current, plan_item, deadlines, resolved)
    step = min(item_state.step, len(phases) - 1) if phases else -1
    in_run_until = 0 <= step < len(phases) and isinstance(phases[step], RunUntilPhase)
    if not (item_state.held or in_run_until):
        return None

    # A body target's altitude and Moon separation are as time-dependent as its
    # position, so the gate samples the *resolved* pointing, not the document.
    pointing = _pointing_for(plan_item, resolved)
    ra_j2000_h = pointing.ra_j2000_h if pointing is not None else None
    dec_j2000_deg = pointing.dec_j2000_deg if pointing is not None else None
    if isinstance(plan_item, MosaicItem) and current.panels and item_state.panel_index is not None:
        panel = current.panels[item_state.panel_index]
        ra_j2000_h = float(deg_to_hours(panel.ra_j2000_deg))  # type: ignore[arg-type]
        dec_j2000_deg = panel.dec_j2000_deg

    now = clock.now()
    forecast = await gate_inputs.forecast()
    target = None
    if ra_j2000_h is not None and dec_j2000_deg is not None:
        target = await gate_inputs.target_snapshot(
            ra_j2000_h=ra_j2000_h, dec_j2000_deg=dec_j2000_deg, at=now
        )
    gate_history.append(build_gate_sample(at=now, forecast=forecast, target=target))
    _trim_gate_history(gate_history, gate_thresholds)

    constraints = getattr(plan_item, "constraints", None)
    return evaluate_gate(
        gate_history,
        thresholds=gate_thresholds,
        currently_held=item_state.held,
        min_alt_deg=constraints.min_alt_deg if constraints is not None else None,
        min_moon_sep_deg=constraints.min_moon_sep_deg if constraints is not None else None,
    )


async def _maybe_resolve_pointing(
    current: CompiledItem | None,
    plan_items: Mapping[str, PlanItem],
    resolver: TargetResolver | None,
    clock: Clock,
) -> tuple[ResolvedPointing | None, str | None]:
    """This tick's answer to "where is the body the current item targets".

    Only for a body target, and only once the item is actually due — resolving
    an ephemeris every poll tick through a four-hour wait buys nothing, and the
    number would be stale by the time it was used anyway. Returns
    ``(None, reason)`` when resolution failed, which :func:`decide` turns into a
    visible recovery wait rather than a slew to nowhere.
    """
    if current is None or current.start_utc is None:
        return None, None
    plan_item = plan_items.get(current.item_id)
    target = getattr(plan_item, "target", None)
    if not isinstance(target, BodyTargetSpec | CometTargetSpec):
        return None, None
    if clock.now() < current.start_utc:
        return None, None
    if resolver is None:
        return None, (
            f"item targets {target.name}, a moving object whose position is only "
            "known at execution time, but no target resolver was supplied to the executor"
        )
    try:
        return await resolver.resolve(target, clock.now()), None
    except Exception as exc:
        # Resolution issues no device action. Keep every failure visible and
        # recheck until the window; a broken resolver must not block cleanup.
        return None, f"could not resolve {target.name}: {type(exc).__name__}: {exc}"


async def _confirm_frame(
    journal: Journal, clock: Clock, run_id: str, decision: ConfirmFrame
) -> RunState:
    """Journal a frame's outcome that the catalogue already proves, without
    re-exposing it — the one place an issued-but-unresolved effect is resolved
    from evidence rather than by repetition (see :class:`ConfirmFrame`)."""
    await journal.append(
        JournalRecord(
            at=clock.now(),
            run_id=run_id,
            kind=RecordKind.ITEM_TRANSITION,
            item_id=decision.item_id,
            panel_index=decision.panel_index,
            attempt=decision.attempt,
            step=decision.step,
            effect=decision.effect,
            effect_outcome=EffectOutcome.COMPLETED,
            reason=decision.reason,
        )
    )
    return reduce(await journal.read_all(run_id))


async def _maybe_count_frames(
    current: CompiledItem | None,
    plan_items: Mapping[str, PlanItem],
    capture: CapturePort | None,
    run_id: str,
) -> tuple[int | None, str | None]:
    """This tick's catalogue frame count for the current exposure block.

    Read on every tick the block is current, deliberately including the wait
    before its start: unlike a body's position, this is not a value that goes
    stale, and tying it to "is the item due yet" would make a resumed run's
    answer depend on where its own clock happened to start — which is exactly
    the class of resume-timing dependency the journal design rules out. It is
    one ``COUNT`` against a table the same loop already re-reads a whole journal
    from.

    Returns ``(None, reason)`` when it cannot be read, which :func:`decide`
    keeps acquisition visibly blocked until its window ends, rather than re-shot:
    unknown is never zero.
    """
    if current is None:
        return None, None
    plan_item = plan_items.get(current.item_id)
    if not isinstance(plan_item, (TakeExposuresItem, GotoAndStackItem, MosaicItem)):
        return None, None
    if capture is None:
        return None, (
            "this is an exposure block, whose progress is the number of frames "
            "already catalogued, but no capture port was supplied to the executor"
        )
    try:
        return await capture.frames_captured(run_id=run_id, item_id=current.item_id), None
    except FrameCountError as exc:
        return None, f"could not read the frame count for this block: {exc}"


async def run(
    *,
    run_id: str,
    schedule: CompiledSchedule,
    plan: Plan,
    journal: Journal,
    device: DeviceCommands,
    events: DeviceEvents,
    clock: Clock,
    controls: ControlSource,
    resolver: TargetResolver | None = None,
    capture: CapturePort | None = None,
    gate_inputs: GateInputs | None = None,
    gate_thresholds: GateThresholds = DEFAULT_GATE_THRESHOLDS,
    deadlines: EffectDeadlines = DEFAULT_DEADLINES,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    plan_snapshot: Callable[[], tuple[CompiledSchedule, Plan]] | None = None,
    supervisor_restarts: bool = False,
) -> RunState:
    """The whole night, headless: journal -> act -> journal, until :func:`decide`
    returns :class:`RunEnded`. Safe to start fresh (an empty journal) or to resume (a
    truncated one, e.g. after a crash) — the first thing this does is read whatever is
    already there and fold it, and every step after that only ever appends.

    **What a resume means for an item targeting a body** (DESIGN.md 3.6). A body's
    position is a fact about a moment, so there are exactly two cases and the
    journal already tells them apart:

    * An effect journalled as *issued* but not yet *resolved* is reissued verbatim,
      with the coordinates that were durably recorded — the same idempotency rule
      every other effect follows. Reissuing is the point: the device may or may not
      have received the first one, and two slews to the same place are one slew.
    * An effect not yet journalled is resolved fresh, at the moment it is issued.
      A run resumed an hour later therefore points where the body is now, not where
      it was when the process died.

    The worst case is a crash between issuing a slew and resolving it, where the
    replayed coordinates are as old as the crash. That is bounded, and bounded by
    something already in the schedule: a body item is compiled into re-centring
    segments (``compiler.MAX_BODY_DRIFT_FRACTION_OF_FRAME``), and a resume that
    lands past the current segment's end finds its ``RunUntilPhase`` already
    elapsed and moves to the next segment, which resolves from scratch. So a
    resumed run's pointing error never outlives one re-centring interval.
    """
    plan_items: dict[str, PlanItem] = {item.id: item for item in plan.items}
    existing = await journal.read_all(run_id)
    if not existing:
        await journal.append(
            JournalRecord(
                at=clock.now(), run_id=run_id, kind=RecordKind.RUN_STARTED, reason="run started"
            )
        )
        existing = await journal.read_all(run_id)
    state = reduce(existing)
    # Durable acquisition progress survives a process; optical field ownership
    # does not. A resumed loop may be sitting at an old dither target. Never
    # let its first retained frame become an unverified new stack reference.
    verified_fields: set[tuple[str, int | None]] = set()

    gate_history: list[GateSample] = []

    while True:
        if plan_snapshot is not None:
            schedule, plan = plan_snapshot()
            plan_items = {item.id: item for item in plan.items}
        control = await controls.poll()
        if control is not None:
            state = await apply_control_command(journal, clock, run_id, state, schedule, control)
            continue

        current = _current_item(schedule, state)
        if state.run_status in {RunStatus.STOPPED, RunStatus.PAUSED}:
            # Safety controls must not depend on a failed optional telemetry or
            # catalogue read. decide() dispatches durable Stop/Park first.
            resolved, resolution_error = None, None
            frames_captured, frames_error = None, None
            focus_required = False
            gate_decision = None
        else:
            progress = None if current is None else state.items.get(current.item_id, ItemRunState())
            window_end = (None if current is None else _recovery_window_end(
                current, plan_items.get(current.item_id), progress.panel_index
            ))
            window_expired = window_end is not None and clock.now() >= window_end
            resolved, resolution_error = await _maybe_resolve_pointing(
                current, plan_items, resolver, clock
            )
            try:
                frames_captured, frames_error = await _maybe_count_frames(
                    current, plan_items, capture, run_id
                )
            except Exception as exc:
                if not window_expired:
                    raise
                frames_captured, frames_error = None, (
                    f"window expired with catalogue unavailable: {type(exc).__name__}: {exc}"
                )
            focus_required = (
                await capture.focus_required()
                if not window_expired
                and capture is not None
                and current is not None
                and isinstance(
                    plan_items.get(current.item_id), GotoAndStackItem | MosaicItem | TakeExposuresItem
                )
                else False
            )
            gate_decision = None if window_expired else await _maybe_evaluate_gate(
                current,
                plan_items,
                state,
                gate_inputs,
                gate_history,
                gate_thresholds,
                clock,
                deadlines,
                resolved,
            )
        decision = decide(
            state,
            schedule=schedule,
            plan_items=plan_items,
            now=clock.now(),
            gate=gate_decision,
            deadlines=deadlines,
            resolved=resolved,
            resolution_error=resolution_error,
            frames_captured=frames_captured,
            frames_error=frames_error,
            focus_required=focus_required,
        )

        if isinstance(decision, RunEnded):
            if state.ended:
                return state
            if decision.pending_run_effect is not None:
                state = await _resolve_pending_run_effect(
                    journal,
                    device,
                    clock,
                    run_id,
                    decision.pending_run_effect,
                    capture=capture,
                )
                continue
            failed_ids = {
                item_id for item_id, item in state.items.items() if item.status is ItemStatus.FAILED
            }
            failed_items = len(failed_ids)
            safety_failed = any(
                isinstance(
                    plan_items.get(item_id), CaptureCalibrationItem | ParkItem | ShutdownItem
                )
                for item_id in failed_ids
            )
            final_status = (
                state.run_status
                if state.run_status is RunStatus.STOPPED
                else RunStatus.FAILED
                if safety_failed
                else RunStatus.COMPLETED_WITH_WARNINGS
                if failed_items
                else RunStatus.COMPLETED
            )
            final_reason = (
                f"{failed_items} item{'s' if failed_items != 1 else ''} failed; "
                "remaining items settled"
                if failed_items
                else decision.reason
            )
            await journal.append(
                JournalRecord(
                    at=clock.now(),
                    run_id=run_id,
                    kind=RecordKind.RUN_ENDED,
                    run_status=final_status,
                    reason=final_reason,
                )
            )
            return reduce(await journal.read_all(run_id))

        if isinstance(decision, PausedWait):
            await clock.sleep_until(clock.now() + timedelta(seconds=poll_interval_s))
            continue

        if isinstance(decision, AwaitOperator):
            await journal.append(
                JournalRecord(
                    at=clock.now(),
                    run_id=run_id,
                    kind=RecordKind.CONTROL,
                    control=ControlKind.PAUSE,
                    run_status=RunStatus.PAUSED,
                    item_id=decision.item_id,
                    step=decision.step,
                    status=ItemStatus.RUNNING,
                    reason=decision.reason,
                )
            )
            state = reduce(await journal.read_all(run_id))
            continue

        if isinstance(decision, WaitUntil):
            if decision.reason is not None:
                item_state = state.items.get(decision.item_id, ItemRunState())
                if item_state.reason != decision.reason:
                    await journal.append(JournalRecord(
                        at=clock.now(), run_id=run_id, kind=RecordKind.ITEM_TRANSITION,
                        item_id=decision.item_id, panel_index=item_state.panel_index,
                        attempt=item_state.attempt, step=item_state.step,
                        status=ItemStatus.RUNNING, reason=decision.reason,
                    ))
                    state = reduce(await journal.read_all(run_id))
            # Ordinary timing waits have no journal write: "waiting" has no side effect and is fully recomputable
            # from `now` vs. the schedule on every call to `decide` (see its
            # docstring), and journalling it would make the journal's *content* — not
            # just its timestamps — depend on incidental clock timing at the moment a
            # particular process happened to observe it, which a resumed run cannot
            # reproduce. A live status of "waiting" for the UI is `PENDING` plus "now
            # < start_utc", derived by the caller, not stored here.
            wake_at = min(decision.until_utc, clock.now() + timedelta(seconds=poll_interval_s))
            await clock.sleep_until(wake_at)
            continue

        if isinstance(decision, IssueEffect):
            field_key = (decision.item_id, decision.panel_index)
            recovery_state = state.items.get(decision.item_id)
            if (recovery_state is not None and recovery_state.recovery_attempts >= 2
                    and recovery_state.retry_at is not None):
                # Connection/tracking repair invalidates the in-process pointing
                # proof; science must solve the original planned field again.
                verified_fields.discard(field_key)
            if (
                current is not None
                and field_key not in verified_fields
                and decision.effect.name in {
                    EffectName.EXPOSE_FRAME, EffectName.DITHER, EffectName.AUTOFOCUS
                }
                # An unresolved offset must finish its stopped/settling recovery
                # before any new physical command. Do not repeat it here.
                and not (decision.resumed and decision.effect.name is EffectName.DITHER)
            ):
                phases = _phases_for(current, plan_items[decision.item_id], deadlines, resolved)
                centering = next((phase.effect for phase in reversed(phases[:decision.step])
                                  if isinstance(phase, EffectPhase)
                                  and phase.panel_index == decision.panel_index
                                  and phase.effect.name is EffectName.CENTER_TARGET), None)
                if centering is not None:
                    decision = IssueEffect(
                        item_id=decision.item_id, step=decision.step,
                        panel_index=decision.panel_index, attempt=decision.attempt,
                        effect=EffectCall(name=EffectName.CENTER_TARGET,
                                          args={**centering.args, "resume_field_verification": True,
                                                "resume_autofocus_during_capture": (
                                                    decision.effect.name is EffectName.AUTOFOCUS
                                                    and bool(decision.effect.args.get("during_capture"))
                                                )}),
                        resumed=False, new_status=decision.new_status,
                    )
            state = await _issue_effect(
                journal,
                device,
                events,
                clock,
                run_id,
                decision,
                capture=capture,
                supervisor_restarts=supervisor_restarts,
                recovery_deadline=(
                    _recovery_window_end(
                        current, plan_items.get(current.item_id), decision.panel_index
                    )
                    if current is not None
                    else None
                )
                or (
                    schedule.night.dark_end_utc or schedule.night.window_end_utc
                    # Every operational recovery is bounded by the night if
                    # the item provides no narrower window.
                ),
            )
            if decision.effect.name is EffectName.DITHER and (
                decision.resumed or state.items[decision.item_id].pending_effect is not None
            ):
                verified_fields.discard(field_key)
            if decision.effect.name is EffectName.CENTER_TARGET:
                last = (await journal.read_all(run_id))[-1]
                if last.effect_outcome is EffectOutcome.COMPLETED and state.run_status is not RunStatus.STOPPED:
                    verified_fields.add(field_key)
            continue

        if isinstance(decision, ConfirmFrame):
            state = await _confirm_frame(journal, clock, run_id, decision)
            continue

        if isinstance(decision, HoldForGate):
            if decision.starting:
                item_state = state.items.get(decision.item_id, ItemRunState())
                issue = IssueEffect(
                    item_id=decision.item_id,
                    step=decision.step,
                    panel_index=decision.panel_index,
                    attempt=item_state.attempt + 1,
                    effect=EffectCall(name=EffectName.GATE_PARK),
                    resumed=False,
                    new_status=None,
                )
                state = await _issue_effect(
                    journal, device, events, clock, run_id, issue, capture=capture, held=True
                )
            else:
                await clock.sleep_until(clock.now() + timedelta(seconds=poll_interval_s))
            continue

        if isinstance(decision, ResumeFromGate):
            item_state = state.items.get(decision.item_id, ItemRunState())
            await journal.append(
                JournalRecord(
                    at=clock.now(),
                    run_id=run_id,
                    kind=RecordKind.ITEM_TRANSITION,
                    item_id=decision.item_id,
                    panel_index=decision.panel_index,
                    attempt=item_state.attempt,
                    step=decision.step,
                    held=False,
                    reason=decision.reason,
                )
            )
            state = reduce(await journal.read_all(run_id))
            continue

        if isinstance(decision, AdvancePhase):
            item_state = state.items.get(decision.item_id, ItemRunState())
            await journal.append(JournalRecord(
                at=clock.now(), run_id=run_id, kind=RecordKind.ITEM_TRANSITION,
                item_id=decision.item_id, panel_index=decision.panel_index,
                attempt=item_state.attempt, step=decision.step, reason=decision.reason,
                effect=item_state.pending_effect,
                effect_outcome=(EffectOutcome.WINDOW_ENDED if item_state.pending_effect is not None else None),
            ))
            state = reduce(await journal.read_all(run_id))
            continue

        if isinstance(decision, CompleteItem):
            await journal.append(
                _terminal_record(
                    run_id,
                    decision.item_id,
                    decision.panel_index,
                    ItemStatus.DONE,
                    decision.reason,
                    state,
                    clock.now(),
                )
            )
            state = reduce(await journal.read_all(run_id))
            continue

        if isinstance(decision, SkipItem):
            await journal.append(
                _terminal_record(
                    run_id,
                    decision.item_id,
                    decision.panel_index,
                    ItemStatus.SKIPPED,
                    decision.reason,
                    state,
                    clock.now(),
                )
            )
            state = reduce(await journal.read_all(run_id))
            continue

        if isinstance(decision, FailItem):
            await journal.append(
                _terminal_record(
                    run_id,
                    decision.item_id,
                    decision.panel_index,
                    ItemStatus.FAILED,
                    decision.reason,
                    state,
                    clock.now(),
                )
            )
            state = reduce(await journal.read_all(run_id))
            continue

        if isinstance(decision, AbortItem):
            await journal.append(
                _terminal_record(
                    run_id,
                    decision.item_id,
                    decision.panel_index,
                    ItemStatus.ABORTED,
                    decision.reason,
                    state,
                    clock.now(),
                )
            )
            state = reduce(await journal.read_all(run_id))
            continue

        raise AssertionError(f"unhandled decision: {decision!r}")  # exhaustiveness guard


__all__ = [
    "ADVISORY_EFFECTS",
    "DEFAULT_DEADLINES",
    "DEFAULT_POLL_INTERVAL_S",
    "NON_ADVANCING_EFFECTS",
    "TERMINAL_ITEM_STATUSES",
    "AbortItem",
    "CapturePort",
    "Clock",
    "CompleteItem",
    "CompletionMode",
    "ConfirmFrame",
    "ControlCommand",
    "ControlKind",
    "ControlSource",
    "Decision",
    "DeviceCommandError",
    "DeviceCommandNotImplementedError",
    "DeviceCommands",
    "DeviceEventTimeoutError",
    "DeviceEvents",
    "EffectCall",
    "EffectDeadlines",
    "EffectName",
    "EffectOutcome",
    "EffectPhase",
    "EphemerisTargetResolver",
    "ExposeLoopPhase",
    "FailItem",
    "FrameCountError",
    "GateInputs",
    "HoldForGate",
    "IssueEffect",
    "ItemRunState",
    "ItemStatus",
    "Journal",
    "JournalRecord",
    "PausedWait",
    "Phase",
    "RecordKind",
    "ResolvedPointing",
    "ResumeFromGate",
    "RunEnded",
    "RunState",
    "RunStatus",
    "RunUntilPhase",
    "SkipItem",
    "SleepPhase",
    "TargetResolutionError",
    "TargetResolver",
    "WaitUntil",
    "decide",
    "reduce",
    "run",
]
