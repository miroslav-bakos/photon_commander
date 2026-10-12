// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Ground-truth first (skymap-viz brief: "two separate mirrored-tile-address
 * bugs shipped today, both because tests checked a formula against
 * itself") — the anchor below is not derived from this module at all: it
 * is the North America Nebula (NGC 7000, 20h58m47s +44d31m) resolving via
 * `ssc/skymap/hips.ang2pix` at order 6 to npix 15363
 * (`ssc/skymap/surveys.py`'s `NSNS_HALPHA` doc comment), which was itself
 * fetched successfully from the live `simg.de` archive. `ang2pix` here is a
 * deliberate line-for-line port of that Python function (`hipsTiles.ts`'s
 * own doc comment); everything after the first test in this file checks
 * internal consistency (round-trips, mesh corners, batching genericity),
 * which is useful but — on its own, as this project has now shipped twice
 * — not sufficient.
 */
import { describe, expect, it } from "vitest";
import { screenToWorld, worldToScreen, type Viewport } from "../../projection";
import {
  ang2pix,
  buildHipsTileMeshGrid,
  equatorialToSurvey,
  faceXyToRaDec,
  hipsTileUrl,
  maxPixelRadiusRad,
  pickHipsOrder,
  pixelCentreRaDec,
  pixelFaceXy,
  surveyToEquatorial,
  visibleHipsTiles,
  type HipsTileKey,
} from "./hipsTiles";

describe("Galactic HiPS frame", () => {
  it("maps the IAU Galactic centre anchor between Galactic and J2000 coordinates", () => {
    const [l, b] = equatorialToSurvey(266.405, -28.936175, "galactic");
    expect(l === 360 ? 0 : l).toBeCloseTo(0, 3);
    expect(b).toBeCloseTo(0, 3);
    const [ra, dec] = surveyToEquatorial(0, 0, "galactic");
    expect(ra).toBeCloseTo(266.405, 3);
    expect(dec).toBeCloseTo(-28.936175, 3);
  });

  it("selects Galactic-indexed tiles using transformed viewport coordinates", () => {
    const viewport: Viewport = {
      widthPx: 100,
      heightPx: 100,
      view: { ra: 266.405, dec: -28.936175, fovDeg: 1 },
    };
    const fixed = () => [266.405, -28.936175] as [number, number];
    const tiles = visibleHipsTiles(viewport, 4, fixed, 1, "galactic");
    const [lon, lat] = equatorialToSurvey(266.405, -28.936175, "galactic");
    expect(tiles).toContainEqual({ order: 4, npix: ang2pix(lon, lat, 4) });
  });
});

const NGC7000_RA_DEG = (20 + 58 / 60 + 47 / 3600) * 15;
const NGC7000_DEC_DEG = 44 + 31 / 60;

function hmsDeg(h: number, m: number, s: number): number {
  return (h + m / 60 + s / 3600) * 15;
}
function dmsDeg(d: number, m: number, s: number, sign: 1 | -1 = 1): number {
  return sign * (d + m / 60 + s / 3600);
}

describe("ang2pix: ground truth against the live archive", () => {
  it("resolves NGC 7000 at order 6 to npix 15363, the address actually fetched from simg.de", () => {
    expect(ang2pix(NGC7000_RA_DEG, NGC7000_DEC_DEG, 6)).toBe(15363);
  });

  /**
   * Anchors added after the equatorial-only anchor above missed a real
   * defect: the vector frame's own placement (`visibleHipsTiles`) went
   * wrong specifically in the north polar cap (owner's screenshot against
   * `b6955f4`, view centred RA 10.56 / Dec +69.79 — deep in the cap, far
   * from NGC 7000's own near-boundary Dec +44.5), which a single
   * equatorial-belt anchor cannot exercise. Every value below is this
   * module's own `ang2pix` cross-checked against `ssc.skymap.hips.ang2pix`
   * directly (not a round-trip against this module's own inverse) —
   * `uv run python -c "from ssc.skymap import hips; ..."` for each.
   */
  it("resolves ground-truth fields in both HEALPix regimes and at the belt/cap boundary", () => {
    // North polar cap: the owner's own reported view centre.
    expect(ang2pix(hmsDeg(0, 42, 14.4), 69.79, 6)).toBe(3765);
    // North polar cap, deep — near Polaris (RA 2h31m49.09s +89d15m50.8s J2000).
    expect(ang2pix(hmsDeg(2, 31, 49.09), dmsDeg(89, 15, 50.8), 6)).toBe(4095);
    // Belt/cap boundary: NGC 7000, Dec +44.5, a few degrees north of the
    // |z| = 2/3 (Dec ~41.8) transition.
    expect(ang2pix(NGC7000_RA_DEG, NGC7000_DEC_DEG, 6)).toBe(15363);
    // Equatorial belt, genuinely low: M42 (RA 5h35m17.3s Dec -5d23m28s).
    expect(ang2pix(hmsDeg(5, 35, 17.3), dmsDeg(5, 23, 28, -1), 6)).toBe(21439);
  });

  it("matches ssc/skymap/hips.py's own pixel_centres at a spread of orders and pixels", () => {
    // Cross-checked against the Python reference offline (see the task's own
    // verification session); pinned here as fixed expected values so this
    // test does not itself need a Python subprocess. Every one of these was
    // independently generated from `hips.pixel_centres`, not from this
    // module — the same anti-self-consistency discipline as the anchor
    // above.
    const cases: { order: number; npix: number; ra: number; dec: number }[] = [
      { order: 0, npix: 4, ra: 0, dec: 0 },
      { order: 0, npix: 0, ra: 45, dec: 41.810314895778596 },
      { order: 0, npix: 1, ra: 135, dec: 41.810314895778596 },
      { order: 1, npix: 15, ra: 315, dec: 66.44353569089876 },
      { order: 6, npix: 15363, ra: 315.00000000000006, dec: 44.20152985105137 },
    ];
    for (const c of cases) {
      const [ra, dec] = pixelCentreRaDec(c.npix, c.order);
      expect(ra).toBeCloseTo(c.ra, 6);
      expect(dec).toBeCloseTo(c.dec, 6);
    }
  });
});

describe("pixelFaceXy / faceXyToRaDec: round-trip through ang2pix", () => {
  it("a pixel's own centre re-resolves (via ang2pix) to the same pixel, at several orders", () => {
    const samples: [number, number][] = [
      [10, 80],
      [200, -30],
      [359, 89],
      [0.5, -89],
      [180, 0],
      [45, 41.8],
    ];
    for (const order of [0, 1, 2, 4, 6]) {
      for (const [ra, dec] of samples) {
        const npix = ang2pix(ra, dec, order);
        const [centreRa, centreDec] = pixelCentreRaDec(npix, order);
        expect(ang2pix(centreRa, centreDec, order)).toBe(npix);
      }
    }
  });

  it("every corner of a pixel's own unit cell is closer to that pixel's centre than one pixel diameter", () => {
    const order = 4;
    const npix = ang2pix(123.4, 56.7, order);
    const nside = 2 ** order;
    const { face, ix, iy } = pixelFaceXy(npix, order);
    const [centreRa, centreDec] = pixelCentreRaDec(npix, order);
    const centre = unitVector(centreRa, centreDec);
    const corners: [number, number][] = [
      [ix / nside, iy / nside],
      [(ix + 1) / nside, iy / nside],
      [ix / nside, (iy + 1) / nside],
      [(ix + 1) / nside, (iy + 1) / nside],
    ];
    const maxRadius = maxPixelRadiusRad(order);
    for (const [xFrac, yFrac] of corners) {
      const [ra, dec] = faceXyToRaDec(face, xFrac, yFrac);
      const angle = Math.acos(Math.max(-1, Math.min(1, dot(centre, unitVector(ra, dec)))));
      expect(angle).toBeLessThanOrEqual(maxRadius * 1.01); // 1% slack for the bound's own approximation
    }
  });
});

function unitVector(raDeg: number, decDeg: number): [number, number, number] {
  const ra = (raDeg * Math.PI) / 180;
  const dec = (decDeg * Math.PI) / 180;
  return [Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec)];
}

