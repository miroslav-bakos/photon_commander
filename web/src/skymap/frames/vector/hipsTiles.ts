// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * HiPS (HEALPix, NESTED) tile math for the vector frame's own
 * background/overlay imagery (`imageryLayer.ts`) — the HiPS counterpart of
 * `mercatorTiles.ts`, added so the vector frame can draw the registry's
 * HiPS-scheme surveys (skymap-viz brief, "NSNS OIII/H-alpha/SII (composite)
 * is dead on skymap (vector)" — every survey but `unwise` and
 * `legacy-surveys-dr11` is HiPS: `nsns-ohs8`, `nsns-halpha`, `nsns-oiii`,
 * `nsns-sii`, `wham-halpha`, plus the disabled `wise`/`sdss` rows).
 *
 * A HiPS tile is a diamond-ish cell in a HEALPix pyramid, not a cell in a
 * lat/long grid, so tile *selection* and *mesh generation* are genuinely
 * new (they cannot reuse `mercatorTiles.ts`'s fraction-space arithmetic);
 * the triangulation/culling of whatever grid this module produces is
 * exactly `mercatorTiles.ts`'s own problem and is shared, unchanged, via
 * `tileMesh.ts`.
 *
 * The nested-HEALPix addressing below (`ang2pix`, the bit
 * compress/spread pair, the `_JRLL`/`_JPLL` face tables, the continuous
 * face-xy -> RA/Dec inverse, `maxPixelRadiusRad`) is a direct line-for-line
 * port of `ssc/skymap/hips.py` — same reference algorithm (Górski et al.
 * 2005 `healpix_base`), same conventions, deliberately not re-derived, so
 * the two addressing schemes cannot drift the way architecture policy warns about for
 * `astro/`. `ssc/skymap/hips.py` uses numpy SWAR bit tricks (`_compress_bits`/
 * `_spread_bits`) that only stay correct up to 32 bits; this module uses
 * plain arithmetic bit-by-bit instead (see `compressBits`/`spreadBits`
 * below) — an implementation difference for JS's 32-bit bitwise operators,
 * not a difference in what is computed, and irrelevant in practice since
 * every registry entry's `maxOrder` is well under either limit (6 for NSNS).
 *
 * Tile *selection* (`visibleHipsTiles`) does not attempt a Mercator-style
 * neighbour walk (`mercatorTiles.ts`'s `visibleTiles`, `x+-1`/`y+-1`
 * padding): a NESTED pixel's neighbours are not simply adjacent indices
 * across a HEALPix face boundary, and getting that wrong is exactly the
 * class of "mirrored/misaddressed tile" defect `mercatorTiles.ts`'s own
 * doc comment describes for the Mercator `x` bug. Instead this mirrors
 * `ssc/skymap/hips.py`'s own `cone_tiles_by_order`: a hierarchical descent
 * from the 12 base pixels, at each order keeping only pixels within
 * `radius + max_pixel_radius_rad(order)` of the query axis — inclusive and
 * monotone in `radius` by construction, and correct at every face boundary
 * and pole without any special-casing, because it never reasons about
 * neighbours at all.
 */
import { worldToScreen, type Viewport } from "../../projection";
import type { TileMeshGrid } from "./tileMesh";

const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;
const HALFPI = Math.PI / 2;

export type HipsFrame = "equatorial" | "galactic";
type Vector3 = readonly [number, number, number];

/** IAU Galactic rotation (J2000). Rows take an equatorial vector to a
 * Galactic vector; the transpose performs the inverse. */
const EQ_TO_GAL: readonly [Vector3, Vector3, Vector3] = [
  [-0.0548755604, -0.8734370902, -0.4838350155],
  [0.4941094279, -0.4448296300, 0.7469822445],
  [-0.867666149, -0.1980763734, 0.4559837762],
];

/** `ssc/skymap/hips.py`'s own `_JRLL`/`_JPLL` — the HEALPix reference
 * implementation's per-face ring/phase tables, verbatim. */
const JRLL: readonly number[] = [2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4];
const JPLL: readonly number[] = [1, 3, 5, 7, 0, 2, 4, 6, 1, 3, 5, 7];

/** One HiPS tile: a NESTED HEALPix pixel `npix` at `order`. */
export interface HipsTileKey {
  order: number;
  npix: number;
}

/** Bit *i* of `value` moves to bit `2*i` (nested HEALPix interleave) — the
 * same operation as `ssc/skymap/hips.py`'s `_spread_bits`, done bit-by-bit
 * with plain arithmetic instead of numpy SWAR masks so it stays correct
 * for any `value` representable exactly as a JS double, not only up to 32
 * bits (see this module's own doc comment). */
function spreadBits(value: number): number {
  let result = 0;
  let remaining = value;
  let bitPos = 0;
  while (remaining > 0) {
    if (remaining % 2 === 1) result += 2 ** (2 * bitPos);
    remaining = Math.floor(remaining / 2);
    bitPos += 1;
  }
  return result;
}

/** Inverse of `spreadBits`: keep the even-position bits of `value` and pack
 * them together — `ssc/skymap/hips.py`'s `_compress_bits`, same
 * arbitrary-magnitude-safe rewrite. */
function compressBits(value: number): number {
  let result = 0;
  let remaining = value;
  let bitPos = 0;
  while (remaining > 0) {
    if (remaining % 2 === 1) result += 2 ** bitPos;
    remaining = Math.floor(remaining / 4);
    bitPos += 1;
  }
  return result;
}

/** The NESTED HEALPix pixel at `order` containing (RA, Dec) in degrees —
 * `ssc/skymap/hips.py`'s `ang2pix`, verbatim algorithm. Ground-truth
 * checked (`hipsTiles.test.ts`) against the same anchor the survey
 * registry itself was verified against: NGC 7000 (20h58m47s +44d31m),
 * order 6, npix 15363 (`ssc/skymap/surveys.py`'s `NSNS_HALPHA` doc
 * comment, fetched live from `simg.de` 2026-09-09). */
export function ang2pix(raDeg: number, decDeg: number, order: number): number {
  const nside = 2 ** order;
  const z = Math.sin(decDeg * DEG2RAD);
  const za = Math.abs(z);
  const phi = (((raDeg % 360) + 360) % 360) * DEG2RAD;
  const tt = (phi / HALFPI) % 4;

  let face: number;
  let ix: number;
  let iy: number;
  if (za <= 2 / 3) {
    const temp1 = nside * (0.5 + tt);
    const temp2 = nside * z * 0.75;
    const jp = Math.floor(temp1 - temp2);
    const jm = Math.floor(temp1 + temp2);
    const ifp = Math.floor(jp / nside);
    const ifm = Math.floor(jm / nside);
    face = ifp === ifm ? (ifp | 4) : ifp < ifm ? ifp : ifm + 8;
    ix = jm % nside;
    iy = nside - (jp % nside) - 1;
  } else {
    const ntt = Math.min(3, Math.floor(tt));
    const tp = tt - ntt;
    const tmp = nside * Math.sqrt(3 * (1 - za));
    const jp = Math.min(Math.floor(tp * tmp), nside - 1);
    const jm = Math.min(Math.floor((1 - tp) * tmp), nside - 1);
    if (z >= 0) {
      face = ntt;
      ix = nside - jm - 1;
      iy = nside - jp - 1;
    } else {
      face = ntt + 8;
      ix = jp;
      iy = jm;
    }
  }
  return face * nside * nside + spreadBits(ix) + spreadBits(iy) * 2;
}

/** Decomposes a NESTED pixel index into its face and integer `(ix, iy)`
 * within that face at `order` — the inverse of `ang2pix`'s own packing,
 * mirroring `ssc/skymap/hips.py`'s `pixel_centres`. */
export function pixelFaceXy(npix: number, order: number): { face: number; ix: number; iy: number } {
  const nside = 2 ** order;
  const faceSize = nside * nside;
  const face = Math.floor(npix / faceSize);
  const remainder = npix - face * faceSize;
  const ix = compressBits(remainder);
  const iy = compressBits(Math.floor(remainder / 2));
  return { face, ix, iy };
}

/**
 * The continuous HEALPix bijection from a face and a *continuous* position
 * within it (`xFrac`, `yFrac`, both in `[0, 1]` across the whole face side)
 * to (RA, Dec) in degrees — `ssc/skymap/hips.py`'s `pixel_centres` formula
 * generalised from pixel centres (`x = (ix+0.5)/nside`) to any point in the
 * unit square, which is what that formula already computes (Górski et al.
 * 2005's own continuous face-xy -> (theta, phi) map; `pixel_centres`
 * evaluates it only at the half-integer offsets a pixel *centre* needs).
 * This is the function that turns a HiPS tile's own corners and subdivided
 * mesh into real sky positions (`buildHipsTileMeshGrid` below).
 */
export function faceXyToRaDec(face: number, xFrac: number, yFrac: number): [raDeg: number, decDeg: number] {
  const jrll = JRLL[face] ?? 0;
  const jpll = JPLL[face] ?? 0;
  const jr = jrll - xFrac - yFrac;
  const north = jr < 1;
  const south = jr > 3;
  const nr = north ? jr : south ? 4 - jr : 1;
  const z = north ? 1 - (jr * jr) / 3 : south ? (4 - jr) ** 2 / 3 - 1 : ((2 - jr) * 2) / 3;
  let tmp = jpll * nr + xFrac - yFrac;
  tmp = ((tmp % 8) + 8) % 8;
  const phi = (0.5 * HALFPI * tmp) / nr;
  const dec = Math.asin(Math.max(-1, Math.min(1, z)));
  const ra = ((phi * RAD2DEG) % 360 + 360) % 360;
  return [ra, dec * RAD2DEG];
}

/** The RA/Dec centre of NESTED pixel `npix` at `order`. */
export function pixelCentreRaDec(npix: number, order: number): [raDeg: number, decDeg: number] {
  const nside = 2 ** order;
  const { face, ix, iy } = pixelFaceXy(npix, order);
  return faceXyToRaDec(face, (ix + 0.5) / nside, (iy + 0.5) / nside);
}

/** An upper bound on the centre-to-corner distance of any pixel at `order`
 * — `ssc/skymap/hips.py`'s `max_pixel_radius_rad`, verbatim: the worst case
 * is a pixel on the equator/polar-cap boundary. */
export function maxPixelRadiusRad(order: number): number {
  const nside = 2 ** order;
  const za = 2 / 3;
  const pa = Math.PI / (4 * nside);
  const t1 = (1 - 1 / nside) ** 2;
  const zb = 1 - t1 / 3;
  const sinA = Math.sqrt(Math.max(0, 1 - za * za));
  const va: [number, number, number] = [sinA * Math.cos(pa), sinA * Math.sin(pa), za];
  const sinB = Math.sqrt(Math.max(0, 1 - zb * zb));
  const vb: [number, number, number] = [sinB, 0, zb];
  const dot = va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2];
  return Math.acos(Math.max(-1, Math.min(1, dot)));
}

