# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``Plan`` → ``CompiledSchedule`` (DESIGN.md 4.2).

Pure and fully testable with no device and no clock of its own: every input
that could vary — the site, the timezone, the night being compiled, the
loaded ephemeris, "now", the scope profile used for mosaic panel geometry —
is a parameter, never read from the environment. Compiling the same
``Plan`` against the same inputs twice produces byte-identical output, which
is what makes the golden-file test (``tests/scheduler/test_compiler.py``)
possible and what lets the DST edge case be a fixed-date test rather than a
"wait until October" test.

**What this module resolves, and what it deliberately does not.** A
``TimeSpec`` becomes an absolute UTC instant here. Whether a target is
actually visible, whether the Moon is too close, whether a mosaic's frame
overlaps the previous item — all of that becomes a :class:`~ssc.plan.Diagnostic`
attached to the compiled item, never a silently adjusted time (CLAUDE.md
invariant 7). Nothing here talks to a device, writes to SQLite, or decides
*when* to act — that is :mod:`ssc.scheduler.executor`, the next step.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Final
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from ssc.astro.bodies import apparent_motion_arcmin_per_hour, body_events
from ssc.astro.comets import (
    CometCatalog,
    CometPosition,
    comet_execution_rejection,
    comet_position,
)
from ssc.astro.events import (
    Ephemeris,
    EventTime,
    LocalTimeKind,
    Outcome,
    Site,
    SolarEvent,
    TargetEvent,
    local_time_kind,
    local_to_utc,
    lunar_events,
    solar_events,
    target,
    target_events,
    target_position,
)
from ssc.astro.framing import (
    HIGH_ALTITUDE_TRANSIT_DEG,
    MountMode,
    Profile,
    field_geometry,
    is_zenith_singular,
)
from ssc.astro.mosaic import MosaicSpec, mosaic_panel_centers
from ssc.astro.polar import PolarAlignmentError, best_conditioned_field
from ssc.astro.targets import resolve_position
from ssc.astro.units import Deg, Hours, hours_to_deg
from ssc.plan.models import (
    AbsoluteTimeSpec,
    AsapTimeSpec,
    AutofocusItem,
    BodyTargetSpec,
    CatalogTargetSpec,
    CometTargetSpec,
    Diagnostic,
    DurationEndSpec,
    EndSpec,
    EventEndSpec,
    EventTimeSpec,
    FixedCountSpec,
    FrameType,
    GotoAndStackItem,
    MosaicItem,
    Plan,
    PlanItem,
    RelativeTimeSpec,
    Severity,
    SlewItem,
    TakeExposuresItem,
    TargetEventEndSpec,
    TargetEventTimeSpec,
    TargetSpec,
    TimeSpec,
    UntilBlockEndSpec,
)
from ssc.scheduler.exposure import (
    FRAME_OVERHEAD_S,
    CameraLimits,
    cycle_duration_s,
    whole_cycles_in,
)

#: DESIGN.md 4.1's "the night of date D" — evening of D to morning of D+1,
#: local. Anchored on local *noon* rather than local midnight so the search
#: window never straddles a DST transition ambiguity at its own edges: noon
#: is never the repeated or skipped hour in any IANA zone in use today.
_NIGHT_ANCHOR_LOCAL_TIME: Final = time(12, 0)

# --- how long a body item may run before the executor re-centres it ----------
#
# DESIGN.md 3.6 calls periodic re-centring a scheduler concern, and the number
# has to live somewhere rather than be discovered from a ruined stack. The
# decision, in full:
#
# *Re-centring is implemented, not merely warned about.* A body item's compiled
# window is cut into segments, and each segment is a fresh slew — the executor
# resolves the body's position again at the top of every one (see
# ``ssc.scheduler.executor``). A segment is therefore both the re-centring
# cadence *and* the bound on how stale a resumed run's pointing can be.
#
# *The interval is sized on the body's whole apparent motion, not on the
# residual left after tracking.* The mount's lunar rate follows the Moon's
# *mean* motion; tonight's actual motion differs from it by an amount the device
# reports nothing about, so SSC does not know the residual and refuses to
# pretend it is small (architecture policy invariant 7 — unknown is never zero). Sizing on
# the full apparent motion is an upper bound: with lunar tracking engaged the
# real drift is smaller, so the frame is re-centred sooner than strictly needed,
# which costs a slew and never a ruined sub.
#
# *The tolerance is a tenth of the frame's inscribed circle* — its short side,
# 2.24 deg = 134.4' on the S30 Pro tele, so 13.4'. At the Moon's ~38.6 arcmin/h
# apparent motion (measured at Bratislava, 2026-09-10 18:00 UTC) that is a
# re-centre roughly every 21 minutes; at Jupiter's 0.5 arcmin/h it is 27 hours,
# i.e. a whole night is one segment and nothing changes for a planet. That is
# the intended shape: the mechanism is uniform, the cost is proportional to how
# fast the target actually moves.
MAX_BODY_DRIFT_FRACTION_OF_FRAME: Final = 0.10

#: A floor on the re-centring cadence, so no plausible ephemeris rate can make
#: the executor thrash the stack. Nothing SSC tracks comes close — it would take
#: a body moving faster than 2.7 arcmin/minute to hit this — but a floor costs
#: one line and an unbounded loop costs a night.
MIN_RECENTRE_INTERVAL_MIN: Final = 5.0


def _captures_light_frames(item: PlanItem) -> bool:
    """Whether daylight can invalidate this item's intended output.

    Target stacks and mosaics are intrinsically light-frame imaging.  The
    universal exposure block says its frame type explicitly.  Calibration
    captures and ordinary device actions remain useful after astronomical
    dawn and must not inherit an imaging warning merely because they have an
    end time.
    """
    return isinstance(item, (GotoAndStackItem, MosaicItem)) or (
        isinstance(item, TakeExposuresItem) and item.frame_type is FrameType.LIGHT
    )


# --- compiled output (persisted and served — a Pydantic model, not a dataclass) --


class CompiledModel(BaseModel):
    """Base for every model in this module: frozen, strict on the way in."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ResolvedEvent(CompiledModel):
    """One solved solar/lunar event, echoed with its outcome — never a bare
    time that hides a polar absence (CLAUDE.md invariant 7)."""

    event: str
    when_utc: datetime | None
    outcome: str
    threshold_deg: float


class NightWindow(CompiledModel):
    """The resolved night: every solar/lunar event solved once and shared by
    every item's ``event``-kind ``TimeSpec`` in this compile."""

    date: str
    tz: str
    window_start_utc: datetime
    window_end_utc: datetime
    #: Astronomical dusk/dawn, if they occur — the "dark window" DESIGN.md 4.4
    #: measures a plan's total time against. ``None`` when the outcome in
    #: ``solar_events`` says the boundary did not occur (e.g. high latitude).
    dark_start_utc: datetime | None
    dark_end_utc: datetime | None
    solar_events: tuple[ResolvedEvent, ...]
    lunar_events: tuple[ResolvedEvent, ...]


class CompiledPanel(CompiledModel):
    """One slice of an item's time with its own pointing.

    Two things produce these, and they are told apart by whether the centre is
    known at compile time:

    * a **mosaic panel** — ``row``/``col`` are its grid position and
      ``ra_j2000_deg``/``dec_j2000_deg`` its fixed sky centre; and
    * a **body item's re-centring segment** — ``row``/``col`` are both ``0``
      and the coordinates are ``None``, because a solar-system body's position
      is a fact about an instant and the executor resolves it at the top of the
      segment (DESIGN.md 3.6, and see :data:`MAX_BODY_DRIFT_FRACTION_OF_FRAME`).

    ``None`` here means "not knowable until this runs", never zero
    (CLAUDE.md invariant 7); nothing may read these two fields without
    branching on it.
    """

    row: int
    col: int
    ra_j2000_deg: float | None
    dec_j2000_deg: float | None
    start_utc: datetime
    end_utc: datetime


