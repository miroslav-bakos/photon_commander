// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { rectWorldCorners, worldToScreen, type Viewport } from "../../projection";
import type { FovShape } from "../../types";
import { isNearHemispherePoint, type TileMeshGrid } from "./tileMesh";

interface Bounds { left: number; right: number; top: number; bottom: number }
function bounds(points: readonly (readonly [number, number])[]): Bounds | undefined {
  if (points.length === 0) return undefined;
  return { left: Math.min(...points.map(([x]) => x)), right: Math.max(...points.map(([x]) => x)),
    top: Math.min(...points.map(([, y]) => y)), bottom: Math.max(...points.map(([, y]) => y)) };
}
function gap(a: Bounds, b: Bounds): number {
  return Math.hypot(Math.max(0, a.left - b.right, b.left - a.right), Math.max(0, a.top - b.bottom, b.top - a.bottom));
}
function clamp(value: number, limit: number): number { return Math.max(0, Math.min(limit, value)); }

/** Screen-space priority using the same projected mesh and FOV as drawing.
 * All tiles intersecting the primary FOV precede surrounding tiles. Bounds
 * are conservative, including when either footprint contains the other. */
export function imageryTilePriority(viewport: Viewport, grid: TileMeshGrid, focus: FovShape | undefined): number {
  const points: [number, number][] = [];
  for (let index = 0; index < grid.screenX.length; index += 1) {
    const x = grid.screenX[index]; const y = grid.screenY[index];
    const ra = grid.ra[index]; const dec = grid.dec[index];
    if (x !== undefined && y !== undefined && ra !== undefined && dec !== undefined && Number.isFinite(x) && Number.isFinite(y) && isNearHemispherePoint(viewport, ra, dec)) points.push([x, y]);
  }
  const tile = bounds(points);
  if (tile === undefined) return Number.MAX_VALUE;
  const center: Bounds = { left: viewport.widthPx / 2, right: viewport.widthPx / 2, top: viewport.heightPx / 2, bottom: viewport.heightPx / 2 };
  let target = center;
  let hasFocus = false;
  if (focus?.kind === "rect" && focus.style.role === "primary") {
    const world = [...rectWorldCorners(focus.centerRa, focus.centerDec, focus.widthDeg, focus.heightDeg, focus.positionAngleDeg), [focus.centerRa, focus.centerDec] as [number, number]];
    const projected = world.filter(([ra, dec]) => isNearHemispherePoint(viewport, ra, dec)).map(([ra, dec]) => worldToScreen(viewport, ra, dec)).filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
    const box = bounds(projected);
    if (box !== undefined) {
      // Offscreen targets still guide requests toward their nearest viewport edge.
      target = { left: clamp(box.left, viewport.widthPx), right: clamp(box.right, viewport.widthPx), top: clamp(box.top, viewport.heightPx), bottom: clamp(box.bottom, viewport.heightPx) };
      hasFocus = true;
    }
  }
  const distance = gap(tile, target);
  const scale = Math.max(1, Math.hypot(viewport.widthPx, viewport.heightPx));
  if (hasFocus && distance === 0) {
    const centerDistance = Math.hypot((tile.left + tile.right - target.left - target.right) / 2, (tile.top + tile.bottom - target.top - target.bottom) / 2);
    return centerDistance / (scale + centerDistance);
  }
  return (hasFocus ? 1 : 0) + distance / (scale + distance);
}