function dot(a: readonly [number, number, number], b: readonly [number, number, number]): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

describe("hipsTileUrl", () => {
  it("substitutes order/dir/npix, dir bucketed to 10,000 like ssc/skymap/hips.tile_dir", () => {
    const template = "/api/skymap/tiles/nsns-ohs8/Norder{order}/Dir{dir}/Npix{npix}.png";
    expect(hipsTileUrl(template, { order: 6, npix: 15363 })).toBe(
      "/api/skymap/tiles/nsns-ohs8/Norder6/Dir10000/Npix15363.png",
    );
    expect(hipsTileUrl(template, { order: 3, npix: 42 })).toBe(
      "/api/skymap/tiles/nsns-ohs8/Norder3/Dir0/Npix42.png",
    );
  });
});

describe("pickHipsOrder", () => {
  it("increases as FOV narrows, and stays within [0, HIPS_ORDER_SAFETY_CAP]", () => {
    const wide = pickHipsOrder(60, 1200, 800);
    const narrow = pickHipsOrder(5, 1200, 800);
    expect(narrow).toBeGreaterThan(wide);
    expect(wide).toBeGreaterThanOrEqual(0);
    expect(narrow).toBeLessThanOrEqual(16);
  });
});

const VIEWPORT: Viewport = { view: { ra: 314.7, dec: 44.5, fovDeg: 5 }, widthPx: 1200, heightPx: 800 };