class CompiledTarget(CompiledModel):
    """The item's target, echoed for display (DESIGN.md 4.1: "the abstraction
    must never hide what will actually happen").

    For a catalogue target this is just the item's own fixed coordinates. For a
    body it is **where the body will be at** ``resolved_at_utc`` — an estimate
    for the UI and the Gantt, never what the executor points at: the executor
    resolves the reference again at the moment each segment starts, which is the
    entire reason the plan document stores a name rather than a position.
    """

    kind: str
    name: str | None
    #: The mount drive rate this target implies. Derived from the target, never
    #: a second user decision (DESIGN.md 3.6).
    tracking_rate: str
    moves: bool
    ra_j2000_deg: float | None
    dec_j2000_deg: float | None
    resolved_at_utc: datetime | None
    #: Apparent motion at the item's start, arcmin/hour — ``None`` for a fixed
    #: target, where the concept does not apply.
    motion_arcmin_per_hour: float | None
    #: How far the target moves across this item's own compiled window, arcmin.
    #: ``None`` when the window did not resolve, or the target does not move.
    drift_arcmin: float | None
    #: Minutes between re-centres, and how many segments the window was cut
    #: into. ``None``/``1`` when no subdivision was needed or possible.
    recentre_interval_min: float | None


class CompiledItem(CompiledModel):
    """One item, resolved. ``start_utc`` is ``None`` iff the item's start could
    not be resolved (unschedulable — never a guessed time, CLAUDE.md
    invariant 7). ``end_utc`` is ``None`` if either that is true, or the start
    resolved fine but the item's own ``end`` could not (its event never
    occurs, or it resolves at or before the start) — same rule, applied to
    the end independently. ``resolution``/``end_resolution`` are the human
    echoes DESIGN.md 4.1 requires ("Astronomical dusk +15 → 22:47" /
    "Astronomical dawn -30 → 04:12"): local for display, ``start_utc``/
    ``end_utc`` remain the truth.
    """

    item_id: str
    kind: str
    enabled: bool
    start_utc: datetime | None
    end_utc: datetime | None
    resolution: str
    end_resolution: str
    #: ``None`` for an item that names no target (startup, filter, shutdown...).
    target: CompiledTarget | None = None
    panels: tuple[CompiledPanel, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


class CompiledSchedule(CompiledModel):
    """``Plan`` → this. Absolute UTC times, mosaics expanded, validated."""

    plan_id: str
    plan_revision: str
    compiled_at: datetime
    night: NightWindow
    items: tuple[CompiledItem, ...]


# --- entry point ---------------------------------------------------------------


@lru_cache(maxsize=16)
def _night_events(
    ephemeris: Ephemeris, site: Site, start: datetime, end: datetime
) -> tuple[tuple[EventTime, ...], tuple[EventTime, ...]]:
    """Reuse immutable night astronomy across edits; location/kernel changes miss.

    Only event tuples are cached, never plan items or execution state. The
    bounded cache keeps memory predictable when browsing dates and locations.
    """
    return (
        solar_events(ephemeris, site, start, end),
        lunar_events(ephemeris, site, start, end),
    )


def compile_plan(
    plan: Plan,
    *,
    site: Site,
    tz: ZoneInfo,
    night_date: date,
    ephemeris: Ephemeris,
    now: datetime,
    profile: Profile | None = None,
    camera_limits: CameraLimits | None = None,
    comet_catalog: CometCatalog | None = None,
) -> CompiledSchedule:
    """Compile ``plan`` for the night of ``night_date`` (evening of ``night_date``
    to morning of ``night_date + 1``, local) at ``site``.

    ``camera_limits`` is what the *camera itself* reports it will accept
    (``Camera.ExposureMin``/``ExposureMax``/``GainMin``/``GainMax``,
    ``FilterWheel.Names``), read by ``backend-core`` and passed in — never
    hardcoded here, and never assumed: without it an exposure block's
    exposure/gain are simply reported as unchecked (DESIGN.md 4.3.0).

    ``profile`` supplies the scope's optics for mosaic panel geometry
    (``framing.lens`` names one of its optics). Without one, mosaic items
    still resolve a start/end time but cannot lay out real panel centres —
    that is reported as a diagnostic on the item, not guessed at.

    ``now`` is only used to stamp ``compiled_at``; every resolved time is a
    function of ``plan``, ``site``, ``tz``, ``night_date`` and the ephemeris,
    never of ``now`` — a schedule compiled at 9pm and one compiled at 3am for
    the same night are identical.
    """
    window_start_utc, window_end_utc = _night_window_utc(night_date, tz)
    solar, lunar = _night_events(ephemeris, site, window_start_utc, window_end_utc)
    dark_start = _find(solar, SolarEvent.ASTRONOMICAL_DUSK)
    dark_end = _find(solar, SolarEvent.ASTRONOMICAL_DAWN)
    # A derived event, not the Sun's lower transit or 00:00 on a civil clock.
    # UTC duration arithmetic also handles a DST transition inside the night.
    midpoint = (
        dark_start.when + (dark_end.when - dark_start.when) / 2
        if dark_start is not None and dark_start.when is not None
        and dark_end is not None and dark_end.when is not None
        and dark_end.when > dark_start.when
        else None
    )
    midnight_event = EventTime(
        event=SolarEvent.ASTRONOMICAL_MIDNIGHT,
        when=midpoint,
        outcome=Outcome.OCCURS if midpoint is not None else (
            Outcome.ALWAYS_ABOVE if dark_start is not None
            and dark_start.outcome is Outcome.ALWAYS_ABOVE else Outcome.NOT_IN_WINDOW
        ),
        threshold_deg=Deg(-18.0),
    )
    solar = tuple(sorted(
        (*solar, midnight_event),
        key=lambda event: (event.when is None, event.when or window_end_utc, event.event),
    ))
    night = NightWindow(
        date=night_date.isoformat(),
        tz=tz.key,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
        dark_start_utc=dark_start.when if dark_start else None,
        dark_end_utc=dark_end.when if dark_end else None,
        solar_events=tuple(_resolved_event(e) for e in solar),
        lunar_events=tuple(_resolved_event(e) for e in lunar),
    )
    dark_end_when = dark_end.when if dark_end else None

    compiled_items: list[CompiledItem] = []
    prev_start: datetime | None = None
    prev_end: datetime | None = None
    for item in plan.items:
        diagnostics: list[Diagnostic] = []
        if isinstance(item, AutofocusItem) and item.filter_name is not None:
            if camera_limits is None or not camera_limits.filter_names:
                diagnostics.append(Diagnostic(
                    severity=Severity.WARNING, item_id=item.id, code="autofocus_filter_names_unknown",
                    message="Autofocus filter cannot be checked until the wheel reports its positions; execution verifies the selected position before focusing.",
                ))
            elif item.filter_name.casefold() not in {name.casefold() for name in camera_limits.filter_names}:
                diagnostics.append(Diagnostic(
                    severity=Severity.ERROR, item_id=item.id, code="unknown_filter_position",
                    message=f"autofocus filter {item.filter_name!r} is not one of the wheel positions ({', '.join(camera_limits.filter_names)})",
                ))
        #: Set only where an item's end is *derived* rather than specified — an
        #: exposure block of a fixed count, whose length is its own arithmetic.
        end_resolution: str | None = None
        start_utc, resolve_diag = _resolve_start(
            item,
            prev_end,
            tz,
            ephemeris,
            site,
            window_start_utc,
            window_end_utc,
            solar,
            lunar,
            comet_catalog,
        )
        diagnostics.extend(resolve_diag)

        target_spec = _item_target(item)
        if isinstance(target_spec, CometTargetSpec) and start_utc is not None:
            try:
                _resolve_comet_position(
                    target_spec, ephemeris, site, start_utc, comet_catalog
                )
            except ValueError as exc:
                diagnostics.append(
                    Diagnostic(
                        severity=Severity.ERROR,
                        item_id=item.id,
                        code="comet_ephemeris_unavailable",
                        message=str(exc),
                    )
                )
                start_utc = None
        compiled_target_override: CompiledTarget | None = None
        if (
            isinstance(item, SlewItem)
            and item.destination == "polar_best"
            and start_utc is not None
        ):
            try:
                field = best_conditioned_field(
                    ephemeris,
                    site,
                    start_utc,
                    point_count=5,
                    step_deg=Deg(15.0),
                )
                compiled_target_override = CompiledTarget(
                    kind="polar_best",
                    name="Polar-alignment best field",
                    tracking_rate="sidereal",
                    moves=False,
                    ra_j2000_deg=float(field.coord.ra_deg),
                    dec_j2000_deg=float(field.coord.dec_deg),
                    resolved_at_utc=start_utc,
                    motion_arcmin_per_hour=None,
                    drift_arcmin=None,
                    recentre_interval_min=None,
                )
            except PolarAlignmentError as exc:
                diagnostics.append(
                    Diagnostic(
                        severity=Severity.ERROR,
                        item_id=item.id,
                        code="polar_best_field_unavailable",
                        message=str(exc),
                    )
                )
        panels: tuple[CompiledPanel, ...] = ()
        recentre_interval_min: float | None = None
        end_utc: datetime | None
        if start_utc is None:
            end_utc = None
        elif isinstance(item, MosaicItem) and isinstance(
            target_spec, BodyTargetSpec | CometTargetSpec
        ):
            # A mosaic lays out fixed panel centres around a fixed target. A body
            # is not one, and no rate the mount offers holds a *grid* on a moving
            # object; SSC refuses rather than laying out panels that are wrong the
            # moment they are computed. It is also not a real want: the Moon's
            # 0.5 deg disk already fits inside a single 2.24 x 3.99 deg frame.
            end_utc = None
            diagnostics.append(
                Diagnostic(
                    severity=Severity.ERROR,
                    item_id=item.id,
                    code=(
                        "mosaic_on_body_target"
                        if isinstance(target_spec, BodyTargetSpec)
                        else "mosaic_on_moving_target"
                    ),
                    message=(
                        f"a mosaic cannot target {target_spec.name}: panel centres are "
                        "fixed relative to the target, and a solar-system object moves "
                        "against the sky while the mosaic runs. Use a goto_and_stack "
                        "item, which re-centres on the body as it goes."
                    ),
                )
            )
        elif isinstance(item, MosaicItem):
            # A mosaic's own ``end`` is *per panel*, not the item's overall
            # end (that is the last panel's end, computed by
            # ``_expand_mosaic`` below) — only a "duration" kind end has a
            # meaning per panel; an event/target_event end is not, and is
            # treated as no duration (0 min) rather than guessed at.
            per_panel_minutes = item.end.minutes if isinstance(item.end, DurationEndSpec) else 0.0
            per_panel = timedelta(minutes=per_panel_minutes)
            panels, mosaic_diag, end_utc = _expand_mosaic(
                item, target_spec, start_utc, per_panel, ephemeris, site, profile
            )
            diagnostics.extend(mosaic_diag)
        else:
            end_utc, end_diag = _resolve_end(
                item,
                item.end,
                start_utc,
                ephemeris,
                site,
                window_start_utc,
                window_end_utc,
                solar,
                lunar,
                comet_catalog,
            )
            diagnostics.extend(end_diag)
            if isinstance(item, TakeExposuresItem):
                end_utc, end_resolution, exposure_diag = _exposure_block(
                    item, start_utc, end_utc, tz, camera_limits
                )
                diagnostics.extend(exposure_diag)
            elif item.end is None and (
                estimate := _device_action_estimate(item, compiled_items)
            ) is not None:
                estimate_min, explanation = estimate
                end_utc = start_utc + timedelta(minutes=estimate_min)
                end_resolution = explanation
            if (
                isinstance(item, GotoAndStackItem)
                and isinstance(target_spec, BodyTargetSpec | CometTargetSpec)
                and end_utc is not None
            ):
                panels, recentre_interval_min, segment_diag = _recentre_segments(
                    item,
                    target_spec,
                    start_utc,
                    end_utc,
                    ephemeris,
                    site,
                    profile,
                    comet_catalog,
                )
                diagnostics.extend(segment_diag)

        if (
            start_utc is not None
            and end_utc is not None
            and prev_start is not None
            and prev_end is not None
            and start_utc < prev_end
            and prev_start < end_utc
        ):
            overlap_start = max(start_utc, prev_start)
            overlap_end = min(end_utc, prev_end)
            diagnostics.append(
                Diagnostic(
                    severity=Severity.WARNING,
                    item_id=item.id,
                    code="overlaps_previous",
                    message=(
                        f"overlaps the previous item by "
                        f"{(overlap_end - overlap_start).total_seconds() / 60.0:.1f} min"
                    ),
                )
            )
        if (
            _captures_light_frames(item)
            and end_utc is not None
            and dark_end_when is not None
            and end_utc > dark_end_when
        ):
            diagnostics.append(
                Diagnostic(
                    severity=Severity.WARNING,
                    item_id=item.id,
                    code="exceeds_dark_window",
                    message=(
                        f"ends at {end_utc.isoformat()}, after the dark window ends at "
                        f"{dark_end_when.isoformat()}"
                    ),
                )
            )

        if target_spec is not None and start_utc is not None and end_utc is not None:
            diagnostics.extend(
                _altitude_diagnostics(
                    item, target_spec, start_utc, end_utc, ephemeris, site, comet_catalog
                )
            )
            diagnostics.extend(
                _moon_diagnostics(item, target_spec, start_utc, ephemeris, site, comet_catalog)
            )
            diagnostics.extend(
                _meridian_diagnostics(
                    item, target_spec, start_utc, end_utc, ephemeris, site, comet_catalog
                )
            )

        compiled_items.append(
            CompiledItem(
                item_id=item.id,
                kind=item.kind,
                enabled=item.enabled,
                start_utc=start_utc,
                end_utc=end_utc,
                resolution=_resolution_string(item.start, start_utc, tz),
                end_resolution=(
                    end_resolution
                    if end_resolution is not None
                    else _end_resolution_string(item.end, end_utc, tz)
                ),
                target=(
                    compiled_target_override
                    if compiled_target_override is not None
                    else (
                        None
                        if target_spec is None
                        else _compiled_target(
                            target_spec,
                            start_utc,
                            end_utc,
                            ephemeris,
                            site,
                            recentre_interval_min,
                            comet_catalog,
                        )
                    )
                ),
                panels=panels,
                diagnostics=tuple(diagnostics),
            )
        )

        # Disabled items are compiled (so the UI can still show them) but
        # never chained from: an executor will not run them, so "after the
        # previous item completes" has to mean the previous item that will
        # actually execute.
        if item.enabled:
            prev_start = start_utc
            prev_end = end_utc

    return CompiledSchedule(
        plan_id=plan.id,
        plan_revision=_plan_revision(plan),
        compiled_at=now,
        night=night,
        items=tuple(compiled_items),
    )


_DEVICE_ACTION_ESTIMATES_MIN: Final[dict[str, float]] = {
    "startup": 8.0,
    "autofocus": 5.0,
    "set_exposure": 0.5,
    "set_gain": 0.5,
    "filter": 0.5,
    "heater": 0.5,
    "dither": 1.0,
    "solve": 2.0,
    "sync": 0.5,
    "slew": 2.0,
    "find_home": 2.0,
    "polar_alignment": 15.0,
    "park": 2.0,
    "unpark": 1.0,
    "capture_calibration": 30.0,
    "shutdown": 2.0,
}


def _device_action_estimate(
    item: PlanItem, previous: list[CompiledItem]
) -> tuple[float, str] | None:
    """Return display/chaining time only; the executor still waits on the device."""
    fallback = _DEVICE_ACTION_ESTIMATES_MIN.get(item.kind)
    if fallback is None:
        return None
    if item.planning_estimate_min is not None:
        return (
            item.planning_estimate_min,
            f"Planning estimate {item.planning_estimate_min:g} min (operator override); "
            "actual completion comes from device state",
        )
    if isinstance(item, SlewItem) and isinstance(item.target, CatalogTargetSpec):
        origin = next((compiled.target for compiled in reversed(previous) if compiled.target), None)
        if origin is not None and origin.ra_j2000_deg is not None and origin.dec_j2000_deg is not None:
            ra1, ra2 = map(math.radians, (origin.ra_j2000_deg, item.target.ra_j2000_h * 15.0))
            dec1, dec2 = map(math.radians, (origin.dec_j2000_deg, item.target.dec_j2000_deg))
            cosine = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(ra2 - ra1)
            distance_deg = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
            seconds = 20.0 + distance_deg / 2.0
            return (
                max(0.5, seconds / 60.0),
                f"Estimated from {distance_deg:.1f}° route at conservative 2°/s + 20 s settle; "
                "actual completion comes from Telescope.Slewing",
            )
    return (
        fallback,
        f"Conservative {fallback:g} min planning estimate; actual completion comes from device state",
    )


# --- the night window ------------------------------------------------------


def _night_window_utc(night_date: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Local noon on ``night_date`` to local noon on ``night_date + 1``, in UTC.

    Noon-anchored rather than midnight-anchored so the window's own edges are
    never the ambiguous or nonexistent hour of a DST transition — the night
    it contains can still hold one, and that is resolved per item, not here.
    ``fold=0`` at noon is unambiguous in every zone in use today.
    """
    start_local = datetime.combine(night_date, _NIGHT_ANCHOR_LOCAL_TIME)
    end_local = datetime.combine(night_date + timedelta(days=1), _NIGHT_ANCHOR_LOCAL_TIME)
    return local_to_utc(start_local, tz, fold=0), local_to_utc(end_local, tz, fold=0)


def _find(events: tuple[EventTime, ...], kind: str) -> EventTime | None:
    return next((e for e in events if e.event == kind), None)


def _resolved_event(event: EventTime) -> ResolvedEvent:
    return ResolvedEvent(
        event=str(event.event),
        when_utc=event.when,
        outcome=str(event.outcome),
        threshold_deg=float(event.threshold_deg),
    )


# --- TimeSpec resolution -----------------------------------------------------


def _resolve_start(
    item: PlanItem,
    prev_end: datetime | None,
    tz: ZoneInfo,
    ephemeris: Ephemeris,
    site: Site,
    window_start_utc: datetime,
    window_end_utc: datetime,
    solar: tuple[EventTime, ...],
    lunar: tuple[EventTime, ...],
    comet_catalog: CometCatalog | None,
) -> tuple[datetime | None, list[Diagnostic]]:
    spec: TimeSpec = item.start

    if isinstance(spec, AbsoluteTimeSpec):
        return _resolve_absolute(item, spec, tz)

    if isinstance(spec, EventTimeSpec):
        return _resolve_event(item, spec, solar, lunar)

    if isinstance(spec, TargetEventTimeSpec):
        return _resolve_target_event(
            item,
            spec,
            ephemeris,
            site,
            window_start_utc,
            window_end_utc,
            comet_catalog,
        )

    if isinstance(spec, RelativeTimeSpec | AsapTimeSpec):
        return _resolve_chained(item, spec, prev_end)

    raise AssertionError(f"unhandled TimeSpec: {spec!r}")  # exhaustiveness guard


def _resolve_absolute(
    item: PlanItem, spec: AbsoluteTimeSpec, tz: ZoneInfo
) -> tuple[datetime | None, list[Diagnostic]]:
    if local_time_kind(spec.local_datetime, tz) is LocalTimeKind.NONEXISTENT:
        return None, [
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="nonexistent_local_time",
                message=(
                    f"{spec.local_datetime.isoformat()} does not exist in {tz.key}: "
                    "the DST spring-forward gap skips it. Choose a time either side "
                    "of the transition."
                ),
            )
        ]
    return local_to_utc(spec.local_datetime, tz, fold=spec.fold), []


# --- targets: one reference, three kinds (DESIGN.md 3.6) ---------------------
#
# Everything below asks the *reference* a question against an instant instead of
# reading coordinates off the item, because a body item has none to read. The
# two branches are kept explicit rather than hidden behind a common accessor:
# the whole failure this feature exists to prevent is code that treats a body's
# position as if it were a constant.


def _item_target(item: PlanItem) -> TargetSpec | None:
    """The item's target reference, or ``None`` for an item that names none."""
    if isinstance(item, SlewItem) and item.destination == "polar_best":
        return None
    target = getattr(item, "target", None)
    if target is None:
        return None
    assert isinstance(
        target, CatalogTargetSpec | BodyTargetSpec | CometTargetSpec
    )  # narrowing only
    return target


def _resolve_comet_position(
    spec: CometTargetSpec,
    ephemeris: Ephemeris,
    site: Site,
    when: datetime,
    catalog: CometCatalog | None,
) -> CometPosition:
    """Resolve a comet for compiler previews using the same safety gate as execution."""
    if catalog is None:
        raise ValueError("no comet catalogue was supplied while compiling this plan")
    if catalog.source_url != spec.source_url:
        raise ValueError("the plan's comet provenance does not match the bundled JPL catalogue")
    if catalog.fetched_at < spec.snapshot_fetched_at:
        raise ValueError(
            "the bundled comet catalogue is older than the snapshot used to author the plan"
        )
    comet = catalog.find(spk_id=spec.spk_id, designation=spec.designation)
    if comet is None:
        raise ValueError(
            f"JPL comet identity {spec.designation} / {spec.spk_id} is unavailable or mismatched"
        )
    rejection = comet_execution_rejection(catalog, comet, when)
    if rejection is not None:
        raise ValueError(rejection)
    return comet_position(ephemeris, site, comet, when)


def _target_ra_dec_at(
    spec: TargetSpec,
    ephemeris: Ephemeris,
    site: Site,
    when: datetime,
    comet_catalog: CometCatalog | None,
) -> tuple[Hours, Deg]:
    """Where the target is at ``when``. For a catalogue target ``when`` is
    ignored (its coordinates are time-independent); for a body it is the whole
    answer."""
    if isinstance(spec, BodyTargetSpec):
        resolved = resolve_position(ephemeris, site, spec.to_ref(), when)
        return resolved.ra_j2000_h, Deg(resolved.dec_j2000_deg)
    if isinstance(spec, CometTargetSpec):
        position = _resolve_comet_position(spec, ephemeris, site, when, comet_catalog)
        return position.ra_j2000_h, position.dec_j2000_deg
    return Hours(spec.ra_j2000_h), Deg(spec.dec_j2000_deg)


def _target_events_of(
    spec: TargetSpec,
    ephemeris: Ephemeris,
    site: Site,
    start: datetime,
    end: datetime,
    threshold: Deg,
    comet_catalog: CometCatalog | None,
) -> tuple[EventTime, ...]:
    """``rises_above``/``transits``/``sets_below`` for either kind of target.

    A body is re-observed at every instant the solver evaluates
    (:func:`ssc.astro.bodies.body_events`) rather than propagated from one
    position — over a night the Moon walks ~7 deg along its own orbit, which
    moves its own rise and set by many minutes.
    """
    if isinstance(spec, BodyTargetSpec):
        return body_events(ephemeris, site, spec.body, start, end, threshold)
    if isinstance(spec, CometTargetSpec):
        midpoint = start + (end - start) / 2
        position = _resolve_comet_position(spec, ephemeris, site, midpoint, comet_catalog)
        return target_events(
            ephemeris, site, position.ra_j2000_h, position.dec_j2000_deg, start, end, threshold
        )
    return target_events(
        ephemeris, site, Hours(spec.ra_j2000_h), Deg(spec.dec_j2000_deg), start, end, threshold
    )


def _resolve_event(
    item: PlanItem,
    spec: EventTimeSpec | EventEndSpec,
    solar: tuple[EventTime, ...],
    lunar: tuple[EventTime, ...],
    *,
    field: str = "start",
) -> tuple[datetime | None, list[Diagnostic]]:
    """Shared by a ``start``'s :class:`EventTimeSpec` and an ``end``'s
    :class:`EventEndSpec` — the two share the same ``event``/``offset_min``
    shape, so nothing about resolving "which event, plus what offset" differs
    between them; only ``field`` names which one, for the diagnostic."""
    pool = solar if isinstance(spec.event, SolarEvent) else lunar
    found = _find(pool, spec.event)
    if found is None or found.when is None:
        outcome = found.outcome if found is not None else Outcome.NOT_IN_WINDOW
        return None, [
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="event_does_not_occur",
                message=(
                    f"{spec.event} did not occur in the night window ({outcome}); "
                    f"this item's {field} cannot be given a guessed time"
                ),
            )
        ]
    return found.when + timedelta(minutes=spec.offset_min), []


def _resolve_target_event(
    item: PlanItem,
    spec: TargetEventTimeSpec | TargetEventEndSpec,
    ephemeris: Ephemeris,
    site: Site,
    window_start_utc: datetime,
    window_end_utc: datetime,
    comet_catalog: CometCatalog | None,
    *,
    field: str = "start",
) -> tuple[datetime | None, list[Diagnostic]]:
    """Shared by a ``start``'s :class:`TargetEventTimeSpec` and an ``end``'s
    :class:`TargetEventEndSpec` — see :func:`_resolve_event`."""
    target_spec = _item_target(item)
    if target_spec is None:
        return None, [
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="target_event_needs_target",
                message=(
                    f"a target_event {field} requires a target-bearing item "
                    "(goto_and_stack, mosaic, slew, or sync with a target)"
                ),
            )
        ]
    threshold = Deg(spec.alt_deg) if spec.alt_deg is not None else Deg(0.0)
    try:
        events = _target_events_of(
            target_spec,
            ephemeris,
            site,
            window_start_utc,
            window_end_utc,
            threshold,
            comet_catalog,
        )
    except ValueError as exc:
        return None, [
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="comet_ephemeris_unavailable",
                message=str(exc),
            )
        ]
    found = _find(events, spec.event)
    if found is None or found.when is None:
        outcome = found.outcome if found is not None else Outcome.NOT_IN_WINDOW
        return None, [
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="target_event_does_not_occur",
                message=(
                    f"{spec.event} did not occur for this target in the night window "
                    f"({outcome}); this item's {field} cannot be given a guessed time"
                ),
            )
        ]
    return found.when + timedelta(minutes=spec.offset_min), []


