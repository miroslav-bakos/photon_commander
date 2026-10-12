// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { CompiledItem, PlanItem } from "../../api/plans";

/**
 * Empirical S30 Pro timings from Pi5 run journals. Normal 30-second light
 * frames had a 5.34 s median read/download/processing burden (864 samples),
 * while dithers had a 5.46 s median (168 samples). Target centring and a
 * nearby-panel slew had medians of 29.85 s and 3.49 s respectively.
 */
export const INTEGRATION_TIMING = {
  frameOverheadS: 5.34,
  ditherS: 5.46,
  centerS: 29.85,
  nearbySlewS: 3.49,
  initialSlewS: 60.64,
  autofocusS: 137.43,
  filterS: 3.2,
} as const;

export interface IntegrationEstimate {
  scheduledS: number;
  frames: number;
  exposureS: number;
  cleanIntegrationS: number;
  setupS: number;
  frameOverheadS: number;
  ditherCount: number;
  ditherS: number;
  efficiencyPct: number;
}

function isExposureItem(item: PlanItem): item is Extract<PlanItem, { kind: "goto_and_stack" | "take_exposures" }> {
  return item.kind === "goto_and_stack" || item.kind === "take_exposures";
}

function fixedFrameLimit(item: Extract<PlanItem, { kind: "goto_and_stack" | "take_exposures" }>): number | null {
  if (item.kind !== "take_exposures" || item.count.kind !== "fixed") return null;
  return item.count.frames;
}

/** Estimate captured light time, before cloud and live-stack rejection losses. */
export function estimateIntegration(
  item: PlanItem,
  compiled: CompiledItem | undefined,
  firstTargetOfNight: boolean,
): IntegrationEstimate | null {
  if (!isExposureItem(item) || !item.enabled || compiled?.start_utc == null || compiled.end_utc == null) return null;
  const scheduledS = (Date.parse(compiled.end_utc) - Date.parse(compiled.start_utc)) / 1000;
  if (!Number.isFinite(scheduledS) || scheduledS <= 0 || item.exposure_sec <= 0) return null;

  let setupS = 0;
  if (item.kind === "goto_and_stack") {
    setupS += firstTargetOfNight ? INTEGRATION_TIMING.initialSlewS : INTEGRATION_TIMING.nearbySlewS;
    setupS += INTEGRATION_TIMING.centerS;
    if (item.autofocus_before) setupS += INTEGRATION_TIMING.autofocusS;
  }
  if (item.filter_name !== null && item.filter_name !== undefined) setupS += INTEGRATION_TIMING.filterS;

  const every = item.dither?.every_n_frames ?? null;
  const frameLimit = fixedFrameLimit(item);
  let frames = 0;
  while (frameLimit === null || frames < frameLimit) {
    const candidate = frames + 1;
    const ditherCount = every !== null && every > 0 ? Math.floor(candidate / every) : 0;
    const usedS = setupS
      + candidate * (item.exposure_sec + INTEGRATION_TIMING.frameOverheadS)
      + ditherCount * INTEGRATION_TIMING.ditherS;
    if (usedS > scheduledS) break;
    frames = candidate;
  }

  const ditherCount = every !== null && every > 0 ? Math.floor(frames / every) : 0;
  const cleanIntegrationS = frames * item.exposure_sec;
  return {
    scheduledS,
    frames,
    exposureS: item.exposure_sec,
    cleanIntegrationS,
    setupS,
    frameOverheadS: frames * INTEGRATION_TIMING.frameOverheadS,
    ditherCount,
    ditherS: ditherCount * INTEGRATION_TIMING.ditherS,
    efficiencyPct: (cleanIntegrationS / scheduledS) * 100,
  };
}

export function formatIntegrationDuration(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  if (hours === 0) return `${String(minutes)} min`;
  return remainder === 0 ? `${String(hours)} h` : `${String(hours)} h ${String(remainder)} min`;
}
