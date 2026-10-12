"""A small, hand-built ``Plan``/``CompiledSchedule`` pair for executor tests.

Deliberately not run through :func:`ssc.scheduler.compiler.compile_plan` — the
executor's contract is "steps a ``CompiledSchedule``", and building one directly, with
a matching ``Plan`` for item payload fields, means these tests need no ephemeris kernel
and stay fast. ``tests/scheduler/test_compiler.py`` already covers ``compile_plan``
itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ssc.astro.bodies import BodyId
from ssc.plan.models import (
    AbsoluteTimeSpec,
    BodyTargetSpec,
    DitherSpec,
    DurationEndSpec,
    FilterItem,
    FixedCountSpec,
    FrameType,
    GotoAndStackItem,
    Plan,
    RelativeTimeSpec,
    ShutdownItem,
    StartupItem,
    TakeExposuresItem,
    UntilBlockEndSpec,
)
from ssc.scheduler.compiler import CompiledItem, CompiledPanel, CompiledSchedule, NightWindow
from ssc.scheduler.exposure import cycle_duration_s

#: The plan's first item starts here; nothing about the executor cares what this value
#: actually is, only that ``Plan``/``CompiledSchedule`` agree on it.
BASE_UTC = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)


def build_plan() -> Plan:
    """startup -> goto_and_stack -> filter -> shutdown, one of each effect shape the
    executor's phase machinery understands: a ``DEVICE_EVENT`` effect on its own
    (startup), a slew/stack/run/stop sequence with a real ``RunUntilPhase`` in the
    middle (goto_and_stack), and a plain ``AWAIT_RETURN`` effect (filter, shutdown)."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Plan(
        id="test-plan",
        name="Crash-resume fixture",
        created_at=now,
        updated_at=now,
        items=(
            StartupItem(
                id="startup",
                start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
                duration_min=5.0,
            ),
            GotoAndStackItem(
                id="goto",
                start=RelativeTimeSpec(offset_min=0.0),
                duration_min=10.0,
                target_name="M42",
                ra_j2000_h=5.5,
                dec_j2000_deg=-5.4,
            ),
            FilterItem(
                id="filter",
                start=RelativeTimeSpec(offset_min=0.0),
                position="Dark",
            ),
            ShutdownItem(
                id="shutdown",
                start=RelativeTimeSpec(offset_min=0.0),
            ),
        ),
    )


def build_schedule(plan: Plan) -> CompiledSchedule:
    """The absolute-time counterpart of :func:`build_plan`, laid out back to back."""
    night = NightWindow(
        date="2026-01-01",
        tz="UTC",
        window_start_utc=BASE_UTC - timedelta(hours=8),
        window_end_utc=BASE_UTC + timedelta(hours=8),
        dark_start_utc=None,
        dark_end_utc=None,
        solar_events=(),
        lunar_events=(),
    )
    cursor = BASE_UTC
    items: list[CompiledItem] = []
    for plan_item in plan.items:
        start = cursor
        minutes = plan_item.end.minutes if isinstance(plan_item.end, DurationEndSpec) else 0.0
        end = start + timedelta(minutes=minutes)
        items.append(
            CompiledItem(
                item_id=plan_item.id,
                kind=plan_item.kind,
                enabled=plan_item.enabled,
                start_utc=start,
                end_utc=end,
                resolution=f"test fixture -> {start.isoformat()}",
                end_resolution=f"test fixture -> {end.isoformat()}",
                panels=(),
                diagnostics=(),
            )
        )
        cursor = end
    return CompiledSchedule(
        plan_id=plan.id,
        plan_revision="test-revision",
        compiled_at=BASE_UTC,
        night=night,
        items=tuple(items),
    )


#: How long each of the body fixture's re-centring segments lasts. Two of them,
#: so a crash can land *between* segments and the "the next segment resolves the
#: body again from scratch" property is actually exercised.
BODY_SEGMENT_MIN = 15.0


