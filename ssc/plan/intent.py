# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The Planning tab's handoff (DESIGN.md 3.4): a :class:`TargetIntent` becomes
one appended :class:`~ssc.plan.models.PlanItem`.

Pure, like the rest of :mod:`ssc.plan`: no id generation, no clock, no I/O.
The caller (``ssc/api/routes/plans.py``) supplies the new item's ``id`` and,
optionally, where in the schedule it starts — this module only decides *what
kind* of item a ``TargetIntent`` becomes and copies its fields across.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from pydantic import Field, model_validator

from ssc.astro.best_targets import MoonSensitivity, moon_sensitivity_of
from ssc.astro.bodies import BodyId
from ssc.astro.catalog import ObjectType
from ssc.plan.models import (
    AsapTimeSpec,
    BodyTargetSpec,
    CatalogTargetSpec,
    CometTargetSpec,
    ConstraintsSpec,
    DurationEndSpec,
    FramingSpec,
    GotoAndStackItem,
    MosaicGridSpec,
    MosaicItem,
    PlanItem,
    PlanModel,
    TargetSpec,
    TimeSpec,
)


class TargetIntentPanel(PlanModel):
    """One zero-based panel centre calculated by the Planning backend."""

    row: Annotated[int, Field(ge=0)]
    col: Annotated[int, Field(ge=0)]
    ra_j2000_h: Annotated[float, Field(ge=0.0, lt=24.0)]
    dec_j2000_deg: Annotated[float, Field(ge=-90.0, le=90.0)]


class TargetIntent(PlanModel):
    """DESIGN.md 3.4's "Send to Scheduling" JSON, verbatim: a target plus the
    framing/mosaic/constraints the Planning tab worked out for it.

    ``mosaic`` is always present — its default (``ra_num=dec_num=1``) is "no
    mosaic" — rather than optional, so one field decides which
    :class:`~ssc.plan.models.PlanItem` kind results: see :func:`intent_to_item`.
    """

    name: str | None = None
    object_type: ObjectType | None = None
    ra_j2000_h: Annotated[float | None, Field(ge=0.0, lt=24.0)] = None
    dec_j2000_deg: Annotated[float | None, Field(ge=-90.0, le=90.0)] = None
    #: A moving target is stored by identity, never by the coordinates it had
    #: in Planning's preview. Mutually exclusive with the fixed coordinates.
    body: BodyId | None = None
    comet: CometTargetSpec | None = None
    framing: FramingSpec = FramingSpec()
    mosaic: MosaicGridSpec = MosaicGridSpec()
    constraints: ConstraintsSpec = ConstraintsSpec()
    #: Exact panel centres returned by ``/api/astro/mosaic`` when Planning's
    #: "schedule panels separately" option is selected. Empty preserves the
    #: ordinary single MosaicItem handoff.
    panels: tuple[TargetIntentPanel, ...] = ()

    @model_validator(mode="after")
    def _one_target_identity(self) -> TargetIntent:
        has_ra = self.ra_j2000_h is not None
        has_dec = self.dec_j2000_deg is not None
        if self.body is not None and self.comet is not None:
            raise ValueError("a target intent cannot name both a body and a comet")
        if self.body is not None:
            # Reuse the plan model's hard Sun refusal here too, at the API
            # boundary, rather than letting a dangerous intent travel farther.
            BodyTargetSpec(body=self.body)
            if has_ra or has_dec:
                raise ValueError("a body intent stores only body identity, not preview coordinates")
        elif self.comet is not None:
            if has_ra or has_dec:
                raise ValueError("a comet intent stores identity, not preview coordinates")
        elif not has_ra or not has_dec:
            raise ValueError("a catalogue intent requires both ra_j2000_h and dec_j2000_deg")
        if self.panels:
            if self.body is not None or self.comet is not None:
                raise ValueError("a moving target cannot be split into fixed mosaic panels")
            expected = {
                (row, col)
                for row in range(self.mosaic.dec_num)
                for col in range(self.mosaic.ra_num)
            }
            actual = {(panel.row, panel.col) for panel in self.panels}
            if len(self.panels) <= 1 or actual != expected:
                raise ValueError(
                    "separate panel centres must cover the complete mosaic grid exactly once"
                )
        return self


