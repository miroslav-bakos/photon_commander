// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { describe, expect, it } from "vitest";
import { screenToWorld, type Viewport } from "../../projection";
import type { FovShape } from "../../types";
import { createHorizontalCamera, horizontalCameraViewport } from "../shared/horizontalCamera";
import type { TileMeshGrid } from "./tileMesh";
import { imageryTilePriority } from "./imageryPriority";

const viewport: Viewport = { view: { ra: 359.9, dec: 45, fovDeg: 10 }, widthPx: 800, heightPx: 600 };
function grid(v: Viewport, left: number, top: number, right: number, bottom: number): TileMeshGrid {
  const points = [[left, top], [right, top], [left, bottom], [right, bottom]] as const;
  const sky = points.map(([x, y]) => screenToWorld(v, x, y));
  return { screenX: Float64Array.from(points.map(([x]) => x)), screenY: Float64Array.from(points.map(([, y]) => y)), ra: Float64Array.from(sky.map(([ra]) => ra)), dec: Float64Array.from(sky.map(([, dec]) => dec)), stride: 2 };
}
function focus(v: Viewport, angle = 0): FovShape {
  const [centerRa, centerDec] = screenToWorld(v, 400, 300);
  return { kind: "rect", id: "primary", centerRa, centerDec, widthDeg: 2, heightDeg: 1, positionAngleDeg: angle, style: { role: "primary" } };
}

describe("imagery tile priorities", () => {
  it("requests FOV intersections before nearby tiles and nearer surroundings before distant ones", () => {
    const fov = focus(viewport);
    const inside = imageryTilePriority(viewport, grid(viewport, 390, 290, 410, 310), fov);
    const enclosing = imageryTilePriority(viewport, grid(viewport, 200, 200, 600, 400), fov);
    const near = imageryTilePriority(viewport, grid(viewport, 500, 290, 520, 310), fov);
    const far = imageryTilePriority(viewport, grid(viewport, 700, 290, 720, 310), fov);
    expect(inside).toBeLessThan(1); expect(enclosing).toBeLessThan(1);
    expect(near).toBeGreaterThanOrEqual(1); expect(near).toBeLessThan(far);
  });

  it.each([0, 45, 90, 175])("handles a rotated rectangle crossing RA zero (%s degrees)", (angle) => {
    expect(imageryTilePriority(viewport, grid(viewport, 395, 295, 405, 305), focus(viewport, angle))).toBeLessThan(1);
  });

  it("uses the real camera basis at the zenith with accumulated roll", () => {
    const site = { latitudeDeg: 48, longitudeDeg: 17, instant: new Date("2026-10-14T19:00:00Z") };
    const v = horizontalCameraViewport(viewport.view, 800, 600, site, createHorizontalCamera(90, 137));
    expect(imageryTilePriority(v, grid(v, 395, 295, 405, 305), focus(v, 73))).toBeLessThan(1);
    expect(imageryTilePriority(v, grid(v, 700, 295, 710, 305), focus(v, 73))).toBeGreaterThanOrEqual(1);
  });

  it("defaults to viewport center and ignores far hemisphere samples", () => {
    expect(imageryTilePriority(viewport, grid(viewport, 390, 290, 410, 310), undefined)).toBe(0);
    expect(imageryTilePriority(viewport, grid(viewport, 500, 290, 510, 310), undefined)).toBeLessThan(imageryTilePriority(viewport, grid(viewport, 700, 290, 710, 310), undefined));
    const far = grid(viewport, 390, 290, 410, 310); far.ra.fill(179.9); far.dec.fill(-45);
    expect(imageryTilePriority(viewport, far, focus(viewport))).toBe(Number.MAX_VALUE);
  });

  it("remains finite for tiny FOV and prioritizes the nearest edge for offscreen targets", () => {
    const tiny = { ...focus(viewport), widthDeg: 1e-12, heightDeg: 1e-12 };
    expect(Number.isFinite(imageryTilePriority(viewport, grid(viewport, 390, 290, 410, 310), tiny))).toBe(true);
    const [centerRa, centerDec] = screenToWorld(viewport, 1000, 300);
    const offscreen = { ...focus(viewport), centerRa, centerDec };
    expect(imageryTilePriority(viewport, grid(viewport, 750, 290, 790, 310), offscreen)).toBeLessThan(imageryTilePriority(viewport, grid(viewport, 10, 290, 50, 310), offscreen));
  });
});
