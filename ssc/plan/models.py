# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The versioned Plan document (DESIGN.md 4) — user intent, portable JSON.

A ``Plan`` is authored (by hand, by the Planning tab's "Send to Scheduling",
or by a template) and never mutated in place: an edit produces a new ``Plan``
with a new ``updated_at``. It carries no absolute UTC times of its own — every
item's ``start`` is a :data:`TimeSpec`, resolved against the Settings
location/elevation/date only at :mod:`ssc.scheduler.compiler` time (DESIGN
4.1). That split is what keeps this module free of a clock: nothing here ever
calls ``datetime.now()``, and nothing here reaches an ephemeris.

``schema_version`` is checked, never guessed. An unknown version is refused
by :func:`parse_plan` with a message naming the version and the fix, rather
than best-effort parsed — a plan document long outlives the code that wrote
it, and a wrong guess here is a wrong observing night.

``None`` means unknown, everywhere in this file. It never means zero
(CLAUDE.md invariant 7).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ssc.astro.bodies import BodyId
from ssc.astro.events import LunarEvent, SolarEvent, TargetEvent
from ssc.astro.framing import MountMode
from ssc.astro.targets import BodyTarget, SunNotATargetError, TrackingRate

#: The only schema version this build *writes*. :func:`parse_plan` also accepts
#: every version in :data:`MIGRATABLE_PLAN_SCHEMA_VERSIONS` and migrates it
#: forward; anything else is refused outright, never best-effort parsed.
#:
#: Version 3 (2026-09-10) added the ``take_exposures`` item (DESIGN.md 4.3.0) —
#: one block carrying its own count, exposure, gain, filter, frame type and
#: dithering. It is a *new item kind*, so the migration from 2 is a relabel and
#: nothing more: a v2 document has no such item and loads unchanged. The bump
#: earns its keep in the other direction, which is the whole reason a document
#: is versioned — a v3 plan handed to a build that predates the item is refused
#: by :func:`parse_plan` with the version named, instead of that build silently
#: dropping the one item the night was actually about.
#:
#: Version 7 (2026-09-16) added execution-safe comet targets. A comet is
#: persisted by JPL identity and snapshot provenance, never by preview
#: coordinates; the executor resolves the current bundled orbit immediately
#: before motion.
#:
#: Version 6 (2026-09-15) made exposure, gain and filter intrinsic to
#: ``goto_and_stack`` and ``mosaic`` items so each independently scheduled
#: panel owns the camera state used for its capture.
#:
#: Version 5 (2026-09-15) added ``autofocus_before`` to ``goto_and_stack`` so
#: independently scheduled mosaic panels can own their focus decision. Older
#: plans migrate with the field's safe ``False`` default.
#:
#: Version 2 (2026-09-10) replaced an item's flat ``target_name``/``ra_j2000_h``/
#: ``dec_j2000_deg`` with a discriminated ``target`` object, so that a
#: solar-system body can be named without coordinates (DESIGN.md 3.6). A v1
#: document still loads — :meth:`TargetFields._flat_catalog_target_alias` folds
#: the flat fields into ``target={"kind": "catalog", ...}`` — but a v2 document
#: is *refused* by a v1 build, which is the point of the bump: a build that does
#: not understand body targets must say so by version rather than silently
#: dropping the target it cannot represent.
CURRENT_PLAN_SCHEMA_VERSION: Final = 7

#: Older versions :func:`parse_plan` knows how to bring forward.
MIGRATABLE_PLAN_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset({1, 2, 3, 4, 5, 6})