function unitVector(raDeg: number, decDeg: number): [number, number, number] {
  const ra = raDeg * DEG2RAD;
  const dec = decDeg * DEG2RAD;
  return [Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec)];
}

function vectorToLonLat(vector: readonly [number, number, number]): [number, number] {
  const lon = ((Math.atan2(vector[1], vector[0]) * RAD2DEG) % 360 + 360) % 360;
  return [lon, Math.asin(Math.max(-1, Math.min(1, vector[2]))) * RAD2DEG];
}

export function equatorialToSurvey(raDeg: number, decDeg: number, frame: HipsFrame): [number, number] {
  if (frame === "equatorial") return [raDeg, decDeg];
  const v = unitVector(raDeg, decDeg);
  const [row0, row1, row2] = EQ_TO_GAL;
  return vectorToLonLat([
    dot3(row0, v),
    dot3(row1, v),
    dot3(row2, v),
  ]);
}

export function surveyToEquatorial(lonDeg: number, latDeg: number, frame: HipsFrame): [number, number] {
  if (frame === "equatorial") return [lonDeg, latDeg];
  const v = unitVector(lonDeg, latDeg);
  const [row0, row1, row2] = EQ_TO_GAL;
  return vectorToLonLat([
    row0[0] * v[0] + row1[0] * v[1] + row2[0] * v[2],
    row0[1] * v[0] + row1[1] * v[1] + row2[1] * v[2],
    row0[2] * v[0] + row1[2] * v[1] + row2[2] * v[2],
  ]);
}

