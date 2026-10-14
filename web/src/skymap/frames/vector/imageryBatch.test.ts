// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The batching arithmetic itself — vertex/index offsetting, atlas UV
 * mapping, and the split into multiple draw calls once a batch would
 * overflow a 16-bit index buffer. Placement accuracy is `mercatorTiles.
 * test.ts`'s job (this module reuses that math unchanged); what is new
 * here, and needs its own coverage, is turning N tiles into one shared
 * buffer set correctly.
 */
import { describe, expect, it } from "vitest";
import type { Viewport } from "../../projection";
import { buildImageryBatches, maxTilesPerBatch, type ResidentTile } from "./imageryBatch";
import { buildHipsTileMeshGrid, type HipsTileKey } from "./hipsTiles";
import { buildTileMeshFracGrid, type TileKey } from "./mercatorTiles";
import { DEFAULT_ATLAS_SLOTS_PER_ROW, TileAtlasAllocator } from "./tileAtlas";

const VIEWPORT: Viewport = { view: { ra: 180, dec: 20, fovDeg: 30 }, widthPx: 1200, heightPx: 800 };
const SUBDIVISIONS = 8;

function residentTilesFor(zoom: number, count: number): ResidentTile<TileKey>[] {
  const atlas = new TileAtlasAllocator(DEFAULT_ATLAS_SLOTS_PER_ROW);
  const tiles: ResidentTile<TileKey>[] = [];
  // Tiles near the view centre (ra=180,dec=20 -> tile-fraction ~ (0.5, ~0.36))
  // at whatever zoom is requested, walked out from there so every one is
  // actually near-hemisphere-visible for VIEWPORT.
  const size = 1 << zoom;
  const cx = Math.floor(size / 2);
  const cy = Math.floor(size * 0.36);
  for (let i = 0; i < count; i += 1) {
    const tile: TileKey = { z: zoom, x: (cx + i) % size, y: cy };
    const key = `${String(tile.z)}/${String(tile.x)}/${String(tile.y)}`;
    const { slot } = atlas.acquire(key, i);
    tiles.push({ key, tile, slot });
  }
  return tiles;
}

describe("buildImageryBatches: single small batch", () => {
  it("produces exactly one batch for a handful of tiles", () => {
    const tiles = residentTilesFor(6, 10);
    const batches = buildImageryBatches(VIEWPORT, tiles, SUBDIVISIONS, DEFAULT_ATLAS_SLOTS_PER_ROW, buildTileMeshFracGrid);
    expect(batches).toHaveLength(1);
    expect(batches[0]?.tileCount).toBe(10);
  });

  it("emits (subdivisions+1)^2 vertices per tile and 6*subdivisions^2 indices per tile", () => {
    const tiles = residentTilesFor(6, 3);
    const batches = buildImageryBatches(VIEWPORT, tiles, SUBDIVISIONS, DEFAULT_ATLAS_SLOTS_PER_ROW, buildTileMeshFracGrid);
    const batch = batches[0];
    expect(batch).toBeDefined();
    if (batch === undefined) throw new Error("unreachable");
    const stride = SUBDIVISIONS + 1;
    expect(batch.positions.length).toBe(3 * stride * stride * 2);
    expect(batch.uvs.length).toBe(batch.positions.length);
    expect(batch.indices.length).toBe(3 * SUBDIVISIONS * SUBDIVISIONS * 6);
  });

  it("every index is in range for its own batch's vertex count", () => {
    const tiles = residentTilesFor(6, 5);
    const batches = buildImageryBatches(VIEWPORT, tiles, SUBDIVISIONS, DEFAULT_ATLAS_SLOTS_PER_ROW, buildTileMeshFracGrid);
    const batch = batches[0];
    expect(batch).toBeDefined();
    if (batch === undefined) throw new Error("unreachable");
    const vertexCount = batch.positions.length / 2;
    for (const index of batch.indices) {
      expect(index).toBeGreaterThanOrEqual(0);
      expect(index).toBeLessThan(vertexCount);
    }
  });

  it("distinct tiles' vertices land in distinct, non-overlapping UV rectangles", () => {
    const tiles = residentTilesFor(6, 4);
    const batches = buildImageryBatches(VIEWPORT, tiles, SUBDIVISIONS, DEFAULT_ATLAS_SLOTS_PER_ROW, buildTileMeshFracGrid);
    const batch = batches[0];
    expect(batch).toBeDefined();
    if (batch === undefined) throw new Error("unreachable");
    const stride = SUBDIVISIONS + 1;
    const perTile = stride * stride;
    const rects: { minU: number; maxU: number; minV: number; maxV: number }[] = [];
    for (let t = 0; t < tiles.length; t += 1) {
      let minU = Infinity;
      let maxU = -Infinity;
      let minV = Infinity;
      let maxV = -Infinity;
      for (let v = 0; v < perTile; v += 1) {
        const u = batch.uvs[(t * perTile + v) * 2] ?? 0;
        const vv = batch.uvs[(t * perTile + v) * 2 + 1] ?? 0;
        minU = Math.min(minU, u);
        maxU = Math.max(maxU, u);
        minV = Math.min(minV, vv);
        maxV = Math.max(maxV, vv);
      }
      rects.push({ minU, maxU, minV, maxV });
    }
    for (let a = 0; a < rects.length; a += 1) {
      for (let b = a + 1; b < rects.length; b += 1) {
        const rectA = rects[a];
        const rectB = rects[b];
        if (rectA === undefined || rectB === undefined) continue;
        const overlapsU = rectA.minU < rectB.maxU && rectB.minU < rectA.maxU;
        const overlapsV = rectA.minV < rectB.maxV && rectB.minV < rectA.maxV;
        expect(overlapsU && overlapsV).toBe(false);
      }
    }
  });
});

