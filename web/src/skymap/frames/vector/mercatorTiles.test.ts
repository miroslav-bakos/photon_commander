// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Answers the imagery layer's own placement acceptance check as pure
 * arithmetic, no browser:
 * "M31/M42 must land on their catalogue positions against the frame's own
 * coordinate grid. State the error in pixels at two FOVs." — and "note
 * honestly how the curvature error behaves at wide FOV and near the zenith."
 *
 * The number under test is the gap between `worldToScreen`'s *exact*
 * projection of a real RA/Dec and `interpolatedScreenPosition`'s affine
 * (barycentric) interpolation of the same point *within the tile mesh's own
 * triangle* — i.e. the error a GPU rasterizer would actually show, since
 * that is the same affine interpolation a textured-triangle draw call does.
 * The Mercator forward/inverse pair being *mutual inverses* is assumed here
 * and not separately fuzzed — any error at a mesh *vertex* is exactly zero
 * by construction as long as that holds, so everything measured below is
 * subdivision/curvature error, not coordinate error. That is a weaker claim
 * than "the formula is correct": `757788c` shipped with self-consistent
 * (forward and inverse agreed with *each other*) but wrong (mirrored on
 * `x`, disagreeing with the real upstream) Mercator math, and every test in
 * this file passed anyway — self-consistency was never what was in
 * question, absolute correctness against the real tile server was, and
 * nothing here checked that until the ground-truth block at the bottom of
 * this file was added in response to that live defect.
 */
import { describe, expect, it } from "vitest";
import { horizontalCoordTransform, type SiteInstant, localMeanSiderealTimeHours } from "../shared/sidereal";
import { cosAngularDistanceFromViewCenter, worldToScreen, type Viewport } from "../../projection";
import {
  buildTileMeshFracGrid,
  everyGridVertexOnNearHemisphere,
  FAR_HEMISPHERE_COSC_MARGIN,
  filteredTileMeshTriangleIndices,
  interpolatedScreenPosition,
  pickZoom,
  tileFracFromRaDec,
  tileMeshTriangleIndices,
  raDecFromTileFrac,
  visibleTiles,
  type TileKey,
} from "./mercatorTiles";
import { screenToWorld } from "../../projection";

const WIDTH_PX = 1200;
const HEIGHT_PX = 800;
const SUBDIVISIONS = 8;

/** The tile (at `zoom`) that contains (`raDeg`, `decDeg`) — used by every
 * test below to find which tile mesh to interpolate against, exactly the
 * way `visibleTiles` + a point-in-tile test would in the real renderer. */
function tileContaining(zoom: number, raDeg: number, decDeg: number): TileKey {
  const size = 1 << zoom;
  const [xFrac, yFrac] = tileFracFromRaDec(raDeg, decDeg);
  return { z: zoom, x: Math.floor(xFrac * size), y: Math.floor(yFrac * size) };
}

function pixelError(viewport: Viewport, tile: TileKey, raDeg: number, decDeg: number): number {
  const exact = worldToScreen(viewport, raDeg, decDeg);
  const approx = interpolatedScreenPosition(viewport, tile, SUBDIVISIONS, raDeg, decDeg);
  expect(approx).not.toBeNull();
  if (approx === null) throw new Error("unreachable");
  return Math.hypot(exact[0] - approx[0], exact[1] - approx[1]);
}

const M31 = { raDeg: 10.6847, decDeg: 41.269, name: "M31" };
const M42 = { raDeg: 83.8221, decDeg: -5.3911, name: "M42" };

describe("mercatorTiles: placement accuracy (M31/M42, equatorial)", () => {
  for (const target of [M31, M42]) {
    for (const fovDeg of [60, 5]) {
      it(`${target.name} at ${String(fovDeg)}deg FOV, centred on the object`, () => {
        const viewport: Viewport = { view: { ra: target.raDeg, dec: target.decDeg, fovDeg }, widthPx: WIDTH_PX, heightPx: HEIGHT_PX };
        const zoom = pickZoom(fovDeg, WIDTH_PX, HEIGHT_PX);
        const tile = tileContaining(zoom, target.raDeg, target.decDeg);
        const errorPx = pixelError(viewport, tile, target.raDeg, target.decDeg);
        console.log(
          `[mercatorTiles] ${target.name} @ ${String(fovDeg)}deg FOV, zoom=${String(zoom)}, subdiv=${String(SUBDIVISIONS)}: ${errorPx.toFixed(3)}px`,
        );
        // Loose sanity bound (catches a broken formula, not a tuning target):
        // the mesh is built from the tile that contains the target, and the
        // target sits somewhere inside one subdivided cell of it, so the
        // error must be well under one cell's own screen size.
        expect(errorPx).toBeLessThan(50);
      });
    }
  }
});

