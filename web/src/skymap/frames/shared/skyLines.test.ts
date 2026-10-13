// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { FovShape } from "../../types";
import { altAzToRaDec, circularDistanceDeg, localMeanSiderealTimeHours, raDecToAltAz } from "./sidereal";
import {
  altAzGridShapes,
  altitudeLimitCircleShape,
  celestialEquatorShape,
  eclipticShape,
  equatorialGridShapes,
  GRID_LABEL_MAX_FOV_DEG,
  cardinalShapeId,
  horizonShapes,
  meridianShapes,
} from "./skyLines";

/** `FovShape.label` is only on the variants that carry one — this reads it
 * back without the union member's absence tripping every `.label` access
 * in a test that only ever builds polylines. */
function shapeLabel(shape: FovShape | undefined): string | undefined {
  return shape !== undefined && "label" in shape ? shape.label : undefined;
}

describe("celestialEquatorShape", () => {
  it("localizes reference labels without changing geometry or identifiers", () => {
    const inputs = { latitudeDeg: 48, longitudeDeg: 17, instant: new Date("2026-10-13T00:00:00Z") };
    const original = meridianShapes(inputs);
    for (const language of ["sk", "cs"] as const) {
      const localized = meridianShapes(inputs, language);
      expect(shapeLabel(localized[0])).toBe("Meridián");
      expect(shapeLabel(localized[1])).toBe("Zenit");
      localized.forEach((shape, index) => {
        expect({ ...shape, label: undefined }).toEqual({ ...original[index], label: undefined });
      });
      const equator = celestialEquatorShape(language);
      expect(shapeLabel(equator)).not.toBe("Celestial equator");
      expect({ ...equator, label: undefined }).toEqual({ ...celestialEquatorShape(), label: undefined });
    }
  });
  it("is a closed Dec = 0 path around the whole sky", () => {
    const shape = celestialEquatorShape();
    if (shape.kind !== "polyline") throw new Error("expected a polyline");
    expect(shape.verticesRaDec.every(([, dec]) => dec === 0)).toBe(true);
    expect(shape.verticesRaDec[0]).toEqual([0, 0]);
    expect(shape.verticesRaDec.at(-1)).toEqual([0, 0]);
    expect(shape.verticesRaDec.length).toBe(361);
  });
});

describe("meridianShapes", () => {
  const inputs = { latitudeDeg: 48.15, longitudeDeg: 17.11, instant: new Date("2026-09-07T21:00:00Z") };

  it("puts the meridian at RA = local sidereal time, pole to pole, and the zenith at (LST, latitude)", () => {
    const [meridian, zenith] = meridianShapes(inputs);
    const lstDeg = localMeanSiderealTimeHours(inputs.instant, inputs.longitudeDeg) * 15;
    if (meridian?.kind !== "polyline" || zenith?.kind !== "circle") throw new Error("unexpected shapes");
    expect(meridian.verticesRaDec.every(([ra]) => ra === lstDeg)).toBe(true);
    expect(meridian.verticesRaDec[0]?.[1]).toBe(-90);
    expect(meridian.verticesRaDec.at(-1)?.[1]).toBe(90);
    expect(zenith.centerRa).toBeCloseTo(lstDeg, 9);
    expect(zenith.centerDec).toBe(48.15);
    expect(zenith.label).toBe("Zenith");
  });

  it("moves the meridian east as the instant advances (it follows the scrubber, not the wall clock)", () => {
    const later = { ...inputs, instant: new Date(inputs.instant.getTime() + 3_600_000) };
    const raNow = (meridianShapes(inputs)[0] as { verticesRaDec: [number, number][] }).verticesRaDec[0]?.[0] ?? NaN;
    const raLater = (meridianShapes(later)[0] as { verticesRaDec: [number, number][] }).verticesRaDec[0]?.[0] ?? NaN;
    const advanced = ((raLater - raNow) % 360 + 360) % 360;
    // One hour of UT is 15.04 deg of sidereal rotation.
    expect(advanced).toBeCloseTo(15.041, 2);
  });
});