describe("visibleHipsTiles", () => {
  it("returns tiles that actually cover the viewport centre", () => {
    const order = 6;
    const tiles = visibleHipsTiles(VIEWPORT, order, screenToWorld);
    expect(tiles.length).toBeGreaterThan(0);
    for (const tile of tiles) expect(tile.order).toBe(order);
    const [centreRa, centreDec] = screenToWorld(VIEWPORT, VIEWPORT.widthPx / 2, VIEWPORT.heightPx / 2);
    const centreNpix = ang2pix(centreRa, centreDec, order);
    expect(tiles.some((t) => t.npix === centreNpix)).toBe(true);
  });

  it("is monotone: a wider FOV never returns fewer tiles than a narrower one centred the same way", () => {
    const order = 4;
    const narrow: Viewport = { ...VIEWPORT, view: { ...VIEWPORT.view, fovDeg: 2 } };
    const wide: Viewport = { ...VIEWPORT, view: { ...VIEWPORT.view, fovDeg: 40 } };
    const narrowTiles = new Set(visibleHipsTiles(narrow, order, screenToWorld).map((t) => t.npix));
    const wideTiles = new Set(visibleHipsTiles(wide, order, screenToWorld).map((t) => t.npix));
    for (const npix of narrowTiles) expect(wideTiles.has(npix)).toBe(true);
  });

  it("covers a full 60deg FOV with no gap at a sampled grid of screen points", () => {
    const wideViewport: Viewport = { view: { ra: 200, dec: 10, fovDeg: 60 }, widthPx: 1200, heightPx: 800 };
    const order = 3;
    const tiles = new Set(visibleHipsTiles(wideViewport, order, screenToWorld).map((t) => t.npix));
    for (let row = 0; row <= 6; row += 1) {
      for (let col = 0; col <= 6; col += 1) {
        const sx = (col / 6) * wideViewport.widthPx;
        const sy = (row / 6) * wideViewport.heightPx;
        const [ra, dec] = screenToWorld(wideViewport, sx, sy);
        const npix = ang2pix(ra, dec, order);
        expect(tiles.has(npix)).toBe(true);
      }
    }
  });

  it("handles a near-pole view with no gap (the hierarchical descent needs no special pole case)", () => {
    const poleViewport: Viewport = { view: { ra: 0, dec: 88, fovDeg: 20 }, widthPx: 1200, heightPx: 800 };
    const order = 4;
    const tiles = new Set(visibleHipsTiles(poleViewport, order, screenToWorld).map((t) => t.npix));
    for (let row = 0; row <= 6; row += 1) {
      for (let col = 0; col <= 6; col += 1) {
        const sx = (col / 6) * poleViewport.widthPx;
        const sy = (row / 6) * poleViewport.heightPx;
        const [ra, dec] = screenToWorld(poleViewport, sx, sy);
        const npix = ang2pix(ra, dec, order);
        expect(tiles.has(npix)).toBe(true);
      }
    }
  });

  /**
   * Pins the live placement defect reported against `b6955f4` (owner's own
   * screenshot: view centred RA 10.56 / Dec +69.79, 60deg FOV, "large
   * skewed quadrilaterals... whole blocks sit at the wrong place") and its
   * root cause: the coarse-order hierarchical descent's own per-level
   * margin (`maxPixelRadiusRad`, tens of degrees at order 0-2) is
   * deliberately generous enough that a pixel whose true *centre* sits
   * outside the real screen radius can still survive every filtering step
   * — order 2, npix 25 here, true centre 58.4deg from the view axis
   * against a real screen radius of 44.1deg. `visibleHipsTiles` must not
   * include it, and its own closest *corner* (45.9deg) confirms it: no
   * part of that pixel is actually within the visible cone.
   */
  it("excludes a pixel whose real shape never reaches the viewport, even though its centre survives the descent's own coarse-level margin", () => {
    const ownerViewport: Viewport = { view: { ra: 10.56, dec: 69.79, fovDeg: 60 }, widthPx: 1400, heightPx: 900 };
    const order = 2;
    const tiles = visibleHipsTiles(ownerViewport, order, screenToWorld);
    expect(tiles.some((t) => t.npix === 25)).toBe(false);
  });

  /**
   * The cheap, decisive arithmetic check the coordinator asked for
   * (distinct from the placement defect above): a batch built from one
   * `visibleHipsTiles` call must never contain the same npix twice, and —
   * since every returned tile shares one `order` — no tile can be another's
   * ancestor either (that would require two different orders in the same
   * call, which the return type does not allow). Duplicate/overlapping
   * draws at partial alpha is exactly the "same data rendered darker"
   * signature reported separately (Orion, Dec +1.32) — traced instead to a
   * genuine per-tile upstream stretch inconsistency (verified directly
   * against the tile bytes, `ssc/skymap/` — outside this frame's own
   * scope), but this invariant is worth holding unconditionally regardless
   * of where that report lands.
   */
  it("never returns the same npix twice, for a spread of real views", () => {
    const views: Viewport[] = [
      { view: { ra: 10.56, dec: 69.79, fovDeg: 60 }, widthPx: 1400, heightPx: 900 },
      { view: { ra: 99.11, dec: 1.32, fovDeg: 60 }, widthPx: 1400, heightPx: 900 },
      { view: { ra: 314.7, dec: 44.5, fovDeg: 5 }, widthPx: 1400, heightPx: 900 },
      { view: { ra: 0, dec: -89, fovDeg: 20 }, widthPx: 1400, heightPx: 900 },
    ];
    for (const v of views) {
      for (const order of [2, 4, 6]) {
        const tiles = visibleHipsTiles(v, order, screenToWorld);
        const npixList = tiles.map((t) => t.npix);
        expect(new Set(npixList).size).toBe(npixList.length);
      }
    }
  });

  /**
   * A direct rasterized-coverage check of what the batch actually draws
   * (not just which npix were selected): for both reported views, sample a
   * fine screen-space grid and assert every triangle in the resulting
   * batch that a sample point falls in is unique per sample — i.e. no
   * on-screen point is covered by two different triangles. This is the
   * same technique used live to rule out geometric overlap as the cause of
   * the Orion "overdraw" report; kept here as a standing regression guard
   * for the placement fix above.
   */
  it("draws no screen point twice, for the owner's reported views", () => {
    const cases: Viewport[] = [
      { view: { ra: 10.56, dec: 69.79, fovDeg: 60 }, widthPx: 1400, heightPx: 900 },
      { view: { ra: 99.11, dec: 1.32, fovDeg: 60 }, widthPx: 1400, heightPx: 900 },
    ];
    for (const viewport of cases) {
      const order = Math.min(pickHipsOrder(viewport.view.fovDeg, viewport.widthPx, viewport.heightPx), 6);
      const tiles = visibleHipsTiles(viewport, order, screenToWorld);
      const subdivisions = 8;
      const cellSize = 20;
      const cols = Math.ceil(viewport.widthPx / cellSize);
      const rows = Math.ceil(viewport.heightPx / cellSize);
      const coverage = new Int32Array(cols * rows);
      const sign = (px: number, py: number, ax: number, ay: number, bx: number, by: number): number =>
        (px - bx) * (ay - by) - (ax - bx) * (py - by);
      const pointInTri = (
        px: number,
        py: number,
        ax: number,
        ay: number,
        bx: number,
        by: number,
        cx: number,
        cy: number,
      ): boolean => {
        const d1 = sign(px, py, ax, ay, bx, by);
        const d2 = sign(px, py, bx, by, cx, cy);
        const d3 = sign(px, py, cx, cy, ax, ay);
        const hasNeg = d1 < 0 || d2 < 0 || d3 < 0;
        const hasPos = d1 > 0 || d2 > 0 || d3 > 0;
        return !(hasNeg && hasPos);
      };
      for (const tile of tiles) {
        const grid = buildHipsTileMeshGrid(viewport, tile, subdivisions);
        const stride = subdivisions + 1;
        for (let j = 0; j < subdivisions; j += 1) {
          for (let i = 0; i < subdivisions; i += 1) {
            const p00 = j * stride + i;
            const p10 = j * stride + (i + 1);
            const p01 = (j + 1) * stride + i;
            const p11 = (j + 1) * stride + (i + 1);
            for (const [a, b, c] of [
              [p00, p10, p01],
              [p10, p11, p01],
            ] as const) {
              const ax = grid.screenX[a] ?? 0;
              const ay = grid.screenY[a] ?? 0;
              const bx = grid.screenX[b] ?? 0;
              const by = grid.screenY[b] ?? 0;
              const cx = grid.screenX[c] ?? 0;
              const cy = grid.screenY[c] ?? 0;
              const minX = Math.max(0, Math.floor(Math.min(ax, bx, cx) / cellSize));
              const maxX = Math.min(cols - 1, Math.ceil(Math.max(ax, bx, cx) / cellSize));
              const minY = Math.max(0, Math.floor(Math.min(ay, by, cy) / cellSize));
              const maxY = Math.min(rows - 1, Math.ceil(Math.max(ay, by, cy) / cellSize));
              for (let gy = minY; gy <= maxY; gy += 1) {
                for (let gx = minX; gx <= maxX; gx += 1) {
                  const px = gx * cellSize + cellSize / 2;
                  const py = gy * cellSize + cellSize / 2;
                  if (pointInTri(px, py, ax, ay, bx, by, cx, cy)) {
                    const idx = gy * cols + gx;
                    coverage[idx] = (coverage[idx] ?? 0) + 1;
                  }
                }
              }
            }
          }
        }
      }
      const overCovered = [...coverage].filter((c) => c > 1).length;
      expect(overCovered).toBe(0);
    }
  });
});

