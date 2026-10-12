// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { AstroBodyModel } from "../api/astro";
import {
  apparentRadiusDeg,
  buildSolarSystemShapes,
  PLANET_MARKER_RADIUS_DEG,
  solarSystemShapeId,
} from "./solarSystem";
import type { FovShape } from "./types";

function asCircle(shape: FovShape | undefined): Extract<FovShape, { kind: "circle" }> {
  if (shape?.kind !== "circle") throw new Error("expected a circle shape");
  return shape;
}

function body(overrides: Partial<AstroBodyModel>): AstroBodyModel {
  return {
    id: "moon",
    name: "Moon",
    kind: "moon",
    ra_j2000_h: 11.2345,
    dec_j2000_deg: 4.567,
    apparent_diameter_arcmin: 31.8,
    illuminated_fraction: 0.62,
    phase_angle_deg: 71.4,
    ...overrides,
  };
}

const allVisible = { sun: true, moon: true, planets: true };

describe("apparentRadiusDeg", () => {
  it("halves the arcmin diameter and converts to degrees", () => {
    // 31.8 arcmin diameter -> 0.53 deg diameter -> 0.265 deg radius.
    expect(apparentRadiusDeg(31.8)).toBeCloseTo(0.265, 5);
  });
});

describe("buildSolarSystemShapes", () => {
  it("draws the Sun and Moon at their true apparent size, role matching kind", () => {
    const sun = body({ id: "sun", name: "Sun", kind: "sun", apparent_diameter_arcmin: 32.0, illuminated_fraction: null, phase_angle_deg: null });
    const moon = body({});
    const shapes = buildSolarSystemShapes([sun, moon], allVisible);
    expect(shapes).toHaveLength(2);

    const sunShape = asCircle(shapes.find((s) => s.id === solarSystemShapeId(sun)));
    expect(sunShape.style.role).toBe("sun");
    expect(sunShape.radiusDeg).toBeCloseTo(32.0 / 120, 5);
    expect(sunShape.centerRa).toBeCloseTo(sun.ra_j2000_h * 15, 5);
    expect(sunShape.centerDec).toBe(sun.dec_j2000_deg);
    expect(sunShape.label).toBe("Sun");

    const moonShape = asCircle(shapes.find((s) => s.id === solarSystemShapeId(moon)));
    expect(moonShape.style.role).toBe("moon");
    expect(moonShape.radiusDeg).toBeCloseTo(31.8 / 120, 5);
    expect(moonShape.label).toBe("Moon");
  });

  it("draws every planet as the same fixed-size marker, ignoring apparent_diameter_arcmin", () => {
    const jupiter = body({ id: "jupiter", name: "Jupiter", kind: "planet", apparent_diameter_arcmin: 0.83, illuminated_fraction: null, phase_angle_deg: 11.2 });
    const mercury = body({ id: "mercury", name: "Mercury", kind: "planet", apparent_diameter_arcmin: 0.08, illuminated_fraction: null, phase_angle_deg: 90 });
    const shapes = buildSolarSystemShapes([jupiter, mercury], allVisible).map(asCircle);
    expect(shapes).toHaveLength(2);
    for (const shape of shapes) {
      expect(shape.radiusDeg).toBe(PLANET_MARKER_RADIUS_DEG);
      expect(shape.style.role).toBe("planet");
    }
  });

  it("omits a kind whose own toggle is off, and keeps the others", () => {
    const sun = body({ id: "sun", name: "Sun", kind: "sun" });
    const moon = body({});
    const mars = body({ id: "mars", name: "Mars", kind: "planet" });
    const shapes = buildSolarSystemShapes([sun, moon, mars], { sun: false, moon: true, planets: false });
    expect(shapes).toHaveLength(1);
    expect(shapes[0]?.id).toBe(solarSystemShapeId(moon));
  });

  it("labels every visible body, unlike the catalogue/star layers' FOV-gated labels", () => {
    const shapes = buildSolarSystemShapes([body({})], allVisible);
    expect(asCircle(shapes[0]).label).toBe("Moon");
  });
});