class PlanModel(BaseModel):
    """Base for every model in this package: frozen, and strict on the way in.

    ``extra="forbid"`` matters more here than almost anywhere else in SSC: a
    plan document is portable JSON that outlives any one build, and a typo'd
    field name silently dropped on load is exactly the kind of "best-effort
    parse" CLAUDE.md invariant 7 rules out.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# --- TimeSpec ----------------------------------------------------------------
#
# DESIGN.md 4.1's five kinds. Every one of them resolves to a UTC instant only
# at compile time (ssc.scheduler.compiler) against a Site, a tz and a night
# date — never here, and never against the wall clock.


class AbsoluteTimeSpec(PlanModel):
    """A wall-clock time authored by the user, e.g. ``2026-08-28T23:15`` local.

    ``local_datetime`` is naive on purpose: the zone is a property of
    *Settings*, not of the plan, so the same plan compiles correctly at a
    different site. ``fold`` picks which of the two instants an ambiguous
    autumn reading (01:30 occurring twice) means — ``0`` for the first
    (pre-transition) occurrence, ``1`` for the second — and is meaningless,
    but harmless, on a unique or nonexistent reading.
    """

    kind: Literal["absolute"] = "absolute"
    local_datetime: datetime
    fold: Literal[0, 1] = 0

    @field_validator("local_datetime")
    @classmethod
    def _must_be_naive(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            raise ValueError(
                "absolute TimeSpec.local_datetime must be a naive local wall-clock "
                "reading (no UTC offset in the JSON) — the zone comes from Settings "
                "at compile time, never from the plan document"
            )
        return value


class EventTimeSpec(PlanModel):
    """A solar or lunar event plus an offset, e.g. astronomical dusk + 15 min."""

    kind: Literal["event"] = "event"
    event: SolarEvent | LunarEvent
    offset_min: float = 0.0


class TargetEventTimeSpec(PlanModel):
    """``rises_above`` / ``transits`` / ``sets_below`` for the item's own target.

    Only meaningful on an item that carries a target (``goto_and_stack``,
    ``mosaic``, ``slew``, or a ``sync`` with explicit coordinates) — the
    compiler reports a diagnostic and marks the item unschedulable if not.
    ``alt_deg`` is required for ``rises_above``/``sets_below`` and must be
    omitted (or ``None``) for ``transits``, which has no altitude of its own.
    """

    kind: Literal["target_event"] = "target_event"
    event: TargetEvent
    alt_deg: float | None = None
    offset_min: float = 0.0

    @model_validator(mode="after")
    def _alt_required_unless_transit(self) -> TargetEventTimeSpec:
        if self.event is not TargetEvent.TRANSITS and self.alt_deg is None:
            raise ValueError(
                f"alt_deg is required for target_event {self.event!r}; only 'transits' can omit it"
            )
        return self


class RelativeTimeSpec(PlanModel):
    """``offset_min`` after the previous item's compiled end time."""

    kind: Literal["relative"] = "relative"
    offset_min: float = 0.0


class AsapTimeSpec(PlanModel):
    """As soon as the previous item completes — a zero-offset ``relative``
    at compile time, since there is no live device to wait on here."""

    kind: Literal["asap"] = "asap"


TimeSpec = Annotated[
    AbsoluteTimeSpec | EventTimeSpec | TargetEventTimeSpec | RelativeTimeSpec | AsapTimeSpec,
    Field(discriminator="kind"),
]


# --- EndSpec -------------------------------------------------------------
#
# An item's end, expressed the same way its start is: either a plain duration
# or "until an event" — astronomical dawn, a target setting below an altitude,
# and so on. Resolved at compile time exactly like TimeSpec (DESIGN.md 4.1);
# never here, and never against the wall clock.


class DurationEndSpec(PlanModel):
    """End ``minutes`` after the item's resolved start — the historical
    ``duration_min`` behaviour, now spelled as one kind of :data:`EndSpec`."""

    kind: Literal["duration"] = "duration"
    minutes: Annotated[float, Field(gt=0.0)]


class EventEndSpec(PlanModel):
    """End at a solar or lunar event plus an offset — e.g. astronomical dawn
    minus 30 minutes. Same event vocabulary as :class:`EventTimeSpec`."""

    kind: Literal["event"] = "event"
    event: SolarEvent | LunarEvent
    offset_min: float = 0.0


class TargetEventEndSpec(PlanModel):
    """``rises_above`` / ``transits`` / ``sets_below`` for the item's own
    target, as an end rather than a start. Same rule as
    :class:`TargetEventTimeSpec`: only meaningful on a target-bearing item —
    the compiler (and :func:`ssc.plan.validate.validate_plan`) reports a
    diagnostic and marks the item unschedulable if not. ``alt_deg`` is
    required for ``rises_above``/``sets_below`` and must be omitted (or
    ``None``) for ``transits``.
    """

    kind: Literal["target_event"] = "target_event"
    event: TargetEvent
    alt_deg: float | None = None
    offset_min: float = 0.0

    @model_validator(mode="after")
    def _alt_required_unless_transit(self) -> TargetEventEndSpec:
        if self.event is not TargetEvent.TRANSITS and self.alt_deg is None:
            raise ValueError(
                f"alt_deg is required for target_event {self.event!r}; only 'transits' can omit it"
            )
        return self


EndSpec = Annotated[
    DurationEndSpec | EventEndSpec | TargetEventEndSpec,
    Field(discriminator="kind"),
]


# --- shared per-kind field groups --------------------------------------------


class FramingSpec(PlanModel):
    """DESIGN.md 3.4 ``framing``. ``lens`` names a profile optic id (``"tele"``,
    ``"wide"``) — resolved against the active scope profile at compile time,
    never validated against a catalogue here."""

    mode: MountMode = MountMode.ALTAZ
    pa_offset_deg: float = 0.0
    lens: str = "tele"


class ConstraintsSpec(PlanModel):
    """DESIGN.md 3.4 ``constraints``. ``None`` means "no limit requested",
    never zero."""

    min_alt_deg: float | None = None
    min_moon_sep_deg: float | None = None


