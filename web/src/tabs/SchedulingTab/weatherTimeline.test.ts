// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { ForecastPoint } from "../../api/weather";
import { assessWeather, weatherSlots } from "./weatherTimeline";

function point(overrides: Partial<ForecastPoint> = {}): ForecastPoint {
  return {
    t: "2026-09-23T20:00:00Z",
    cloud_total: 10,
    cloud_low: 0,
    cloud_mid: 0,
    cloud_high: 10,
    precip_prob: 0,
    humidity: 60,
    dew_point_c: 5,
    temp_c: 10,
    wind_ms: 2,
    wind_direction_deg: 90,
    visibility_m: 20_000,
    source: "metno",
    ...overrides,
  };
}

describe("weather timeline", () => {
  it("uses the scheduler's green, amber, and red cloud thresholds", () => {
    expect(assessWeather(point({ cloud_total: 29 })).quality).toBe("good");
    expect(assessWeather(point({ cloud_total: 30 })).quality).toBe("marginal");
    expect(assessWeather(point({ cloud_total: 60 })).quality).toBe("poor");
  });

  it("escalates otherwise clear hours for rain, wind, and dew risk", () => {
    expect(assessWeather(point({ precip_prob: 55 })).quality).toBe("poor");
    expect(assessWeather(point({ wind_ms: 7 })).quality).toBe("marginal");
    expect(assessWeather(point({ temp_c: 6, dew_point_c: 5.5 })).quality).toBe("poor");
  });

  it("builds midpoint-bounded hourly slots clipped to the night", () => {
    const points = [point({ t: "2026-09-23T20:00:00Z" }), point({ t: "2026-09-23T21:00:00Z" })];
    const slots = weatherSlots(points, { startMs: Date.parse("2026-09-23T19:45:00Z"), endMs: Date.parse("2026-09-23T21:15:00Z") });
    expect(slots.map(({ startMs, endMs }) => [new Date(startMs).toISOString(), new Date(endMs).toISOString()])).toEqual([
      ["2026-09-23T19:45:00.000Z", "2026-09-23T20:30:00.000Z"],
      ["2026-09-23T20:30:00.000Z", "2026-09-23T21:15:00.000Z"],
    ]);
  });
});