def _resolve_chained(
    item: PlanItem, spec: RelativeTimeSpec | AsapTimeSpec, prev_end: datetime | None
) -> tuple[datetime | None, list[Diagnostic]]:
    if prev_end is None:
        code = "relative_no_previous" if isinstance(spec, RelativeTimeSpec) else "asap_no_previous"
        return None, [
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code=code,
                message=(
                    "no previous (enabled) item's completion time to chain from; "
                    f"{spec.kind} cannot be resolved"
                ),
            )
        ]
    offset = spec.offset_min if isinstance(spec, RelativeTimeSpec) else 0.0
    return prev_end + timedelta(minutes=offset), []


# --- EndSpec resolution -------------------------------------------------


def _resolve_end(
    item: PlanItem,
    spec: EndSpec | None,
    start_utc: datetime,
    ephemeris: Ephemeris,
    site: Site,
    window_start_utc: datetime,
    window_end_utc: datetime,
    solar: tuple[EventTime, ...],
    lunar: tuple[EventTime, ...],
    comet_catalog: CometCatalog | None,
) -> tuple[datetime | None, list[Diagnostic]]:
    """``end`` resolved through the same resolvers ``start`` uses
    (:func:`_resolve_event`/:func:`_resolve_target_event`), then checked
    against ``start_utc``: an event/target_event end at or before the start
    is exactly as unschedulable as an end whose event never occurs — never a
    guessed end (CLAUDE.md invariant 7). ``spec is None`` means "no end
    specified", the historical instantaneous-item default: the end equals
    the start. A ``duration`` end is never checked against ``start_utc`` —
    ``DurationEndSpec.minutes`` is validated ``> 0``, so it is always after
    the start by construction; the only way it could compute otherwise is
    sub-microsecond float rounding in ``timedelta``, not a real conflict.
    """
    if spec is None:
        return start_utc, []

    if isinstance(spec, DurationEndSpec):
        return start_utc + timedelta(minutes=spec.minutes), []

    end_utc: datetime | None
    diagnostics: list[Diagnostic]
    if isinstance(spec, EventEndSpec):
        end_utc, diagnostics = _resolve_event(item, spec, solar, lunar, field="end")
    elif isinstance(spec, TargetEventEndSpec):
        end_utc, diagnostics = _resolve_target_event(
            item,
            spec,
            ephemeris,
            site,
            window_start_utc,
            window_end_utc,
            comet_catalog,
            field="end",
        )
    else:
        raise AssertionError(f"unhandled EndSpec: {spec!r}")  # exhaustiveness guard

    if end_utc is not None and end_utc <= start_utc:
        diagnostics = [
            *diagnostics,
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="end_before_start",
                message=(
                    f"resolved end {end_utc.isoformat()} is not after the start "
                    f"{start_utc.isoformat()}; this item's end cannot be scheduled"
                ),
            ),
        ]
        end_utc = None
    return end_utc, diagnostics


