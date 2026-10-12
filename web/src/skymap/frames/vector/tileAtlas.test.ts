// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The LRU slot-allocation policy `imageryLayer.ts` relies on to keep GPU
 * texture memory bounded while panning — checked as pure bookkeeping, no
 * WebGL context needed.
 */
import { describe, expect, it } from "vitest";
import { atlasSlotUvRect, TileAtlasAllocator, type AtlasSlot } from "./tileAtlas";

describe("TileAtlasAllocator: basic acquisition", () => {
  it("gives a fresh tile a free slot, not already resident", () => {
    const atlas = new TileAtlasAllocator(4); // 16 slots
    const result = atlas.acquire("8/1/1", 0);
    expect(result.alreadyResident).toBe(false);
    expect(result.evicted).toBeNull();
    expect(atlas.residentCount).toBe(1);
    expect(atlas.has("8/1/1")).toBe(true);
  });

  it("returns the same slot for the same tile key, marked already-resident", () => {
    const atlas = new TileAtlasAllocator(4);
    const first = atlas.acquire("8/1/1", 0);
    const second = atlas.acquire("8/1/1", 5);
    expect(second.alreadyResident).toBe(true);
    expect(second.evicted).toBeNull();
    expect(second.slot.index).toBe(first.slot.index);
  });

  it("never runs out: capacity many distinct tiles all get distinct slots with no eviction", () => {
    const atlas = new TileAtlasAllocator(4); // 16 slots
    const seenIndices = new Set<number>();
    for (let i = 0; i < atlas.capacity; i += 1) {
      const result = atlas.acquire(`8/${String(i)}/1`, i);
      expect(result.evicted).toBeNull();
      seenIndices.add(result.slot.index);
    }
    expect(seenIndices.size).toBe(atlas.capacity);
  });
});

describe("TileAtlasAllocator: eviction keeps memory bounded", () => {
  it("evicts the globally least-recently-used tile once the atlas is full", () => {
    const atlas = new TileAtlasAllocator(2); // 4 slots
    atlas.acquire("t0", 0);
    atlas.acquire("t1", 1);
    atlas.acquire("t2", 2);
    atlas.acquire("t3", 3);
    expect(atlas.residentCount).toBe(4);

    // Touch t1..t3 again (later frames) so t0 is unambiguously the oldest.
    atlas.acquire("t1", 4);
    atlas.acquire("t2", 5);
    atlas.acquire("t3", 6);

    const result = atlas.acquire("t4", 7);
    expect(result.evicted).toBe("t0");
    expect(atlas.has("t0")).toBe(false);
    expect(atlas.has("t4")).toBe(true);
    expect(atlas.residentCount).toBe(4); // never exceeds capacity.
  });

  it("resident count never exceeds capacity across many more tiles than slots (bounded growth while panning)", () => {
    const atlas = new TileAtlasAllocator(4); // 16 slots
    for (let i = 0; i < 500; i += 1) {
      atlas.acquire(`8/${String(i)}/1`, i);
      expect(atlas.residentCount).toBeLessThanOrEqual(atlas.capacity);
    }
    expect(atlas.residentCount).toBe(atlas.capacity);
  });

  it("re-acquiring a tile refreshes its recency and protects it from the next eviction", () => {
    const atlas = new TileAtlasAllocator(2); // 4 slots
    atlas.acquire("t0", 0);
    atlas.acquire("t1", 1);
    atlas.acquire("t2", 2);
    atlas.acquire("t3", 3);
    // Without this touch, t0 would be evicted next (as in the test above).
    atlas.acquire("t0", 10);

    const result = atlas.acquire("t4", 11);
    expect(result.evicted).toBe("t1"); // now the oldest untouched tile.
    expect(atlas.has("t0")).toBe(true);
  });

  it("reset() drops every assignment so every slot is free again", () => {
    const atlas = new TileAtlasAllocator(2);
    atlas.acquire("t0", 0);
    atlas.acquire("t1", 1);
    atlas.reset();
    expect(atlas.residentCount).toBe(0);
    const result = atlas.acquire("t0", 2);
    expect(result.alreadyResident).toBe(false);
    expect(result.evicted).toBeNull();
  });
});

describe("atlasSlotUvRect", () => {
  it("tiles the unit square exactly, before half-texel inset, across every slot", () => {
    const slotsPerRow = 4;
    const size = 1 / slotsPerRow;
    for (let row = 0; row < slotsPerRow; row += 1) {
      for (let col = 0; col < slotsPerRow; col += 1) {
        const slot: AtlasSlot = { index: row * slotsPerRow + col, col, row };
        const rect = atlasSlotUvRect(slot, slotsPerRow, 256);
        const halfTexel = 0.5 / (slotsPerRow * 256);
        expect(rect.u0).toBeCloseTo(col * size + halfTexel, 10);
        expect(rect.v0).toBeCloseTo(row * size + halfTexel, 10);
        expect(rect.u1).toBeCloseTo((col + 1) * size - halfTexel, 10);
        expect(rect.v1).toBeCloseTo((row + 1) * size - halfTexel, 10);
      }
    }
  });

  it("insets strictly inside the slot (u0 < u1, v0 < v1) even at a small tile size", () => {
    const slot: AtlasSlot = { index: 0, col: 0, row: 0 };
    const rect = atlasSlotUvRect(slot, 16, 256);
    expect(rect.u0).toBeGreaterThan(0);
    expect(rect.u1).toBeLessThan(1 / 16);
    expect(rect.u0).toBeLessThan(rect.u1);
    expect(rect.v0).toBeLessThan(rect.v1);
  });

  it("distinct slots never overlap", () => {
    const slotsPerRow = 4;
    const a = atlasSlotUvRect({ index: 0, col: 0, row: 0 }, slotsPerRow, 256);
    const b = atlasSlotUvRect({ index: 1, col: 1, row: 0 }, slotsPerRow, 256);
    expect(a.u1).toBeLessThanOrEqual(b.u0);
  });
});