def intent_to_item(
    intent: TargetIntent,
    *,
    item_id: str,
    start: TimeSpec | None = None,
    duration_min: float | None = None,
    auto_lp_filter: bool = False,
) -> PlanItem:
    """A ``goto_and_stack`` or ``mosaic`` item carrying ``intent``'s fields.

    A real grid (``ra_num > 1`` or ``dec_num > 1``) becomes ``mosaic``;
    otherwise the intent is a single frame and becomes ``goto_and_stack``.
    A fixed target keeps its coordinates; a moving target keeps only its body
    identity so the scheduler resolves it again at execution time.

    ``start`` defaults to :class:`~ssc.plan.models.AsapTimeSpec` — "append
    this after whatever the plan already does" — which is what "Send to
    Scheduling" means when the user has not chosen a time explicitly; the
    compiler reports the ordinary ``asap_no_previous`` diagnostic if this
    lands as the plan's first item.
    """
    resolved_start = start if start is not None else AsapTimeSpec()
    is_mosaic = intent.mosaic.ra_num > 1 or intent.mosaic.dec_num > 1
    target: TargetSpec
    if intent.body is not None:
        target = BodyTargetSpec(body=intent.body)
    elif intent.comet is not None:
        target = intent.comet
    else:
        assert intent.ra_j2000_h is not None and intent.dec_j2000_deg is not None
        target = CatalogTargetSpec(
            name=intent.name,
            ra_j2000_h=intent.ra_j2000_h,
            dec_j2000_deg=intent.dec_j2000_deg,
        )
    filter_name = _automatic_filter(intent, enabled=auto_lp_filter)
    if is_mosaic:
        return MosaicItem(
            id=item_id,
            start=resolved_start,
            end=_end_of(duration_min),
            target=target,
            framing=intent.framing,
            mosaic=intent.mosaic,
            constraints=intent.constraints,
            exposure_sec=60.0,
            filter_name=filter_name,
        )
    return GotoAndStackItem(
        id=item_id,
        start=resolved_start,
        end=_end_of(duration_min),
        target=target,
        framing=intent.framing,
        constraints=intent.constraints,
        exposure_sec=60.0,
        filter_name=filter_name,
    )


def intent_to_items(
    intent: TargetIntent,
    *,
    item_ids: Sequence[str],
    start: TimeSpec | None = None,
    duration_min: float | None = None,
    auto_lp_filter: bool = False,
) -> tuple[PlanItem, ...]:
    """Materialise an intent as one item, or one independently editable item
    per exact panel centre when Planning requested separation."""
    if not intent.panels:
        if len(item_ids) != 1:
            raise ValueError("a non-separated intent requires exactly one item id")
        return (
            intent_to_item(
                intent,
                item_id=item_ids[0],
                start=start,
                duration_min=duration_min,
                auto_lp_filter=auto_lp_filter,
            ),
        )
    if len(item_ids) != len(intent.panels):
        raise ValueError("separate panel item ids must match the panel count")
    base_name = intent.name or "Mosaic"
    items: list[PlanItem] = []
    for index, panel in enumerate(sorted(intent.panels, key=lambda value: (value.row, value.col))):
        items.append(
            GotoAndStackItem(
                id=item_ids[index],
                start=(start if index == 0 and start is not None else AsapTimeSpec()),
                end=_end_of(duration_min),
                target=CatalogTargetSpec(
                    name=f"{base_name} {index + 1}",
                    ra_j2000_h=panel.ra_j2000_h,
                    dec_j2000_deg=panel.dec_j2000_deg,
                ),
                framing=intent.framing,
                constraints=intent.constraints,
                exposure_sec=60.0,
                filter_name=_automatic_filter(intent, enabled=auto_lp_filter),
            )
        )
    return tuple(items)


__all__ = ["TargetIntent", "TargetIntentPanel", "intent_to_item", "intent_to_items"]


def _end_of(duration_min: float | None) -> DurationEndSpec | None:
    """The intent's optional duration as an end spec; ``None`` stays ``None``."""
    return None if duration_min is None else DurationEndSpec(minutes=duration_min)


def _automatic_filter(intent: TargetIntent, *, enabled: bool) -> str | None:
    if not enabled or intent.object_type is None:
        return None
    return (
        "LP"
        if moon_sensitivity_of(intent.object_type) is MoonSensitivity.NARROWBAND_FRIENDLY
        else None
    )