# --- the exposure block (DESIGN.md 4.3.0) ------------------------------------


def _exposure_block(
    item: TakeExposuresItem,
    start_utc: datetime,
    end_utc: datetime | None,
    tz: ZoneInfo,
    limits: CameraLimits | None,
) -> tuple[datetime | None, str | None, list[Diagnostic]]:
    """An exposure block's end, its echo, and everything the compiler can
    honestly say about it before the night.

    A **fixed count with no end of its own** is the one item in the vocabulary
    whose end is *derived*: sixty 30-second frames take sixty whole cycles, so
    that is what the Gantt shows and what the next ``relative`` item chains
    from. The echo says so in full (``60 x 30 s + 4 s overhead -> 23:34``)
    rather than presenting the number as though the user typed it — DESIGN.md
    4.1: "the abstraction must never hide what will actually happen". It is an
    estimate, and it is deliberately *not* a window: nothing cuts that block
    short, because the user asked for sixty frames, not for a slot.

    Everything else here is a diagnostic. The compiler does not silently
    shorten a count, move an end, or clamp an exposure to what the camera
    accepts (CLAUDE.md invariant 7); it says what will happen and leaves the
    plan as written.
    """
    diagnostics = _camera_limit_diagnostics(item, limits)

    if item.end is None and isinstance(item.count, FixedCountSpec):
        cycle_s = cycle_duration_s(item.exposure_sec)
        derived_end = start_utc + timedelta(seconds=item.count.frames * cycle_s)
        echo = (
            f"{item.count.frames} x {item.exposure_sec:g} s + {FRAME_OVERHEAD_S:g} s overhead "
            f"→ {derived_end.astimezone(tz).strftime('%H:%M')}"
        )
        return derived_end, echo, diagnostics

    if isinstance(item.count, UntilBlockEndSpec) and (item.end is None or end_utc is None):
        diagnostics.append(
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="open_ended_exposure_needs_end",
                message=(
                    "this block exposes until its window closes, but "
                    + (
                        "it has no end"
                        if item.end is None
                        else "its end did not resolve (see this item's other diagnostics)"
                    )
                    + ": with no window there is no number of frames to take, and SSC "
                    "will not invent one"
                ),
            )
        )
        return end_utc, None, diagnostics

    if end_utc is not None:
        window_s = (end_utc - start_utc).total_seconds()
        fits = whole_cycles_in(window_s, item.exposure_sec)
        cycle_s = cycle_duration_s(item.exposure_sec)
        if fits == 0:
            diagnostics.append(
                Diagnostic(
                    severity=Severity.WARNING,
                    item_id=item.id,
                    code="no_whole_cycle_fits",
                    message=(
                        f"the block's window is {window_s / 60.0:.1f} min but one whole "
                        f"cycle is {cycle_s:.1f} s ({item.exposure_sec:g} s exposure + "
                        f"{FRAME_OVERHEAD_S:g} s download); no frame can both start and "
                        "finish inside it, so the block will take none"
                    ),
                )
            )
        elif isinstance(item.count, FixedCountSpec) and fits < item.count.frames:
            diagnostics.append(
                Diagnostic(
                    severity=Severity.WARNING,
                    item_id=item.id,
                    code="count_exceeds_window",
                    message=(
                        f"{item.count.frames} frames of {item.exposure_sec:g} s need "
                        f"{item.count.frames * cycle_s / 60.0:.1f} min, but the block's "
                        f"window is {window_s / 60.0:.1f} min; it will stop after {fits} "
                        "frames rather than start one it cannot finish"
                    ),
                )
            )
    return end_utc, None, diagnostics


