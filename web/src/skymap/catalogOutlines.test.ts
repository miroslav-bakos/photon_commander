// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { FieldObjectModel } from "../api/skymap";
import { buildCatalogOutlineShapes, catalogShapeId, ellipseWorldVertices } from "./catalogOutlines";
import { normalizeRaDeltaDeg, worldToScreen } from "./projection";

function object(overrides: Partial<FieldObjectModel> = {}): FieldObjectModel {
  return {
    designation: "NGC 891",
    common_name: null,
    ra_j2000_h: 2.3756,
    dec_j2000_deg: 42.349,
    object_type: "galaxy_spiral",
    size_major_arcmin: 12.0,
    size_minor_arcmin: 2.0,
    position_angle_deg: 22.0,
    source: "OpenNGC by Mattia Verga, CC BY-SA 4.0",
    // `FieldObjectModel.sources` (plural) is a concurrent astro-math change
    // this fixture predates; an empty default keeps every existing case's
    // intent (one `source`) unchanged.
    sources: [],
    ...overrides,
  };
}

/** The major-axis end: the first vertex by construction. */
function tip(vertices: [number, number][]): [number, number] {
  const first = vertices[0];
  if (first === undefined) throw new Error("no vertices");
  return first;
}

/** Extent of a vertex set along RA (scaled by cos dec) and along Dec, degrees. */
function extents(vertices: [number, number][], raDeg: number, decDeg: number): { alongRa: number; alongDec: number } {
  const cosDec = Math.cos((decDeg * Math.PI) / 180);
  const dRa = vertices.map(([ra]) => normalizeRaDeltaDeg(ra - raDeg) * cosDec);
  const dDec = vertices.map(([, dec]) => dec - decDeg);
  return {
    alongRa: Math.max(...dRa) - Math.min(...dRa),
    alongDec: Math.max(...dDec) - Math.min(...dDec),
  };
}

describe("ellipseWorldVertices — position angle is north through east", () => {
  it("PA 0: elongated along Dec (major axis north-south)", () => {
    const v = ellipseWorldVertices(100, 20, 1, 0.25, 0);
    const { alongRa, alongDec } = extents(v, 100, 20);
    expect(alongDec).toBeCloseTo(2, 2);
    expect(alongRa).toBeCloseTo(0.5, 2);
  });

  it("PA 90: elongated along RA (major axis east-west)", () => {
    const v = ellipseWorldVertices(100, 20, 1, 0.25, 90);
    const { alongRa, alongDec } = extents(v, 100, 20);
    expect(alongRa).toBeCloseTo(2, 2);
    expect(alongDec).toBeCloseTo(0.5, 2);
  });

  it("PA 45: the major-axis end lies north-EAST of the centre, i.e. at higher RA — through east, not west", () => {
    const [tipRa, tipDec] = tip(ellipseWorldVertices(100, 20, 1, 0.25, 45));
    expect(normalizeRaDeltaDeg(tipRa - 100)).toBeGreaterThan(0);
    expect(tipDec).toBeGreaterThan(20);
  });

  it("on screen (north up, east left — projection.ts) a PA 45 major axis runs from lower-right to upper-left", () => {
    const viewport = { view: { ra: 100, dec: 20, fovDeg: 4 }, widthPx: 800, heightPx: 800 };
    const [tipRa, tipDec] = tip(ellipseWorldVertices(100, 20, 1, 0.25, 45));
    const [x, y] = worldToScreen(viewport, tipRa, tipDec);
    expect(x).toBeLessThan(400); // east is left
    expect(y).toBeLessThan(400); // north is up
  });

  it("straddles RA 0h/24h without a jump", () => {
    const v = ellipseWorldVertices(0.05, 0, 0.5, 0.5, 30);
    for (const [ra] of v) expect(Math.abs(normalizeRaDeltaDeg(ra - 0.05))).toBeLessThan(0.6);
  });
});

describe("buildCatalogOutlineShapes", () => {
  it("draws an oriented galaxy as a solid ellipse labelled at the major-axis end when big enough", () => {
    const shapes = buildCatalogOutlineShapes([object()], { fovDeg: 2 });
    expect(shapes).toHaveLength(1);
    const [outline] = shapes;
    if (outline?.kind !== "polygon") throw new Error("expected polygon");
    expect(outline.id).toBe(catalogShapeId("NGC 891", "outline"));
    expect(outline.style).toEqual({ role: "catalog" });
    expect(outline.label).toBe("NGC 891");
    expect(outline.labelAt).toEqual(outline.verticesRaDec[0]);
  });

  it("drops the label for an object under 4% of the field of view", () => {
    // 12' = 0.2 deg; 4% of a 6 deg field is 0.24 deg.
    const shapes = buildCatalogOutlineShapes([object()], { fovDeg: 6 });
    expect(shapes[0]?.kind === "polygon" && shapes[0].label).toBeUndefined();
  });

  it("never guesses an orientation: no PA or no minor axis is a dotted circle of the major axis", () => {
    for (const missing of [{ position_angle_deg: null }, { size_minor_arcmin: null }]) {
      const [outline] = buildCatalogOutlineShapes([object(missing)], { fovDeg: 2 });
      if (outline?.kind !== "polygon") throw new Error("expected polygon");
      expect(outline.style).toEqual({ role: "catalog", dashed: false, dotted: true });
      const { alongRa, alongDec } = extents(outline.verticesRaDec, 2.3756 * 15, 42.349);
      expect(alongRa).toBeCloseTo(0.2, 2);
      expect(alongDec).toBeCloseTo(0.2, 2);
    }
  });

  it("skips an object with no size at all rather than inventing one", () => {
    expect(buildCatalogOutlineShapes([object({ size_major_arcmin: null })], { fovDeg: 2 })).toEqual([]);
  });

  it("styles by type: globular = circle with cross, planetary = ring, H II = dashed, dark nebula = dashed and unfilled", () => {
    const globular = buildCatalogOutlineShapes([object({ object_type: "globular_cluster", designation: "M13" })], { fovDeg: 2 });
    expect(globular.map((s) => s.id)).toEqual([
      catalogShapeId("M13", "outline"),
      catalogShapeId("M13", "cross-ew"),
      catalogShapeId("M13", "cross-ns"),
    ]);
    const planetary = buildCatalogOutlineShapes([object({ object_type: "planetary_nebula", designation: "M57" })], { fovDeg: 2 });
    expect(planetary.map((s) => s.id)).toEqual([catalogShapeId("M57", "outline"), catalogShapeId("M57", "ring")]);
    const hii = buildCatalogOutlineShapes([object({ object_type: "hii_region" })], { fovDeg: 2 });
    expect(hii[0]?.style).toEqual({ role: "catalog", dashed: true });
    const dark = buildCatalogOutlineShapes([object({ object_type: "dark_nebula" })], { fovDeg: 2 });
    expect(dark[0]?.style).toEqual({ role: "catalog", dashed: true, fillOpacity: 0 });
    const open = buildCatalogOutlineShapes([object({ object_type: "open_cluster" })], { fovDeg: 2 });
    expect(open[0]?.style).toEqual({ role: "catalog", dotted: true });
  });
});
