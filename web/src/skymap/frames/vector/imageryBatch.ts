// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pure batch assembly for the vector frame's imagery layer: given the tiles
 * currently resident in an atlas and the current `Viewport`, build the
 * vertex/UV/index arrays one GL draw call needs to paint all of them at
 * once — the batching fix the skymap-viz brief asked for (the imagery
 * spike issued one draw call per tile, measured ~85/frame; this issues one
 * per `maxTilesPerBatch` tiles, ordinarily one total).
 *
 * Scheme-agnostic by construction (skymap-viz brief, "teach the imagery
 * layer to draw HiPS surveys"): this module never builds a tile's own mesh
 * itself, it is handed a `buildGrid` function (`mercatorTiles.ts`'s
 * `buildTileMeshFracGrid` for a Mercator tile, `hipsTiles.ts`'s
 * `buildHipsTileMeshGrid` for a HiPS one) and only assembles whatever
 * `TileMeshGrid` that produces — the same hemisphere culling
 * (`everyGridVertexOnNearHemisphere`/`filteredTileMeshTriangleIndices`,
 * `tileMesh.ts`) and atlas UV mapping (`tileAtlas.ts`'s `atlasSlotUvRect`)
 * apply either way, since both are already generic over the grid's own
 * screen/RA/Dec arrays rather than over how those arrays were computed.
 * Kept GL-free (`Float32Array`/`Uint16Array` in, out) so the assembly logic
 * — vertex/index offsetting, batch splitting, hemisphere filtering, atlas
 * UV mapping — is unit-tested without a browser (`imageryBatch.test.ts`).
 */
import type { Viewport } from "../../projection";
import { everyGridVertexOnNearHemisphere, filteredTileMeshTriangleIndices, tileMeshTriangleIndices, type TileMeshGrid } from "./tileMesh";
import { atlasSlotUvRect, type AtlasSlot } from "./tileAtlas";

export interface ResidentTile<T> {
  key: string;
  tile: T;
  slot: AtlasSlot;
}

export interface ImageryBatch {
  /** `x,y` screen-pixel pairs. */
  positions: Float32Array;
  /** `u,v` atlas-texture-coordinate pairs, one per position. */
  uvs: Float32Array;
  indices: Uint16Array;
  tileCount: number;
}

/**
 * How many tiles' vertices fit in one `Uint16Array` index buffer —
 * `floor(65536 / verticesPerTile)`. Not a tuning knob so much as a safety
 * cap: an ordinary viewport (`pickZoom`'s `targetTilesAcross`) never gets
 * close to this, so in practice `buildImageryBatches` returns exactly one
 * batch; this only ever produces more than one for an unusually coarse
 * zoom/very wide FOV, trading "one draw call" for "a small, bounded number
 * of them" rather than overflowing a 16-bit index.
 */
export function maxTilesPerBatch(subdivisions: number): number {
  const verticesPerTile = (subdivisions + 1) * (subdivisions + 1);
  return Math.max(1, Math.floor(65536 / verticesPerTile));
}

export function buildImageryBatches<T>(
  viewport: Viewport,
  residentTiles: readonly ResidentTile<T>[],
  subdivisions: number,
  slotsPerRow: number,
  buildGrid: (viewport: Viewport, tile: T, subdivisions: number) => TileMeshGrid,
): ImageryBatch[] {
  const stride = subdivisions + 1;
  const perTileVertexCount = stride * stride;
  const cap = maxTilesPerBatch(subdivisions);
  const batches: ImageryBatch[] = [];

  for (let start = 0; start < residentTiles.length; start += cap) {
    const chunk = residentTiles.slice(start, start + cap);
    const positions = new Float32Array(chunk.length * perTileVertexCount * 2);
    const uvs = new Float32Array(chunk.length * perTileVertexCount * 2);
    const indices: number[] = [];
    let vertexBase = 0;

    for (const resident of chunk) {
      const grid = buildGrid(viewport, resident.tile, subdivisions);
      const rect = atlasSlotUvRect(resident.slot, slotsPerRow);
      for (let idx = 0; idx < perTileVertexCount; idx += 1) {
        const i = idx % stride;
        const j = Math.floor(idx / stride);
        // Column 0 is the tile image's first column, row 0 its first row.
        // Both map straight through into the slot's own inset rectangle.
        //
        // `v` used to be `1 - j / subdivisions`, on the reasoning that image
        // row 0 is the tile's north edge so `v` needed flipping. That would
        // hold only if the upload flipped rows -- but `imageryLayer.ts`
        // never sets `UNPACK_FLIP_Y_WEBGL` and the fragment shader passes
        // `vUV` through untouched, so WebGL's default unpack already puts
        // image row 0 at `v = 0`. The flip was applied a second time.
        //
        // Every tile was therefore drawn vertically mirrored *inside a
        // correctly placed mesh*, which is why it survived so long: tile
        // selection, projection accuracy, coverage and orientation were all
        // measured and all correct. Placement tests cannot see it. It read
        // as hard-edged bands of mismatched brightness, reported over days
        // as "tiles rows shifted", "wrong direction nor shape" and "whole
        // rows are sideways". Confirmed by rendering M42 headlessly before
        // and after: bands present, then gone (`scripts/skymap-render.mjs`).
        const localU = i / subdivisions;
        const localV = j / subdivisions;
        const vi = (vertexBase + idx) * 2;
        positions[vi] = grid.screenX[idx] ?? 0;
        positions[vi + 1] = grid.screenY[idx] ?? 0;
        uvs[vi] = rect.u0 + localU * (rect.u1 - rect.u0);
        uvs[vi + 1] = rect.v0 + localV * (rect.v1 - rect.v0);
      }
      // Fast path (the overwhelming majority of tiles): every vertex is on
      // the near hemisphere, so the tile's own full, static triangle set
      // applies unchanged. Slow path: a tile straddling the 90deg circle
      // needs its far-hemisphere triangles dropped this frame — see
      // `mercatorTiles.ts`'s own doc comment on why this must be
      // per-triangle, not per-tile.
      const localIndices = everyGridVertexOnNearHemisphere(viewport, grid)
        ? tileMeshTriangleIndices(subdivisions)
        : filteredTileMeshTriangleIndices(viewport, grid, subdivisions);
      for (const local of localIndices) indices.push(local + vertexBase);
      vertexBase += perTileVertexCount;
    }
    batches.push({ positions, uvs, indices: Uint16Array.from(indices), tileCount: chunk.length });
  }
  return batches;
}