def _camera_limit_diagnostics(
    item: TakeExposuresItem, limits: CameraLimits | None
) -> list[Diagnostic]:
    """Check the block against what the camera *reported*, or say that nothing
    was checked. Never against a constant in this file (DESIGN.md 4.3.0)."""
    if limits is None:
        return [
            Diagnostic(
                severity=Severity.INFO,
                item_id=item.id,
                code="camera_limits_unknown",
                message=(
                    f"exposure {item.exposure_sec:g} s and gain {item.gain} were not "
                    "checked: no camera limits were supplied to this compile, and SSC "
                    "will not assume a range it has not read from the device"
                ),
            )
        ]

    diagnostics: list[Diagnostic] = []
    if not limits.exposure_min_s <= item.exposure_sec <= limits.exposure_max_s:
        diagnostics.append(
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="exposure_out_of_range",
                message=(
                    f"exposure {item.exposure_sec:g} s is outside the camera's own "
                    f"reported range [{limits.exposure_min_s:g}, {limits.exposure_max_s:g}] s"
                ),
            )
        )
    if not limits.gain_min <= item.gain <= limits.gain_max:
        diagnostics.append(
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="gain_out_of_range",
                message=(
                    f"gain {item.gain} is outside the camera's own reported range "
                    f"[{limits.gain_min}, {limits.gain_max}]"
                ),
            )
        )
    if (
        item.filter_name is not None
        and limits.filter_names
        and item.filter_name not in limits.filter_names
    ):
        diagnostics.append(
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="unknown_filter_position",
                message=(
                    f"filter {item.filter_name!r} is not one of the wheel's reported "
                    f"positions ({', '.join(limits.filter_names)})"
                ),
            )
        )
    return diagnostics