class MosaicGridSpec(PlanModel):
    """DESIGN.md 3.3/3.4 ``mosaic``. Deliberately unconstrained here — the
    ``ra_num``/``dec_num`` >= 1 and ``overlap_pct`` in range checks live in
    :func:`ssc.plan.validate.validate_plan` as diagnostics, not as a hard
    parse failure, so a malformed grid can still be loaded, shown and fixed
    in the UI rather than rejected outright."""

    ra_num: int = 1
    dec_num: int = 1
    overlap_pct: float = 20.0


# --- TargetSpec ---------------------------------------------------------------
#
# DESIGN.md 3.6. An item's target is one of three *references*, never a mixture:
# fixed catalogue coordinates, the name of a solar-system body, or a durable
# JPL comet identity. The moving-target
# case carries no coordinates at all, which is the whole point — measured over
# 6 h of one night the Moon moves 233.6' in RA, Mercury 22.7', Mars 10.1',
# Jupiter 3.1', Uranus 0.005'. A plan that resolved "Moon" when it was written
# and stored the answer would image empty sky; the plan holds the reference and
# the executor asks where it is when the item starts.
#
# A discriminated union rather than one record with nullable coordinates: a
# nullable position is a position someone will eventually read without checking.


class CatalogTargetSpec(PlanModel):
    """Fixed J2000 coordinates, as the Planning tab hands them over (DESIGN.md
    3.4 boundary: RA in hours, Dec in degrees). This is what every plan item
    carried before schema_version 2, and what a v1 document migrates into."""

    kind: Literal["catalog"] = "catalog"
    name: str | None = None
    ra_j2000_h: Annotated[float, Field(ge=0.0, lt=24.0)]
    dec_j2000_deg: Annotated[float, Field(ge=-90.0, le=90.0)]

    @property
    def tracking_rate(self) -> TrackingRate:
        return TrackingRate.SIDEREAL

    @property
    def moves(self) -> bool:
        return False


class BodyTargetSpec(PlanModel):
    """A solar-system body named, never located (DESIGN.md 3.6).

    **The Sun is refused here, not merely absent.** A plan document is portable
    JSON and therefore untrusted input: it can be hand-edited, imported, or
    written by another tool. ``_refuse_the_sun`` runs
    :class:`ssc.astro.targets.BodyTarget`'s own constructor so that this layer
    can never drift from the resolver's rule, and re-raises as a ``ValueError``
    so the refusal arrives as an ordinary validation error (a 422 at the API
    boundary) carrying the full reason rather than as a 500.

    There is deliberately **no tracking-rate field**. DESIGN.md 3.6: "A body
    target and its tracking rate are one decision, not two" — the rate is a
    function of the body (:func:`ssc.astro.targets.tracking_rate_for_body`), so
    the document has no way to express a wrong one.
    """

    kind: Literal["body"] = "body"
    body: BodyId

    @field_validator("body")
    @classmethod
    def _refuse_the_sun(cls, value: BodyId) -> BodyId:
        try:
            BodyTarget(value)
        except SunNotATargetError as exc:
            raise ValueError(str(exc)) from exc
        return value

    def to_ref(self) -> BodyTarget:
        """The :mod:`ssc.astro` reference this names. Refuses the Sun a second
        time, in the resolver's own words, for any path that reached here
        without validation."""
        return BodyTarget(self.body)

    @property
    def name(self) -> str:
        return self.body.value.capitalize()

    @property
    def tracking_rate(self) -> TrackingRate:
        return self.to_ref().tracking_rate

    @property
    def moves(self) -> bool:
        return True


