// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Web Mercator tile math for the vector frame's own background/overlay
 * imagery (`imageryLayer.ts`) — grown out of a spike ("can the vector frame
 * draw survey tiles reprojected into its alt-az view, in the right place,
 * fast enough?", skymap-viz brief 2026-09-09) that proved projection,
 * stitching and frame cost before this became the shipped renderer
 * (skymap-viz brief 2026-09-09, "turn the imagery spike into a real
 * background layer"). Wired into `SkyMap.tsx`/`setSurveys`/`setLayers` via
 * `imageryLayer.ts`, which is `frames/vector/main.ts`'s own call site.
 *
 * Scope: any **Mercator-scheme** survey the registry offers
 * (`ssc/skymap/surveys.py`'s `TileScheme.MERCATOR` — today `unwise` and
 * `legacy-surveys-dr11`), addressed by the registry's own
 * `tileUrlTemplate` (`mercatorTileUrl` below) — never a survey id or host
 * hardcoded here. Every template is same-origin, this frame's own tile
 * proxy (`ssc/api/routes/skymap.py`'s `mercator_tile`) — never
 * legacysurvey.org or any other archive. `isRestrictedImagesetName` still
 * gates which survey may reach this module at all (`imageryLayer.ts`'s
 * `refusalForImagery`), the same belt-and-braces check every other backend
 * makes before drawing a name it was handed.
 *
 * Math split from rendering on purpose: `tileFracFromRaDec`/
 * `raDecFromTileFrac` mirror `ssc/skymap/mercator.py`'s `_x_frac`/`_y_frac`
 * (forward) and its Web-Mercator inverse (the standard Gudermannian —
 * used by every XYZ slippy-map viewer, the same convention the module
 * docstring there verifies against Legacy Surveys' own `get_tile_wcs`), so
 * the placement question is answerable as pure arithmetic and unit-tested
 * without a browser (`mercatorTiles.test.ts`). `buildTileMeshFracGrid` and
 * `interpolatedScreenPosition` are shared by that test and by
 * `imageryLayer.ts`'s WebGL path, so the tessellation the renderer actually
 * draws is exactly the tessellation the accuracy numbers describe — not a
 * second guess at it.
 */
import { worldToScreen, type Viewport } from "../../projection";
import { isNearHemispherePoint, type TileMeshGrid } from "./tileMesh";

// Re-exported for backward compatibility: every consumer of these four
// (this module's own tests included) imported them from here before they
// moved to `tileMesh.ts` to be shared with `hipsTiles.ts` — see that
// module's own doc comment for why the split happened.
export {
  everyGridVertexOnNearHemisphere,
  FAR_HEMISPHERE_COSC_MARGIN,
  filteredTileMeshTriangleIndices,
  tileMeshTriangleIndices,
  type TileMeshGrid,
} from "./tileMesh";

/** Web Mercator's own polar clipping latitude — see `ssc/skymap/mercator.py`'s
 * `MAX_LAT_DEG` docstring for the derivation. */
export const MERCATOR_MAX_LAT_DEG = 85.05112878;

/** Legacy Surveys' own viewer's declared max zoom (`ssc/skymap/mercator.py`'s
 * `MAX_ZOOM`) — the deepest tile this module will ever ask for. */
export const MERCATOR_MAX_ZOOM = 16;

const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;

/**
 * RA/Dec, degrees -> Mercator tile-fraction space `[0,1)x[0,1)`. `y`
 * increases south, the ordinary XYZ slippy-map convention. `x` increases
 * *west* (RA decreasing), the opposite of the naive assumption this
 * function shipped with in `757788c` (`x = ra/360`, "x increasing east" —
 * a plausible-looking guess, never checked against the real server, and
 * wrong) and the direct cause of the live "M31 appears twice, cropped
 * differently, background seamless" defect the owner found on SIT: every
 * tile address this function computed pointed at the *mirror image* of the
 * intended sky position, and a self-similar star field looks the same
 * either way — only a unique object like M31 made the wrong-tile fetch
 * visible.
 *
 * Verified directly against the live upstream, not re-derived from a
 * docstring (2026-09-09): `decals-web`'s own `map/utils.py`
 * (`get_tile_wcs`, `github.com/legacysurvey/decals-web`) builds each
 * tile's WCS with `rx = zoomscale/2 - x` — note the sign, `scale/2 - x`,
 * not `x - scale/2` — referenced against `anwcs_create_mercator_2(180, 0,
 * ...)`; working through what that implies for pixel-x vs RA, then
 * confirming empirically: fetching
 * `https://c.legacysurvey.org/viewer/unwise-neo7/1/8/7/95.jpg` (the address
 * this function's old formula computed for M31, RA 10.6847 Dec 41.269) is
 * an ordinary, unrelated star field, while
 * `.../unwise-neo7/1/8/248/95.jpg` — `255 - 7`, mirroring only `x`, `y`
 * unchanged — shows M31 filling the frame, its companion galaxy in the
 * corner, exactly as `legacysurvey.org/viewer`'s own RA/Dec-driven cutout
 * endpoint shows for the same coordinates. `y` needed no correction: the
 * unchanged `y=95` already put M31 at the tile's own centre, and
 * `ssc/skymap/mercator.py`'s own module doc already recorded an
 * independent check of `y` against WWT's `MercatorTile` (this module's own
 * verification only had reason to doubt `x`).
 *
 * `ssc/skymap/mercator.py`'s `_x_frac`/`cone_tiles_by_zoom` use the same
 * unmirrored formula this function shipped with — flagged to skymap-tiles
 * as a separate, *not yet confirmed*, possible bug in cone/prefetch tile
 * selection; out of scope here (`ssc/skymap/**` is not owned by this
 * agent) and not fixed as part of this change. It cannot be the cause of
 * *this* defect: nothing in `ssc/skymap/mercator.py` decides which tile a
 * background-imagery request actually serves — the client's own computed
 * `(x, y)` is proxied verbatim (`ssc/api/routes/skymap.py`'s
 * `mercator_tile`) — `_x_frac` there is used only for cache-prefetch
 * estimation, a different feature this module does not touch.
 */
export function tileFracFromRaDec(raDeg: number, decDeg: number): [xFrac: number, yFrac: number] {
  const xFrac = (1 - ((raDeg % 360) + 360) % 360 / 360) % 1;
  const lat = Math.max(-MERCATOR_MAX_LAT_DEG, Math.min(MERCATOR_MAX_LAT_DEG, decDeg));
  const yFrac = (1 - Math.asinh(Math.tan(lat * DEG2RAD)) / Math.PI) / 2;
  return [xFrac, yFrac];
}

/** Inverse of `tileFracFromRaDec` — same mirrored-`x` correction, same
 * unchanged Web Mercator inverse (Gudermannian function) for `y`/dec. See
 * `tileFracFromRaDec`'s own doc comment for how the mirror was found and
 * verified. */
export function raDecFromTileFrac(xFrac: number, yFrac: number): [raDeg: number, decDeg: number] {
  const raDeg = (360 - ((xFrac % 1) + 1) % 1 * 360) % 360;
  const decDeg = Math.atan(Math.sinh(Math.PI * (1 - 2 * yFrac))) * RAD2DEG;
  return [raDeg, decDeg];
}

/** Chooses a zoom level so that roughly `targetTilesAcross` tiles span the
 * viewport's own width — not WWT's own `baseTileDegrees` heuristic (which
 * existed to satisfy its internal tiling loop, not this, before it was
 * removed with the rest of that engine, `THIRD_PARTY_NOTICES.md` §9). Clamped to
 * `[0, MERCATOR_MAX_ZOOM]`: below 5deg FOV the naive pixel-per-tile formula
 * would ask for zoom levels the upstream does not have, which would just
 * 404 forever, not "more detail". */
export function pickZoom(fovDeg: number, widthPx: number, heightPx: number, targetTilesAcross = 6): number {
  const fovWidthDeg = (fovDeg * widthPx) / Math.max(1, heightPx);
  const tileDeg = Math.max(1e-6, fovWidthDeg / targetTilesAcross);
  const z = Math.round(Math.log2(360 / tileDeg));
  return Math.max(0, Math.min(MERCATOR_MAX_ZOOM, z));
}

export interface TileKey {
  z: number;
  x: number;
  y: number;
}

/** Every tile at `zoom` whose fraction-space box may intersect the
 * viewport, found the same way `screenToWorld`-based hit-testing elsewhere
 * in this frame works: sample a grid of screen points, unproject each
 * through *this viewport's own* projection (so alt-az/`coordTransform`,
 * roll and FOV are all already accounted for — the same seam every other
 * consumer of `Viewport` in this frame goes through), and collect the tiles
 * those points fall in. A one-tile margin on every side absorbs a sample
 * grid that is coarser than the viewport's true curvature — cheap insurance
 * against a visible gap at the edge, the same trade `cone_tiles_by_zoom`
 * (`ssc/skymap/mercator.py`) makes with its own one-tile margin.
 *
 * A candidate none of whose centre or four corners is on the near
 * hemisphere is dropped here, before it is ever fetched or drawn: fetching
 * it would be bandwidth for nothing and, worse, a near-guaranteed clamp
 * artefact once drawn (see `FAR_HEMISPHERE_COSC_MARGIN`). Checked at five
 * points, not the tile's centre alone: at a coarse zoom (few, huge tiles —
 * a tile can span most of a hemisphere) a tile's own *geometric* centre can
 * sit far from the small part of it actually facing the viewer even though
 * a corner (or the true centre-of-view point, which is always one of the
 * five for whichever tile contains it) does not — checking the centre
 * alone was verified live to reject the one tile actually covering the
 * view centre at zoom 1, which would have left a hole in the sky instead of
 * fixing a duplicate. This is deliberately still a coarse, whole-tile test,
 * biased toward keeping a tile when in doubt: it only ever removes a tile
 * unambiguously wrong at every one of the five points, never one that might
 * be genuinely partly visible; a tile that straddles the 90deg circle
 * (survives this check, but has some far corner) is culled per-triangle at
 * draw time instead (`imageryLayer.ts`), which is the only way to
 * keep its real, near-hemisphere part on screen without either drawing a
 * clamp artefact or dropping visible imagery.
 */
export function visibleTiles(
  viewport: Viewport,
  zoom: number,
  screenToWorld: (viewport: Viewport, x: number, y: number) => [number, number],
  sampleGrid = 9,
): TileKey[] {
  const size = 1 << zoom;
  const seen = new Set<string>();
  const tiles: TileKey[] = [];
  const tileMayBeVisible = (x: number, y: number): boolean => {
    const corners: [number, number][] = [
      [x / size, y / size],
      [(x + 1) / size, y / size],
      [x / size, (y + 1) / size],
      [(x + 1) / size, (y + 1) / size],
      [(x + 0.5) / size, (y + 0.5) / size],
    ];
    return corners.some(([xFrac, yFrac]) => {
      const [ra, dec] = raDecFromTileFrac(xFrac, yFrac);
      return isNearHemispherePoint(viewport, ra, dec);
    });
  };
  const add = (rawX: number, y: number): void => {
    if (y < 0 || y >= size) return;
    const x = ((rawX % size) + size) % size;
    const key = `${String(x)},${String(y)}`;
    if (seen.has(key)) return;
    if (!tileMayBeVisible(x, y)) return;
    seen.add(key);
    tiles.push({ z: zoom, x, y });
  };
  for (let row = 0; row <= sampleGrid; row += 1) {
    for (let col = 0; col <= sampleGrid; col += 1) {
      const sx = (col / sampleGrid) * viewport.widthPx;
      const sy = (row / sampleGrid) * viewport.heightPx;
      const [ra, dec] = screenToWorld(viewport, sx, sy);
      const [xFrac, yFrac] = tileFracFromRaDec(ra, dec);
      const tx = Math.floor(xFrac * size);
      const ty = Math.floor(yFrac * size);
      for (let dy = -1; dy <= 1; dy += 1) {
        for (let dx = -1; dx <= 1; dx += 1) add(tx + dx, ty + dy);
      }
    }
  }
  return tiles;
}

/** One tile's tessellation grid, in tile-fraction space plus this
 * viewport's exact screen projection of every grid vertex —
 * `(subdivisions+1) x (subdivisions+1)` vertices. `subdivisions` is the
 * curvature/cost knob measured directly: too coarse and
 * a "rectangle" tile draws with visible straight-edge error near the pole or
 * at wide FOV (`skymap-viz.md`'s spherical-geometry hazard, for a tile
 * instead of a FOV rectangle); too fine and the vertex/triangle count costs
 * frame time for no visible gain. */
export function buildTileMeshFracGrid(viewport: Viewport, tile: TileKey, subdivisions: number): TileMeshGrid {
  const stride = subdivisions + 1;
  const size = 1 << tile.z;
  const screenX = new Float64Array(stride * stride);
  const screenY = new Float64Array(stride * stride);
  const ra = new Float64Array(stride * stride);
  const dec = new Float64Array(stride * stride);
  for (let j = 0; j < stride; j += 1) {
    const yFrac = (tile.y + j / subdivisions) / size;
    for (let i = 0; i < stride; i += 1) {
      const xFrac = (tile.x + i / subdivisions) / size;
      const [r, d] = raDecFromTileFrac(xFrac, yFrac);
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

/**
 * Where a real point (`targetRaDeg`, `targetDecDeg`) would land on screen if
 * drawn as this tile mesh's own triangles would rasterize it — i.e. affine
 * (barycentric) interpolation of the *screen* corners of whichever triangle
 * the point's tile-fraction position falls in, using the same diagonal
 * `tileMeshTriangleIndices` cuts. This is the mesh's own approximation of
 * the true curved projection; comparing it against `worldToScreen`'s exact
 * projection of the same point is the pixel-error number this module's own
 * acceptance check asks for (M31/M42 "against the frame's own coordinate
 * grid").
 *
 * `null` if the point does not fall inside this tile.
 */
export function interpolatedScreenPosition(
  viewport: Viewport,
  tile: TileKey,
  subdivisions: number,
  targetRaDeg: number,
  targetDecDeg: number,
): [x: number, y: number] | null {
  const size = 1 << tile.z;
  const [xFrac, yFrac] = tileFracFromRaDec(targetRaDeg, targetDecDeg);
  const localX = xFrac * size - tile.x;
  const localY = yFrac * size - tile.y;
  if (localX < 0 || localX >= 1 || localY < 0 || localY >= 1) return null;

  const cellXf = localX * subdivisions;
  const cellYf = localY * subdivisions;
  const i = Math.min(subdivisions - 1, Math.floor(cellXf));
  const j = Math.min(subdivisions - 1, Math.floor(cellYf));
  const fx = cellXf - i;
  const fy = cellYf - j;

  const corner = (ci: number, cj: number): [number, number] => {
    const gx = (tile.x + ci / subdivisions) / size;
    const gy = (tile.y + cj / subdivisions) / size;
    const [r, d] = raDecFromTileFrac(gx, gy);
    return worldToScreen(viewport, r, d);
  };
  const p00 = corner(i, j);
  const p10 = corner(i + 1, j);
  const p01 = corner(i, j + 1);
  const p11 = corner(i + 1, j + 1);

  let a: [number, number];
  let b: [number, number];
  let c: [number, number];
  let wa: number;
  let wb: number;
  let wc: number;
  if (fx + fy <= 1) {
    a = p00;
    b = p10;
    c = p01;
    wb = fx;
    wc = fy;
    wa = 1 - wb - wc;
  } else {
    const gx = 1 - fx;
    const gy = 1 - fy;
    a = p11;
    b = p01;
    c = p10;
    wb = gx;
    wc = gy;
    wa = 1 - wb - wc;
  }
  return [a[0] * wa + b[0] * wb + c[0] * wc, a[1] * wa + b[1] * wb + c[1] * wc];
}

/**
 * A Mercator-scheme survey's own `tileUrlTemplate` (`SkySurvey.tileUrlTemplate`,
 * `{level}`/`{x}`/`{y}` placeholders — `ssc.skymap.surveys.client_tile_template`),
 * with this tile's own coordinates substituted in. WWT's own `imagesets.ts`
 * did the same substitution for its own placeholder names, kept in its own
 * module rather than shared because it also imported `@wwtelescope/engine`
 * types, which had to never reach this chunk (the bundle-separation gate) —
 * moot now that WWT is gone (`THIRD_PARTY_NOTICES.md` §9), but this function
 * stays the vector frame's own regardless. The template itself is always
 * same-origin (the registry only ever emits this frame's own tile proxy
 * path); this function never takes a caller-supplied host.
 */
export function mercatorTileUrl(template: string, tile: TileKey): string {
  return template
    .replace("{level}", String(tile.z))
    .replace("{x}", String(tile.x))
    .replace("{y}", String(tile.y));
}