# --- mosaic expansion --------------------------------------------------------


def _expand_mosaic(
    item: MosaicItem,
    spec: TargetSpec | None,
    start_utc: datetime,
    per_panel: timedelta,
    ephemeris: Ephemeris,
    site: Site,
    profile: Profile | None,
) -> tuple[tuple[CompiledPanel, ...], list[Diagnostic], datetime]:
    if (
        item.mosaic.ra_num < 1
        or item.mosaic.dec_num < 1
        or not 0.0 <= item.mosaic.overlap_pct < 100.0
    ):
        return (
            (),
            [
                Diagnostic(
                    severity=Severity.ERROR,
                    item_id=item.id,
                    code="mosaic_invalid_grid",
                    message=(
                        f"ra_num={item.mosaic.ra_num}, dec_num={item.mosaic.dec_num}, "
                        f"overlap_pct={item.mosaic.overlap_pct} is not a valid grid; panels "
                        "were not expanded"
                    ),
                )
            ],
            start_utc,
        )

    if profile is None:
        return (
            (),
            [
                Diagnostic(
                    severity=Severity.WARNING,
                    item_id=item.id,
                    code="mosaic_panels_not_expanded",
                    message=(
                        "no scope profile was supplied to the compiler; panel geometry "
                        "needs a profile's FieldGeometry, so only the mosaic's start time "
                        "was resolved, not its panel centres"
                    ),
                )
            ],
            start_utc,
        )

    try:
        geometry = field_geometry(profile, item.framing.lens)
    except KeyError as exc:
        return (
            (),
            [
                Diagnostic(
                    severity=Severity.ERROR,
                    item_id=item.id,
                    code="unknown_lens",
                    message=str(exc),
                )
            ],
            start_utc,
        )

    # Body targets never reach here: ``compile_plan`` refuses a mosaic on one
    # before calling this (``mosaic_on_body_target``), so the target is a
    # catalogue one and its coordinates are the fixed centre the grid is laid
    # out around.
    assert isinstance(spec, CatalogTargetSpec)  # narrowing only
    grid = MosaicSpec(
        ra_num=item.mosaic.ra_num, dec_num=item.mosaic.dec_num, overlap_pct=item.mosaic.overlap_pct
    )
    centers = mosaic_panel_centers(
        Deg(float(hours_to_deg(Hours(spec.ra_j2000_h)))), Deg(spec.dec_j2000_deg), geometry, grid
    )

    panels: list[CompiledPanel] = []
    cursor = start_utc
    for center in centers:
        panel_end = cursor + per_panel
        panels.append(
            CompiledPanel(
                row=center.row,
                col=center.col,
                ra_j2000_deg=float(center.ra_j2000_deg),
                dec_j2000_deg=float(center.dec_j2000_deg),
                start_utc=cursor,
                end_utc=panel_end,
            )
        )
        cursor = panel_end
    return tuple(panels), [], cursor


# --- body re-centring segments -----------------------------------------------