describe("buildImageryBatches: splits once a batch would overflow a 16-bit index buffer", () => {
  it("maxTilesPerBatch matches the 65536-vertex cap", () => {
    const stride = SUBDIVISIONS + 1;
    expect(maxTilesPerBatch(SUBDIVISIONS)).toBe(Math.floor(65536 / (stride * stride)));
  });

  it("more tiles than fit in one batch produce multiple batches, none exceeding the cap", () => {
    const cap = maxTilesPerBatch(SUBDIVISIONS);
    const tiles = residentTilesFor(10, cap + 5);
    const batches = buildImageryBatches(VIEWPORT, tiles, SUBDIVISIONS, DEFAULT_ATLAS_SLOTS_PER_ROW, buildTileMeshFracGrid);
    expect(batches.length).toBe(2);
    const totalTiles = batches.reduce((sum, b) => sum + b.tileCount, 0);
    expect(totalTiles).toBe(cap + 5);
    for (const batch of batches) {
      expect(batch.positions.length / 2).toBeLessThanOrEqual(65536);
    }
  });
});

describe("buildImageryBatches: empty input", () => {
  it("returns no batches for zero tiles", () => {
    expect(buildImageryBatches(VIEWPORT, [], SUBDIVISIONS, DEFAULT_ATLAS_SLOTS_PER_ROW, buildTileMeshFracGrid)).toEqual([]);
  });
});

/**
 * The invariant batching must preserve, checked as pure arithmetic
 * (skymap-viz brief, "rows shifted relative to each other" reported live
 * against `b07d4d8` — investigated at length: the vertex/index offsetting
 * this test targets was the coordinator's own primary suspicion for that
 * report, and is verified correct here; the atlas UV-mirroring "fix" tried
 * during that investigation was itself wrong and reverted — see the PR
 * discussion, not reproduced here — leaving this test as the durable,
 * GPU-free guard against the specific bug class that *was* plausible: a
 * tile's own screen-space mesh silently reading another tile's vertex data
 * because of a wrong per-tile vertex-buffer offset once N>1 tiles share one
 * buffer).
 *
 * For every tile in a multi-tile batch, this reads back that tile's own
 * slice of `positions` (using nothing but its own index in the input array
 * and `perTileVertexCount` — exactly the offset `buildImageryBatches` is
 * supposed to use) and compares it, vertex for vertex, against
 * `buildTileMeshFracGrid`'s own direct output for that same tile — the
 * "unbatched per-tile path" ground truth, computed completely independently
 * of `buildImageryBatches`'s internal offsetting.
 */
