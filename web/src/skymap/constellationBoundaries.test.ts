// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { ConstellationBoundaryRegionModel } from "../api/skymap";
import {
  buildConstellationBoundaryShapes,
  constellationBoundaryShapeId,
  CONSTELLATION_LABEL_MIN_FOV_FRACTION,
  constellationRegionGeometry,
} from "./constellationBoundaries";
import { GRID_LABEL_MAX_FOV_DEG } from "./frames/shared/skyLines";
import type { FovShape } from "./types";

/** Every shape this module builds is a `"polygon"` — narrows the `FovShape`
 * union so `.label`/`.labelAt` are reachable, the same pattern
 * `catalogOutlines.test.ts` uses for its own outline shapes. */
function asPolygon(shape: FovShape | undefined): Extract<FovShape, { kind: "polygon" }> {
  if (shape?.kind !== "polygon") throw new Error("expected a polygon shape");
  return shape;
}

function square(abbreviation: string, name: string, raDeg: number, decDeg: number, halfSizeDeg: number): ConstellationBoundaryRegionModel {
  return {
    abbreviation,
    name,
    rings: [
      [
        { ra_j2000_deg: raDeg - halfSizeDeg, dec_j2000_deg: decDeg - halfSizeDeg },
        { ra_j2000_deg: raDeg + halfSizeDeg, dec_j2000_deg: decDeg - halfSizeDeg },
        { ra_j2000_deg: raDeg + halfSizeDeg, dec_j2000_deg: decDeg + halfSizeDeg },
        { ra_j2000_deg: raDeg - halfSizeDeg, dec_j2000_deg: decDeg + halfSizeDeg },
      ],
    ],
  };
}

describe("constellationRegionGeometry", () => {
  it("computes a centroid at the square's own centre", () => {
    // dec = 0 so the RA-vs-Dec `cos(dec)` foreshortening in `extentDeg` is
    // exactly 1, and the diagonal distance is a plain Euclidean check.
    const [geometry] = constellationRegionGeometry([square("ORI", "Orion", 85, 0, 10)]);
    expect(geometry).toBeDefined();
    expect(geometry?.centroidRaDeg).toBeCloseTo(85, 5);
    expect(geometry?.centroidDecDeg).toBeCloseTo(0, 5);
    // The farthest vertex is at the diagonal, sqrt(2) * halfSize away; extent
    // is twice that.
    expect(geometry?.extentDeg).toBeCloseTo(2 * Math.SQRT2 * 10, 3);
  });

  it("uses a circular mean for RA, correct for a region straddling 0h/24h", () => {
    // A square centred on RA 0 straddles the wrap: vertices at RA 355 and 5.
    const region = square("PSC", "Pisces", 0, 10, 5);
    const wrapped: ConstellationBoundaryRegionModel = {
      ...region,
      rings: [region.rings[0]?.map((v) => ({ ...v, ra_j2000_deg: (v.ra_j2000_deg + 360) % 360 })) ?? []],
    };
    const [geometry] = constellationRegionGeometry([wrapped]);
    expect(geometry).toBeDefined();
    // A plain arithmetic mean of {355, 5, 5, 355} would give 180 — the
    // wrong side of the sky. The circular mean must land near 0/360.
    const centroid = geometry?.centroidRaDeg ?? Number.NaN;
    const distanceFromZero = Math.min(centroid, 360 - centroid);
    expect(distanceFromZero).toBeLessThan(1);
  });

  it("preserves every ring's vertices verbatim, in order", () => {
    const region = square("CRU", "Crux", 190, -60, 3);
    const [geometry] = constellationRegionGeometry([region]);
    expect(geometry?.rings).toEqual([
      region.rings[0]?.map((v): [number, number] => [v.ra_j2000_deg, v.dec_j2000_deg]),
    ]);
  });
});

