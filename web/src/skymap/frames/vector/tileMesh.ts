// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The part of a tile mesh that is genuinely scheme-agnostic: once *some*
 * tile-scheme module (`mercatorTiles.ts` for Web Mercator,
 * `hipsTiles.ts` for HiPS/HEALPix) has produced a `stride x stride` grid of
 * screen positions and the RA/Dec each vertex projects from, triangulating
 * that grid and deciding which triangles are safe to draw (far-hemisphere
 * culling) needs no knowledge of how the grid's RA/Dec values were
 * computed. Split out of `mercatorTiles.ts` (skymap-viz brief, "teach the
 * imagery layer to draw HiPS surveys") so `hipsTiles.ts` reuses this
 * unchanged instead of re-deriving it — `mercatorTiles.ts` re-exports these
 * three for backward compatibility with its own existing imports/tests.
 */
import { cosAngularDistanceFromViewCenter, type Viewport } from "../../projection";

/**
 * Below this, a point is treated as behind the viewer rather than on the
 * screen at all. Not exactly 0: a small positive margin so a tile/triangle
 * exactly grazing the 90deg circle does not flicker in and out one pixel of
 * camera motion at a time. Originally `mercatorTiles.ts`'s own fix for a
 * live defect there (`1eb2807`) but the reasoning is scheme-agnostic — any
 * tile mesh that draws a triangle instead of a small `FovShape` can reach
 * far enough from the view centre for `worldToScreen`'s far-hemisphere
 * clamp to matter (see that function's own doc comment).
 */
export const FAR_HEMISPHERE_COSC_MARGIN = 0.05;

/** Whether (`raDeg`, `decDeg`) is on the near hemisphere from `viewport`'s
 * own centre — see `FAR_HEMISPHERE_COSC_MARGIN`'s own doc comment. */
export function isNearHemispherePoint(viewport: Viewport, raDeg: number, decDeg: number): boolean {
  return cosAngularDistanceFromViewCenter(viewport, raDeg, decDeg) > FAR_HEMISPHERE_COSC_MARGIN;
}

/** One tile's tessellation grid: `(subdivisions+1) x (subdivisions+1)`
 * vertices, screen position and source RA/Dec for each, `stride =
 * subdivisions + 1`. Produced by a scheme's own mesh builder
 * (`buildTileMeshFracGrid` for Mercator, `buildHipsTileMeshGrid` for HiPS). */
export interface TileMeshGrid {
  screenX: Float64Array;
  screenY: Float64Array;
  ra: Float64Array;
  dec: Float64Array;
  stride: number;
}

/** Triangle indices for a `TileMeshGrid` — two triangles per cell, split
 * along the diagonal `imageryBatch.ts`'s UV mapping and
 * `interpolatedScreenPosition`-style interpolation both assume, so the
 * accuracy numbers describe exactly what the renderer rasterizes and not a
 * different tessellation. */
export function tileMeshTriangleIndices(subdivisions: number): Uint32Array {
  const stride = subdivisions + 1;
  const indices = new Uint32Array(subdivisions * subdivisions * 6);
  let k = 0;
  for (let j = 0; j < subdivisions; j += 1) {
    for (let i = 0; i < subdivisions; i += 1) {
      const p00 = j * stride + i;
      const p10 = j * stride + (i + 1);
      const p01 = (j + 1) * stride + i;
      const p11 = (j + 1) * stride + (i + 1);
      indices[k++] = p00;
      indices[k++] = p10;
      indices[k++] = p01;
      indices[k++] = p10;
      indices[k++] = p11;
      indices[k++] = p01;
    }
  }
  return indices;
}

/** Whether every vertex of a `TileMeshGrid` is on the near hemisphere from
 * `viewport`'s centre — the fast-path check the imagery layer uses to
 * decide whether a tile can use the shared, pre-built
 * (`tileMeshTriangleIndices`) index buffer as-is, or needs the slower
 * per-triangle filter below. True for the overwhelming majority of tiles
 * (anything not straddling the 90deg circle). */
export function everyGridVertexOnNearHemisphere(
  viewport: Viewport,
  grid: { ra: Float64Array; dec: Float64Array },
): boolean {
  for (let i = 0; i < grid.ra.length; i += 1) {
    const ra = grid.ra[i];
    const dec = grid.dec[i];
    if (ra === undefined || dec === undefined) continue;
    if (!isNearHemispherePoint(viewport, ra, dec)) return false;
  }
  return true;
}

/** `tileMeshTriangleIndices`, minus any triangle with a vertex on the far
 * hemisphere. Per-triangle, not per-tile, so a tile straddling the 90deg
 * circle still draws its real, near-hemisphere part instead of being
 * dropped whole or drawn whole with a clamped, meaningless wedge stretched
 * across the canvas. Only called for a tile that
 * `everyGridVertexOnNearHemisphere` already found `false` for. */
export function filteredTileMeshTriangleIndices(
  viewport: Viewport,
  grid: { ra: Float64Array; dec: Float64Array },
  subdivisions: number,
): Uint16Array {
  const stride = subdivisions + 1;
  const near = new Uint8Array(stride * stride);
  for (let i = 0; i < near.length; i += 1) {
    const ra = grid.ra[i];
    const dec = grid.dec[i];
    near[i] = ra !== undefined && dec !== undefined && isNearHemispherePoint(viewport, ra, dec) ? 1 : 0;
  }
  const indices: number[] = [];
  for (let j = 0; j < subdivisions; j += 1) {
    for (let i = 0; i < subdivisions; i += 1) {
      const p00 = j * stride + i;
      const p10 = j * stride + (i + 1);
      const p01 = (j + 1) * stride + i;
      const p11 = (j + 1) * stride + (i + 1);
      if (near[p00] === 1 && near[p10] === 1 && near[p01] === 1) indices.push(p00, p10, p01);
      if (near[p10] === 1 && near[p11] === 1 && near[p01] === 1) indices.push(p10, p11, p01);
    }
  }
  return new Uint16Array(indices);
}
