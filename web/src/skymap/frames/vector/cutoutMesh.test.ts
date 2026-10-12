// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `buildCutoutMeshGrid`'s own placement-accuracy check, the cutout
 * counterpart of `mercatorTiles.test.ts`'s "M31/M42 must land on their
 * catalogue positions" measurement: the gap between `worldToScreen`'s exact
 * projection of a real point on the piece and a barycentric (affine)
 * interpolation of the same point within the mesh's own triangle — i.e. the
 * error a GPU rasterizer actually shows for a textured triangle, not a
 * looser "does the formula look right" check.
 */
import { describe, expect, it } from "vitest";
import { worldToScreen, type Viewport } from "../../projection";
import type { CutoutWcs } from "../../types";
import { wcsPixelToWorld } from "../shared/cutoutWcs";
import { buildCutoutMesh, buildCutoutMeshGrid, cutoutMeshUv } from "./cutoutMesh";

const WIDTH_PX = 1200;
const HEIGHT_PX = 800;
const SUBDIVISIONS = 8;

/** Same synthetic-WCS builder as `cutoutWcs.test.ts` (kept local rather
 * than shared — both are small, self-contained test fixtures). */
function syntheticWcs(opts: { crval1: number; crval2: number; size: number; scaleDeg: number; paDeg: number }): CutoutWcs {
  const { crval1, crval2, size, scaleDeg, paDeg } = opts;
  const pa = (paDeg * Math.PI) / 180;
  const rotate = (x: number, y: number): [number, number] => [
    x * Math.cos(pa) + y * Math.sin(pa),
    -x * Math.sin(pa) + y * Math.cos(pa),
  ];
  const [colEast, colNorth] = rotate(-1, 0);
  const [rowEast, rowNorth] = rotate(0, 1);
  return {
    crpix1: size / 2 + 0.5,
    crpix2: size / 2 + 0.5,
    crval1,
    crval2,
    cd1_1: colEast * scaleDeg,
    cd2_1: colNorth * scaleDeg,
    cd1_2: rowEast * scaleDeg,
    cd2_2: rowNorth * scaleDeg,
    ctype1: "RA---TAN",
    ctype2: "DEC--TAN",
  };
}

function piece(wcs: CutoutWcs, widthPx: number, heightPx: number) {
  return {
    id: "test",
    displayPngUrl: "/test.png",
    wcs,
    widthPx,
    heightPx,
    opacity: 1,
    creditLabel: "test",
    creditUrl: "",
  };
}

describe("buildCutoutMeshGrid", () => {
  it("agrees with wcsPixelToWorld/worldToScreen at every grid corner", () => {
    const size = 400;
    const wcs = syntheticWcs({ crval1: 200, crval2: 10, size, scaleDeg: 0.003, paDeg: 20 });
    const viewport: Viewport = { view: { ra: 200, dec: 10, fovDeg: 3 }, widthPx: WIDTH_PX, heightPx: HEIGHT_PX };
    const grid = buildCutoutMeshGrid(viewport, piece(wcs, size, size), SUBDIVISIONS);
    const stride = SUBDIVISIONS + 1;
    const corners: [number, number][] = [
      [0, 0],
      [SUBDIVISIONS, 0],
      [0, SUBDIVISIONS],
      [SUBDIVISIONS, SUBDIVISIONS],
    ];
    for (const [i, j] of corners) {
      const idx = j * stride + i;
      const [expectedRa, expectedDec] = wcsPixelToWorld(wcs, (i / SUBDIVISIONS) * size, (j / SUBDIVISIONS) * size);
      expect(grid.ra[idx]).toBeCloseTo(expectedRa, 9);
      expect(grid.dec[idx]).toBeCloseTo(expectedDec, 9);
      const [expectedX, expectedY] = worldToScreen(viewport, expectedRa, expectedDec);
      expect(grid.screenX[idx]).toBeCloseTo(expectedX, 6);
      expect(grid.screenY[idx]).toBeCloseTo(expectedY, 6);
    }
  });
});

describe("buildCutoutMesh", () => {
  it("returns one vertex per grid cell corner, in bounds indices, and matching screen positions", () => {
    const size = 300;
    const wcs = syntheticWcs({ crval1: 100, crval2: 20, size, scaleDeg: 0.002, paDeg: 0 });
    const viewport: Viewport = { view: { ra: 100, dec: 20, fovDeg: 3 }, widthPx: WIDTH_PX, heightPx: HEIGHT_PX };
    const grid = buildCutoutMeshGrid(viewport, piece(wcs, size, size), SUBDIVISIONS);
    const mesh = buildCutoutMesh(viewport, piece(wcs, size, size), SUBDIVISIONS);
    const stride = SUBDIVISIONS + 1;
    expect(mesh.positions.length).toBe(stride * stride * 2);
    expect(mesh.uvs.length).toBe(stride * stride * 2);
    // Two triangles per cell, three indices each.
    expect(mesh.indices.length).toBe(SUBDIVISIONS * SUBDIVISIONS * 6);
    for (const index of mesh.indices) expect(index).toBeLessThan(stride * stride);
    for (let k = 0; k < stride * stride; k += 1) {
      expect(mesh.positions[k * 2]).toBeCloseTo(grid.screenX[k] ?? NaN, 6);
      expect(mesh.positions[k * 2 + 1]).toBeCloseTo(grid.screenY[k] ?? NaN, 6);
    }
  });
});