class CometTargetSpec(PlanModel):
    """A comet stored by durable JPL identity and source provenance.

    Preview coordinates are intentionally absent. At execution the current
    bundled snapshot must contain the same ``spk_id`` + ``designation`` pair,
    pass the conservative orbit-quality guards, and be propagated for the
    actual slew instant. ``snapshot_fetched_at`` records what the user selected
    without pinning execution to that possibly older set of elements.
    """

    kind: Literal["comet"] = "comet"
    spk_id: Annotated[int, Field(ge=1)]
    designation: Annotated[str, Field(min_length=1)]
    full_name: Annotated[str, Field(min_length=1)]
    source_url: Annotated[str, Field(min_length=1)]
    snapshot_fetched_at: datetime
    solution_date: str | None = None
    element_epoch_jd_tdb: float

    @field_validator("snapshot_fetched_at")
    @classmethod
    def _require_aware_snapshot_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("comet snapshot_fetched_at must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def name(self) -> str:
        return self.full_name

    @property
    def tracking_rate(self) -> TrackingRate:
        return TrackingRate.SIDEREAL

    @property
    def moves(self) -> bool:
        return True


TargetSpec = Annotated[
    CatalogTargetSpec | BodyTargetSpec | CometTargetSpec,
    Field(discriminator="kind"),
]


def _flat_target_alias(data: Any, *, required: bool) -> Any:
    """Fold a schema_version 1 item's flat ``target_name``/``ra_j2000_h``/
    ``dec_j2000_deg`` into ``target={"kind": "catalog", ...}``.

    The same shape of deprecated-alias migration :meth:`PlanItemBase.
    _duration_min_alias` already does for ``duration_min``, and the reason v1
    documents still load unchanged. The flat fields are dropped, never
    re-serialised: a plan saved by this build is a v2 document.
    """
    if not isinstance(data, Mapping):
        return data
    flat = {key for key in ("target_name", "ra_j2000_h", "dec_j2000_deg") if key in data}
    if not flat:
        return data
    data = dict(data)
    name = data.pop("target_name", None)
    ra = data.pop("ra_j2000_h", None)
    dec = data.pop("dec_j2000_deg", None)
    if data.get("target") is not None:
        raise ValueError(
            "an item carries both the schema_version 1 flat target fields "
            f"({', '.join(sorted(flat))}) and a schema_version 2 'target' object; "
            "they are two spellings of one thing, so give exactly one"
        )
    if ra is None and dec is None:
        # A v1 ``sync`` with only a label, or none of the three: there is no
        # catalogue position to build, so leave ``target`` absent rather than
        # inventing coordinates (architecture policy invariant 7).
        if required:
            raise ValueError(
                "a target-bearing item needs either ra_j2000_h + dec_j2000_deg or a 'target' object"
            )
        return data
    data["target"] = {"kind": "catalog", "name": name, "ra_j2000_h": ra, "dec_j2000_deg": dec}
    return data


class TargetFields(PlanModel):
    """The item's own target — required.

    ``target_name``/``ra_j2000_h``/``dec_j2000_deg`` survive as read-only
    properties so that every caller which already asks an item where it points
    keeps working; they are ``None`` for a body target, which is the honest
    answer (CLAUDE.md invariant 7) and forces a caller that needs real
    coordinates to resolve the reference against an instant.
    """

    target: TargetSpec

    @model_validator(mode="before")
    @classmethod
    def _flat_catalog_target_alias(cls, data: Any) -> Any:
        return _flat_target_alias(data, required=True)

    @property
    def target_name(self) -> str | None:
        return self.target.name

    @property
    def ra_j2000_h(self) -> float | None:
        """J2000 RA in hours for a fixed target; ``None`` for a body, whose
        position is a fact about an instant and has to be asked for with one."""
        return self.target.ra_j2000_h if isinstance(self.target, CatalogTargetSpec) else None

    @property
    def dec_j2000_deg(self) -> float | None:
        return self.target.dec_j2000_deg if isinstance(self.target, CatalogTargetSpec) else None


class OptionalTargetFields(PlanModel):
    """As :class:`TargetFields`, but the target may be absent altogether — for
    a ``sync`` that syncs to the last plate solve rather than to a named
    target."""

    target: TargetSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def _flat_catalog_target_alias(cls, data: Any) -> Any:
        return _flat_target_alias(data, required=False)

    @property
    def target_name(self) -> str | None:
        return self.target.name if self.target is not None else None

    @property
    def ra_j2000_h(self) -> float | None:
        return self.target.ra_j2000_h if isinstance(self.target, CatalogTargetSpec) else None

    @property
    def dec_j2000_deg(self) -> float | None:
        return self.target.dec_j2000_deg if isinstance(self.target, CatalogTargetSpec) else None


# --- item base -----------------------------------------------------------


class PlanItemBase(PlanModel):
    """Fields every item kind shares.

    ``end`` is an *estimate* for Gantt display and dark-window accounting,
    never a completion signal — completion comes from device events at
    execution time (CLAUDE.md), so a compiled ``end_utc`` derived from it is
    a placeholder, not a promise. ``None``/omitted means an instantaneous
    item (its resolved end equals its resolved start).

    ``duration_min`` is accepted on input as a **deprecated alias** for
    ``end={"kind": "duration", "minutes": duration_min}`` — schema_version 1
    shipped with a bare ``duration_min`` float before an item could end "until
    an event", and existing stored plans still use it. :meth:`_duration_min_alias`
    maps it onto ``end`` (only when ``end`` itself is absent) and drops the old
    field; it is never re-serialised. This alias may be removed once
    ``CURRENT_PLAN_SCHEMA_VERSION`` is bumped past 1.
    """

    id: Annotated[str, Field(min_length=1)]
    start: TimeSpec
    end: EndSpec | None = None
    enabled: bool = True
    planning_estimate_min: Annotated[float, Field(gt=0.0)] | None = None
    timeout_min: Annotated[float, Field(gt=0.0)] | None = None

    @model_validator(mode="before")
    @classmethod
    def _duration_min_alias(cls, data: Any) -> Any:
        if not isinstance(data, Mapping) or "duration_min" not in data:
            return data
        data = dict(data)
        minutes = data.pop("duration_min")
        # 0/None both meant "instantaneous item" under the old field (see the
        # class docstring); DurationEndSpec.minutes must be > 0, so both map
        # onto "no end specified" rather than a rejected zero-length end.
        if data.get("end") is None and minutes is not None and minutes > 0:
            data["end"] = {"kind": "duration", "minutes": minutes}
        return data


# --- the DESIGN.md 4.3 item vocabulary ---------------------------------------


class StartupItem(PlanItemBase):
    """``ssc.startup`` — level, dark, calibrate. Gate everything behind this."""

    kind: Literal["startup"] = "startup"
    target: TargetSpec | None = None
    polar_point_count: int | None = None
    polar_step_deg: float | None = None
    polar_exposure_s: float | None = None
    polar_gain: int | None = None
    polar_start_point: Literal["best", "current", "seestar", "explicit"] | None = None
    target_ra_hours: float | None = None
    target_dec_deg: float | None = None


class WaitItem(PlanItemBase):
    """Scheduler-internal: a pause, compiled purely from ``start``/``end``."""

    kind: Literal["wait"] = "wait"
    requires_confirmation: bool = False
    instructions: str | None = None


class AutofocusItem(PlanItemBase):
    """``Focuser`` + ``ssc.autofocus``.

    ``temperature_trigger_delta_c`` is the device-side re-trigger threshold
    (DESIGN.md 4.3: "schedule by temperature delta, not only by clock"); the
    compiler only resolves ``start`` from the clock — evaluating a live
    temperature delta is an executor concern, out of scope for a pure,
    device-free compile.
    """

    kind: Literal["autofocus"] = "autofocus"
    temperature_trigger_delta_c: float | None = None


class DitherSpec(PlanModel):
    """Absolute, bounded dither range and frame cadence for an imaging item."""

    pixels: Annotated[float, Field(gt=0.0)] | None = None
    min_pixels: Annotated[float, Field(ge=0.0)] | None = None
    max_pixels: Annotated[float, Field(gt=0.0)] | None = None
    every_n_frames: Annotated[int, Field(ge=1)] = 6

    @model_validator(mode="after")
    def _range_is_ordered(self) -> DitherSpec:
        if (self.min_pixels is None) != (self.max_pixels is None):
            raise ValueError("dither min_pixels and max_pixels must be provided together")
        if (
            self.min_pixels is not None
            and self.max_pixels is not None
            and self.min_pixels > self.max_pixels
        ):
            raise ValueError("dither min_pixels must not exceed max_pixels")
        return self


def _default_dither() -> DitherSpec:
    return DitherSpec(min_pixels=15.0, max_pixels=200.0, every_n_frames=6)


class GotoAndStackItem(PlanItemBase, TargetFields):
    """``ssc.goto_and_stack``."""

    kind: Literal["goto_and_stack"] = "goto_and_stack"
    framing: FramingSpec = FramingSpec()
    constraints: ConstraintsSpec = ConstraintsSpec()
    #: Run autofocus after this item's slew and before live stacking starts.
    #: This belongs to the imaging item so independently scheduled mosaic
    #: panels can choose it independently too.
    autofocus_before: bool = False
    exposure_sec: Annotated[float, Field(gt=0.0)] = 10.0
    gain: Annotated[int, Field(ge=0)] = 200
    filter_name: str | None = None
    live_stack: bool = True
    dither: DitherSpec | None = Field(default_factory=_default_dither)


class MosaicItem(PlanItemBase, TargetFields):
    """``ssc.mosaic``. A ``"duration"``-kind ``end`` here is *per panel* — the
    compiler lays panels out end to end, so the item's total span is
    ``panel_count * end.minutes``. An ``"event"``/``"target_event"`` ``end``
    is not meaningful per-panel and is treated as no duration (0 min).
    """

    kind: Literal["mosaic"] = "mosaic"
    framing: FramingSpec = FramingSpec()
    mosaic: MosaicGridSpec = MosaicGridSpec()
    constraints: ConstraintsSpec = ConstraintsSpec()
    exposure_sec: Annotated[float, Field(gt=0.0)] = 10.0
    gain: Annotated[int, Field(ge=0)] = 200
    filter_name: str | None = None
    live_stack: bool = True
    dither: DitherSpec | None = Field(default_factory=_default_dither)


class SetExposureItem(PlanItemBase):
    """``ssc.set_exposure``."""

    kind: Literal["set_exposure"] = "set_exposure"
    exposure_sec: float


class SetGainItem(PlanItemBase):
    """``Camera.Gain``."""

    kind: Literal["set_gain"] = "set_gain"
    gain: int


class FilterItem(PlanItemBase):
    """``FilterWheel.Position``. ``position`` matches the ASCOM index-or-name
    the driver accepts (e.g. ``"Dark"`` for the calibration shutter position)."""

    kind: Literal["filter"] = "filter"
    position: str


class HeaterItem(PlanItemBase):
    """``ssc.set_heater``.

    DESIGN.md 4.3: "schedule by dew-point margin, not by clock" — as with
    autofocus, evaluating a live dew-point margin is an executor concern;
    the compiler resolves only the clock-based fallback ``start``.
    """

    kind: Literal["heater"] = "heater"
    heater_on: bool = True


class DitherItem(PlanItemBase):
    """``ssc.set_dither``."""

    kind: Literal["dither"] = "dither"
    dither_pixels: float | None = None


class SolveItem(PlanItemBase):
    """``ssc.solve`` / ``ssc.last_solve``, at the current pointing."""

    kind: Literal["solve"] = "solve"


class SyncItem(PlanItemBase, OptionalTargetFields):
    """``Telescope.SyncToCoordinates``. Omit coordinates to sync to the last
    plate solve instead of named target coordinates."""

    kind: Literal["sync"] = "sync"


class SlewItem(PlanItemBase, OptionalTargetFields):
    """``Telescope.SlewToCoordinatesAsync`` to a target or PA best field."""

    kind: Literal["slew"] = "slew"
    destination: Literal["target", "polar_best"] | None = None
    unpark_before: bool = False

    @model_validator(mode="before")
    @classmethod
    def _discard_obsolete_controls(cls, data: Any) -> Any:
        """Accept plans saved before Slew's PA/parking controls were removed."""
        if not isinstance(data, Mapping):
            return data
        cleaned = dict(data)
        cleaned.pop("polar_point_count", None)
        cleaned.pop("polar_step_deg", None)
        cleaned.pop("park_after", None)
        return cleaned

    @model_validator(mode="after")
    def _destination_has_target(self) -> SlewItem:
        if (self.destination or "target") == "target" and self.target is None:
            raise ValueError("a target slew requires target coordinates or a body")
        return self


class FindHomeItem(PlanItemBase):
    """``Telescope.FindHome`` (shown as Slew home)."""

    kind: Literal["find_home"] = "find_home"
    park_after: bool = False


class PolarAlignmentItem(PlanItemBase):
    """Run the unattended polar-alignment procedure."""

    kind: Literal["polar_alignment"] = "polar_alignment"
    point_count: int = 3
    step_deg: float = 30.0
    exposure_s: float = 2.0
    gain: int = 200
    start_point: Literal["best", "current", "seestar", "explicit"] = "best"
    start_ra_hours: float | None = None
    start_dec_deg: float | None = None


class ParkItem(PlanItemBase):
    """``Telescope.Park``."""

    kind: Literal["park"] = "park"


class UnparkItem(PlanItemBase):
    """``Telescope.Unpark``."""

    kind: Literal["unpark"] = "unpark"


class CaptureCalibrationItem(PlanItemBase):
    """``ssc.capture_calibration`` — darks matched to the session just imaged.
    Scheduled *after* the lights (DESIGN.md 4.3), typically at nautical dawn
    with ``FilterWheel.Position = Dark``."""

    kind: Literal["capture_calibration"] = "capture_calibration"
    frame_type: Literal["bias", "dark", "flat"] = "dark"
    # ``None`` appeared in schema-v3 plans, where the action was only a
    # placeholder.  Keep accepting it during migration and resolve it to the
    # safe default of 50 in the shared calibration runner.
    count: Annotated[int, Field(ge=1, le=500)] | None = 50
    exposure_sec: Annotated[float, Field(gt=0.0)] | None = 30.0
    gain: Annotated[int, Field(ge=0)] | None = 200
    filter_name: str | None = "Dark"
    optic_id: str | None = "tele"
    flat_source: Literal["panel", "sky"] | None = None
    generate_master: bool = True
    safe_state: bool = True

    @model_validator(mode="after")
    def fields_match_frame_type(self) -> CaptureCalibrationItem:
        if self.frame_type == "dark" and self.exposure_sec is None:
            raise ValueError("dark calibration requires exposure_sec")
        if self.frame_type == "flat" and (self.flat_source is None or self.optic_id is None):
            raise ValueError("flat calibration requires optic_id and flat_source")
        return self


class FrameType(StrEnum):
    """What an exposure *is* — the same closed set, and the same wire strings,
    as :class:`ssc.capture.frame.FrameType`.

    Restated here rather than imported, for exactly the reason
    :class:`ssc.store.capture.CaptureFrameRecord` restates it too: ``ssc.plan``
    is pure (CLAUDE.md invariant 3), and ``ssc.capture``'s package ``__init__``
    reaches ``ssc.store`` — importing it here would drag SQLite in behind the
    plan document. The two enums are held to the same four values by
    ``tests/plan/test_models.py``, which may import both.

    A dark is *not* a separate item kind: it is a ``take_exposures`` block with
    ``frame_type: dark`` through the ``Dark`` shutter position (DESIGN.md 4.3.0).
    """

    LIGHT = "light"
    DARK = "dark"
    FLAT = "flat"
    BIAS = "bias"


class FixedCountSpec(PlanModel):
    """A known integration: exactly ``frames`` frames, however long that takes."""

    kind: Literal["fixed"] = "fixed"
    frames: Annotated[int, Field(ge=1)]


class UntilBlockEndSpec(PlanModel):
    """Expose until the block's own window closes — "image this until it sets".

    A block with this count **must** carry an ``end``: without one there is no
    window to fill, which :func:`ssc.plan.validate.validate_plan` and the
    compiler both report rather than guessing a duration
    (CLAUDE.md invariant 7).
    """

    kind: Literal["until_block_end"] = "until_block_end"


ExposureCount = Annotated[
    FixedCountSpec | UntilBlockEndSpec,
    Field(discriminator="kind"),
]


class CenterAfterDriftSpec(PlanModel):
    enabled: bool = True
    threshold_pixels: Annotated[float, Field(gt=0.0)] = 400.0


class TakeExposuresItem(PlanItemBase, OptionalTargetFields):
    """**The exposure block** (DESIGN.md 4.3.0) — one item carrying every
    parameter one imaging run needs.

    > Owner, 2026-09-10: "i would like to have univerzal take exposures block
    > within which we would configure how many or if until the end of block,
    > what gain, what filter, which type, what exposure time, and dithering
    > settings all withing the exposure block".

    The block *sets* what it needs rather than inheriting it: the executor
    issues this item's own filter, exposure and gain before its first frame,
    every time. That is what makes a block reproducible in isolation, survive
    reordering, and resume without archaeology — a few redundant property
    writes per block cost nothing against a 30-second exposure, and the
    alternative (``set_gain`` an hour ago, invisible here) is the state leak
    that decomposition into verbs made inevitable.

    ``exposure_sec`` and ``gain`` are bounded by the *camera's own reported*
    limits, never by a constant in this file: the check lives in
    :func:`ssc.scheduler.compiler.compile_plan`, which takes them as
    :class:`ssc.scheduler.exposure.CameraLimits` read from
    ``Camera.ExposureMin``/``ExposureMax``/``GainMin``/``GainMax``. Without
    those limits the compiler says so as a diagnostic rather than assuming the
    value is fine.

    ``filter_name`` is a ``FilterWheel`` position by name (``"Dark"``, ``"IR"``,
    ``"LP"`` on the S30 Pro). ``None`` means the block does not touch the
    wheel — honest, but a state leak by definition, so
    :func:`ssc.plan.validate.validate_plan` says so out loud.
    """

    kind: Literal["take_exposures"] = "take_exposures"
    count: ExposureCount
    exposure_sec: Annotated[float, Field(gt=0.0)]
    gain: Annotated[int, Field(ge=0)]
    filter_name: str | None = None
    frame_type: FrameType = FrameType.LIGHT
    dither: DitherSpec | None = None
    center_after_drift: CenterAfterDriftSpec | None = None
    framing: FramingSpec = FramingSpec()
    constraints: ConstraintsSpec = ConstraintsSpec()
    live_stack: bool = False


class ShutdownItem(PlanItemBase):
    """``ssc.shutdown``. Every plan should end with one — see
    :func:`ssc.plan.validate.validate_plan`."""

    kind: Literal["shutdown"] = "shutdown"


PlanItem = Annotated[
    StartupItem
    | WaitItem
    | AutofocusItem
    | GotoAndStackItem
    | MosaicItem
    | SetExposureItem
    | SetGainItem
    | FilterItem
    | HeaterItem
    | DitherItem
    | SolveItem
    | SyncItem
    | SlewItem
    | FindHomeItem
    | PolarAlignmentItem
    | ParkItem
    | UnparkItem
    | CaptureCalibrationItem
    | TakeExposuresItem
    | ShutdownItem,
    Field(discriminator="kind"),
]


# --- the Plan itself ----------------------------------------------------------


class Plan(PlanModel):
    """A whole observing plan: portable JSON, versioned (DESIGN.md 4.2)."""

    schema_version: Literal[7] = CURRENT_PLAN_SCHEMA_VERSION
    id: Annotated[str, Field(min_length=1)]

    @model_validator(mode="before")
    @classmethod
    def _migrate_version(cls, data: Any) -> Any:
        """Bring a known older ``schema_version`` forward, here rather than only
        in :func:`parse_plan`, because :class:`Plan` is embedded directly in the
        API's request models — a v1 document arriving over HTTP has to migrate
        exactly as a v1 document read from SQLite does.

        The migration itself is a relabel: v1's flat per-item target fields are
        folded into ``target`` by :func:`_flat_target_alias` during validation of
        the items. Anything *not* in
        :data:`MIGRATABLE_PLAN_SCHEMA_VERSIONS` is left alone and refused by the
        ``Literal`` below — :func:`parse_plan` catches it first and says why.
        """
        if isinstance(data, Mapping) and data.get("schema_version") in (
            MIGRATABLE_PLAN_SCHEMA_VERSIONS
        ):
            return {**data, "schema_version": CURRENT_PLAN_SCHEMA_VERSION}
        return data

    name: str
    items: tuple[PlanItem, ...] = ()
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "Plan.created_at/updated_at must be timezone-aware (CLAUDE.md "
                "invariant 5); naive datetimes are refused, never guessed at"
            )
        return value.astimezone(UTC)