function dot3(a: readonly [number, number, number], b: readonly [number, number, number]): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

/** A safety ceiling on the order this module will ever pick or address —
 * comfortably above every registry entry's own `maxOrder` (6, for NSNS;
 * `imageryLayer.ts` clamps to that separately), so this is never the limit
 * that actually bites; it exists only so a degenerate FOV cannot make
 * `pickHipsOrder` return something absurd. */
export const HIPS_ORDER_SAFETY_CAP = 16;

/** Roughly `sqrt(4*pi steradians / 12)` in degrees — the angular side of
 * one of the 12 base HEALPix pixels at order 0, matching
 * `scripts/build-wham-hips.py`'s own `BASE_PIXEL_SIDE_DEG`. A HEALPix
 * pixel's angular size at `order` is this divided by `2**order`. */
export const HEALPIX_BASE_PIXEL_SIDE_DEG = Math.sqrt((4 * Math.PI) / 12) * RAD2DEG;

/** Chooses a HEALPix order so that roughly `targetTilesAcross` pixels span
 * the viewport's own width — the HiPS counterpart of `mercatorTiles.ts`'s
 * `pickZoom`, same reasoning (a fixed number of tiles across the FOV,
 * independent of any one survey's `maxOrder`, which `imageryLayer.ts`
 * clamps to separately per layer). */