describe("mercatorTiles: curvature error scales with tile angular size (equatorial)", () => {
  for (const fovDeg of [60, 5]) {
    it(`worst-case error over all visible tiles at ${String(fovDeg)}deg FOV`, () => {
      const view = { ra: 180, dec: 20, fovDeg };
      const viewport: Viewport = { view, widthPx: WIDTH_PX, heightPx: HEIGHT_PX };
      const zoom = pickZoom(fovDeg, WIDTH_PX, HEIGHT_PX);
      const tiles = visibleTiles(viewport, zoom, screenToWorld);
      let worst = 0;
      let worstAt: [number, number] = [0, 0];
      for (const tile of tiles) {
        const grid = buildTileMeshFracGrid(viewport, tile, SUBDIVISIONS);
        // Sample every cell's midpoint (in tile-fraction space) — the point
        // in a cell farthest, in parameter space, from every one of its own
        // four corners, so the worst place a straight-edge approximation can
        // be wrong.
        const size = 1 << tile.z;
        for (let j = 0; j < SUBDIVISIONS; j += 1) {
          for (let i = 0; i < SUBDIVISIONS; i += 1) {
            const xFrac = (tile.x + (i + 0.5) / SUBDIVISIONS) / size;
            const yFrac = (tile.y + (j + 0.5) / SUBDIVISIONS) / size;
            const [ra, dec] = raDecFromTileFrac(xFrac, yFrac);
            const approx = interpolatedScreenPosition(viewport, tile, SUBDIVISIONS, ra, dec);
            if (approx === null) continue;
            const exact = worldToScreen(viewport, ra, dec);
            const err = Math.hypot(exact[0] - approx[0], exact[1] - approx[1]);
            if (err > worst) {
              worst = err;
              worstAt = [ra, dec];
            }
          }
        }
        void grid; // built to confirm the mesh assembles without throwing; not otherwise inspected here.
      }
      console.log(
        `[mercatorTiles] equatorial ${String(fovDeg)}deg FOV: ${String(tiles.length)} tiles at zoom ${String(zoom)}, worst-case cell-midpoint error ${worst.toFixed(3)}px at ra=${worstAt[0].toFixed(2)} dec=${worstAt[1].toFixed(2)}`,
      );
      expect(tiles.length).toBeGreaterThan(0);
    });
  }
});

describe("mercatorTiles: curvature error near the zenith (horizontal projection)", () => {
  // A site/instant constructed so RA `zenithRaDeg` sits exactly on the
  // meridian at hour angle 0 and `dec == latitude`, i.e. the view centred
  // there is looking straight up — the single hardest case `main.ts`'s own
  // `hzCamera` doc comment describes (azimuth ill-conditioned at the pole of
  // the alt-az system), stacked here on top of the Mercator tile grid's own
  // pole (at dec=+-90) to see whether the two singularities compound.
  const LATITUDE_DEG = 34;
  const LONGITUDE_DEG = -118;
  const instant = new Date("2026-06-01T00:00:00Z");
  const lstDeg = localMeanSiderealTimeHours(instant, LONGITUDE_DEG) * 15;
  const site: SiteInstant = { latitudeDeg: LATITUDE_DEG, longitudeDeg: LONGITUDE_DEG, instant };
  const zenithRaDeg = lstDeg;
  const zenithDecDeg = LATITUDE_DEG;

  function worstCellError(viewCenterDecDeg: number, fovDeg: number): { worst: number; tiles: number; zoom: number } {
    const view = { ra: zenithRaDeg, dec: viewCenterDecDeg, fovDeg };
    const viewport: Viewport = { view, widthPx: WIDTH_PX, heightPx: HEIGHT_PX, coordTransform: horizontalCoordTransform(site) };
    const zoom = pickZoom(fovDeg, WIDTH_PX, HEIGHT_PX);
    const tiles = visibleTiles(viewport, zoom, screenToWorld);
    let worst = 0;
    for (const tile of tiles) {
      const size = 1 << tile.z;
      for (let j = 0; j < SUBDIVISIONS; j += 1) {
        for (let i = 0; i < SUBDIVISIONS; i += 1) {
          const xFrac = (tile.x + (i + 0.5) / SUBDIVISIONS) / size;
          const yFrac = (tile.y + (j + 0.5) / SUBDIVISIONS) / size;
          const [ra, dec] = raDecFromTileFrac(xFrac, yFrac);
          const approx = interpolatedScreenPosition(viewport, tile, SUBDIVISIONS, ra, dec);
          if (approx === null) continue;
          const exact = worldToScreen(viewport, ra, dec);
          const err = Math.hypot(exact[0] - approx[0], exact[1] - approx[1]);
          if (err > worst) worst = err;
        }
      }
    }
    return { worst, tiles: tiles.length, zoom };
  }

  it("60deg FOV: pointed at the zenith vs. pointed 45deg away from it", () => {
    const atZenith = worstCellError(zenithDecDeg, 60);
    const away = worstCellError(zenithDecDeg - 45, 60);
    console.log(
      `[mercatorTiles] horizontal 60deg FOV at zenith: ${String(atZenith.tiles)} tiles/zoom ${String(atZenith.zoom)}, worst ${atZenith.worst.toFixed(3)}px` +
        ` | 45deg off zenith: ${String(away.tiles)} tiles/zoom ${String(away.zoom)}, worst ${away.worst.toFixed(3)}px`,
    );
    expect(atZenith.tiles).toBeGreaterThan(0);
    expect(away.tiles).toBeGreaterThan(0);
  });

  it("5deg FOV: pointed at the zenith vs. pointed 45deg away from it", () => {
    const atZenith = worstCellError(zenithDecDeg, 5);
    const away = worstCellError(zenithDecDeg - 45, 5);
    console.log(
      `[mercatorTiles] horizontal 5deg FOV at zenith: ${String(atZenith.tiles)} tiles/zoom ${String(atZenith.zoom)}, worst ${atZenith.worst.toFixed(3)}px` +
        ` | 45deg off zenith: ${String(away.tiles)} tiles/zoom ${String(away.zoom)}, worst ${away.worst.toFixed(3)}px`,
    );
    expect(atZenith.tiles).toBeGreaterThan(0);
    expect(away.tiles).toBeGreaterThan(0);
  });
});

