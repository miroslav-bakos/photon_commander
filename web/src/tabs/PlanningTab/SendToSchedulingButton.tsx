// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §3.4's handoff: the target, framing, mosaic and constraints the
 * Planning tab worked out become one `TargetIntent`, appended to a plan by
 * `POST /api/plans/{id}/items/from-intent` (the server decides whether that
 * is a `goto_and_stack` or a `mosaic`, `ssc.plan.intent.intent_to_item`).
 *
 * Which plan: the date-named plan for this planning night, or a new one.
 * Then Scheduling is
 * opened on the new item — so the very next thing the user sees is the
 * resolved start time, not a list to hunt through.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../../components/Button";
import type { EventTimeModel, MosaicPanelModel } from "../../api/astro";
import { usePlansQuery, useScheduleIntent, type TargetIntent } from "../../api/plans";
import { useConnectionStore } from "../../store/connectionStore";
import { newPlanDocument } from "../SchedulingTab/itemKinds";
import { useSchedulingUiStore } from "../SchedulingTab/schedulingUiStore";

export function SendToSchedulingButton({
  intent,
  nightLabel,
  unavailableReason,
  mosaicPanels = [],
  solarEvents,
}: {
  intent: TargetIntent | null;
  nightLabel: string;
  unavailableReason?: string;
  mosaicPanels?: MosaicPanelModel[];
  solarEvents?: EventTimeModel[];
}) {
  const navigate = useNavigate();
  const snapshotPlans = useConnectionStore((s) => s.snapshot?.plans);
  const plansQuery = usePlansQuery();
  const scheduleIntent = useScheduleIntent();
  const [error, setError] = useState<string | null>(null);
  const busy = scheduleIntent.isPending;
  const isMosaic = intent !== null && (intent.mosaic.ra_num > 1 || intent.mosaic.dec_num > 1);
  const hasResolvedPanels = intent?.body == null && mosaicPanels.length > 1;
  const panelsPending = isMosaic && !hasResolvedPanels;
  const divideNight = isMosaic;
  const panelCount = hasResolvedPanels ? mosaicPanels.length : 1;
  const dusk = solarEvents?.find((event) => event.event === "astronomical_dusk");
  const dawn = solarEvents?.find((event) => event.event === "astronomical_dawn");
  const duskMs = dusk?.outcome === "occurs" && dusk.when !== null ? Date.parse(dusk.when) : NaN;
  const dawnMs = dawn?.outcome === "occurs" && dawn.when !== null ? Date.parse(dawn.when) : NaN;
  const panelDurationMin = Number.isFinite(duskMs) && Number.isFinite(dawnMs) && dawnMs > duskMs
    ? (dawnMs - duskMs) / 60_000 / panelCount
    : null;
  const darknessUnavailable = divideNight && panelDurationMin === null;

  const send = async (target: TargetIntent) => {
    setError(null);
    const plans = snapshotPlans ?? plansQuery.data?.plans ?? [];
    try {
      if (darknessUnavailable) {
        throw new Error("Astronomical dusk and dawn must both occur on this night to divide it between panels.");
      }
      const planName = `Plan for ${nightLabel}`;
      const planId = plans.find((p) => p.name === planName)?.id ?? null;
      const scheduledTarget: TargetIntent = hasResolvedPanels
        ? {
            ...target,
            panels: mosaicPanels.map(({ center }) => ({
              row: center.row,
              col: center.col,
              ra_j2000_h: center.ra_j2000_h,
              dec_j2000_deg: center.dec_j2000_deg,
            })),
          }
        : target;
      const result = await scheduleIntent.mutateAsync({
        plan_id: planId,
        night_date: nightLabel,
        intent: scheduledTarget,
        ...(divideNight ? { duration_min: panelDurationMin } : {}),
        ...(planId === null ? { new_plan: newPlanDocument(planName) } : {}),
      });
      useSchedulingUiStore.getState().select(result.document.id, result.item_id, nightLabel);
      await navigate("/scheduling");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not send to Scheduling");
    }
  };

  return (
    <div className="flex flex-col gap-2 border-t border-border pt-3">
      <Button
        variant="primary"
        disabled={intent === null || busy || darknessUnavailable || panelsPending}
        onClick={() => {
          if (intent !== null) void send(intent);
        }}
      >
        {busy ? "Scheduling…" : "Schedule"}
      </Button>
      {intent === null && <p className="text-xs text-fg-muted">{unavailableReason ?? "Resolve a target first."}</p>}
      {panelsPending && <p className="text-xs text-fg-muted">Mosaic panels are still resolving.</p>}
      {darknessUnavailable && (
        <p className="text-xs text-danger" role="alert">
          Astronomical dusk and dawn must both occur on this night to divide it between panels.
        </p>
      )}
      {intent !== null && (
        <p className="text-xs text-fg-muted">
          Appends {hasResolvedPanels
            ? `${mosaicPanels.length.toString()} independently timed panel items`
            : "a goto + capture"} for{" "}
          {intent.name ?? "this target"} to {`Plan for ${nightLabel}`}, creating it if needed.
        </p>
      )}
      {error !== null && (
        <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-xs text-danger" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