class UnknownPlanVersionError(ValueError):
    """A plan's ``schema_version`` is not one this build understands.

    Raised instead of best-effort parsing (CLAUDE.md invariant 7 / DESIGN.md
    4.2): a wrong guess about document shape is a wrong observing night.
    """


def parse_plan(raw: Mapping[str, Any]) -> Plan:
    """Parse a plan document, migrating a known older ``schema_version`` forward
        and refusing an unknown one outright.

    The migration itself lives on
        :class:`Plan` (:meth:`Plan._migrate_version`), so that a document arriving
        through the API's request models migrates identically; what this function
        adds is the *refusal*, with the version named and the fix stated, instead of
        Pydantic's "input should be 2".

        Anything outside :data:`MIGRATABLE_PLAN_SCHEMA_VERSIONS` and
        :data:`CURRENT_PLAN_SCHEMA_VERSION` is refused with the version named
        (CLAUDE.md invariant 7 / DESIGN.md 4.2) — including a *newer* version, which
        is exactly what an older build must do when handed a plan containing a body
        target it has no way to represent.
    """
    version = raw.get("schema_version")
    if version not in MIGRATABLE_PLAN_SCHEMA_VERSIONS and version != CURRENT_PLAN_SCHEMA_VERSION:
        supported = ", ".join(
            str(v) for v in sorted({*MIGRATABLE_PLAN_SCHEMA_VERSIONS, CURRENT_PLAN_SCHEMA_VERSION})
        )
        raise UnknownPlanVersionError(
            f"plan schema_version {version!r} is not understood by this build of "
            f"SSC (supported: {supported}); refusing to best-effort parse it. "
            "A newer version means the document uses a feature this build cannot "
            "represent — upgrade SSC rather than editing the version number."
        )
    return Plan.model_validate(raw)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Diagnostic(PlanModel):
    """One finding against a plan or a compiled item — always with a reason
    (CLAUDE.md: "every decision is logged with its reason"). ``item_id`` is
    ``None`` for a plan-level diagnostic that names no single item."""

    severity: Severity
    item_id: str | None
    code: str
    message: str