def _frame_tolerance_arcmin(
    item: PlanItem, profile: Profile | None
) -> tuple[float | None, Diagnostic | None]:
    """How far the target may drift before the item re-centres, in arcmin.

    Derived from the optic the item's ``framing.lens`` names — never a constant,
    because a different optic is a different tolerance. Without a profile the
    frame size is genuinely unknown, so this returns ``None`` and a diagnostic
    saying the item was not segmented, rather than guessing a frame
    (CLAUDE.md invariant 7).
    """
    framing = getattr(item, "framing", None)
    lens = framing.lens if framing is not None else "tele"
    if profile is None:
        return None, Diagnostic(
            severity=Severity.WARNING,
            item_id=item.id,
            code="body_not_segmented",
            message=(
                "no scope profile was supplied to the compiler, so the frame size is "
                "unknown and this body item was not split into re-centring segments; "
                "it will run as one slew and the target will drift within the frame"
            ),
        )
    try:
        geometry = field_geometry(profile, lens)
    except KeyError as exc:
        return None, Diagnostic(
            severity=Severity.ERROR, item_id=item.id, code="unknown_lens", message=str(exc)
        )
    return float(geometry.inscribed_circle_deg) * 60.0 * MAX_BODY_DRIFT_FRACTION_OF_FRAME, None


def _recentre_segments(
    item: PlanItem,
    spec: BodyTargetSpec | CometTargetSpec,
    start_utc: datetime,
    end_utc: datetime,
    ephemeris: Ephemeris,
    site: Site,
    profile: Profile | None,
    comet_catalog: CometCatalog | None,
) -> tuple[tuple[CompiledPanel, ...], float | None, list[Diagnostic]]:
    """Cut a body item's window into equal segments, each ending before the body
    has drifted further than the frame tolerance.

    Returns the segments (coordinate-less — the executor resolves each one's
    centre when it starts), the interval in minutes, and any diagnostics. One
    segment covering the whole window means no re-centring was needed; that is
    the normal outcome for every planet.
    """
    diagnostics: list[Diagnostic] = []
    tolerance_arcmin, tolerance_diag = _frame_tolerance_arcmin(item, profile)
    if tolerance_diag is not None:
        diagnostics.append(tolerance_diag)
    window_min = (end_utc - start_utc).total_seconds() / 60.0
    if tolerance_arcmin is None or window_min <= 0.0:
        return (), None, diagnostics

    if isinstance(spec, BodyTargetSpec):
        motion = float(apparent_motion_arcmin_per_hour(ephemeris, site, start_utc, spec.body))
    else:
        first = _resolve_comet_position(spec, ephemeris, site, start_utc, comet_catalog)
        second = _resolve_comet_position(
            spec, ephemeris, site, start_utc + timedelta(hours=1), comet_catalog
        )
        mean_dec = math.radians(
            (float(first.dec_j2000_deg) + float(second.dec_j2000_deg)) / 2.0
        )
        dra_deg = (float(second.ra_j2000_h) - float(first.ra_j2000_h)) * 15.0
        dra_deg = (dra_deg + 180.0) % 360.0 - 180.0
        ddec_deg = float(second.dec_j2000_deg) - float(first.dec_j2000_deg)
        motion = math.hypot(dra_deg * math.cos(mean_dec), ddec_deg) * 60.0
    if motion <= 0.0:
        # A body that is not moving at all is not a body; refuse to divide by it
        # rather than emit an infinite segment count.
        return (), None, diagnostics
    interval_min = max(MIN_RECENTRE_INTERVAL_MIN, tolerance_arcmin / motion * 60.0)
    count = max(1, math.ceil(window_min / interval_min))
    segment = (end_utc - start_utc) / count

    segments: list[CompiledPanel] = []
    cursor = start_utc
    for index in range(count):
        segment_end = start_utc + segment * (index + 1)
        segments.append(
            CompiledPanel(
                row=0,
                col=0,
                ra_j2000_deg=None,
                dec_j2000_deg=None,
                start_utc=cursor,
                end_utc=segment_end,
            )
        )
        cursor = segment_end

    if count > 1:
        diagnostics.append(
            Diagnostic(
                severity=Severity.INFO,
                item_id=item.id,
                code=(
                    "body_recentred"
                    if isinstance(spec, BodyTargetSpec)
                    else "moving_target_recentred"
                ),
                message=(
                    f"{spec.name} moves {motion:.1f} arcmin/h; at a "
                    f"{tolerance_arcmin:.1f} arcmin tolerance "
                    f"({MAX_BODY_DRIFT_FRACTION_OF_FRAME:.0%} of the frame) this "
                    f"{window_min:.0f} min item is re-centred every "
                    f"{window_min / count:.0f} min, in {count} segments"
                ),
            )
        )
    return tuple(segments), window_min / count, diagnostics


def _compiled_target(
    spec: TargetSpec,
    start_utc: datetime | None,
    end_utc: datetime | None,
    ephemeris: Ephemeris,
    site: Site,
    recentre_interval_min: float | None,
    comet_catalog: CometCatalog | None,
) -> CompiledTarget:
    """The display echo. Nothing here is what the executor points at."""
    if isinstance(spec, CatalogTargetSpec):
        return CompiledTarget(
            kind=spec.kind,
            name=spec.name,
            tracking_rate=str(spec.tracking_rate),
            moves=False,
            ra_j2000_deg=float(hours_to_deg(Hours(spec.ra_j2000_h))),
            dec_j2000_deg=spec.dec_j2000_deg,
            resolved_at_utc=None,
            motion_arcmin_per_hour=None,
            drift_arcmin=None,
            recentre_interval_min=None,
        )
    if start_utc is None:
        # The start never resolved, so there is no instant to evaluate the
        # ephemeris at. The reference and its rate are still worth echoing.
        return CompiledTarget(
            kind=spec.kind,
            name=spec.name,
            tracking_rate=str(spec.tracking_rate),
            moves=True,
            ra_j2000_deg=None,
            dec_j2000_deg=None,
            resolved_at_utc=None,
            motion_arcmin_per_hour=None,
            drift_arcmin=None,
            recentre_interval_min=None,
        )
    if isinstance(spec, BodyTargetSpec):
        resolved = resolve_position(ephemeris, site, spec.to_ref(), start_utc)
        resolved_name = resolved.name
        resolved_ra_deg = float(resolved.ra_j2000_deg)
        resolved_dec_deg = float(resolved.dec_j2000_deg)
        motion = float(apparent_motion_arcmin_per_hour(ephemeris, site, start_utc, spec.body))
        tracking_rate = str(resolved.tracking_rate)
    else:
        comet_at_start = _resolve_comet_position(
            spec, ephemeris, site, start_utc, comet_catalog
        )
        comet_after_hour = _resolve_comet_position(
            spec, ephemeris, site, start_utc + timedelta(hours=1), comet_catalog
        )
        mean_dec = math.radians(
            (float(comet_at_start.dec_j2000_deg) + float(comet_after_hour.dec_j2000_deg)) / 2.0
        )
        dra_deg = (
            (float(comet_after_hour.ra_j2000_h) - float(comet_at_start.ra_j2000_h)) * 15.0
            + 180.0
        ) % 360.0 - 180.0
        ddec_deg = float(comet_after_hour.dec_j2000_deg) - float(
            comet_at_start.dec_j2000_deg
        )
        motion = math.hypot(dra_deg * math.cos(mean_dec), ddec_deg) * 60.0
        resolved_name = comet_at_start.comet.full_name
        resolved_ra_deg = float(hours_to_deg(comet_at_start.ra_j2000_h))
        resolved_dec_deg = float(comet_at_start.dec_j2000_deg)
        tracking_rate = str(spec.tracking_rate)
    drift: float | None = None
    if end_utc is not None and end_utc > start_utc:
        drift = motion * (end_utc - start_utc).total_seconds() / 3600.0
    return CompiledTarget(
        kind=spec.kind,
        name=resolved_name,
        tracking_rate=tracking_rate,
        moves=True,
        ra_j2000_deg=resolved_ra_deg,
        dec_j2000_deg=resolved_dec_deg,
        resolved_at_utc=start_utc,
        motion_arcmin_per_hour=motion,
        drift_arcmin=drift,
        recentre_interval_min=recentre_interval_min,
    )


# --- per-item diagnostics against the resolved window ------------------------


