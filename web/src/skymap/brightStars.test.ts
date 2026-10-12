// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { BrightStarModel } from "../api/skymap";
import { buildBrightStarShapes, starDotRadiusDeg, starShapeId, STARS_MAX_FOV_DEG } from "./brightStars";
import { OUTLINES_MAX_FOV_DEG } from "./catalogOutlines";
import { GRID_LABEL_MAX_FOV_DEG } from "./frames/shared/skyLines";
import type { FovShape } from "./types";

function asCircle(shape: FovShape | undefined): Extract<FovShape, { kind: "circle" }> {
  if (shape?.kind !== "circle") throw new Error("expected a circle shape");
  return shape;
}

function star(overrides: Partial<BrightStarModel>): BrightStarModel {
  return {
    name: "Betelgeuse",
    designation: "HR 2061",
    ra_j2000_h: 88.792939 / 15,
    dec_j2000_deg: 7.407064,
    magnitude: 0.45,
    ...overrides,
  };
}

describe("STARS_MAX_FOV_DEG", () => {
  it("reuses catalogOutlines' own ceiling rather than a second constant", () => {
    expect(STARS_MAX_FOV_DEG).toBe(OUTLINES_MAX_FOV_DEG);
  });
});

describe("starDotRadiusDeg", () => {
  it("is larger for a brighter (lower-magnitude) star", () => {
    expect(starDotRadiusDeg(-1.46)).toBeGreaterThan(starDotRadiusDeg(0.45));
    expect(starDotRadiusDeg(0.45)).toBeGreaterThan(starDotRadiusDeg(3.9));
  });

  it("clamps rather than inverting outside the bundled table's own magnitude range", () => {
    const brightest = starDotRadiusDeg(-10);
    const faintest = starDotRadiusDeg(10);
    expect(starDotRadiusDeg(-1.46)).toBeLessThanOrEqual(brightest);
    expect(starDotRadiusDeg(4.0)).toBeGreaterThanOrEqual(faintest);
    expect(brightest).toBeGreaterThan(faintest);
  });
});

describe("buildBrightStarShapes", () => {
  it("draws one circle per star, at its own position, role 'star'", () => {
    const stars = [
      star({}),
      star({ name: "Bellatrix", designation: "HR 1790", ra_j2000_h: 81.282764 / 15, dec_j2000_deg: 6.349703, magnitude: 1.64 }),
    ];
    const shapes = buildBrightStarShapes(stars, { fovDeg: 10, viewRaDeg: 85, viewDecDeg: 5 });
    expect(shapes).toHaveLength(2);
    const betelgeuse = asCircle(shapes.find((s) => s.id === starShapeId("Betelgeuse")));
    expect(betelgeuse.centerRa).toBeCloseTo(88.792939, 5);
    expect(betelgeuse.centerDec).toBeCloseTo(7.407064, 5);
    expect(betelgeuse.style.role).toBe("star");
  });

  it("labels stars near the current view when the field is narrow enough", () => {
    const shapes = buildBrightStarShapes([star({})], { fovDeg: 10, viewRaDeg: 85, viewDecDeg: 5 });
    expect(asCircle(shapes[0]).label).toBe("Betelgeuse");
  });

  it("draws a dot but no label once the view is wider than GRID_LABEL_MAX_FOV_DEG", () => {
    const shapes = buildBrightStarShapes([star({})], {
      fovDeg: GRID_LABEL_MAX_FOV_DEG + 1,
      viewRaDeg: 85,
      viewDecDeg: 5,
    });
    expect(shapes).toHaveLength(1);
    expect(asCircle(shapes[0]).label).toBeUndefined();
  });

  it("draws a dot but no label while the view has not settled (fovDeg null)", () => {
    const shapes = buildBrightStarShapes([star({})], { fovDeg: null, viewRaDeg: 85, viewDecDeg: 5 });
    expect(asCircle(shapes[0]).label).toBeUndefined();
  });

  it("caps labels at the nearest stars to the view, same nearestLabels rule as the grid/constellation layers", () => {
    const many: BrightStarModel[] = Array.from({ length: 30 }, (_, i) => ({
      name: `Star ${String(i)}`,
      designation: `HR ${String(1000 + i)}`,
      ra_j2000_h: (85 + i * 0.05) / 15,
      dec_j2000_deg: 5,
      magnitude: 2.0,
    }));
    const shapes = buildBrightStarShapes(many, { fovDeg: 5, viewRaDeg: 85, viewDecDeg: 5 });
    const labelled = shapes.filter((s) => "label" in s && s.label !== undefined);
    expect(labelled.length).toBeLessThanOrEqual(20);
    expect(labelled.length).toBeGreaterThan(0);
    // Every shape still gets a dot, whether labelled or not.
    expect(shapes).toHaveLength(30);
  });
});
