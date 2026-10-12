// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useAutoNightMutation, type AutoNightRequest, type MountMode } from "../../api/astro";
import { useSettingsQuery } from "../../api/settings";
import { siteNightWindow } from "./siteNightWindow";
import { useCreatePlanForNight } from "../../api/plans";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { Field } from "../../components/Field";
import { useSchedulingUiStore } from "../SchedulingTab/schedulingUiStore";
import type { BestTargetCategory } from "./planningUiStore";
import { objectTypesForCategories } from "./targetCategories";

const REASONS: Record<string, string> = {
  location_timezone_required: "Set the observing location time zone before planning.",
  emission_lp_moon: "Emission target with LP while the Moon is up.",
  emission_lp: "Emission target with LP.",
  moon_down_ir: "UV/IR-cut while the Moon is down or near new.",
  moon_separated_ir: "Broadband target sufficiently separated from the Moon.",
  setup_or_no_safe_target: "Setup time or no safe target is available.",
  shutdown_or_no_safe_target: "Shutdown time or no safe target is available.",
  sampled_policy_weather_unchecked: "Sampled sky geometry; weather has not been checked.",
  no_safe_targets: "No safe targets match the selected constraints.",
  wide_ir_only_weather_unchecked: "Wide optic uses UV/IR-cut only; weather has not been checked.",
};
function explanation(code: string) { return REASONS[code] ?? code; }
function clock(when: string, timeZone: string) { return new Date(when).toLocaleString(undefined, { timeZone, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }

export function AutoNightPanel({ nightDate, profileId, opticId, mountMode, minAltitudeDeg, categories, siteKey, lpAvailable, timeZone }: {
  startTime: string; endTime: string; nightDate: string; profileId: string | null; opticId: string | null;
  mountMode: MountMode; minAltitudeDeg: number; categories: readonly BestTargetCategory[];
  siteKey: string | null; lpAvailable: boolean; timeZone: string;
}) {
  const [blockMinutes, setBlockMinutes] = useState("90");
  const [maxTargets, setMaxTargets] = useState("5");
  const [exposure, setExposure] = useState("30");
  const [gain, setGain] = useState("200");
  const [ir, setIr] = useState(true);
  const [lp, setLp] = useState(true);
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const [savedKey, setSavedKey] = useState<string | null>(null);
  const settings = useSettingsQuery();
  const siteWindow = useMemo(() => siteNightWindow(nightDate, timeZone), [nightDate, timeZone]);
  const preview = useAutoNightMutation();
  const save = useCreatePlanForNight();
  const filters: ("IR" | "LP")[] = [...(ir ? ["IR" as const] : []), ...(lp && lpAvailable ? ["LP" as const] : [])];
  const body: AutoNightRequest | null = profileId === null || opticId === null || siteKey === null ? null : {
    start_time: siteWindow.start.toISOString(), end_time: siteWindow.end.toISOString(), profile_id: profileId, optic_id: opticId, mount_mode: mountMode,
    allowed_object_types: objectTypesForCategories(categories), min_altitude_deg: minAltitudeDeg,
    max_candidate_magnitude: 13, available_filters: filters, block_minutes: Number(blockMinutes),
    max_targets: Number(maxTargets), exposure_sec: Number(exposure), gain: Number(gain),
  };
  const key = JSON.stringify([body, siteKey, nightDate, settings.data?.horizon]);
  const valid = body !== null && categories.length > 0 && filters.length > 0
    && Number.isInteger(body.block_minutes) && body.block_minutes >= 15 && body.block_minutes <= 240
    && Number.isInteger(body.max_targets) && body.max_targets >= 1 && body.max_targets <= 12
    && Number.isFinite(body.exposure_sec) && body.exposure_sec > 0 && body.exposure_sec <= 3600
    && gain.trim() !== "" && Number.isInteger(body.gain) && body.gain >= 0 && body.gain <= 10000;
  const stale = preview.data !== undefined && previewKey !== key;
  const busy = preview.isPending || save.isPending;
  const canSave = valid && !busy && preview.data !== undefined && previewKey === key && savedKey !== key && preview.data.blocks.length > 0;
  async function generate() {
    if (!valid || busy) return;
    const requestKey = key;
    setPreviewKey(null);
    setSavedKey(null);
    try { await preview.mutateAsync(body); setPreviewKey(requestKey); } catch { /* Mutation exposes the exact error. */ }
  }
  async function savePlan() {
    if (!canSave) return;
    const requestKey = key;
    try {
      const record = await save.mutateAsync({ document: preview.data.document, night_date: nightDate });
      setSavedKey(requestKey);
      useSchedulingUiStore.getState().select(record.document.id, null, nightDate);
    } catch { /* Keep the preview; never overwrite an existing plan. */ }
  }
  return (
    <Card>
      <CardTitle>Automatic night plan</CardTitle>
      <p className="text-sm text-fg-muted">Preview a new plan from your selected target categories, optics, altitude limit and available filters. Nothing starts automatically.</p>
      <p className="mt-2 text-xs text-fg-muted">{clock(siteWindow.start.toISOString(), timeZone)} – {clock(siteWindow.end.toISOString(), timeZone)} · {minAltitudeDeg}° · {categories.length} <span>selected categories</span></p>
      <div className="mt-3 grid grid-cols-2 gap-3">
        <Field label="Block duration (minutes)" type="number" min={15} max={240} value={blockMinutes} onChange={(event) => { setBlockMinutes(event.target.value); }} />
        <Field label="Maximum targets" type="number" min={1} max={12} value={maxTargets} onChange={(event) => { setMaxTargets(event.target.value); }} />
        <Field label="Exposure (seconds)" type="number" min={0.001} max={3600} step="any" value={exposure} onChange={(event) => { setExposure(event.target.value); }} />
        <Field label="Gain" type="number" min={0} max={10000} value={gain} onChange={(event) => { setGain(event.target.value); }} />
      </div>
      <fieldset className="mt-3"><legend className="text-sm font-medium">Available optical filters</legend>
        <p className="text-xs text-fg-muted">LP is used for emission targets; broadband targets use UV/IR-cut.</p>
        <label className="flex min-h-11 items-center gap-2"><input type="checkbox" checked={ir} onChange={(event) => { setIr(event.target.checked); }} />UV/IR-cut (IR)</label>
        <label className="flex min-h-11 items-center gap-2"><input type="checkbox" disabled={!lpAvailable} checked={lp && lpAvailable} onChange={(event) => { setLp(event.target.checked); }} />Light pollution (LP)</label>
      </fieldset>
      {!lpAvailable && <p className="text-xs text-fg-muted">LP is unavailable for the selected optic.</p>}
      {siteKey === null && <p role="status" className="text-sm text-warning">Set an observing location before generating a plan.</p>}
      {!valid && siteKey !== null && <p role="status" className="text-sm text-warning">Select a category and filter, and enter positive planning values.</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="primary" disabled={!valid || busy} onClick={() => { void generate(); }}>{preview.isPending ? "Planning the night…" : "Preview night plan"}</Button>
        <Button disabled={!canSave} onClick={() => { void savePlan(); }}>{save.isPending ? "Saving new plan…" : "Save as new plan"}</Button>
      </div>
      {preview.isError && <p role="alert" className="mt-2 text-sm text-danger"><span>Could not generate the night plan.</span> {explanation(preview.error.message)}</p>}
      {save.isError && <p role="alert" className="mt-2 text-sm text-danger"><span>Could not save the new plan.</span> {explanation(save.error.message)}</p>}
      {stale && <p role="status" className="mt-2 text-sm text-warning">Planning inputs changed. Generate a fresh preview before saving.</p>}
      {savedKey !== null && save.data !== undefined && <p role="status" className="mt-2 text-sm"><span>New plan saved.</span> <Link className="inline-flex min-h-11 items-center text-accent underline" to="/scheduling" onClick={() => { useSchedulingUiStore.getState().select(save.data.document.id, null, save.variables.night_date ?? nightDate); }}>Open in Scheduling</Link></p>}
      {preview.data !== undefined && <div className={`mt-3 space-y-3 ${stale ? "opacity-60" : ""}`}>
        <p className="text-sm"><span>Darkness</span>: {preview.data.darkness_hours.toFixed(1)} h · <span>Science time reserved</span>: {preview.data.scheduled_hours.toFixed(1)} h · <span>Moon illumination</span>: {(preview.data.moon_illuminated_fraction * 100).toFixed(0)}%</p>
        {preview.data.note !== null && <p className="text-sm text-fg-muted">{explanation(preview.data.note)}</p>}
        <ol className="space-y-2" aria-label="Night plan preview">{[
          ...preview.data.blocks.map((block) => ({ kind: "block" as const, start: block.start_time, block })),
          ...preview.data.gaps.map((gap) => ({ kind: "gap" as const, start: gap.start_time, gap })),
        ].sort((left, right) => left.start.localeCompare(right.start)).map((entry) => entry.kind === "gap"
          ? <li key={`gap:${entry.start}`} className="text-sm text-warning"><span>Unscheduled time</span>: {clock(entry.gap.start_time, timeZone)} – {clock(entry.gap.end_time, timeZone)} · {explanation(entry.gap.reason_code)}</li>
          : (() => { const block = entry.block; return <li key={`${block.designation}:${block.start_time}`} className="rounded-lg border border-border p-3 text-sm">
          <p className="font-semibold">{block.designation}{block.common_name ? ` · ${block.common_name}` : ""} · {block.filter_name}</p>
          <p><span>Reserved window (includes 10-minute setup)</span>: {clock(block.start_time, timeZone)} – {clock(block.end_time, timeZone)}</p>
          <p className="text-xs text-fg-muted"><span>Altitude peak</span>: {block.peak_altitude_deg.toFixed(1)}° · {clock(block.peak_when, timeZone)} · <span>Minimum altitude</span>: {block.min_altitude_deg.toFixed(1)}° · <span>Moon separation</span>: {block.min_moon_separation_deg.toFixed(1)}° · <span>Moon altitude</span>: {block.max_moon_altitude_deg.toFixed(1)}°</p>
          <p className="text-xs text-fg-muted">{explanation(block.reason_code)}</p>
        </li>; })())}</ol>

      </div>}
    </Card>
  );
}
