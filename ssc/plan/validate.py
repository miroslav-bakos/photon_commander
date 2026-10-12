# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Structural checks on a :class:`~ssc.plan.models.Plan`, independent of any
site, clock or ephemeris.

Everything here is checkable on the document alone — id uniqueness, chain
shape, grid sanity — before the far more expensive question of "when does
this actually happen" is asked at :mod:`ssc.scheduler.compiler` time. Kept
deliberately separate from the compiler so a plan editor can validate on every
keystroke without touching an ephemeris.
"""

from __future__ import annotations

from ssc.plan.models import (
    CaptureCalibrationItem,
    Diagnostic,
    MosaicItem,
    Plan,
    RelativeTimeSpec,
    Severity,
    TakeExposuresItem,
    TargetEventEndSpec,
    UntilBlockEndSpec,
)


def validate_plan(plan: Plan) -> list[Diagnostic]:
    """Every structural finding against ``plan``, in a stable, deterministic order."""
    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_duplicate_ids(plan))
    diagnostics.extend(_relative_on_first_item(plan))
    diagnostics.extend(_missing_shutdown(plan))
    diagnostics.extend(_mosaic_grids(plan))
    diagnostics.extend(_target_event_end_needs_target(plan))
    diagnostics.extend(_exposure_blocks(plan))
    return diagnostics


def _duplicate_ids(plan: Plan) -> list[Diagnostic]:
    seen: dict[str, int] = {}
    for item in plan.items:
        seen[item.id] = seen.get(item.id, 0) + 1
    return [
        Diagnostic(
            severity=Severity.ERROR,
            item_id=item_id,
            code="duplicate_id",
            message=f"item id {item_id!r} is used {count} times; item ids must be unique",
        )
        for item_id, count in seen.items()
        if count > 1
    ]


def _relative_on_first_item(plan: Plan) -> list[Diagnostic]:
    if not plan.items:
        return []
    first = plan.items[0]
    if isinstance(first.start, RelativeTimeSpec):
        return [
            Diagnostic(
                severity=Severity.ERROR,
                item_id=first.id,
                code="relative_first_item",
                message=(
                    "the first item cannot use a 'relative' start — there is no "
                    "previous item to chain from"
                ),
            )
        ]
    return []


def _missing_shutdown(plan: Plan) -> list[Diagnostic]:
    if not plan.items:
        return []
    last = plan.items[-1]
    if isinstance(last, CaptureCalibrationItem) and last.enabled and last.safe_state:
        return [
            Diagnostic(
                severity=Severity.INFO,
                item_id=last.id,
                code="safe_calibration_parks",
                message=(
                    "the final calibration item has Safe state enabled: tracking will be "
                    "disabled and the telescope will move home and park before calibration; "
                    "it remains parked when calibration finishes"
                ),
            )
        ]
    if last.kind != "shutdown":
        return [
            Diagnostic(
                severity=Severity.WARNING,
                item_id=last.id,
                code="missing_shutdown",
                message=(
                    "the plan does not end with a 'shutdown' item; the scope will "
                    "not be parked and shut down automatically at the end of the run"
                ),
            )
        ]
    return []


def _mosaic_grids(plan: Plan) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for item in plan.items:
        if not isinstance(item, MosaicItem):
            continue
        if item.mosaic.ra_num < 1 or item.mosaic.dec_num < 1:
            diagnostics.append(
                Diagnostic(
                    severity=Severity.ERROR,
                    item_id=item.id,
                    code="mosaic_dims_invalid",
                    message=(
                        f"mosaic ra_num={item.mosaic.ra_num}, dec_num={item.mosaic.dec_num}; "
                        "both must be at least 1"
                    ),
                )
            )
        if not 0.0 <= item.mosaic.overlap_pct < 100.0:
            diagnostics.append(
                Diagnostic(
                    severity=Severity.ERROR,
                    item_id=item.id,
                    code="mosaic_overlap_out_of_range",
                    message=(f"mosaic overlap_pct={item.mosaic.overlap_pct}; must be in [0, 100)"),
                )
            )
    return diagnostics


def _target_event_end_needs_target(plan: Plan) -> list[Diagnostic]:
    """The same diagnostic family :mod:`ssc.scheduler.compiler` reports for a
    ``target_event`` *start* on a non-target item, checked here structurally
    (on the document alone, no ephemeris) for an item's ``end`` — a plan
    editor can flag this on every keystroke, before the far more expensive
    "when does this actually happen" question the compiler answers.
    """
    diagnostics: list[Diagnostic] = []
    for item in plan.items:
        if not isinstance(item.end, TargetEventEndSpec):
            continue
        # A *body* target counts here as much as a catalogue one: it has no
        # coordinates in the document, but it does have rise/transit/set times
        # (``ssc.astro.targets.target_ref_events``), which is what a
        # ``target_event`` end needs. The check is "does this item name a
        # target at all", not "does it carry numbers".
        if getattr(item, "target", None) is not None:
            continue
        diagnostics.append(
            Diagnostic(
                severity=Severity.ERROR,
                item_id=item.id,
                code="target_event_needs_target",
                message=(
                    "a target_event end requires a target-bearing item "
                    "(goto_and_stack, mosaic, slew, or sync with coordinates)"
                ),
            )
        )
    return diagnostics


def _exposure_blocks(plan: Plan) -> list[Diagnostic]:
    """The two things about an exposure block that are wrong on the document
    alone, before any site or ephemeris is involved (DESIGN.md 4.3.0).

    Everything else about a block — is the exposure inside the camera's own
    ``ExposureMin``/``ExposureMax``, does the fixed count fit in the window —
    needs either the device's reported limits or resolved times, so it is the
    compiler's to report, not this module's.
    """
    diagnostics: list[Diagnostic] = []
    for item in plan.items:
        if not isinstance(item, TakeExposuresItem):
            continue
        if isinstance(item.count, UntilBlockEndSpec) and item.end is None:
            diagnostics.append(
                Diagnostic(
                    severity=Severity.ERROR,
                    item_id=item.id,
                    code="open_ended_exposure_needs_end",
                    message=(
                        "this block exposes until its window closes but has no end: "
                        "give it a duration, or an end at an event (astronomical dawn, "
                        "the target setting below an altitude). SSC will not invent one, "
                        "so as written the block has no frames to take"
                    ),
                )
            )
        if item.filter_name is None:
            diagnostics.append(
                Diagnostic(
                    severity=Severity.WARNING,
                    item_id=item.id,
                    code="exposure_block_sets_no_filter",
                    message=(
                        "this block names no filter, so it images through whatever "
                        "position the wheel was left in by whatever ran before it. That "
                        "is the state leak the exposure block exists to close — name a "
                        "filter position unless this scope has no wheel"
                    ),
                )
            )
    return diagnostics


__all__ = ["validate_plan"]
