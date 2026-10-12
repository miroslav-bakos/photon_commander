"""``ssc.plan.intent`` — a ``TargetIntent`` becomes exactly one ``PlanItem``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ssc.astro.bodies import BodyId
from ssc.astro.catalog import ObjectType
from ssc.plan import (
    AbsoluteTimeSpec,
    AsapTimeSpec,
    BodyTargetSpec,
    CometTargetSpec,
    GotoAndStackItem,
    MosaicItem,
)
from ssc.plan.intent import TargetIntent, intent_to_item, intent_to_items


def _intent(**overrides: object) -> TargetIntent:
    base: dict[str, object] = {"ra_j2000_h": 6.5317, "dec_j2000_deg": 4.9407, "name": "NGC 2244"}
    base.update(overrides)
    return TargetIntent.model_validate(base)


def test_a_default_intent_becomes_a_goto_and_stack_item() -> None:
    item = intent_to_item(_intent(), item_id="item-1")
    assert isinstance(item, GotoAndStackItem)
    assert item.id == "item-1"
    assert item.target_name == "NGC 2244"
    assert item.ra_j2000_h == 6.5317
    assert item.dec_j2000_deg == 4.9407
    assert isinstance(item.start, AsapTimeSpec)
    assert item.exposure_sec == 60.0


def test_a_real_grid_becomes_a_mosaic_item() -> None:
    intent = _intent(mosaic={"ra_num": 2, "dec_num": 2, "overlap_pct": 20})
    item = intent_to_item(intent, item_id="item-2")
    assert isinstance(item, MosaicItem)
    assert item.mosaic.ra_num == 2
    assert item.mosaic.dec_num == 2
    assert item.exposure_sec == 60.0


def test_a_1x1_grid_still_becomes_goto_and_stack() -> None:
    intent = _intent(mosaic={"ra_num": 1, "dec_num": 1, "overlap_pct": 0})
    item = intent_to_item(intent, item_id="item-3")
    assert isinstance(item, GotoAndStackItem)


def test_auto_lp_defaults_only_narrowband_friendly_targets() -> None:
    emission = intent_to_item(
        _intent(object_type=ObjectType.EMISSION_NEBULA),
        item_id="emission",
        auto_lp_filter=True,
    )
    galaxy = intent_to_item(
        _intent(object_type=ObjectType.GALAXY), item_id="galaxy", auto_lp_filter=True
    )
    disabled = intent_to_item(
        _intent(object_type=ObjectType.HII_REGION), item_id="disabled"
    )
    assert emission.filter_name == "LP"
    assert galaxy.filter_name is None
    assert disabled.filter_name is None


def test_auto_lp_applies_to_every_separated_panel() -> None:
    intent = _intent(
        object_type=ObjectType.HII_REGION,
        mosaic={"ra_num": 2, "dec_num": 1, "overlap_pct": 20},
        panels=[
            {"row": 0, "col": 0, "ra_j2000_h": 6.50, "dec_j2000_deg": 4.90},
            {"row": 0, "col": 1, "ra_j2000_h": 6.56, "dec_j2000_deg": 4.98},
        ],
    )
    items = intent_to_items(intent, item_ids=["one", "two"], auto_lp_filter=True)
    assert [item.filter_name for item in items] == ["LP", "LP"]


def test_an_explicit_start_overrides_the_asap_default() -> None:
    start = AbsoluteTimeSpec(local_datetime="2026-08-28T23:15:00")  # type: ignore[arg-type]
    item = intent_to_item(_intent(), item_id="item-4", start=start)
    assert item.start == start


def test_framing_and_constraints_carry_across() -> None:
    intent = _intent(
        framing={"mode": "eq", "pa_offset_deg": 5.0, "lens": "wide"},
        constraints={"min_alt_deg": 25.0, "min_moon_sep_deg": 30.0},
    )
    item = intent_to_item(intent, item_id="item-5")
    assert isinstance(item, GotoAndStackItem)
    assert item.framing.mode.value == "eq"
    assert item.framing.lens == "wide"
    assert item.constraints.min_alt_deg == 25.0
    assert item.constraints.min_moon_sep_deg == 30.0


def test_a_body_intent_keeps_identity_and_drops_preview_coordinates() -> None:
    item = intent_to_item(TargetIntent(body=BodyId.MOON), item_id="item-body")
    assert isinstance(item, GotoAndStackItem)
    assert item.target == BodyTargetSpec(body=BodyId.MOON)
    assert item.ra_j2000_h is None
    assert item.dec_j2000_deg is None


def test_a_body_intent_refuses_frozen_coordinates() -> None:
    with pytest.raises(ValueError, match="only body identity"):
        TargetIntent(body=BodyId.JUPITER, ra_j2000_h=1.0, dec_j2000_deg=2.0)


def test_a_comet_intent_keeps_identity_and_drops_preview_coordinates() -> None:
    target = CometTargetSpec(
        spk_id=1004136,
        designation="2026 P1",
        full_name="P/2026 P1 (PANSTARRS)",
        source_url="https://ssd-api.jpl.nasa.gov/sbdb_query.api?fields=orbit",
        snapshot_fetched_at=datetime(2026, 9, 16, tzinfo=UTC),
        solution_date="2026-09-15 13:42:29",
        element_epoch_jd_tdb=2461274.5,
    )

    item = intent_to_item(TargetIntent(name=target.full_name, comet=target), item_id="comet")

    assert isinstance(item, GotoAndStackItem)
    assert item.target == target
    assert item.ra_j2000_h is None
    assert item.dec_j2000_deg is None


def test_a_comet_intent_refuses_frozen_preview_coordinates() -> None:
    target = CometTargetSpec(
        spk_id=1004136,
        designation="2026 P1",
        full_name="P/2026 P1 (PANSTARRS)",
        source_url="https://ssd-api.jpl.nasa.gov/sbdb_query.api?fields=orbit",
        snapshot_fetched_at=datetime(2026, 9, 16, tzinfo=UTC),
        element_epoch_jd_tdb=2461274.5,
    )
    with pytest.raises(ValueError, match="identity, not preview coordinates"):
        TargetIntent(comet=target, ra_j2000_h=1.0, dec_j2000_deg=2.0)


def test_exact_mosaic_centres_become_independent_panel_items() -> None:
    intent = _intent(
        mosaic={"ra_num": 2, "dec_num": 1, "overlap_pct": 20},
        panels=[
            {"row": 0, "col": 0, "ra_j2000_h": 6.50, "dec_j2000_deg": 4.90},
            {"row": 0, "col": 1, "ra_j2000_h": 6.56, "dec_j2000_deg": 4.98},
        ],
    )
    items = intent_to_items(intent, item_ids=["panel-1", "panel-2"], duration_min=20)
    assert [item.id for item in items] == ["panel-1", "panel-2"]
    assert all(isinstance(item, GotoAndStackItem) for item in items)
    assert [item.target_name for item in items] == [
        "NGC 2244 1",
        "NGC 2244 2",
    ]
    assert [(item.ra_j2000_h, item.dec_j2000_deg) for item in items] == [
        (6.50, 4.90),
        (6.56, 4.98),
    ]
    assert all(item.autofocus_before is False for item in items)
    assert all(item.exposure_sec == 60.0 for item in items)
    assert all(item.gain == 200 for item in items)
    assert all(item.filter_name is None for item in items)
    assert all(isinstance(item.start, AsapTimeSpec) for item in items)


def test_separate_panel_centres_must_cover_the_grid_exactly() -> None:
    with pytest.raises(ValueError, match="complete mosaic grid"):
        _intent(
            mosaic={"ra_num": 2, "dec_num": 1, "overlap_pct": 20},
            panels=[
                {"row": 0, "col": 0, "ra_j2000_h": 6.50, "dec_j2000_deg": 4.90},
                {"row": 0, "col": 0, "ra_j2000_h": 6.56, "dec_j2000_deg": 4.98},
            ],
        )
