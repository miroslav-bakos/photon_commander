// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { CompiledItem, PlanItem } from "../../api/plans";
import { estimateIntegration, formatIntegrationDuration } from "./integrationEstimate";

function item(overrides: Partial<Extract<PlanItem, { kind: "goto_and_stack" }>> = {}): Extract<PlanItem, { kind: "goto_and_stack" }> {
  return {
    id: "panel-1",
    kind: "goto_and_stack",
    live_stack: true,
    start: { kind: "asap" },
    end: { kind: "duration", minutes: 166.7603666667 },
    enabled: true,
    target: { kind: "catalog", name: "IC 1805 1", ra_j2000_h: 2.47, dec_j2000_deg: 61.09 },
    framing: { mode: "eq", pa_offset_deg: 0, lens: "tele" },
    constraints: {},
    autofocus_before: false,
    exposure_sec: 60,
    gain: 200,
    filter_name: null,
    dither: { pixels: null, min_pixels: 15, max_pixels: 200, every_n_frames: 5 },
    ...overrides,
  };
}

const compiled: CompiledItem = {
  item_id: "panel-1",
  kind: "goto_and_stack",
  enabled: true,
  start_utc: "2026-09-23T18:31:01.517Z",
  end_utc: "2026-09-23T21:17:47.139Z",
  resolution: "Astronomical dusk → 20:31",
  end_resolution: "166.76 min → 23:17",
  panels: [],
  diagnostics: [],
};

describe("estimateIntegration", () => {
  it("budgets the longer first slew, measured frame overhead, and dithers", () => {
    const estimate = estimateIntegration(item(), compiled, true);
    expect(estimate).toMatchObject({ frames: 149, ditherCount: 29, cleanIntegrationS: 8_940 });
    expect(estimate?.efficiencyPct).toBeCloseTo(89.35, 1);
  });

  it("gives a nearby follow-on panel one additional frame", () => {
    const estimate = estimateIntegration(item(), compiled, false);
    expect(estimate).toMatchObject({ frames: 150, ditherCount: 30, cleanIntegrationS: 9_000 });
  });

  it("removes dither burden when dithering is disabled", () => {
    const withDither = estimateIntegration(item(), compiled, false);
    const withoutDither = estimateIntegration(item({ dither: null }), compiled, false);
    expect(withoutDither?.ditherCount).toBe(0);
    expect(withoutDither?.frames).toBeGreaterThan(withDither?.frames ?? 0);
  });

  it("returns zero frames for a window shorter than setup and one exposure", () => {
    const short = { ...compiled, end_utc: "2026-09-23T18:31:31.517Z" };
    expect(estimateIntegration(item(), short, true)?.frames).toBe(0);
  });

  it("formats integration time without decimal-hour ambiguity", () => {
    expect(formatIntegrationDuration(9_000)).toBe("2 h 30 min");
    expect(formatIntegrationDuration(1_800)).toBe("30 min");
  });
});