def build_body_plan() -> Plan:
    """startup -> goto_and_stack on the Moon -> shutdown.

    Hand-built like :func:`build_plan`, and for the same reason: the executor's
    contract is "steps a ``CompiledSchedule``", so these tests need no ephemeris.
    The Moon's *motion* is supplied by ``LinearBodyResolver`` instead, which
    makes "was this position replayed from the journal or resolved again?" a
    difference a test can see exactly rather than approximately.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Plan(
        id="body-test-plan",
        name="Crash-resume body fixture",
        created_at=now,
        updated_at=now,
        items=(
            StartupItem(
                id="startup",
                start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
                duration_min=5.0,
            ),
            GotoAndStackItem(
                id="goto-moon",
                start=RelativeTimeSpec(offset_min=0.0),
                end=DurationEndSpec(minutes=BODY_SEGMENT_MIN * 2),
                target=BodyTargetSpec(body=BodyId.MOON),
            ),
            ShutdownItem(id="shutdown", start=RelativeTimeSpec(offset_min=0.0)),
        ),
    )


def build_body_schedule(plan: Plan) -> CompiledSchedule:
    """:func:`build_body_plan` laid out back to back, with the Moon item cut
    into two coordinate-less re-centring segments — the shape
    ``compiler._recentre_segments`` produces."""
    schedule = build_schedule(plan)
    items: list[CompiledItem] = []
    for item in schedule.items:
        if item.item_id != "goto-moon" or item.start_utc is None or item.end_utc is None:
            items.append(item)
            continue
        segment = (item.end_utc - item.start_utc) / 2
        items.append(
            item.model_copy(
                update={
                    "panels": tuple(
                        CompiledPanel(
                            row=0,
                            col=0,
                            ra_j2000_deg=None,
                            dec_j2000_deg=None,
                            start_utc=item.start_utc + segment * index,
                            end_utc=item.start_utc + segment * (index + 1),
                        )
                        for index in range(2)
                    )
                }
            )
        )
    return schedule.model_copy(update={"items": tuple(items)})


# --- the exposure block fixture (DESIGN.md 4.3.0) ---------------------------
#
# Sixty frames is the brief's own example, and it is what makes "interrupted
# after 37, resumes at 38" a real assertion rather than an illustrative one.

#: Frames in the fixed-count block.
EXPOSURE_FRAMES = 60
#: Short enough that the whole simulated block is instant on a fake clock, long
#: enough that exposure and overhead are visibly different numbers.
EXPOSURE_SEC = 10.0
EXPOSURE_GAIN = 200
EXPOSURE_FILTER = "LP"
#: Two dithers inside sixty frames — enough for the crash harness to land on a
#: dither's own issue/resolve pair, few enough to keep the journal readable.
DITHER_EVERY_N_FRAMES = 20
DITHER_PIXELS = 12.0


def build_exposure_plan() -> Plan:
    """startup -> a fixed-count ``take_exposures`` block -> shutdown.

    The block carries everything it needs (filter, exposure, gain, frame type,
    dithering) and has no ``end`` of its own: sixty frames is a known
    integration, not a slot, so nothing may cut it short.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Plan(
        id="exposure-test-plan",
        name="Exposure block fixture",
        created_at=now,
        updated_at=now,
        items=(
            StartupItem(
                id="startup",
                start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
                duration_min=5.0,
            ),
            TakeExposuresItem(
                id="lights",
                start=RelativeTimeSpec(offset_min=0.0),
                count=FixedCountSpec(frames=EXPOSURE_FRAMES),
                exposure_sec=EXPOSURE_SEC,
                gain=EXPOSURE_GAIN,
                filter_name=EXPOSURE_FILTER,
                frame_type=FrameType.LIGHT,
                dither=DitherSpec(pixels=DITHER_PIXELS, every_n_frames=DITHER_EVERY_N_FRAMES),
            ),
            ShutdownItem(id="shutdown", start=RelativeTimeSpec(offset_min=0.0)),
        ),
    )


def build_exposure_schedule(plan: Plan) -> CompiledSchedule:
    """:func:`build_exposure_plan` laid out back to back, with the block's own
    end *derived* from its frame count exactly as ``compiler._exposure_block``
    derives it — so the shutdown after it sits where a real compile would put
    it, even though nothing in the block's execution depends on that estimate.
    """
    schedule = build_schedule(plan)
    derived = timedelta(seconds=EXPOSURE_FRAMES * cycle_duration_s(EXPOSURE_SEC))
    items: list[CompiledItem] = []
    shift = timedelta()
    for item in schedule.items:
        assert item.start_utc is not None and item.end_utc is not None
        start = item.start_utc + shift
        end = item.end_utc + shift
        if item.item_id == "lights":
            end += derived
            shift += derived
        items.append(item.model_copy(update={"start_utc": start, "end_utc": end}))
    return schedule.model_copy(update={"items": tuple(items)})


def build_open_ended_plan(
    *, exposure_sec: float, window_min: float, item_id: str = "open-block"
) -> Plan:
    """One ``until_block_end`` block with an explicit window — the fixture for
    "does a whole cycle fit", which is the only thing that decides its length."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Plan(
        id="open-ended-plan",
        name="Open-ended exposure block",
        created_at=now,
        updated_at=now,
        items=(
            TakeExposuresItem(
                id=item_id,
                start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
                end=DurationEndSpec(minutes=window_min),
                count=UntilBlockEndSpec(),
                exposure_sec=exposure_sec,
                gain=EXPOSURE_GAIN,
                filter_name=EXPOSURE_FILTER,
            ),
        ),
    )


__all__ = [
    "BASE_UTC",
    "BODY_SEGMENT_MIN",
    "DITHER_EVERY_N_FRAMES",
    "DITHER_PIXELS",
    "EXPOSURE_FILTER",
    "EXPOSURE_FRAMES",
    "EXPOSURE_GAIN",
    "EXPOSURE_SEC",
    "build_body_plan",
    "build_body_schedule",
    "build_exposure_plan",
    "build_exposure_schedule",
    "build_open_ended_plan",
    "build_plan",
    "build_schedule",
]