describe("buildConstellationBoundaryShapes", () => {
  const regions = [square("ORI", "Orion", 85, 5, 15), square("LYR", "Lyra", 285, 38, 3)];
  const geometry = constellationRegionGeometry(regions);

  it("draws one unfilled polygon per ring, the 'constellation' role, no fillOpacity", () => {
    const shapes = buildConstellationBoundaryShapes(geometry, { fovDeg: null, viewRaDeg: 0, viewDecDeg: 0 });
    expect(shapes).toHaveLength(2);
    for (const shape of shapes) {
      expect(shape.kind).toBe("polygon");
      expect(shape.style.role).toBe("constellation");
      expect(shape.style.fillOpacity).toBeUndefined();
    }
    expect(shapes.map((s) => s.id)).toEqual([constellationBoundaryShapeId("ORI", 0), constellationBoundaryShapeId("LYR", 0)]);
  });

  it("shows no labels when the field of view is unknown (null)", () => {
    const shapes = buildConstellationBoundaryShapes(geometry, { fovDeg: null, viewRaDeg: 85, viewDecDeg: 5 });
    expect(shapes.every((s) => asPolygon(s).label === undefined)).toBe(true);
  });

  it("shows no labels once the field of view is wider than the shared grid-label ceiling", () => {
    const shapes = buildConstellationBoundaryShapes(geometry, {
      fovDeg: GRID_LABEL_MAX_FOV_DEG + 0.01,
      viewRaDeg: 85,
      viewDecDeg: 5,
    });
    expect(shapes.every((s) => asPolygon(s).label === undefined)).toBe(true);
  });

  it("labels a region only once the view is zoomed in enough that it is 'large enough on screen'", () => {
    // A tiny fixture region (extent well under a degree) so both sides of
    // `CONSTELLATION_LABEL_MIN_FOV_FRACTION` are reachable within the grid's own FoV
    // ceiling — a real constellation is always many degrees across, so this
    // exercises the same rule catalogue outlines use for a small object.
    const tiny = constellationRegionGeometry([square("TNY", "Tiny", 85, 5, 0.05)]);
    const tinyExtent = tiny[0]?.extentDeg ?? 0;

    const tooWideForItsSize = tinyExtent / CONSTELLATION_LABEL_MIN_FOV_FRACTION + 1;
    const wideShapes = buildConstellationBoundaryShapes(tiny, { fovDeg: tooWideForItsSize, viewRaDeg: 85, viewDecDeg: 5 });
    expect(asPolygon(wideShapes[0]).label).toBeUndefined();

    const narrowEnough = tinyExtent / CONSTELLATION_LABEL_MIN_FOV_FRACTION - 1;
    const narrowShapes = buildConstellationBoundaryShapes(tiny, { fovDeg: narrowEnough, viewRaDeg: 85, viewDecDeg: 5 });
    const narrowShape = asPolygon(narrowShapes[0]);
    // The full name, not the "TNY" abbreviation — the fixture deliberately
    // gives the two different values, so this asserts which one is drawn.
    expect(narrowShape.label).toBe("Tiny");
    expect(narrowShape.labelAt).toEqual([tiny[0]?.centroidRaDeg, tiny[0]?.centroidDecDeg]);
  });

  it("caps labels to the nearest-to-view N, like the coordinate grid's own rule, rather than labelling every qualifying region", () => {
    // 20 small, tightly-packed, equally-sized regions — every one qualifies
    // as "large enough on screen" at this FoV — spread out in Dec so
    // "nearest the view" has a real ordering. Only the nearest few
    // (`MAX_CONSTELLATION_LABELS`, 5) should end up labelled.
    const manyRegions = Array.from({ length: 20 }, (_, i) => square(`R${String(i)}`, `Region ${String(i)}`, 100, i * 4, 1));
    const manyGeometry = constellationRegionGeometry(manyRegions);
    const shapes = buildConstellationBoundaryShapes(manyGeometry, { fovDeg: 5, viewRaDeg: 100, viewDecDeg: 0 });
    const labels = shapes.map((s) => asPolygon(s).label);
    const labelled = labels.filter((label) => label !== undefined);
    expect(labelled.length).toBeLessThanOrEqual(5);
    expect(labelled.length).toBeGreaterThan(0);
    // The nearest region to the view (R0, at Dec 0) must be among them —
    // labelled by full name, not abbreviation.
    expect(labelled).toContain("Region 0");
    // The farthest region (R19, at Dec 76) must not be.
    expect(labelled).not.toContain("Region 19");
  });

  it("labels Serpens' first ring only, never its second (disjoint Caput/Cauda) ring", () => {
    const serpens: ConstellationBoundaryRegionModel = {
      abbreviation: "SER",
      name: "Serpens",
      rings: [square("SER", "Serpens", 240, 5, 2).rings[0] ?? [], square("SER", "Serpens", 260, 0, 2).rings[0] ?? []],
    };
    const serpensGeometry = constellationRegionGeometry([serpens]);
    const shapes = buildConstellationBoundaryShapes(serpensGeometry, { fovDeg: 5, viewRaDeg: 250, viewDecDeg: 2.5 });
    expect(shapes).toHaveLength(2);
    const [first, second] = shapes;
    expect(first?.id).toBe(constellationBoundaryShapeId("SER", 0));
    expect(second?.id).toBe(constellationBoundaryShapeId("SER", 1));
    expect(asPolygon(second).label).toBeUndefined();
  });
});
