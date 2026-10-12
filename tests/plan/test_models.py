"""``ssc.plan.models`` — no ephemeris, no clock, no I/O; these run instantly."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from ssc.astro.events import SolarEvent, TargetEvent
from ssc.plan import (
    CURRENT_PLAN_SCHEMA_VERSION,
    AbsoluteTimeSpec,
    AsapTimeSpec,
    EventTimeSpec,
    GotoAndStackItem,
    Plan,
    RelativeTimeSpec,
    ShutdownItem,
    SlewItem,
    StartupItem,
    TargetEventTimeSpec,
    UnknownPlanVersionError,
    models,
    parse_plan,
)

_NOW = "2026-08-01T12:00:00Z"


def _plan_dict(**overrides: object) -> dict:
    base = {
        "schema_version": CURRENT_PLAN_SCHEMA_VERSION,
        "id": "p1",
        "name": "test",
        "items": [],
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return base


# --- schema_version -----------------------------------------------------


def test_parse_plan_accepts_current_version() -> None:
    plan = parse_plan(_plan_dict())
    assert plan.schema_version == CURRENT_PLAN_SCHEMA_VERSION


def test_calibration_item_gain_defaults_to_200() -> None:
    item = models.CaptureCalibrationItem(id="cal", start=AsapTimeSpec())
    assert item.gain == 200


def test_new_pa_defaults_to_three_points_over_sixty_degrees() -> None:
    item = models.PolarAlignmentItem(id="pa", start=AsapTimeSpec())
    assert (item.point_count, item.step_deg) == (3, 30.0)
    saved = models.PolarAlignmentItem(id="saved", start=AsapTimeSpec(), point_count=5, step_deg=15)
    assert (saved.point_count, saved.step_deg) == (5, 15.0)


def test_new_imaging_dither_defaults_to_six_preserving_explicit_cadence() -> None:
    assert models._default_dither().every_n_frames == 6
    assert models.DitherSpec(every_n_frames=5).every_n_frames == 5


@pytest.mark.parametrize("version", [0, 8, 99, None, "1"])
def test_parse_plan_refuses_unknown_version(version: object) -> None:
    with pytest.raises(UnknownPlanVersionError, match="schema_version"):
        parse_plan(_plan_dict(schema_version=version))


def test_a_newer_version_says_the_build_is_the_thing_to_change() -> None:
    """The compatibility story for body targets, from the older build's side: a
    v1 build handed a v2 document must refuse it by *version*, naming the fix,
    rather than dropping the target it cannot represent (DESIGN.md 4.2)."""
    with pytest.raises(UnknownPlanVersionError, match="upgrade SSC"):
        parse_plan(_plan_dict(schema_version=CURRENT_PLAN_SCHEMA_VERSION + 1))


def test_plan_model_itself_also_refuses_a_bad_version() -> None:
    """Belt and braces: constructing ``Plan`` directly (bypassing ``parse_plan``)
    still cannot produce a plan with an unsupported version — Pydantic's own
    ``Literal`` constraint is the second line of defence."""
    with pytest.raises(ValidationError):
        Plan.model_validate(_plan_dict(schema_version=CURRENT_PLAN_SCHEMA_VERSION + 1))


# --- schema_version 3: the exposure block (DESIGN.md 4.3.0) --------------


_V2_PLAN_WITH_NO_EXPOSURE_BLOCK = {
    "schema_version": 2,
    "id": "older",
    "name": "a plan written before the exposure block existed",
    "created_at": _NOW,
    "updated_at": _NOW,
    "items": [
        {
            "kind": "goto_and_stack",
            "id": "goto",
            "start": {"kind": "asap"},
            "end": {"kind": "duration", "minutes": 45},
            "target": {
                "kind": "catalog",
                "name": "NGC 2244",
                "ra_j2000_h": 6.5317,
                "dec_j2000_deg": 4.9407,
            },
        }
    ],
}


def test_a_v2_plan_without_the_new_item_still_loads() -> None:
    """The forward half of the bump: adding an item kind must not strand a
    single stored plan. Nothing about a v2 document changes on the way in."""
    plan = parse_plan(dict(_V2_PLAN_WITH_NO_EXPOSURE_BLOCK))
    assert plan.schema_version == CURRENT_PLAN_SCHEMA_VERSION == 7
    item = plan.items[0]
    assert isinstance(item, GotoAndStackItem)
    assert item.target_name == "NGC 2244"
    assert (item.exposure_sec, item.gain, item.filter_name) == (10.0, 200, None)


def test_a_plan_using_the_exposure_block_is_refused_by_a_build_that_predates_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half the version bump actually exists for.

    A build that predates the exposure block is emulated exactly where it
    differs — the version gate ``parse_plan`` checks — by putting the older
    build's supported set back. That build must refuse the document by
    *version*, naming the fix, rather than parsing what it recognises and
    silently dropping the one item the night was about (DESIGN.md 4.2).
    """
    document = _plan_dict(
        items=[
            {
                "kind": "take_exposures",
                "id": "lights",
                "start": {"kind": "asap"},
                "count": {"kind": "fixed", "frames": 60},
                "exposure_sec": 30.0,
                "gain": 200,
                "filter_name": "LP",
            }
        ]
    )
    assert parse_plan(dict(document)).items[0].kind == "take_exposures"

    monkeypatch.setattr(models, "CURRENT_PLAN_SCHEMA_VERSION", 2)
    monkeypatch.setattr(models, "MIGRATABLE_PLAN_SCHEMA_VERSIONS", frozenset({1}))
    with pytest.raises(UnknownPlanVersionError) as excinfo:
        models.parse_plan(document)
    message = str(excinfo.value)
    assert "schema_version 7" in message
    assert "supported: 1, 2" in message
    assert "upgrade SSC" in message