/**
 * Regression coverage for the live defect reported against `1eb2807`:
 * "draws fine but there is problem with stitching, the objects repeat
 * themselves cutted". Two independent mechanisms can produce exactly that
 * symptom — a far-hemisphere point still gets a finite, meaningless screen
 * position from `worldToScreen`'s deliberate clamp (its own doc comment),
 * and a Mercator x index that is not wrapped the way the tile proxy expects
 * would serve the same tile twice at two screen positions. Both are
 * checked here as pure arithmetic; neither test above could have caught
 * this, since both measured error at vertices of tiles *assumed* to be the
 * right, once-only set — never whether that assumption held.
 */
describe("mercatorTiles: no duplicated or far-hemisphere tiles (stitching defect regression)", () => {
  /** cos(angular distance) of a tile's own centre and four corners from
   * `viewport`'s view centre — the same five points `visibleTiles`'s own
   * `tileMayBeVisible` checks internally, re-derived here (not imported)
   * so this test exercises the *contract*, not the implementation. */
  function tileProbeCoscs(viewport: Viewport, tile: TileKey): number[] {
    const size = 1 << tile.z;
    const points: [number, number][] = [
      [tile.x / size, tile.y / size],
      [(tile.x + 1) / size, tile.y / size],
      [tile.x / size, (tile.y + 1) / size],
      [(tile.x + 1) / size, (tile.y + 1) / size],
      [(tile.x + 0.5) / size, (tile.y + 0.5) / size],
    ];
    return points.map(([xFrac, yFrac]) => {
      const [ra, dec] = raDecFromTileFrac(xFrac, yFrac);
      return cosAngularDistanceFromViewCenter(viewport, ra, dec);
    });
  }

  const site: SiteInstant = { latitudeDeg: 34, longitudeDeg: -118, instant: new Date("2026-06-01T00:00:00Z") };

  /** Scenarios chosen to stress each mechanism: a wide/short viewport (the
   * live defect's own likely trigger — see `imageryLayer.ts`'s
   * report on aspect ratio and `pickZoom`'s tile-size heuristic), the RA
   * 0h/24h seam, and the camera near the horizon in a real alt-az
   * (`coordTransform`) view — "where far-hemisphere tiles are most likely"
   * per the live report's own follow-up. */
  const scenarios: { label: string; viewport: Viewport }[] = [
    {
      label: "normal aspect, 60deg FOV",
      viewport: { view: { ra: 180, dec: 20, fovDeg: 60 }, widthPx: 1200, heightPx: 800 },
    },
    {
      label: "normal aspect, 5deg FOV",
      viewport: { view: { ra: 180, dec: 20, fovDeg: 5 }, widthPx: 1200, heightPx: 800 },
    },
    {
      label: "wide/short aspect, 60deg FOV (the live defect's own likely trigger)",
      viewport: { view: { ra: 180, dec: 0, fovDeg: 60 }, widthPx: 2400, heightPx: 200 },
    },
    {
      label: "RA 0h/24h seam, 60deg FOV",
      viewport: { view: { ra: 0.05, dec: 10, fovDeg: 60 }, widthPx: 1200, heightPx: 800 },
    },
    {
      label: "RA 0h/24h seam, wide/short aspect",
      viewport: { view: { ra: 359.9, dec: -10, fovDeg: 60 }, widthPx: 2400, heightPx: 200 },
    },
    {
      label: "horizontal projection, camera near the horizon",
      viewport: {
        view: { ra: 200, dec: -20, fovDeg: 60 },
        widthPx: 1200,
        heightPx: 800,
        coordTransform: horizontalCoordTransform(site),
      },
    },
    {
      label: "horizontal projection, camera near the horizon, wide/short aspect",
      viewport: {
        view: { ra: 200, dec: -20, fovDeg: 60 },
        widthPx: 2400,
        heightPx: 200,
        coordTransform: horizontalCoordTransform(site),
      },
    },
  ];

  for (const { label, viewport } of scenarios) {
    it(`${label}: no duplicate (z,x,y) and every tile has at least one near-hemisphere probe point`, () => {
      const zoom = pickZoom(viewport.view.fovDeg, viewport.widthPx, viewport.heightPx);
      const tiles = visibleTiles(viewport, zoom, screenToWorld);
      expect(tiles.length).toBeGreaterThan(0); // a fix that leaves a hole in the sky is not a fix.

      const size = 1 << zoom;
      const seen = new Set<string>();
      for (const tile of tiles) {
        const key = `${String(tile.z)},${String(tile.x)},${String(tile.y)}`;
        expect(seen.has(key)).toBe(false); // no duplicate (z,x,y): mechanism 2 (index wrap) would show up here.
        seen.add(key);

        // Mercator x wraps modulo 2**z (RA is cyclic); y never does (dec is
        // not) — ssc/skymap/mercator.py's own convention, checked directly
        // against what this module actually emits, not just inspected.
        expect(tile.x).toBeGreaterThanOrEqual(0);
        expect(tile.x).toBeLessThan(size);
        expect(tile.y).toBeGreaterThanOrEqual(0);
        expect(tile.y).toBeLessThan(size);

        // Mechanism 1: no tile whose entire extent (all five probe points)
        // is on the far hemisphere — this is what `visibleTiles` failed
        // before today's fix (verified live: a wide/short 60deg-FOV
        // viewport returned tiles with a centre cosc of -0.53, unambiguously
        // the opposite side of the sky, before this test existed).
        const coscs = tileProbeCoscs(viewport, tile);
        expect(coscs.some((c) => c > FAR_HEMISPHERE_COSC_MARGIN)).toBe(true);
      }
    });
  }

  it("RA 0h/24h seam: both seam-adjacent tiles (x=0 and x=size-1) are selected exactly once", () => {
    const viewport: Viewport = { view: { ra: 0, dec: 0, fovDeg: 60 }, widthPx: 1200, heightPx: 800 };
    const zoom = pickZoom(viewport.view.fovDeg, viewport.widthPx, viewport.heightPx);
    const size = 1 << zoom;
    const tiles = visibleTiles(viewport, zoom, screenToWorld);
    const keys = tiles.map((t) => `${String(t.x)},${String(t.y)}`);
    expect(new Set(keys).size).toBe(keys.length); // no duplicates.
    const hasColumn0 = tiles.some((t) => t.x === 0);
    const hasLastColumn = tiles.some((t) => t.x === size - 1);
    expect(hasColumn0).toBe(true);
    expect(hasLastColumn).toBe(true);
  });

  it("a tile mesh straddling the 90deg circle: the naive full triangle set includes a far-hemisphere triangle, the filtered set does not", () => {
    // A tile deliberately much larger than the viewport (zoom 1: each tile
    // spans a quarter of the whole sky), forced directly rather than
    // through `visibleTiles`/`pickZoom`, so this test isolates the
    // mesh-culling functions from tile-selection policy — a controlled
    // reproduction of "a tile straddling the 90deg circle", independent of
    // which aspect ratio happens to pick a coarse zoom in practice.
    const viewport: Viewport = { view: { ra: 180, dec: 0, fovDeg: 60 }, widthPx: 1200, heightPx: 800 };
    const tile: TileKey = { z: 1, x: 1, y: 1 }; // contains the view centre (verified: its own centre corner is (180, 0)).
    const subdivisions = 8;
    const grid = buildTileMeshFracGrid(viewport, tile, subdivisions);

    expect(everyGridVertexOnNearHemisphere(viewport, grid)).toBe(false); // confirms this tile actually straddles the circle.

    const naive = tileMeshTriangleIndices(subdivisions);
    const filtered = filteredTileMeshTriangleIndices(viewport, grid, subdivisions);

    const hasFarVertex = (indices: ArrayLike<number>): boolean => {
      for (let k = 0; k < indices.length; k += 3) {
        for (let v = 0; v < 3; v += 1) {
          const idx = indices[k + v] ?? 0;
          const ra = grid.ra[idx] ?? 0;
          const dec = grid.dec[idx] ?? 0;
          if (cosAngularDistanceFromViewCenter(viewport, ra, dec) <= FAR_HEMISPHERE_COSC_MARGIN) return true;
        }
      }
      return false;
    };

    // This is the defect itself, reproduced directly: the naive set (what
    // `imageryLayer.ts` drew for every tile before today's fix)
    // includes at least one triangle with a far-hemisphere vertex, which
    // `worldToScreen` would have clamped to a finite, meaningless position
    // and rasterised anyway — a duplicated/cut-looking fragment on screen.
    expect(hasFarVertex(naive)).toBe(true);
    // The fix: none survive filtering, and filtering actually removed some.
    expect(hasFarVertex(filtered)).toBe(false);
    expect(filtered.length).toBeLessThan(naive.length);
    expect(filtered.length).toBeGreaterThan(0); // the tile's real, near-hemisphere part must still draw.
  });
});