describe("equatorialGridShapes", () => {
  const base = { fovDeg: 60, viewRaDeg: 0, viewDecDeg: 0, strengthenEquator: true };

  it("draws 24 RA lines (pole to pole) and 17 Dec lines (RA full circle, every 10 deg)", () => {
    const shapes = equatorialGridShapes(base);
    const raLines = shapes.filter((s) => s.id.startsWith("skyline:eq-grid:ra:"));
    const decLines = shapes.filter((s) => s.id.startsWith("skyline:eq-grid:dec:"));
    expect(raLines).toHaveLength(24);
    expect(decLines).toHaveLength(17);

    for (const line of raLines) {
      if (line.kind !== "polyline") throw new Error("expected a polyline");
      const decs = line.verticesRaDec.map(([, d]) => d);
      const ras = line.verticesRaDec.map(([r]) => r);
      // Pole to pole, so all 24 meridians converge at the celestial poles
      // the way every planetarium draws them. They used to stop at +/-80,
      // leaving the pole an empty gap with two dozen lines pointing at
      // nothing.
      expect(Math.min(...decs)).toBeCloseTo(-90, 9);
      expect(Math.max(...decs)).toBeCloseTo(90, 9);
      expect(new Set(ras).size).toBe(1); // constant RA
    }
    for (const line of decLines) {
      if (line.kind !== "polyline") throw new Error("expected a polyline");
      // A closed loop around the whole sky at this Dec: starts and ends at
      // the same point, RA 0.
      expect(line.verticesRaDec[0]?.[0]).toBe(0);
      expect(line.verticesRaDec.at(-1)).toEqual(line.verticesRaDec[0]);
      expect(line.verticesRaDec.length).toBeGreaterThan(2);
    }
  });

  it("strengthens the Dec = 0 line only when the equator toggle is off", () => {
    const equatorOff = equatorialGridShapes({ ...base, strengthenEquator: true });
    expect(equatorOff.find((s) => s.id === "skyline:eq-grid:dec:0")?.style.role).toBe("skyline");

    const equatorOn = equatorialGridShapes({ ...base, strengthenEquator: false });
    expect(equatorOn.find((s) => s.id === "skyline:eq-grid:dec:0")?.style.role).toBe("coordGrid");
  });

  it("labels only near the view and only within the label FoV ceiling, capped at 12", () => {
    // Keyed off the constant, not a literal: the ceiling was raised 30 -> 60
    // once it was found to hide every label at the map's own opening view.
    const wide = equatorialGridShapes({ ...base, fovDeg: GRID_LABEL_MAX_FOV_DEG + 0.01 });
    expect(wide.every((s) => shapeLabel(s) === undefined)).toBe(true);

    const narrow = equatorialGridShapes({ ...base, fovDeg: 20 });
    const labelled = narrow.filter((s) => shapeLabel(s) !== undefined);
    expect(labelled.length).toBeGreaterThan(0);
    expect(labelled.length).toBeLessThanOrEqual(12);
    expect(shapeLabel(narrow.find((s) => s.id === "skyline:eq-grid:ra:0"))).toBe("0h");
    expect(shapeLabel(narrow.find((s) => s.id === "skyline:eq-grid:dec:0"))).toBe("0°");
  });
});

describe("altAzGridShapes", () => {
  const altAzSite = { latitudeDeg: 48.15, longitudeDeg: 17.11, instant: new Date("2026-09-07T22:00:00Z") };
  // A view centred exactly on the alt=40/az=0 grid lines' own intersection,
  // so the labelling test below knows exactly which ids must come back
  // labelled rather than guessing from whatever the view happens to be near.
  const [viewRaDeg, viewDecDeg] = altAzToRaDec(40, 0, altAzSite);
  const base = { ...altAzSite, fovDeg: 60, viewRaDeg, viewDecDeg, strengthenHorizon: true };

  it("draws 24 azimuth lines and 9 altitude circles (0..80 deg, every 10)", () => {
    const shapes = altAzGridShapes(base);
    expect(shapes.filter((s) => s.id.startsWith("skyline:altaz-grid:az:"))).toHaveLength(24);
    expect(shapes.filter((s) => s.id.startsWith("skyline:altaz-grid:alt:"))).toHaveLength(9);
  });

  it("strengthens the alt = 0 (horizon) line only when the Horizon toggle is off", () => {
    const horizonOff = altAzGridShapes({ ...base, strengthenHorizon: true });
    expect(horizonOff.find((s) => s.id === "skyline:altaz-grid:alt:0")?.style.role).toBe("skyline");

    const horizonOn = altAzGridShapes({ ...base, strengthenHorizon: false });
    expect(horizonOn.find((s) => s.id === "skyline:altaz-grid:alt:0")?.style.role).toBe("coordGrid");
  });

  it("every vertex on the alt=40 circle round-trips to alt 40, within 1e-6 deg", () => {
    const shapes = altAzGridShapes(base);
    const altLine = shapes.find((s) => s.id === "skyline:altaz-grid:alt:40");
    if (altLine?.kind !== "polyline") throw new Error("expected a polyline");
    for (const [ra, dec] of altLine.verticesRaDec) {
      const [alt] = raDecToAltAz(ra, dec, base);
      expect(Math.abs(alt - 40)).toBeLessThan(1e-6);
    }
  });

  it("labels only near the view and only within the label FoV ceiling, capped at 12 — the same rule equatorialGridShapes uses", () => {
    const wide = altAzGridShapes({ ...base, fovDeg: GRID_LABEL_MAX_FOV_DEG + 0.01 });
    expect(wide.every((s) => shapeLabel(s) === undefined)).toBe(true);

    const narrow = altAzGridShapes({ ...base, fovDeg: 20 });
    const labelled = narrow.filter((s) => shapeLabel(s) !== undefined);
    expect(labelled.length).toBeGreaterThan(0);
    expect(labelled.length).toBeLessThanOrEqual(12);
    // The view was built exactly at alt=40/az=0 (`base`'s own setup), so
    // both lines through that point must be among the labelled ones.
    expect(shapeLabel(narrow.find((s) => s.id === "skyline:altaz-grid:az:0"))).toBe("0°");
    expect(shapeLabel(narrow.find((s) => s.id === "skyline:altaz-grid:alt:40"))).toBe("40°");
  });
});