export function pickHipsOrder(fovDeg: number, widthPx: number, heightPx: number, targetTilesAcross = 6): number {
  const fovWidthDeg = (fovDeg * widthPx) / Math.max(1, heightPx);
  const tileDeg = Math.max(1e-6, fovWidthDeg / targetTilesAcross);
  const order = Math.round(Math.log2(HEALPIX_BASE_PIXEL_SIDE_DEG / tileDeg));
  return Math.max(0, Math.min(HIPS_ORDER_SAFETY_CAP, order));
}

/**
 * Every NESTED pixel at exactly `order` that may intersect the viewport —
 * the HiPS counterpart of `mercatorTiles.ts`'s `visibleTiles`, but built as
 * a hierarchical cone search (see this module's own doc comment for why)
 * rather than a screen-space sample-and-pad grid.
 *
 * The query axis is the viewport's own true screen centre (through
 * `screenToWorld`, so any `coordTransform`/roll is already accounted for);
 * the radius is the largest angular distance from that centre to any of a
 * `sampleGrid x sampleGrid` set of points spread across the screen —
 * measured through the real projection, not assumed from `fovDeg` alone,
 * so a distorted (e.g. alt-az near zenith) view still gets a radius that
 * actually covers what is on screen. The hierarchical descent itself is
 * inclusive by the same margin `ssc/skymap/hips.py`'s `cone_tiles_by_order`
 * uses (`maxPixelRadiusRad(order)` at each level) — a superset of the exact
 * intersection, never a subset.
 *
 * That margin is appropriate for `cone_tiles_by_order`'s own use (a small,
 * fixed search radius around one target), but at a **coarse** order over a
 * **wide** FOV it stops being a tight bound: `maxPixelRadiusRad` is itself
 * tens of degrees at order 0-2, so a candidate's centre can be added at
 * order 0 and survive every descent step while its true position is far
 * outside anything the viewport could show — and a HEALPix pixel that far
 * off-axis still gnomonically projects to *some* finite screen position
 * (this is not literally the far-hemisphere clamp `FAR_HEMISPHERE_COSC_
 * MARGIN` guards against; every one of these points is still well within
 * the near hemisphere), which is exactly a huge, wrongly-placed,
 * wrongly-shaped quadrilateral landing across the frame — the live defect
 * reported against `b6955f4` (owner's screenshot, view centred Dec +69.79,
 * 60deg FOV: "large skewed quadrilaterals... whole blocks sit at the wrong
 * place"). Verified directly: at that exact view, the widest false
 * positive the descent alone produced (order 2, npix 25) had its true
 * centre 58.4deg from the view axis while the real screen radius measured
 * only 44.1deg — comfortably inside the deliberately generous margin
 * (`radius + maxPixelRadiusRad(2)` = 58.7deg) yet nowhere near the visible
 * sky.
 *
 * The fix mirrors `mercatorTiles.ts`'s own `visibleTiles` in spirit, at two
 * levels:
 *
 * 1. **A tight final filter using the pixel's own real shape.** The
 *    hierarchical descent still uses the circular, per-level margin above
 *    to walk down from the 12 base pixels — cheap, and provably never
 *    drops a genuinely-covered pixel — but a survivor is only kept once it
 *    reaches `order` if the *closest* of its own centre and four corners
 *    (computed from its real `(ix, iy)` cell via `faceXyToRaDec`, not a
 *    worst-case circular bound) is actually within `radius` of the view
 *    axis. A HEALPix pixel's shape is not a circle — `maxPixelRadiusRad`
 *    is deliberately the *worst case* over every pixel at that order so
 *    the descent margin stays valid everywhere, which is exactly why it is
 *    too loose to use as the final word for one specific pixel whose real
 *    corners are known.
 * 2. **A direct-sample confirmation as a second, independent path to
 *    inclusion** — mirroring `visibleTiles`'s own forward "which tile does
 *    this screen point actually land in" test: the pixel `ang2pix` returns
 *    for the view centre, or for any of the same `sampleGrid` screen
 *    samples used to size `radius`, is always kept even if its corner
 *    distance check above is marginally outside `radius` (the discrete
 *    sample grid can only bound `radius` from below, the same reasoning
 *    `visibleTiles`'s own one-tile padding exists for).
 *
 * Verified against the live defect this replaced (owner's screenshot,
 * view centred Dec +69.79, 60deg FOV): the widest false positive the old,
 * margin-only descent produced (order 2, npix 25) had a true centre
 * 58.4deg from the view axis and its *closest corner* still 45.9deg away,
 * against a real screen radius of 44.1deg — outside both criteria above,
 * so it is now dropped; `hipsTiles.test.ts` pins this exact case.
 */
