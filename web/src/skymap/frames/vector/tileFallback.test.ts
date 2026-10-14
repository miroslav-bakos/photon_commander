// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { describe, expect, it } from "vitest";
import { residentHipsDescendants, residentMercatorDescendants, resolveHipsFallback, resolveMercatorFallback } from "./tileFallback";

const resident = (...keys: string[]) => (key: string) => keys.includes(key);
const full = { u0: 0, v0: 0, u1: 1, v1: 1 };

describe("nearest resident tile fallback", () => {
  it("prefers exact tiles and reports missing imagery", () => {
    expect(resolveMercatorFallback({ z: 2, x: 1, y: 2 }, resident("mercator/2/1/2", "mercator/0/0/0")))
      .toEqual({ key: "mercator/2/1/2", uvRect: full });
    expect(resolveHipsFallback({ order: 2, npix: 13 }, resident("hips/2/13", "hips/0/0")))
      .toEqual({ key: "hips/2/13", uvRect: full });
    expect(resolveMercatorFallback({ z: 0, x: 0, y: 0 }, resident())).toBeNull();
    expect(resolveHipsFallback({ order: 0, npix: 11 }, resident())).toBeNull();
  });

  it.each([[0, 0], [1, 0], [0, 1], [1, 1]])("samples Mercator child quadrant %s,%s without flipping rows", (x, y) => {
    expect(resolveMercatorFallback({ z: 2, x: 2 + x, y: 2 + y }, resident("mercator/1/1/1")))
      .toEqual({ key: "mercator/1/1/1", uvRect: { u0: x / 2, v0: y / 2, u1: (x + 1) / 2, v1: (y + 1) / 2 } });
  });

  it.each([[0, 0, 0], [1, 0, 1], [2, 1, 0], [3, 1, 1]])("samples HiPS nested child %s with columns=iy rows=ix", (child, u, v) => {
    expect(resolveHipsFallback({ order: 2, npix: 20 + child }, resident("hips/1/5")))
      .toEqual({ key: "hips/1/5", uvRect: { u0: u / 2, v0: v / 2, u1: (u + 1) / 2, v1: (v + 1) / 2 } });
  });

  it("selects the closest available multilevel ancestor", () => {
    expect(resolveMercatorFallback({ z: 4, x: 11, y: 6 }, resident("mercator/2/2/1", "mercator/0/0/0")))
      .toEqual({ key: "mercator/2/2/1", uvRect: { u0: .75, v0: .5, u1: 1, v1: .75 } });
    // Face 7, ix=3, iy=2 at order 2: nested remainder=13.
    expect(resolveHipsFallback({ order: 2, npix: 7 * 16 + 13 }, resident("hips/0/7")))
      .toEqual({ key: "hips/0/7", uvRect: { u0: .5, v0: .75, u1: .75, v1: 1 } });
  });

  it("keeps every descendant sample inside its own face's resident root", () => {
    for (let npix = 0; npix < 12 * 64; npix += 1) {
      const result = resolveHipsFallback({ order: 3, npix }, resident(`hips/0/${String(Math.floor(npix / 64))}`));
      expect(result).not.toBeNull();
      if (result === null) throw new Error("Expected root fallback");
      for (const value of Object.values(result.uvRect)) {
        expect(value).toBeGreaterThanOrEqual(0);
        expect(value).toBeLessThanOrEqual(1);
      }
      expect(result.uvRect.u1 - result.uvRect.u0).toBe(1 / 8);
      expect(result.uvRect.v1 - result.uvRect.v0).toBe(1 / 8);
    }
  });
});


describe("resident descendants while zooming out", () => {
  it("keeps partial Mercator coverage and skips finer tiles covered by a coarser cached child", () => {
    const keys = ["mercator/4/8/8", "mercator/3/4/4", "mercator/4/11/10", "mercator/3/3/4", "hips/3/4", "mercator/2/2/2"];
    expect(residentMercatorDescendants({ z: 2, x: 2, y: 2 }, keys)).toEqual([
      { key: "mercator/3/4/4", tile: { z: 3, x: 4, y: 4 } },
      { key: "mercator/4/11/10", tile: { z: 4, x: 11, y: 10 } },
    ]);
  });

  it("respects Mercator world edges and rejects invalid or foreign keys", () => {
    expect(residentMercatorDescendants({ z: 1, x: 0, y: 0 }, ["mercator/2/0/0", "mercator/2/3/0", "mercator/2/4/0", "mercator/2/0/4", "mercator/-1/0/0", "mercator/99/0/0", "other"])).toEqual([
      { key: "mercator/2/0/0", tile: { z: 2, x: 0, y: 0 } },
    ]);
  });

  it("keeps HiPS nested descendants within their face and removes overlapping deeper descendants", () => {
    // Root face 7; child 29 covers descendants 116..119, while 123 is separate.
    expect(residentHipsDescendants({ order: 0, npix: 7 }, ["hips/2/116", "hips/1/29", "hips/2/123", "hips/1/28", "hips/2/127", "hips/2/128", "hips/0/7"])).toEqual([
      { key: "hips/1/28", tile: { order: 1, npix: 28 } },
      { key: "hips/1/29", tile: { order: 1, npix: 29 } },
      { key: "hips/2/123", tile: { order: 2, npix: 123 } },
      { key: "hips/2/127", tile: { order: 2, npix: 127 } },
    ]);
  });

  it("ignores malformed HiPS keys and duplicate cached coverage", () => {
    expect(residentHipsDescendants({ order: 0, npix: 0 }, ["hips/1/1", "hips/1/1", "hips/1/48", "hips/1/-1", "hips/25/0", "hips/2/1.5", "mercator/1/0/0"])).toEqual([
      { key: "hips/1/1", tile: { order: 1, npix: 1 } },
    ]);
  });
});