__all__ = [
    "CURRENT_PLAN_SCHEMA_VERSION",
    "MIGRATABLE_PLAN_SCHEMA_VERSIONS",
    "AbsoluteTimeSpec",
    "AsapTimeSpec",
    "AutofocusItem",
    "BodyTargetSpec",
    "CaptureCalibrationItem",
    "CatalogTargetSpec",
    "CometTargetSpec",
    "ConstraintsSpec",
    "Diagnostic",
    "DitherItem",
    "DitherSpec",
    "DurationEndSpec",
    "EndSpec",
    "EventEndSpec",
    "EventTimeSpec",
    "ExposureCount",
    "FilterItem",
    "FixedCountSpec",
    "FrameType",
    "FramingSpec",
    "GotoAndStackItem",
    "HeaterItem",
    "MosaicGridSpec",
    "MosaicItem",
    "OptionalTargetFields",
    "ParkItem",
    "Plan",
    "PlanItem",
    "PlanItemBase",
    "PlanModel",
    "RelativeTimeSpec",
    "SetExposureItem",
    "SetGainItem",
    "Severity",
    "ShutdownItem",
    "SlewItem",
    "SolveItem",
    "StartupItem",
    "SyncItem",
    "TakeExposuresItem",
    "TargetEventEndSpec",
    "TargetEventTimeSpec",
    "TargetFields",
    "TargetSpec",
    "TimeSpec",
    "UnknownPlanVersionError",
    "UntilBlockEndSpec",
    "WaitItem",
    "parse_plan",
]
