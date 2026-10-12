// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import {
  azimuthCollides,
  clampAltitudeDeg,
  horizonStats,
  roundToStep,
  widestGapMidpointDeg,
  wrapAzimuthDeg,
} from "./horizonMath";

describe("wrapAzimuthDeg", () => {
  it("leaves an in-range azimuth untouched", () => {
    expect(wrapAzimuthDeg(45)).toBe(45);
    expect(wrapAzimuthDeg(0)).toBe(0);
  });

  it("wraps a negative azimuth into [0, 360)", () => {
    expect(wrapAzimuthDeg(-10)).toBe(350);
  });

  it("wraps 360 and beyond back to 0", () => {
    expect(wrapAzimuthDeg(360)).toBe(0);
    expect(wrapAzimuthDeg(370)).toBe(10);
  });
});

describe("clampAltitudeDeg", () => {
  it("clamps to the server's [-10, 90] bound", () => {
    expect(clampAltitudeDeg(-50)).toBe(-10);
    expect(clampAltitudeDeg(120)).toBe(90);
    expect(clampAltitudeDeg(30)).toBe(30);
  });
});

describe("roundToStep", () => {
  it("snaps to the nearest half degree", () => {
    expect(roundToStep(12.24, 0.5)).toBe(12);
    expect(roundToStep(12.26, 0.5)).toBe(12.5);
  });
});

describe("azimuthCollides", () => {
  const points = [
    { azimuth_deg: 10, altitude_deg: 5 },
    { azimuth_deg: 350, altitude_deg: 5 },
  ];

  it("detects a near-duplicate on the plain side", () => {
    expect(azimuthCollides(points, 10.05, 0.5)).toBe(true);
    expect(azimuthCollides(points, 90, 0.5)).toBe(false);
  });

  it("detects a near-duplicate across the north seam", () => {
    // 359 and 2 are 3 degrees apart through north, not 357 the other way —
    // a naive |a - b| comparison would miss this collision entirely.
    const seamPoints = [{ azimuth_deg: 359, altitude_deg: 5 }];
    expect(azimuthCollides(seamPoints, 2, 5)).toBe(true);
    expect(azimuthCollides(seamPoints, 2, 2)).toBe(false);
  });

  it("ignores the point at excludeIndex (dragging a point past its own slot)", () => {
    expect(azimuthCollides(points, 10, 0.5, 0)).toBe(false);
  });
});

describe("widestGapMidpointDeg", () => {
  it("is 0 for an empty profile", () => {
    expect(widestGapMidpointDeg([])).toBe(0);
  });

  it("is the antipode of a single point", () => {
    expect(widestGapMidpointDeg([{ azimuth_deg: 0, altitude_deg: 0 }])).toBe(180);
  });

  it("picks the middle of the largest gap, including the one that wraps through north", () => {
    // Gaps: 10->200 (190, the largest), 200->350 (150), 350->10 (20).
    const points = [
      { azimuth_deg: 10, altitude_deg: 0 },
      { azimuth_deg: 200, altitude_deg: 0 },
      { azimuth_deg: 350, altitude_deg: 0 },
    ];
    expect(widestGapMidpointDeg(points)).toBeCloseTo(105, 6);
  });
});

describe("horizonStats", () => {
  it("is null for an empty profile — absent, not zero", () => {
    expect(horizonStats([])).toBeNull();
  });

  it("min/max/mean of a flat profile all equal the flat altitude", () => {
    const stats = horizonStats([
      { azimuth_deg: 0, altitude_deg: 5 },
      { azimuth_deg: 180, altitude_deg: 5 },
    ]);
    expect(stats).toEqual({ minAltitudeDeg: 5, maxAltitudeDeg: 5, meanAltitudeDeg: 5 });
  });

  it("weights the mean by azimuth span, not by point count", () => {
    // A short 10-degree wide spike to 90 degrees altitude at north, flat at 0
    // elsewhere, should barely move the mean above 0.
    const stats = horizonStats([
      { azimuth_deg: 355, altitude_deg: 0 },
      { azimuth_deg: 0, altitude_deg: 90 },
      { azimuth_deg: 5, altitude_deg: 0 },
      { azimuth_deg: 180, altitude_deg: 0 },
    ]);
    expect(stats?.minAltitudeDeg).toBe(0);
    expect(stats?.maxAltitudeDeg).toBe(90);
    expect(stats?.meanAltitudeDeg).toBeLessThan(2);
  });

  it("closes the cycle through the point list's wraparound segment", () => {
    // Two points only: the "second half" of the circle (180 -> 350, wrapping
    // through the 350->180 gap the short way) must still be accounted for.
    const stats = horizonStats([
      { azimuth_deg: 180, altitude_deg: 10 },
      { azimuth_deg: 350, altitude_deg: 30 },
    ]);
    // Segment 180->350 (span 170): avg 20. Segment 350->180 wrap (span 190): avg 20.
    expect(stats?.meanAltitudeDeg).toBeCloseTo(20, 6);
  });
});