describe("buildImageryBatches: batched positions match the unbatched per-tile computation (b07d4d8 investigation)", () => {
  it("every tile's own vertex slice equals buildTileMeshFracGrid's direct output for that tile, across a multi-tile, multi-row batch", () => {
    const zoom = 6;
    const tiles: ResidentTile<TileKey>[] = [];
    const atlas = new TileAtlasAllocator(DEFAULT_ATLAS_SLOTS_PER_ROW);
    // Deliberately spans several distinct source-tile rows *and* columns —
    // "rows shifted relative to each other" is a claim about this axis, so
    // a test that only ever touched one row of the source tile grid could
    // not have caught it.
    const size = 1 << zoom;
    const cx = Math.floor(size / 2);
    const cy = Math.floor(size * 0.36);
    for (let dy = -2; dy <= 2; dy += 1) {
      for (let dx = -2; dx <= 2; dx += 1) {
        const tile: TileKey = { z: zoom, x: cx + dx, y: cy + dy };
        const key = `${String(tile.z)}/${String(tile.x)}/${String(tile.y)}`;
        const { slot } = atlas.acquire(key, tiles.length);
        tiles.push({ key, tile, slot });
      }
    }
    expect(tiles.length).toBe(25); // 5x5 — several rows and columns, one batch (well under maxTilesPerBatch).

    const batches = buildImageryBatches(VIEWPORT, tiles, SUBDIVISIONS, DEFAULT_ATLAS_SLOTS_PER_ROW, buildTileMeshFracGrid);
    expect(batches).toHaveLength(1);
    const batch = batches[0];
    expect(batch).toBeDefined();
    if (batch === undefined) throw new Error("unreachable");

    const stride = SUBDIVISIONS + 1;
    const perTileVertexCount = stride * stride;
    for (const [tileIndex, resident] of tiles.entries()) {
      const expected = buildTileMeshFracGrid(VIEWPORT, resident.tile, SUBDIVISIONS);
      const vertexBase = tileIndex * perTileVertexCount;
      for (let idx = 0; idx < perTileVertexCount; idx += 1) {
        // `Float32Array` storage (`ImageryBatch.positions`) against
        // `buildTileMeshFracGrid`'s `Float64Array` — the comparison is for a
        // wrong *tile offset* (pixels), not sub-float32-precision equality,
        // so a tolerance well under a pixel but above float32 rounding
        // (~1e-4px here) is the right bar, not exact equality.
        const gotX = batch.positions[(vertexBase + idx) * 2];
        const gotY = batch.positions[(vertexBase + idx) * 2 + 1];
        expect(gotX).toBeCloseTo(expected.screenX[idx] ?? NaN, 3);
        expect(gotY).toBeCloseTo(expected.screenY[idx] ?? NaN, 3);
      }
    }
  });

  it("index offsets are contiguous and non-overlapping per tile — no index ever references a different tile's vertex block", () => {
    const zoom = 6;
    const tiles: ResidentTile<TileKey>[] = [];
    const atlas = new TileAtlasAllocator(DEFAULT_ATLAS_SLOTS_PER_ROW);
    const size = 1 << zoom;
    const cx = Math.floor(size / 2);
    const cy = Math.floor(size * 0.36);
    for (let dy = -2; dy <= 2; dy += 1) {
      for (let dx = -2; dx <= 2; dx += 1) {
        const tile: TileKey = { z: zoom, x: cx + dx, y: cy + dy };
        const key = `${String(tile.z)}/${String(tile.x)}/${String(tile.y)}`;
        const { slot } = atlas.acquire(key, tiles.length);
        tiles.push({ key, tile, slot });
      }
    }
    const batches = buildImageryBatches(VIEWPORT, tiles, SUBDIVISIONS, DEFAULT_ATLAS_SLOTS_PER_ROW, buildTileMeshFracGrid);
    const batch = batches[0];
    expect(batch).toBeDefined();
    if (batch === undefined) throw new Error("unreachable");

    const stride = SUBDIVISIONS + 1;
    const perTileVertexCount = stride * stride;
    // Every triangle's three indices must fall within the SAME tile's own
    // vertex block — a triangle straddling two tiles' blocks would mean a
    // wrong offset somewhere (exactly the "vertex/index stride" bug class).
    for (let k = 0; k < batch.indices.length; k += 3) {
      const a = batch.indices[k] ?? -1;
      const b = batch.indices[k + 1] ?? -1;
      const c = batch.indices[k + 2] ?? -1;
      const tileOf = (index: number): number => Math.floor(index / perTileVertexCount);
      expect(tileOf(a)).toBe(tileOf(b));
      expect(tileOf(a)).toBe(tileOf(c));
    }
  });
});