describe("cutoutMeshUv", () => {
  it("maps pixel row 0 (the decoded image's first scanline) to v=1, the last row to v=0", () => {
    expect(cutoutMeshUv(0, 0, SUBDIVISIONS)).toEqual([0, 1]);
    expect(cutoutMeshUv(0, SUBDIVISIONS, SUBDIVISIONS)).toEqual([0, 0]);
  });

  it("maps pixel column 0 to u=0, increasing with column — no horizontal mirroring", () => {
    const [u0] = cutoutMeshUv(0, 0, SUBDIVISIONS);
    const [uLast] = cutoutMeshUv(SUBDIVISIONS, 0, SUBDIVISIONS);
    expect(u0).toBe(0);
    expect(uLast).toBe(1);
  });
});

/** Barycentric interpolation of a real point within the mesh's own
 * triangle — the cutout counterpart of `mercatorTiles.ts`'s
 * `interpolatedScreenPosition`, built here rather than exported from
 * `cutoutMesh.ts` since nothing in production code needs it: the renderer
 * draws the mesh's actual triangles via `tileMesh.ts`'s shared
 * triangulation, it never separately interpolates a point within one. */
function interpolatedCutoutScreenPosition(
  viewport: Viewport,
  wcs: CutoutWcs,
  widthPx: number,
  heightPx: number,
  subdivisions: number,
  targetPxCol: number,
  targetPxRow: number,
): [number, number] {
  const cellXf = (targetPxCol / widthPx) * subdivisions;
  const cellYf = (targetPxRow / heightPx) * subdivisions;
  const i = Math.min(subdivisions - 1, Math.max(0, Math.floor(cellXf)));
  const j = Math.min(subdivisions - 1, Math.max(0, Math.floor(cellYf)));
  const fx = cellXf - i;
  const fy = cellYf - j;

  const corner = (ci: number, cj: number): [number, number] => {
    const [r, d] = wcsPixelToWorld(wcs, (ci / subdivisions) * widthPx, (cj / subdivisions) * heightPx);
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

describe("cutoutMesh: curvature error at the frame's own subdivision (8)", () => {
  // A view centred exactly on the piece's own tangent point degenerates to
  // a pure rotation+scale between the two gnomonic maps — zero curvature
  // error regardless of subdivision, and not a realistic case (the user
  // pans/zooms independently of any one piece's own centre). Every case
  // below centres the view on a point 60% of the way to a piece corner
  // instead, at a handful of FOVs including tighter than the piece itself
  // (zoomed into part of it) and wider (the piece small in a broader
  // background view) — the two ends of what "at the target and a couple of
  // FOVs" (this task's own verification ask) means for a static piece.
  for (const [label, decDeg, paDeg] of [
    ["low dec, unrotated", 10, 0] as const,
    ["mid dec, rotated 37deg", 45, 37] as const,
    ["high dec (75deg), rotated -90deg", 75, -90] as const,
  ]) {
    for (const fovDeg of [1, 3, 8]) {
      it(`piece at the 3deg cap, off-centre view at ${String(fovDeg)}deg FOV (${label})`, () => {
        const sizeDeg = 3;
        const size = 500;
        const scaleDeg = sizeDeg / size;
        const wcs = syntheticWcs({ crval1: 200, crval2: decDeg, size, scaleDeg, paDeg });
        const [cornerRa, cornerDec] = wcsPixelToWorld(wcs, size, size);
        const viewRa = 200 + 0.6 * (cornerRa - 200);
        const viewDec = decDeg + 0.6 * (cornerDec - decDeg);
        const view = { ra: viewRa, dec: viewDec, fovDeg };
        const viewport: Viewport = { view, widthPx: WIDTH_PX, heightPx: HEIGHT_PX };

        let worst = 0;
        let worstAt: [number, number] = [0, 0];
        for (let j = 0; j < SUBDIVISIONS; j += 1) {
          for (let i = 0; i < SUBDIVISIONS; i += 1) {
            const pxCol = ((i + 0.5) / SUBDIVISIONS) * size;
            const pxRow = ((j + 0.5) / SUBDIVISIONS) * size;
            const [ra, dec] = wcsPixelToWorld(wcs, pxCol, pxRow);
            const exact = worldToScreen(viewport, ra, dec);
            const approx = interpolatedCutoutScreenPosition(viewport, wcs, size, size, SUBDIVISIONS, pxCol, pxRow);
            const err = Math.hypot(exact[0] - approx[0], exact[1] - approx[1]);
            if (err > worst) {
              worst = err;
              worstAt = [ra, dec];
            }
          }
        }
        console.log(
          `[cutoutMesh] ${label} @ ${String(fovDeg)}deg FOV: worst-case cell-midpoint error ${worst.toFixed(4)}px at ra=${worstAt[0].toFixed(3)} dec=${worstAt[1].toFixed(3)}`,
        );
        // Loose sanity bound (catches a broken mesh/projection, not a
        // tuning target) — `mercatorTiles.test.ts` uses the same style of
        // bound (<50px) for tiles spanning much larger angles; a 3deg-cap
        // cutout piece at subdivisions=8 stays far tighter than that.
        expect(worst).toBeLessThan(5);
      });
    }
  }
});