export function visibleHipsTiles(
  viewport: Viewport,
  order: number,
  screenToWorld: (viewport: Viewport, x: number, y: number) => [number, number],
  sampleGrid = 9,
  frame: HipsFrame = "equatorial",
): HipsTileKey[] {
  const [centreRa, centreDec] = screenToWorld(viewport, viewport.widthPx / 2, viewport.heightPx / 2);
  const [centreLon, centreLat] = equatorialToSurvey(centreRa, centreDec, frame);
  const axis = unitVector(centreLon, centreLat);

  const confirmed = new Set<number>([ang2pix(centreLon, centreLat, order)]);
  let minCos = 1;
  for (let row = 0; row <= sampleGrid; row += 1) {
    for (let col = 0; col <= sampleGrid; col += 1) {
      const sx = (col / sampleGrid) * viewport.widthPx;
      const sy = (row / sampleGrid) * viewport.heightPx;
      const [ra, dec] = screenToWorld(viewport, sx, sy);
      const [lon, lat] = equatorialToSurvey(ra, dec, frame);
      confirmed.add(ang2pix(lon, lat, order));
      const cosAngle = dot3(axis, unitVector(lon, lat));
      if (cosAngle < minCos) minCos = cosAngle;
    }
  }
  const radius = Math.acos(Math.max(-1, Math.min(1, minCos)));

  let candidates: number[] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
  for (let currentOrder = 0; currentOrder <= order; currentOrder += 1) {
    const margin = radius + maxPixelRadiusRad(currentOrder);
    candidates = candidates.filter((npix) => {
      const [ra, dec] = pixelCentreRaDec(npix, currentOrder);
      const cosAngle = dot3(axis, unitVector(ra, dec));
      return Math.acos(Math.max(-1, Math.min(1, cosAngle))) <= margin;
    });
    if (currentOrder < order) {
      const children: number[] = [];
      for (const npix of candidates) {
        children.push(npix * 4, npix * 4 + 1, npix * 4 + 2, npix * 4 + 3);
      }
      candidates = children;
    }
  }

  const nside = 2 ** order;
  const closestApproachRad = (npix: number): number => {
    const { face, ix, iy } = pixelFaceXy(npix, order);
    const points: [number, number][] = [
      [(ix + 0.5) / nside, (iy + 0.5) / nside],
      [ix / nside, iy / nside],
      [(ix + 1) / nside, iy / nside],
      [ix / nside, (iy + 1) / nside],
      [(ix + 1) / nside, (iy + 1) / nside],
    ];
    let closest = Infinity;
    for (const [xFrac, yFrac] of points) {
      const [ra, dec] = faceXyToRaDec(face, xFrac, yFrac);
      const cosAngle = dot3(axis, unitVector(ra, dec));
      closest = Math.min(closest, Math.acos(Math.max(-1, Math.min(1, cosAngle))));
    }
    return closest;
  };

  return candidates
    .filter((npix) => confirmed.has(npix) || closestApproachRad(npix) <= radius)
    .map((npix) => ({ order, npix }));
}

