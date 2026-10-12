// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * A `CutoutLayerPiece`'s own tessellation grid: the cutout counterpart of
 * `mercatorTiles.ts`'s `buildTileMeshFracGrid`/`hipsTiles.ts`'s
 * `buildHipsTileMeshGrid`. Same shape (`TileMeshGrid`), same downstream
 * triangulation and far-hemisphere culling (`tileMesh.ts`, shared
 * unchanged) — a "rectangle" on the sky is not a rectangle on screen for a
 * cutout piece any more than it is for a tile, so this needs the same
 * `subdivisions`-vertex mesh rather than four corners and a screen-space
 * quad.
 *
 * Deliberately *not* built on `imageryLayer.ts`'s atlas/batch machinery
 * (`tileAtlas.ts`, `imageryBatch.ts`): those exist to pack many same-size
 * (`ATLAS_TILE_PX`) tiles sharing one texture and one draw call, LRU-evicted
 * as a bounded cache of a much larger tile pyramid. A cutout set is a
 * handful of host-selected, arbitrarily-sized pieces (`CutoutLayerPiece`'s
 * own `widthPx`/`heightPx`) reconciled by id every `setCutoutLayers` call,
 * never fetched again once loaded and never evicted while still selected —
 * packing them into a shared, resampled-to-256px atlas would both cost
 * real resolution (a cutout is what the owner frames against) and buy
 * nothing an eviction policy is for. Each piece keeps its own
 * natural-resolution texture and draws with its own `gl.drawElements` call
 * instead; at the piece counts this frame ever shows (a handful of
 * sources, once per resolved target) that stays well inside the 16ms
 * scrubber budget without needing to be batched.
 *
 * What *is* shared, not duplicated: the WCS pixel -> sky math
 * (`../shared/cutoutWcs.ts`'s `wcsPixelToWorld`), this viewport's own
 * projection (`worldToScreen`), and the mesh triangulation/culling
 * (`tileMesh.ts`) — the same three seams every other imagery source in
 * this frame goes through.
 */
import { worldToScreen, type Viewport } from "../../projection";
import type { CutoutLayerPiece } from "../../types";
import { wcsPixelToWorld } from "../shared/cutoutWcs";
import {
  everyGridVertexOnNearHemisphere,
  filteredTileMeshTriangleIndices,
  tileMeshTriangleIndices,
  type TileMeshGrid,
} from "./tileMesh";

/** `piece`'s own `(subdivisions+1) x (subdivisions+1)` tessellation grid,
 * in this viewport's screen space. Indexed the same corner-space pixel
 * convention as `wcsPixelToWorld`/`piece.widthPx`/`heightPx`: `i` runs
 * `0..widthPx` (column), `j` runs `0..heightPx` (row), `j = 0` at the
 * piece's own pixel row 0 — the decoded image's first scanline
 * (`cutoutWcs.ts`'s own doc comment on why no row flip is ever applied
 * between the two). */
export function buildCutoutMeshGrid(viewport: Viewport, piece: CutoutLayerPiece, subdivisions: number): TileMeshGrid {
  const stride = subdivisions + 1;
  const screenX = new Float64Array(stride * stride);
  const screenY = new Float64Array(stride * stride);
  const ra = new Float64Array(stride * stride);
  const dec = new Float64Array(stride * stride);
  for (let j = 0; j < stride; j += 1) {
    const pxRow = (j / subdivisions) * piece.heightPx;
    for (let i = 0; i < stride; i += 1) {
      const pxCol = (i / subdivisions) * piece.widthPx;
      const [r, d] = wcsPixelToWorld(piece.wcs, pxCol, pxRow);
      const [sx, sy] = worldToScreen(viewport, r, d);
      const idx = j * stride + i;
      screenX[idx] = sx;
      screenY[idx] = sy;
      ra[idx] = r;
      dec[idx] = d;
    }
  }
  return { screenX, screenY, ra, dec, stride };
}

/** `(u, v)` texture coordinates for grid vertex `(i, j)` of a `stride x
 * stride` cutout mesh — the whole `[0,1]` square of the piece's own
 * texture, never an atlas slot rect (see this module's own doc comment on
 * why cutouts skip the atlas). `u` runs directly with pixel column (no
 * texture upload here ever mirrors columns). `v` is flipped
 * (`1 - j/subdivisions`) to match this frame's own texture-row convention
 * for an unflipped image upload (`imageryLayer.ts`'s `TileSource`, which
 * cutout textures share the same upload path as: `texImage2D`/
 * `texSubImage2D` with `UNPACK_FLIP_Y_WEBGL` at its default `false`) — the
 * same convention `mercatorTiles.ts`'s/`hipsTiles.ts`'s own tile meshes
 * use for "row 0 of the fetched image", verified against those already
 * correctly-rendering layers rather than re-derived from the WebGL spec
 * alone. */
export function cutoutMeshUv(i: number, j: number, subdivisions: number): [u: number, v: number] {
  return [i / subdivisions, 1 - j / subdivisions];
}

/** One piece's whole GL-ready draw payload — vertex positions, UVs and
 * triangle indices — kept GL-free (`Float32Array`/`Uint16Array` in, out)
 * for the same reason `imageryBatch.ts` is: the assembly logic is
 * unit-tested without a browser (`cutoutMesh.test.ts`), and
 * `imageryLayer.ts`'s draw path only ever uploads and draws whatever this
 * returns, never reaches back into the mesh/culling maths itself. Always
 * exactly one mesh per piece (no atlas slot, no multi-piece batching — see
 * this module's own doc comment on why); a piece straddling the
 * far-hemisphere boundary still draws its real, near-hemisphere part via
 * the same per-triangle filter every other imagery source uses. */
export interface CutoutMesh {
  positions: Float32Array;
  uvs: Float32Array;
  indices: Uint16Array;
}

export function buildCutoutMesh(viewport: Viewport, piece: CutoutLayerPiece, subdivisions: number): CutoutMesh {
  const grid = buildCutoutMeshGrid(viewport, piece, subdivisions);
  const stride = subdivisions + 1;
  const vertexCount = stride * stride;
  const positions = new Float32Array(vertexCount * 2);
  const uvs = new Float32Array(vertexCount * 2);
  for (let j = 0; j < stride; j += 1) {
    for (let i = 0; i < stride; i += 1) {
      const idx = j * stride + i;
      positions[idx * 2] = grid.screenX[idx] ?? 0;
      positions[idx * 2 + 1] = grid.screenY[idx] ?? 0;
      const [u, v] = cutoutMeshUv(i, j, subdivisions);
      uvs[idx * 2] = u;
      uvs[idx * 2 + 1] = v;
    }
  }
  const localIndices = everyGridVertexOnNearHemisphere(viewport, grid)
    ? tileMeshTriangleIndices(subdivisions)
    : filteredTileMeshTriangleIndices(viewport, grid, subdivisions);
  return { positions, uvs, indices: Uint16Array.from(localIndices) };
}
