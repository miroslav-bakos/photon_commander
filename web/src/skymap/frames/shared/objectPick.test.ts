// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { Viewport } from "../../projection";
import { worldToScreen } from "../../projection";
import type { FovShape } from "../../types";
import { catalogDesignationFromShapeId, pickCatalogObjectId } from "./objectPick";

const viewport: Viewport = { view: { ra: 83.633, dec: 22.0145, fovDeg: 2 }, widthPx: 800, heightPx: 600 };

describe("catalogDesignationFromShapeId", () => {
  it("extracts the designation from a catalog shape id", () => {
    expect(catalogDesignationFromShapeId("catalog:M31:outline")).toBe("M31");
    expect(catalogDesignationFromShapeId("catalog:NGC 891:ring")).toBe("NGC 891");
  });

  it("returns null for a non-catalog id", () => {
    expect(catalogDesignationFromShapeId("primary")).toBeNull();
    expect(catalogDesignationFromShapeId("constellation:ORI:0")).toBeNull();
  });
});

describe("pickCatalogObjectId", () => {
  it("finds the designation of a polygon outline containing the click", () => {
    const shapes: FovShape[] = [
      {
        kind: "polygon",
        id: "catalog:M31:outline",
        verticesRaDec: [
          [83.6, 21.9],
          [83.7, 21.9],
          [83.7, 22.1],
          [83.6, 22.1],
        ],
        style: { role: "catalog" },
      },
    ];
    const [cx, cy] = worldToScreen(viewport, 83.65, 22.0);
    expect(pickCatalogObjectId(shapes, viewport, cx, cy)).toBe("M31");
  });

  it("returns null for a click that hits no catalogue shape", () => {
    const shapes: FovShape[] = [
      {
        kind: "polygon",
        id: "catalog:M31:outline",
        verticesRaDec: [
          [83.6, 21.9],
          [83.7, 21.9],
          [83.7, 22.1],
          [83.6, 22.1],
        ],
        style: { role: "catalog" },
      },
    ];
    expect(pickCatalogObjectId(shapes, viewport, 10, 10)).toBeNull();
  });

  it("ignores non-catalog shapes such as the primary FOV rectangle", () => {
    const [cx, cy] = worldToScreen(viewport, 83.633, 22.0145);
    const shapes: FovShape[] = [
      {
        kind: "rect",
        id: "primary",
        centerRa: 83.633,
        centerDec: 22.0145,
        widthDeg: 1,
        heightDeg: 1,
        positionAngleDeg: 0,
        style: { role: "primary" },
      },
    ];
    expect(pickCatalogObjectId(shapes, viewport, cx, cy)).toBeNull();
  });

  it("hits a circle glyph (e.g. a star or a keep-region dot) within its radius", () => {
    const shapes: FovShape[] = [
      {
        kind: "circle",
        id: "catalog:M13:core",
        centerRa: 83.633,
        centerDec: 22.0145,
        radiusDeg: 0.05,
        style: { role: "catalog" },
      },
    ];
    const [cx, cy] = worldToScreen(viewport, 83.633, 22.0145);
    expect(pickCatalogObjectId(shapes, viewport, cx, cy)).toBe("M13");
  });

  it("prefers the smaller nested outline", () => {
    const shapes: FovShape[] = [
      { kind: "polygon", id: "catalog:M31:outline", verticesRaDec: [[83.4, 21.8], [83.9, 21.8], [83.9, 22.3], [83.4, 22.3]], style: { role: "catalog" } },
      { kind: "polygon", id: "catalog:M32:outline", verticesRaDec: [[83.6, 21.95], [83.68, 21.95], [83.68, 22.08], [83.6, 22.08]], style: { role: "catalog" } },
    ];
    const [cx, cy] = worldToScreen(viewport, 83.64, 22.01);
    expect(pickCatalogObjectId(shapes, viewport, cx, cy)).toBe("M32");
  });

  it("allows clicking a catalogue label", () => {
    const shapes: FovShape[] = [{
      kind: "polygon",
      id: "catalog:M42:outline",
      verticesRaDec: [[83.6, 21.9], [83.7, 21.9], [83.7, 22.1], [83.6, 22.1]],
      style: { role: "catalog" },
      label: "M42",
      labelAt: [84.0, 22.4],
    }];
    const [lx, ly] = worldToScreen(viewport, 84.0, 22.4);
    expect(pickCatalogObjectId(shapes, viewport, lx, ly)).toBe("M42");
  });
});
