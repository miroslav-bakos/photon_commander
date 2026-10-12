import { describe, expect, it } from "vitest";
import type { AnnualAltitudeNightModel } from "../../api/astro";
import { annualDarkPeakAltitude } from "./nightAltitudeGeometry";

function night(overrides: Partial<AnnualAltitudeNightModel>): AnnualAltitudeNightModel {
  return {
    window_start: "2026-01-01T11:00:00Z",
    darkness_hours: 12,
    peak_altitude_deg: 42,
    peak_when: "2026-01-01T20:00:00Z",
    max_altitude_deg: 83,
    max_when: "2026-01-01T10:00:00Z",
    hours_above_limit: 4,
    ...overrides,
  };
}

describe("annualDarkPeakAltitude", () => {
  it("uses the seasonally varying dark-window peak rather than fixed culmination", () => {
    const winter = night({ peak_altitude_deg: 82, max_altitude_deg: 83 });
    const summer = night({ peak_altitude_deg: 14, max_altitude_deg: 83 });

    expect(annualDarkPeakAltitude(winter)).toBe(82);
    expect(annualDarkPeakAltitude(summer)).toBe(14);
  });

  it("has no curve point when astronomical darkness does not occur", () => {
    expect(annualDarkPeakAltitude(night({ peak_altitude_deg: null, peak_when: null }))).toBeNull();
  });
});