/**
 * `buildImageryBatches` is generic over the tile-mesh builder it is handed
 * (skymap-viz brief, "teach the imagery layer to draw HiPS surveys") — this
 * is the check that it is not secretly still Mercator-shaped: the same
 * assembly logic, unmodified, batches HiPS tiles built from
 * `hipsTiles.ts`'s own `buildHipsTileMeshGrid`.
 */
describe("buildImageryBatches: also batches a HiPS tile-mesh builder unchanged", () => {
  it("produces one batch with correct vertex/index counts for HiPS tiles", () => {
    const atlas = new TileAtlasAllocator(DEFAULT_ATLAS_SLOTS_PER_ROW);
    const tiles: ResidentTile<HipsTileKey>[] = [];
    // A handful of sibling npix at order 6 near the viewport's own centre
    // (ra=180,dec=20) — exact placement does not matter here, only that
    // `buildHipsTileMeshGrid` is a valid, distinct `buildGrid`.
    const baseNpix = 6000;
    for (let i = 0; i < 4; i += 1) {
      const tile: HipsTileKey = { order: 6, npix: baseNpix + i };
      const key = `${String(tile.order)}/${String(tile.npix)}`;
      const { slot } = atlas.acquire(key, i);
      tiles.push({ key, tile, slot });
    }
    const batches = buildImageryBatches(VIEWPORT, tiles, SUBDIVISIONS, DEFAULT_ATLAS_SLOTS_PER_ROW, buildHipsTileMeshGrid);
    expect(batches).toHaveLength(1);
    expect(batches[0]?.tileCount).toBe(4);
    const stride = SUBDIVISIONS + 1;
    expect(batches[0]?.positions.length).toBe(4 * stride * stride * 2);
    expect(batches[0]?.indices.length).toBeGreaterThan(0);
    expect(batches[0]?.indices.length).toBeLessThanOrEqual(4 * SUBDIVISIONS * SUBDIVISIONS * 6);
  });
});

describe("ancestor image sampling", () => {
  it("maps the child mesh through the parent quadrant and atlas inset without a vertical flip", () => {
    const tile = residentTilesFor(6, 1)[0];
    if (tile === undefined) throw new Error("Expected tile");
    const normal = buildImageryBatches(VIEWPORT, [tile], 1, DEFAULT_ATLAS_SLOTS_PER_ROW, buildTileMeshFracGrid)[0];
    const sampled = buildImageryBatches(VIEWPORT, [{ ...tile, uvRect: { u0: .5, v0: .25, u1: .75, v1: .5 } }], 1, DEFAULT_ATLAS_SLOTS_PER_ROW, buildTileMeshFracGrid)[0];
    if (normal === undefined || sampled === undefined) throw new Error("Expected batches");
    expect(sampled.positions).toEqual(normal.positions);
    expect(sampled.indices).toEqual(normal.indices);
    const left = (normal.uvs[0] ?? 0);
    const top = (normal.uvs[1] ?? 0);
    const width = (normal.uvs[2] ?? 0) - left;
    const height = (normal.uvs[5] ?? 0) - top;
    expect(sampled.uvs[0]).toBeCloseTo(left + width * .5);
    expect(sampled.uvs[1]).toBeCloseTo(top + height * .25);
    expect(sampled.uvs[6]).toBeCloseTo(left + width * .75);
    expect(sampled.uvs[7]).toBeCloseTo(top + height * .5);
    expect(sampled.tileCount).toBe(1);
  });
});