def test_the_model_migrates_a_v1_document_the_same_way_parse_plan_does() -> None:
    """``Plan`` is embedded directly in the API's request models, so a v1
    document arriving over HTTP must migrate exactly as one read from SQLite."""
    assert Plan.model_validate(_plan_dict(schema_version=1)).schema_version == (
        CURRENT_PLAN_SCHEMA_VERSION
    )


# --- TimeSpec discriminated union ----------------------------------------


def test_timespec_round_trips_through_each_kind() -> None:
    item = StartupItem(id="a", start=AbsoluteTimeSpec(local_datetime=datetime(2026, 8, 28, 23, 15)))  # noqa: DTZ001
    plan = Plan(id="p", name="n", items=(item,), created_at=_NOW, updated_at=_NOW)  # type: ignore[arg-type]
    dumped = plan.model_dump(mode="json")
    restored = Plan.model_validate(dumped)
    assert restored.items[0].start == item.start


def test_absolute_timespec_rejects_tz_aware_datetime() -> None:
    with pytest.raises(ValidationError, match="naive"):
        AbsoluteTimeSpec.model_validate({"local_datetime": "2026-08-28T23:15:00+01:00"})


def test_target_event_requires_alt_deg_unless_transit() -> None:
    with pytest.raises(ValidationError, match="alt_deg"):
        TargetEventTimeSpec(event=TargetEvent.RISES_ABOVE)
    # transits does not need one:
    TargetEventTimeSpec(event=TargetEvent.TRANSITS)


def test_event_timespec_accepts_solar_and_lunar() -> None:
    EventTimeSpec(event=SolarEvent.ASTRONOMICAL_DUSK)
    from ssc.astro.events import LunarEvent

    EventTimeSpec(event=LunarEvent.MOONRISE)


def test_relative_and_asap_defaults() -> None:
    assert RelativeTimeSpec().offset_min == 0.0
    assert AsapTimeSpec().kind == "asap"


# --- strictness -----------------------------------------------------------


def test_unknown_field_is_refused_not_dropped() -> None:
    """extra='forbid': a typo'd field name in a portable JSON document must be
    loud, never silently ignored (CLAUDE.md invariant 7)."""
    with pytest.raises(ValidationError):
        StartupItem.model_validate({"id": "a", "start": {"kind": "asap"}, "not_a_real_field": True})


def test_slew_accepts_and_discards_removed_pa_and_park_controls() -> None:
    item = SlewItem.model_validate(
        {
            "id": "a",
            "start": {"kind": "asap"},
            "destination": "polar_best",
            "target": None,
            "polar_point_count": 5,
            "polar_step_deg": 30,
            "park_after": True,
        }
    )

    assert (
        item.model_dump().keys().isdisjoint({"polar_point_count", "polar_step_deg", "park_after"})
    )


def test_goto_and_stack_validates_ra_dec_range() -> None:
    with pytest.raises(ValidationError):
        GotoAndStackItem(id="g", start=AsapTimeSpec(), ra_j2000_h=24.0, dec_j2000_deg=0.0)
    with pytest.raises(ValidationError):
        GotoAndStackItem(id="g", start=AsapTimeSpec(), ra_j2000_h=0.0, dec_j2000_deg=91.0)


def test_plan_created_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Plan(
            id="p",
            name="n",
            items=(ShutdownItem(id="s", start=AsapTimeSpec()),),
            created_at="2026-08-01T12:00:00",  # no offset
            updated_at=_NOW,
        )


@pytest.mark.parametrize("kind", ["goto_and_stack", "mosaic"])
def test_imaging_live_stack_defaults_on_and_accepts_opt_out(kind: str) -> None:
    cls = models.GotoAndStackItem if kind == "goto_and_stack" else models.MosaicItem
    args = {
        "id": "panel",
        "start": models.AsapTimeSpec(),
        "target": models.CatalogTargetSpec(name="M42", ra_j2000_h=5.5, dec_j2000_deg=-5.0),
    }
    assert cls(**args).live_stack is True
    assert cls(**args, live_stack=False).live_stack is False