describe("buildHipsTileMeshGrid", () => {
  it("produces a stride x stride grid whose corner vertices project through worldToScreen consistently", () => {
    const tile: HipsTileKey = { order: 6, npix: ang2pix(NGC7000_RA_DEG, NGC7000_DEC_DEG, 6) };
    const subdivisions = 4;
    const grid = buildHipsTileMeshGrid(VIEWPORT, tile, subdivisions);
    const stride = subdivisions + 1;
    expect(grid.stride).toBe(stride);
    expect(grid.screenX.length).toBe(stride * stride);
    for (let idx = 0; idx < stride * stride; idx += 1) {
      const ra = grid.ra[idx];
      const dec = grid.dec[idx];
      expect(ra).toBeDefined();
      expect(dec).toBeDefined();
      if (ra === undefined || dec === undefined) continue;
      const [sx, sy] = worldToScreen(VIEWPORT, ra, dec);
      expect(grid.screenX[idx]).toBeCloseTo(sx, 6);
      expect(grid.screenY[idx]).toBeCloseTo(sy, 6);
    }
  });

  it("every grid vertex's RA/Dec lies within one tile diameter of the tile's own centre", () => {
    const order = 5;
    const npix = ang2pix(NGC7000_RA_DEG, NGC7000_DEC_DEG, order);
    const tile: HipsTileKey = { order, npix };
    const grid = buildHipsTileMeshGrid(VIEWPORT, tile, 6);
    const [centreRa, centreDec] = pixelCentreRaDec(npix, order);
    const centre = unitVector(centreRa, centreDec);
    const bound = maxPixelRadiusRad(order) * 2.5; // corner-to-corner, generous slack
    for (let idx = 0; idx < grid.ra.length; idx += 1) {
      const ra = grid.ra[idx];
      const dec = grid.dec[idx];
      if (ra === undefined || dec === undefined) continue;
      const angle = Math.acos(Math.max(-1, Math.min(1, dot(centre, unitVector(ra, dec)))));
      expect(angle).toBeLessThanOrEqual(bound);
    }
  });
});
