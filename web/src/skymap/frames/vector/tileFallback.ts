// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { HIPS_ORDER_SAFETY_CAP, pixelFaceXy, type HipsTileKey } from "./hipsTiles";
import type { TileUvRect } from "./imageryBatch";
import { MERCATOR_MAX_ZOOM, type TileKey } from "./mercatorTiles";

export interface TileFallback {
  key: string;
  uvRect: TileUvRect;
}

/** Keep drawing the child's mesh, sampling its nearest available ancestor. */
export function resolveMercatorFallback(tile: TileKey, hasKey: (key: string) => boolean): TileFallback | null {
  for (let z = tile.z; z >= 0; z -= 1) {
    const scale = 2 ** (tile.z - z);
    const x = Math.floor(tile.x / scale);
    const y = Math.floor(tile.y / scale);
    const key = `mercator/${String(z)}/${String(x)}/${String(y)}`;
    if (hasKey(key)) {
      const u0 = (tile.x - x * scale) / scale;
      const v0 = (tile.y - y * scale) / scale;
      return { key, uvRect: { u0, v0, u1: u0 + 1 / scale, v1: v0 + 1 / scale } };
    }
  }
  return null;
}

export function resolveHipsFallback(tile: HipsTileKey, hasKey: (key: string) => boolean): TileFallback | null {
  const child = pixelFaceXy(tile.npix, tile.order);
  for (let order = tile.order; order >= 0; order -= 1) {
    const levels = tile.order - order;
    const scale = 2 ** levels;
    const npix = Math.floor(tile.npix / 4 ** levels);
    const key = `hips/${String(order)}/${String(npix)}`;
    if (hasKey(key)) {
      const parent = pixelFaceXy(npix, order);
      // HiPS image columns step iy; rows step ix (buildHipsTileMeshGrid).
      const u0 = (child.iy - parent.iy * scale) / scale;
      const v0 = (child.ix - parent.ix * scale) / scale;
      return { key, uvRect: { u0, v0, u1: u0 + 1 / scale, v1: v0 + 1 / scale } };
    }
  }
  return null;
}

/** Coarsest cached descendants supply non-overlapping coverage while zooming out. */
export function residentMercatorDescendants(tile: TileKey, keys: Iterable<string>): { key: string; tile: TileKey }[] {
  const candidates: { key: string; tile: TileKey }[] = [];
  for (const key of keys) {
    const match = /^mercator\/(\d+)\/(\d+)\/(\d+)$/.exec(key);
    if (match === null) continue;
    const z = Number(match[1]);
    const x = Number(match[2]);
    const y = Number(match[3]);
    if (!Number.isSafeInteger(z) || z <= tile.z || z > MERCATOR_MAX_ZOOM || x >= 2 ** z || y >= 2 ** z) continue;
    const scale = 2 ** (z - tile.z);
    if (Math.floor(x / scale) === tile.x && Math.floor(y / scale) === tile.y) candidates.push({ key, tile: { z, x, y } });
  }
  candidates.sort((a, b) => a.tile.z - b.tile.z || a.tile.y - b.tile.y || a.tile.x - b.tile.x);
  const selected: typeof candidates = [];
  for (const candidate of candidates) {
    if (selected.some(({ tile: coarse }) => {
      const scale = 2 ** (candidate.tile.z - coarse.z);
      return Math.floor(candidate.tile.x / scale) === coarse.x && Math.floor(candidate.tile.y / scale) === coarse.y;
    })) continue;
    selected.push(candidate);
  }
  return selected;
}

export function residentHipsDescendants(tile: HipsTileKey, keys: Iterable<string>): { key: string; tile: HipsTileKey }[] {
  const candidates: { key: string; tile: HipsTileKey }[] = [];
  for (const key of keys) {
    const match = /^hips\/(\d+)\/(\d+)$/.exec(key);
    if (match === null) continue;
    const order = Number(match[1]);
    const npix = Number(match[2]);
    if (!Number.isSafeInteger(order) || order <= tile.order || order > HIPS_ORDER_SAFETY_CAP || !Number.isSafeInteger(npix) || npix >= 12 * 4 ** order) continue;
    if (Math.floor(npix / 4 ** (order - tile.order)) === tile.npix) candidates.push({ key, tile: { order, npix } });
  }
  candidates.sort((a, b) => a.tile.order - b.tile.order || a.tile.npix - b.tile.npix);
  const selected: typeof candidates = [];
  for (const candidate of candidates) {
    if (selected.some(({ tile: coarse }) => Math.floor(candidate.tile.npix / 4 ** (candidate.tile.order - coarse.order)) === coarse.npix)) continue;
    selected.push(candidate);
  }
  return selected;
}