def _altitude_diagnostics(
    item: PlanItem,
    spec: TargetSpec,
    start_utc: datetime,
    end_utc: datetime,
    ephemeris: Ephemeris,
    site: Site,
    comet_catalog: CometCatalog | None,
) -> list[Diagnostic]:
    constraints = getattr(item, "constraints", None)
    min_alt_deg = constraints.min_alt_deg if constraints is not None else None
    if min_alt_deg is None or end_utc <= start_utc:
        return []
    events = _target_events_of(
        spec, ephemeris, site, start_utc, end_utc, Deg(min_alt_deg), comet_catalog
    )
    untimed = {e.event: e.outcome for e in events if e.when is None}
    outcome = untimed.get(TargetEvent.RISES_ABOVE) or untimed.get(TargetEvent.SETS_BELOW)
    if outcome is Outcome.ALWAYS_BELOW:
        return [
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="below_min_altitude",
                message=f"the target never reaches min_alt_deg={min_alt_deg} during this item's window",
            )
        ]
    if outcome is Outcome.ALWAYS_ABOVE:
        return []
    # Either an OCCURS crossing was found within the window, or the outcome
    # was NOT_IN_WINDOW for this narrow slice — either way the target is not
    # reliably above the limit for the whole item.
    return [
        Diagnostic(
            severity=Severity.WARNING,
            item_id=item.id,
            code="dips_below_min_altitude",
            message=(
                f"the target is not above min_alt_deg={min_alt_deg} for the whole of "
                "this item's window"
            ),
        )
    ]


def _moon_diagnostics(
    item: PlanItem,
    spec: TargetSpec,
    start_utc: datetime,
    ephemeris: Ephemeris,
    site: Site,
    comet_catalog: CometCatalog | None,
) -> list[Diagnostic]:
    constraints = getattr(item, "constraints", None)
    min_sep_deg = constraints.min_moon_sep_deg if constraints is not None else None
    if min_sep_deg is None:
        return []
    # Evaluated at the item's start only, for a body target as for a fixed one:
    # a min-moon-separation constraint is a "should I image this tonight at all"
    # question, and the compiler answers it with the geometry at the moment the
    # item begins rather than pretending to integrate it over the window.
    ra_h, dec_deg = _target_ra_dec_at(spec, ephemeris, site, start_utc, comet_catalog)
    instant = ephemeris.timescale.from_datetime(start_utc)
    observer = ephemeris.observer(site)
    apparent_moon = observer.at(instant).observe(ephemeris.moon).apparent()
    apparent_target = observer.at(instant).observe(target(ra_h, dec_deg)).apparent()
    separation_deg = float(apparent_moon.separation_from(apparent_target).degrees)
    if separation_deg < min_sep_deg:
        return [
            Diagnostic(
                severity=Severity.WARNING,
                item_id=item.id,
                code="moon_too_close",
                message=(
                    f"the Moon is {separation_deg:.1f} deg away at the item's start, "
                    f"closer than the {min_sep_deg:.1f} deg minimum"
                ),
            )
        ]
    return []


def _meridian_diagnostics(
    item: PlanItem,
    spec: TargetSpec,
    start_utc: datetime,
    end_utc: datetime,
    ephemeris: Ephemeris,
    site: Site,
    comet_catalog: CometCatalog | None,
) -> list[Diagnostic]:
    framing = getattr(item, "framing", None)
    if framing is None or framing.mode is not MountMode.ALTAZ or end_utc <= start_utc:
        return []
    events = _target_events_of(
        spec, ephemeris, site, start_utc, end_utc, Deg(0.0), comet_catalog
    )
    transit = next(
        (e for e in events if e.event == TargetEvent.TRANSITS and e.when is not None), None
    )
    if transit is None or transit.when is None:
        return []
    ra_h, dec_deg = _target_ra_dec_at(
        spec, ephemeris, site, transit.when, comet_catalog
    )
    position = target_position(ephemeris, site, ra_h, dec_deg, transit.when)
    if float(position.altitude_deg) < float(HIGH_ALTITUDE_TRANSIT_DEG):
        return []
    zenith_note = (
        " (within the zenith singularity — the parallactic angle is momentarily undefined)"
        if is_zenith_singular(position.hour_angle_deg, position.declination_deg, site.latitude_deg)
        else ""
    )
    return [
        Diagnostic(
            severity=Severity.WARNING,
            item_id=item.id,
            code="high_altitude_meridian_crossing",
            message=(
                f"transits at {float(position.altitude_deg):.1f} deg altitude around "
                f"{transit.when.strftime('%H:%M')} UTC; an alt-az session through a high "
                f"meridian crossing rotates the field sharply{zenith_note}"
            ),
        )
    ]


# --- the human echo (DESIGN.md 4.1: "the abstraction must never hide what
# will actually happen") -------------------------------------------------


def _resolution_string(spec: TimeSpec, start_utc: datetime | None, tz: ZoneInfo) -> str:
    label = _spec_label(spec)
    if start_utc is None:
        return f"{label} → unresolved"
    return f"{label} → {start_utc.astimezone(tz).strftime('%H:%M')}"


def _spec_label(spec: TimeSpec) -> str:
    if isinstance(spec, AbsoluteTimeSpec):
        return "Absolute"
    if isinstance(spec, EventTimeSpec):
        return _humanize(spec.event) + _offset_suffix(spec.offset_min)
    if isinstance(spec, TargetEventTimeSpec):
        alt = f" {spec.alt_deg:g}°" if spec.alt_deg is not None else ""
        return _humanize(spec.event) + alt + _offset_suffix(spec.offset_min)
    if isinstance(spec, RelativeTimeSpec):
        return "Previous item" + _offset_suffix(spec.offset_min)
    if isinstance(spec, AsapTimeSpec):
        return "ASAP after previous item"
    raise AssertionError(f"unhandled TimeSpec: {spec!r}")  # exhaustiveness guard


def _end_resolution_string(spec: EndSpec | None, end_utc: datetime | None, tz: ZoneInfo) -> str:
    label = _end_spec_label(spec)
    if end_utc is None:
        return f"{label} → unresolved"
    return f"{label} → {end_utc.astimezone(tz).strftime('%H:%M')}"


def _end_spec_label(spec: EndSpec | None) -> str:
    if spec is None:
        return "No end specified"
    if isinstance(spec, DurationEndSpec):
        return f"{spec.minutes:g} min"
    if isinstance(spec, EventEndSpec):
        return _humanize(spec.event) + _offset_suffix(spec.offset_min)
    if isinstance(spec, TargetEventEndSpec):
        alt = f" {spec.alt_deg:g}°" if spec.alt_deg is not None else ""
        return _humanize(spec.event) + alt + _offset_suffix(spec.offset_min)
    raise AssertionError(f"unhandled EndSpec: {spec!r}")  # exhaustiveness guard


def _offset_suffix(offset_min: float) -> str:
    return f" {offset_min:+g}" if offset_min != 0.0 else ""


def _humanize(value: str) -> str:
    text = value.replace("_", " ")
    return text[:1].upper() + text[1:]


def _plan_revision(plan: Plan) -> str:
    """A content hash of the plan, so a persisted ``CompiledSchedule`` can be
    checked for staleness without re-diffing the whole document."""
    return hashlib.sha256(plan.model_dump_json().encode("utf-8")).hexdigest()


__all__ = [
    "MAX_BODY_DRIFT_FRACTION_OF_FRAME",
    "MIN_RECENTRE_INTERVAL_MIN",
    "CompiledItem",
    "CompiledPanel",
    "CompiledSchedule",
    "CompiledTarget",
    "NightWindow",
    "ResolvedEvent",
    "compile_plan",
]