/**
 * One HiPS tile's tessellation grid: `(subdivisions+1) x (subdivisions+1)`
 * vertices spanning the pixel's own `(ix, iy)` unit cell, each mapped
 * through `faceXyToRaDec` then `worldToScreen` — the HiPS counterpart of
 * `mercatorTiles.ts`'s `buildTileMeshFracGrid`, sharing its output shape
 * (`TileMeshGrid`) so `imageryBatch.ts`'s batching is scheme-agnostic.
 *
 * Grid axis convention matches the pixel data every HiPS tile actually
 * ships (`scripts/build-wham-hips.py`'s own doc comment, and the WWT
 * engine's `HealpixTile` texture mapping it was verified against): image
 * row 0 is the pixel's own north-ish corner and increases along the
 * HEALPix `ix` axis; image column 0 increases along `iy`. So this grid's
 * `j` (row) index steps `ix`, and its `i` (column) index steps `iy` — the
 * mirror image of `buildTileMeshFracGrid`'s roles (there, `i`/column steps
 * Mercator's own `x`, `j`/row steps its `y`), because a HEALPix pixel's two
 * axes are not "RA-like" and "Dec-like" the way a Mercator tile's are.
 * `imageryBatch.ts` itself does not care which sky direction `i`/`j` step —
 * it only assumes `i` is the image's column and `j` its row, which this
 * mapping satisfies by construction.
 */
export function buildHipsTileMeshGrid(
  viewport: Viewport,
  tile: HipsTileKey,
  subdivisions: number,
  frame: HipsFrame = "equatorial",
): TileMeshGrid {
  const nside = 2 ** tile.order;
  const { face, ix, iy } = pixelFaceXy(tile.npix, tile.order);
  const stride = subdivisions + 1;
  const screenX = new Float64Array(stride * stride);
  const screenY = new Float64Array(stride * stride);
  const ra = new Float64Array(stride * stride);
  const dec = new Float64Array(stride * stride);
  for (let j = 0; j < stride; j += 1) {
    const xFrac = (ix + j / subdivisions) / nside;
    for (let i = 0; i < stride; i += 1) {
      const yFrac = (iy + i / subdivisions) / nside;
      const [lon, lat] = faceXyToRaDec(face, xFrac, yFrac);
      const [r, d] = surveyToEquatorial(lon, lat, frame);
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

/** A HiPS survey's own `tileUrlTemplate`
 * (`{order}`/`{dir}`/`{npix}` placeholders,
 * `ssc.skymap.surveys.client_tile_template`) with this tile's own
 * coordinates substituted in — the HiPS counterpart of `mercatorTiles.ts`'s
 * `mercatorTileUrl`. `dir` mirrors `ssc/skymap/hips.py`'s `tile_dir`
 * exactly (tiles grouped 10,000 to a directory); always same-origin, since
 * the template itself always comes from this frame's own tile proxy. */
export function hipsTileUrl(template: string, tile: HipsTileKey): string {
  const dir = Math.floor(tile.npix / 10_000) * 10_000;
  return template
    .replace("{order}", String(tile.order))
    .replace("{dir}", String(dir))
    .replace("{npix}", String(tile.npix));
}
