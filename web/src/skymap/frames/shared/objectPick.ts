// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Which catalogue object (if any) a screen-space click landed on. Pure
 * geometry, no DOM/canvas/engine import — the vector frame (`frames/vector/main.ts`)
 * is the first caller, but nothing here is vector-specific: any backend that
 * wants "click a DSO glyph" can reuse it the same way `fovDrag.ts`'s hit
 * testing is shared today.
 *
 * `catalogOutlines.ts`'s own id convention (`catalogShapeId`,
 * `catalog:<designation>:<part>`) is the only thing this file knows about
 * "what a catalogue shape is" — it never imports `catalogOutlines.ts` itself,
 * to stay a one-directional dependency (shape-builders don't need to know
 * how they get picked).
 */
import { worldToScreen, type Viewport } from "../../projection";
import type { FovShape } from "../../types";

const CATALOG_ID_PREFIX = "catalog:";

/** The designation a catalogue shape id encodes (`catalog:M31:outline` ->
 * `"M31"`), or `null` for a shape that is not a catalogue outline at all. */
export function catalogDesignationFromShapeId(id: string): string | null {
  if (!id.startsWith(CATALOG_ID_PREFIX)) return null;
  const rest = id.slice(CATALOG_ID_PREFIX.length);
  const sep = rest.lastIndexOf(":");
  if (sep <= 0) return null;
  return rest.slice(0, sep);
}

/** Extra screen-space margin around a shape's own stroke that still counts
 * as a hit — the same reasoning `fovDrag.ts`'s `FOV_DRAG_TOUCH_PAD_PX` gives
 * for dragging: a shape's stroke is a couple of pixels wide, nowhere near a
 * reliable click/touch target. */
export const OBJECT_CLICK_PAD_PX = 10;

function screenPolygon(vertices: readonly [ra: number, dec: number][], viewport: Viewport): [number, number][] {
  return vertices.map(([ra, dec]) => worldToScreen(viewport, ra, dec));
}

function pointInPolygon(px: number, py: number, poly: readonly [number, number][]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i];
    const b = poly[j];
    if (a === undefined || b === undefined) continue;
    const [xi, yi] = a;
    const [xj, yj] = b;
    const crosses = yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi;
    if (crosses) inside = !inside;
  }
  return inside;
}

function distanceToPoint(px: number, py: number, x: number, y: number): number {
  return Math.hypot(px - x, py - y);
}

/**
 * The designation of the catalogue object whose outline/glyph contains (or
 * is within `OBJECT_CLICK_PAD_PX` of) screen position (`px`, `py`) — `null`
 * when the click hit no catalogue shape. When several designations overlap,
 * the smallest containing outline wins, and labels are clickable too.
 */
export function pickCatalogObjectId(
  shapes: readonly FovShape[],
  viewport: Viewport,
  px: number,
  py: number,
): string | null {
  let best: { designation: string; score: number } | null = null;
  for (const shape of shapes) {
    const designation = catalogDesignationFromShapeId(shape.id);
    if (designation === null) continue;
    if (shape.kind === "polygon") {
      const poly = screenPolygon(shape.verticesRaDec, viewport);
      if (poly.length < 3) continue;
      const area = Math.abs(poly.reduce((sum, [x, y], i) => {
        const [nx, ny] = poly[(i + 1) % poly.length] ?? [x, y];
        return sum + x * ny - nx * y;
      }, 0)) / 2;
      if (pointInPolygon(px, py, poly)) best = choose(best, designation, area);
      const hit = poly.some(([x, y], i) => {
        const [nx, ny] = poly[(i + 1) % poly.length] ?? [x, y];
        return segmentDistance(px, py, x, y, nx, ny) <= OBJECT_CLICK_PAD_PX;
      });
      if (hit) best = choose(best, designation, area + 1_000_000);
    } else if (shape.kind === "circle") {
      const [cx, cy] = worldToScreen(viewport, shape.centerRa, shape.centerDec);
      // Screen-space radius: the pixel distance to a point one radius north
      // of centre. Fine for the small dots/rings catalogue glyphs actually
      // use (a few arcminutes to a couple of degrees) — not meant for a
      // sky-spanning circle, which this frame never draws via `"circle"`.
      const [, edgeY] = worldToScreen(viewport, shape.centerRa, shape.centerDec + shape.radiusDeg);
      const radiusPx = Math.abs(cy - edgeY);
      if (distanceToPoint(px, py, cx, cy) <= radiusPx + OBJECT_CLICK_PAD_PX) {
        best = choose(best, designation, Math.PI * radiusPx * radiusPx + 1_000_000);
      }
    }
    if ("labelAt" in shape && shape.labelAt !== undefined && "label" in shape && shape.label !== undefined) {
      const [lx, ly] = worldToScreen(viewport, shape.labelAt[0], shape.labelAt[1]);
      const halfWidth = Math.max(10, shape.label.length * 4.5);
      if (Math.abs(px - lx) <= halfWidth && Math.abs(py - ly) <= 9) best = choose(best, designation, -1);
    }
  }
  return best?.designation ?? null;
}

function choose(current: { designation: string; score: number } | null, designation: string, score: number) {
  return current === null || score < current.score ? { designation, score } : current;
}

function segmentDistance(px: number, py: number, x1: number, y1: number, x2: number, y2: number): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  const t = lenSq === 0 ? 0 : Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lenSq));
  return distanceToPoint(px, py, x1 + t * dx, y1 + t * dy);
}
