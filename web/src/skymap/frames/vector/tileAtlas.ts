// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pure GPU-texture-atlas bookkeeping for the vector frame's batched imagery
 * layer (`imageryLayer.ts`) — no WebGL calls here, only which tile owns
 * which atlas slot and when a slot is evicted, so the allocation policy is
 * unit-tested without a browser (`tileAtlas.test.ts`).
 *
 * One atlas is a fixed `slotsPerRow x slotsPerRow` grid of `ATLAS_TILE_PX`
 * texture cells packed into a single square texture — the batching fix the
 * skymap-viz brief asked for: every visible, loaded tile shares one texture
 * object and one vertex/index buffer, so drawing them costs one GL draw
 * call (`imageryLayer.ts`'s `MercatorImageryLayer.draw`) instead of one per
 * tile (the imagery spike's own measured ~85 draws/frame).
 *
 * Budget, stated explicitly: `DEFAULT_ATLAS_SLOTS_PER_ROW` (16, 256 slots)
 * resident tiles per atlas, `ATLAS_TILE_PX` (256) each = 256 * 256*256*4
 * bytes ~= 64MiB of GPU texture memory per active layer — comfortably above
 * what a `pickZoom` viewport ever needs on screen at once (a few dozen
 * tiles) while still bounding total growth: panning for any length of time
 * evicts the least-recently-used slot rather than growing without bound.
 * "Recently used" is frame-counted (`acquire`'s `frame` argument), not
 * wall-clock, so it is exact and cheap to compare — no `Date.now()` needed
 * inside a per-frame draw path.
 */

/** The Mercator tile proxy's own fixed tile size (`ssc/skymap/mercator.py`'s
 * own module doc: "a 2^z x 2^z grid of 256px tiles"). */
export const ATLAS_TILE_PX = 256;

/** 16*16 = 256 slots, a 4096px square atlas texture — within
 * `MAX_TEXTURE_SIZE` on every GPU this frame is expected to run on
 * (2048 is the WebGL1 spec's own guaranteed floor; 4096 is universal in
 * practice on anything shipping since well before this project started). */
export const DEFAULT_ATLAS_SLOTS_PER_ROW = 16;

export interface AtlasSlot {
  readonly index: number;
  readonly col: number;
  readonly row: number;
}

interface SlotState extends AtlasSlot {
  tileKey: string | null;
  lastUsedFrame: number;
}

export interface AtlasAcquireResult {
  slot: AtlasSlot;
  /** The tile key `slot` held before this call, if a *different* tile's
   * slot had to be evicted to make room — `null` when the slot was free or
   * already held `tileKey`. The caller (`imageryLayer.ts`) uses this only
   * for its own bookkeeping (marking the evicted tile's texture data as
   * gone); the atlas texture's pixels themselves are always overwritten by
   * the caller after `acquire` for a slot that was not `alreadyResident`. */
  evicted: string | null;
  /** Whether `slot` already held `tileKey` — i.e. no image upload is
   * needed, only a recency touch. */
  alreadyResident: boolean;
}

/**
 * LRU slot allocator for one atlas texture. `slotsPerRow * slotsPerRow`
 * slots total; `acquire` always returns a slot, evicting the
 * least-recently-used one if every slot already holds a *different* tile
 * than the one requested — an atlas is never "full" in the sense of
 * refusing a request, since that would just mean the tile can never be
 * drawn, the opposite of the point of a bounded cache.
 */
export class TileAtlasAllocator {
  readonly slotsPerRow: number;
  private readonly slots: SlotState[];
  private readonly byTileKey = new Map<string, SlotState>();
  private readonly free: SlotState[];

  constructor(slotsPerRow: number = DEFAULT_ATLAS_SLOTS_PER_ROW) {
    this.slotsPerRow = slotsPerRow;
    this.slots = [];
    for (let row = 0; row < slotsPerRow; row += 1) {
      for (let col = 0; col < slotsPerRow; col += 1) {
        this.slots.push({ index: row * slotsPerRow + col, col, row, tileKey: null, lastUsedFrame: -1 });
      }
    }
    this.free = [...this.slots];
  }

  get capacity(): number {
    return this.slots.length;
  }

  get residentCount(): number {
    return this.byTileKey.size;
  }

  /** Whether `tileKey` currently occupies a slot, without touching its
   * recency the way `acquire` does. */
  has(tileKey: string): boolean {
    return this.byTileKey.has(tileKey);
  }

  slotFor(tileKey: string): AtlasSlot | null {
    return this.byTileKey.get(tileKey) ?? null;
  }

  /** Reserves a slot for `tileKey` at `frame`, evicting the globally
   * least-recently-used slot if none is free and `tileKey` is not already
   * resident. Always marks the returned slot used at `frame`, whether or
   * not it was already resident — an `acquire` call is itself a use. */
  acquire(tileKey: string, frame: number): AtlasAcquireResult {
    const existing = this.byTileKey.get(tileKey);
    if (existing !== undefined) {
      existing.lastUsedFrame = frame;
      return { slot: existing, evicted: null, alreadyResident: true };
    }
    let target = this.free.pop();
    let evicted: string | null = null;
    if (target === undefined) {
      target = this.leastRecentlyUsed();
      evicted = target.tileKey;
      if (evicted !== null) this.byTileKey.delete(evicted);
    }
    target.tileKey = tileKey;
    target.lastUsedFrame = frame;
    this.byTileKey.set(tileKey, target);
    return { slot: target, evicted, alreadyResident: false };
  }

  private leastRecentlyUsed(): SlotState {
    let best: SlotState | undefined;
    for (const slot of this.slots) {
      if (best === undefined || slot.lastUsedFrame < best.lastUsedFrame) best = slot;
    }
    if (best === undefined) throw new Error("[skymap] TileAtlasAllocator: zero-capacity atlas");
    return best;
  }

  /** Drops every slot's tile assignment without touching GPU state — used
   * when the whole atlas texture itself is being torn down
   * (`imageryLayer.ts`'s per-survey cleanup), so a stale `byTileKey` entry
   * can never outlive the texture it names. */
  reset(): void {
    this.byTileKey.clear();
    this.free.length = 0;
    for (const slot of this.slots) {
      slot.tileKey = null;
      slot.lastUsedFrame = -1;
      this.free.push(slot);
    }
  }
}

/**
 * A slot's own UV rectangle within the atlas texture, inset by half a
 * texel on every side — the standard atlas-bleed fix: at exactly `u=0` or
 * `u=1`, `LINEAR` filtering samples up to half a texel from the
 * *neighbouring* slot, which for a photographic tile atlas shows as a
 * visible seam of the wrong tile's colour at every slot boundary. Insetting
 * by half a texel keeps every sample strictly inside this slot's own
 * `ATLAS_TILE_PX` pixels, at the cost of a half-texel of edge coverage that
 * is imperceptible at this tile size.
 */
export function atlasSlotUvRect(
  slot: AtlasSlot,
  slotsPerRow: number,
  tilePx: number = ATLAS_TILE_PX,
): { u0: number; v0: number; u1: number; v1: number } {
  const atlasPx = slotsPerRow * tilePx;
  const halfTexel = 0.5 / atlasPx;
  const size = 1 / slotsPerRow;
  const u0 = slot.col * size;
  const v0 = slot.row * size;
  return { u0: u0 + halfTexel, v0: v0 + halfTexel, u1: u0 + size - halfTexel, v1: v0 + size - halfTexel };
}
