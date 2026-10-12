"""``ssc.plan.validate`` — structural checks only, no ephemeris and no clock."""

from __future__ import annotations

from ssc.plan import (
    AsapTimeSpec,
    CaptureCalibrationItem,
    MosaicGridSpec,
    MosaicItem,
    Plan,
    RelativeTimeSpec,
    Severity,
    ShutdownItem,
    StartupItem,
    validate_plan,
)

_NOW = "2026-08-01T12:00:00Z"


def _plan(*items: object) -> Plan:
    return Plan(id="p", name="n", items=tuple(items), created_at=_NOW, updated_at=_NOW)  # type: ignore[arg-type]


def test_empty_plan_has_no_diagnostics() -> None:
    assert validate_plan(_plan()) == []


def test_duplicate_ids_are_an_error() -> None:
    plan = _plan(
        StartupItem(id="dup", start=AsapTimeSpec()),
        ShutdownItem(id="dup", start=AsapTimeSpec()),
    )
    diagnostics = validate_plan(plan)
    codes = [d.code for d in diagnostics]
    assert codes.count("duplicate_id") == 1
    assert diagnostics[0].severity is Severity.ERROR
    assert diagnostics[0].item_id == "dup"


def test_relative_first_item_is_an_error() -> None:
    plan = _plan(StartupItem(id="a", start=RelativeTimeSpec(offset_min=5)))
    diagnostics = validate_plan(plan)
    assert any(
        d.code == "relative_first_item" and d.severity is Severity.ERROR for d in diagnostics
    )


def test_relative_is_fine_when_not_first() -> None:
    plan = _plan(
        StartupItem(id="a", start=AsapTimeSpec()),
        ShutdownItem(id="b", start=RelativeTimeSpec(offset_min=5)),
    )
    assert not any(d.code == "relative_first_item" for d in validate_plan(plan))


def test_missing_shutdown_is_a_warning() -> None:
    plan = _plan(StartupItem(id="a", start=AsapTimeSpec()))
    diagnostics = validate_plan(plan)
    finding = next(d for d in diagnostics if d.code == "missing_shutdown")
    assert finding.severity is Severity.WARNING
    assert finding.item_id == "a"


def test_ending_in_shutdown_has_no_warning() -> None:
    plan = _plan(
        StartupItem(id="a", start=AsapTimeSpec()),
        ShutdownItem(id="b", start=RelativeTimeSpec(offset_min=5)),
    )
    assert not any(d.code == "missing_shutdown" for d in validate_plan(plan))


def test_ending_in_safe_calibration_reports_parked_state() -> None:
    plan = _plan(CaptureCalibrationItem(id="darks", start=AsapTimeSpec(), safe_state=True))

    diagnostics = validate_plan(plan)

    finding = next(d for d in diagnostics if d.code == "safe_calibration_parks")
    assert finding.severity is Severity.INFO
    assert finding.item_id == "darks"
    assert "move home and park" in finding.message
    assert not any(d.code == "missing_shutdown" for d in diagnostics)


def test_ending_in_unsafe_calibration_still_warns() -> None:
    plan = _plan(CaptureCalibrationItem(id="darks", start=AsapTimeSpec(), safe_state=False))

    diagnostics = validate_plan(plan)

    assert any(d.code == "missing_shutdown" for d in diagnostics)
    assert not any(d.code == "safe_calibration_parks" for d in diagnostics)


def test_disabled_safe_calibration_does_not_claim_to_park() -> None:
    plan = _plan(
        CaptureCalibrationItem(id="darks", start=AsapTimeSpec(), enabled=False, safe_state=True)
    )

    diagnostics = validate_plan(plan)

    assert any(d.code == "missing_shutdown" for d in diagnostics)
    assert not any(d.code == "safe_calibration_parks" for d in diagnostics)


def test_mosaic_dims_below_one_is_an_error() -> None:
    plan = _plan(
        MosaicItem(
            id="m",
            start=AsapTimeSpec(),
            ra_j2000_h=1.0,
            dec_j2000_deg=1.0,
            mosaic=MosaicGridSpec(ra_num=0, dec_num=1, overlap_pct=10),
        )
    )
    diagnostics = validate_plan(plan)
    assert any(
        d.code == "mosaic_dims_invalid" and d.severity is Severity.ERROR for d in diagnostics
    )


def test_mosaic_overlap_out_of_range_is_an_error() -> None:
    plan = _plan(
        MosaicItem(
            id="m",
            start=AsapTimeSpec(),
            ra_j2000_h=1.0,
            dec_j2000_deg=1.0,
            mosaic=MosaicGridSpec(ra_num=1, dec_num=1, overlap_pct=100.0),
        )
    )
    diagnostics = validate_plan(plan)
    assert any(
        d.code == "mosaic_overlap_out_of_range" and d.severity is Severity.ERROR
        for d in diagnostics
    )


def test_valid_mosaic_grid_has_no_diagnostics() -> None:
    plan = _plan(
        MosaicItem(
            id="m",
            start=AsapTimeSpec(),
            ra_j2000_h=1.0,
            dec_j2000_deg=1.0,
            mosaic=MosaicGridSpec(ra_num=2, dec_num=2, overlap_pct=20.0),
        ),
        ShutdownItem(id="b", start=RelativeTimeSpec(offset_min=5)),
    )
    assert validate_plan(plan) == []