describe("eclipticShape", () => {
  it("passes through RA 0h/Dec 0 (the vernal equinox, lambda=0)", () => {
    const shape = eclipticShape(60);
    if (shape.kind !== "polyline") throw new Error("expected a polyline");
    expect(shape.verticesRaDec[0]?.[0]).toBeCloseTo(0, 9);
    expect(shape.verticesRaDec[0]?.[1]).toBeCloseTo(0, 9);
    expect(shape.label).toBe("Ecliptic");
    expect(shape.style.role).toBe("skyline");
    expect(shape.style.dashed).toBe(true);
  });

  it("passes through RA 6h/Dec +23.44 (the summer solstice, lambda=90) when sampled at that exact point", () => {
    // fovDeg = 1 buckets to a 1-deg step, so lambda=90 is an exact sample
    // (not interpolated) at index 90.
    const shape = eclipticShape(1);
    if (shape.kind !== "polyline") throw new Error("expected a polyline");
    const point = shape.verticesRaDec[90];
    expect(point?.[0]).toBeCloseTo(90, 6);
    expect(point?.[1]).toBeCloseTo(23.44, 6);
  });
});

describe("horizonShapes", () => {
  const site = { latitudeDeg: 48.15, longitudeDeg: 17.11, instant: new Date("2026-09-07T22:00:00Z"), fovDeg: 10 };

  it("is the closed alt = 0 line, dashed, labelled 'Horizon'", () => {
    const shape = horizonShapes(site)[0];
    if (shape?.kind !== "polyline") throw new Error("expected a polyline");
    expect(shape.label).toBe("Horizon");
    expect(shape.style.dashed).toBe(true);
    expect(shape.verticesRaDec[0]).toEqual(shape.verticesRaDec.at(-1));
    for (const [ra, dec] of shape.verticesRaDec) {
      const [alt] = raDecToAltAz(ra, dec, site);
      expect(Math.abs(alt)).toBeLessThan(1e-6);
    }
  });

  it("marks N/E/S/W on the ring, each at alt = 0 and its own azimuth", () => {
    const shapes = horizonShapes(site);
    const expected: [string, number][] = [
      ["N", 0],
      ["E", 90],
      ["S", 180],
      ["W", 270],
    ];
    for (const [name, azDeg] of expected) {
      const marker = shapes.find((s) => s.id === cardinalShapeId(name));
      if (marker?.kind !== "circle") throw new Error(`expected a circle for ${name}`);
      expect(marker.label).toBe(name);
      const [alt, az] = raDecToAltAz(marker.centerRa, marker.centerDec, site);
      expect(Math.abs(alt)).toBeLessThan(1e-6);
      // Azimuth wraps, so compare the shorter way round: N at 0 must not
      // fail against a computed 359.9999.
      expect(circularDistanceDeg(az, azDeg)).toBeLessThan(1e-6);
    }
  });
});

describe("altitudeLimitCircleShape", () => {
  const site = { latitudeDeg: 48.15, longitudeDeg: 17.11, instant: new Date("2026-09-07T22:00:00Z"), fovDeg: 10 };

  it("is a closed circle whose every vertex sits at exactly the configured altitude, for a spread of azimuths, always the fixed danger role", () => {
    const shape = altitudeLimitCircleShape({ ...site, altitudeLimitDeg: 20 });
    if (shape.kind !== "polyline") throw new Error("expected a polyline");
    expect(shape.style.role).toBe("danger");
    expect(shape.label).toBe("20° altitude limit");
    expect(shape.verticesRaDec[0]).toEqual(shape.verticesRaDec.at(-1));
    // A spread of azimuths, not just the ones the shape itself sampled —
    // the round trip through raDecToAltAz is the actual assertion, so
    // interpolating between vertices would only prove the sampling grid
    // agrees with itself.
    for (const [ra, dec] of shape.verticesRaDec) {
      const [alt] = raDecToAltAz(ra, dec, site);
      expect(alt).toBeCloseTo(20, 6);
    }
  });

  it("tracks whatever altitude limit it is given, not just the horizon's 0deg", () => {
    const shape = altitudeLimitCircleShape({ ...site, altitudeLimitDeg: 35 });
    if (shape.kind !== "polyline") throw new Error("expected a polyline");
    for (const [ra, dec] of shape.verticesRaDec) {
      const [alt] = raDecToAltAz(ra, dec, site);
      expect(alt).toBeCloseTo(35, 6);
    }
  });

  it("is never themed like the alt-az grid's own altitude circles — always 'danger', regardless of the grid's 'coordGrid' role", () => {
    const limit = altitudeLimitCircleShape({ ...site, altitudeLimitDeg: 20 });
    const grid = altAzGridShapes({ ...site, viewRaDeg: 0, viewDecDeg: 0, strengthenHorizon: false });
    const gridAlt20 = grid.find((s) => s.id === "skyline:altaz-grid:alt:20");
    expect(limit.style.role).toBe("danger");
    expect(gridAlt20?.style.role).toBe("coordGrid");
  });
});
