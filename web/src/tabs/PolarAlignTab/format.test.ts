// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import {
  altitudeInstruction,
  azimuthInstruction,
  formatAngleMagnitude,
  formatArcsec,
  formatCorrectionMagnitudeArcmin,
  formatCorrectionMagnitudeArcsec,
  minutesSince,
  s30ProPaSubLimitSeconds,
} from "./format";

describe("formatAngleMagnitude", () => {
  it("uses degrees for gross errors", () => {
    expect(formatAngleMagnitude(2.308)).toBe("2.31°");
  });

  it("uses degrees below one degree", () => {
    expect(formatAngleMagnitude(-0.2)).toBe("0.20°");
  });

  it("uses two decimals below one arcminute", () => {
    expect(formatAngleMagnitude(30 / 3600)).toBe("0.01°");
  });
});

describe("s30ProPaSubLimitSeconds", () => {
  it("estimates a half-pixel PA drift budget from the actual tele pixel scale", () => {
    expect(s30ProPaSubLimitSeconds(0.25, "Seestar S30 Pro")).toBeCloseTo(28.5, 0);
    expect(s30ProPaSubLimitSeconds(0.125, "Seestar S30 Pro")).toBeCloseTo(57, 0);
  });

  it("does not invent a limit for unknown devices or invalid errors", () => {
    expect(s30ProPaSubLimitSeconds(0.25, null)).toBeNull();
    expect(s30ProPaSubLimitSeconds(Number.NaN, "Seestar S30 Pro")).toBeNull();
    expect(s30ProPaSubLimitSeconds(0, "Seestar S30 Pro")).toBe(Number.POSITIVE_INFINITY);
  });
});

describe("formatArcsec", () => {
  it("renders null as unknown, never as a numeric-looking 0.00", () => {
    expect(formatArcsec(null)).toBe("unknown");
  });

  it("renders a real value with one decimal and the arcsec mark", () => {
    expect(formatArcsec(12.34)).toBe("12.3″");
  });
});

describe("formatCorrectionMagnitudeArcmin", () => {
  it("is always positive, in arcminutes, regardless of the correction's sign", () => {
    expect(formatCorrectionMagnitudeArcmin(0.2)).toBe("12.0′");
    expect(formatCorrectionMagnitudeArcmin(-0.2)).toBe("12.0′");
  });
});

it("formats a correction in whole arcseconds", () => {
  expect(formatCorrectionMagnitudeArcsec(0.05)).toBe("180″");
});

describe("altitude/azimuth instructions — ssc.astro.polar.PolarAlignmentModel's sign convention", () => {
  it("a positive altitude correction points up", () => {
    expect(altitudeInstruction(0.5)).toBe("UP");
  });

  it("a negative altitude correction points down", () => {
    expect(altitudeInstruction(-0.5)).toBe("DOWN");
  });

  it("maps azimuth corrections to east/west using the hemisphere", () => {
    expect(azimuthInstruction(0.5, 48.7)).toBe("EAST");
    expect(azimuthInstruction(-0.5, 48.7)).toBe("WEST");
    expect(azimuthInstruction(0.5, -33.9)).toBe("WEST");
    expect(azimuthInstruction(-0.5, -33.9)).toBe("EAST");
  });
});

describe("minutesSince", () => {
  it("is null for an unparseable timestamp rather than a fabricated age", () => {
    expect(minutesSince("not-a-date", Date.now())).toBeNull();
  });

  it("computes elapsed minutes", () => {
    const now = Date.parse("2026-09-11T20:05:00Z");
    expect(minutesSince("2026-09-11T20:00:00Z", now)).toBeCloseTo(5, 5);
  });
});