/**
 * Regression coverage for the live defect reported *after* `757788c`
 * (which fixed a real but different bug — far-hemisphere culling — and
 * still shipped this one): "M31 appears twice ... the background star
 * field is completely seamless." Root cause: `tileFracFromRaDec`'s `x`
 * formula was mirrored relative to the real upstream server (`x = ra/360`
 * instead of the correct `x = 1 - ra/360`) — self-consistent with its own
 * inverse, so every geometry/placement/curvature test above (all of which
 * only ever check internal consistency — "does this land where the
 * formula itself predicts", never "does the formula match the real tile
 * server") passed while this was live and visibly broken on screen.
 *
 * A dense star field is self-similar, so a wrong-but-plausible-looking
 * `(x, y)` mostly fetches a tile that *looks* correct even though it names
 * the wrong sky position — invisible until a unique object (M31) lands on
 * the wrong tile and a fragment of it appears somewhere it should not.
 * Guarding against a repeat needs a check against ground truth, not
 * self-consistency: this hardcodes a `(zoom, x, y)` verified by directly
 * fetching it from the real, live upstream (2026-09-09) —
 * `https://c.legacysurvey.org/viewer/unwise-neo7/1/8/248/95.jpg` shows M31
 * filling the frame with its companion galaxy in the corner, matching
 * `legacysurvey.org/viewer`'s own RA/Dec cutout endpoint for the same
 * coordinates; the pre-fix formula's own answer for the same input,
 * `.../8/7/95.jpg`, is an unrelated, ordinary star field. See
 * `tileFracFromRaDec`'s own doc comment for the rest of the verification
 * (including why only `x` needed correcting, not `y`).
 */
describe("mercatorTiles: tile address matches the real upstream (M31 ground truth)", () => {
  it("M31 (RA 10.6847, Dec 41.269) resolves to the verified-live tile (8, 248, 95), not the mirrored (8, 7, 95)", () => {
    const zoom = 8;
    const size = 1 << zoom;
    const [xFrac, yFrac] = tileFracFromRaDec(10.6847, 41.269);
    const x = Math.floor(xFrac * size);
    const y = Math.floor(yFrac * size);
    expect({ x, y }).toEqual({ x: 248, y: 95 });
  });

  it("round-trips through raDecFromTileFrac back to the same tile", () => {
    const zoom = 8;
    const size = 1 << zoom;
    const [ra, dec] = raDecFromTileFrac((248 + 0.5) / size, (95 + 0.5) / size);
    // Loose bound: this is the tile's own *centre*, not M31's exact
    // coordinates — it only needs to land inside the same, single tile.
    const [xFrac, yFrac] = tileFracFromRaDec(ra, dec);
    expect(Math.floor(xFrac * size)).toBe(248);
    expect(Math.floor(yFrac * size)).toBe(95);
  });
});
