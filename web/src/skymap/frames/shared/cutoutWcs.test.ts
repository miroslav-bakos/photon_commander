// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { CutoutWcs } from "../../types";
import { wcsPixelToWorld } from "./cutoutWcs";

function sepArcsec(a: readonly [number, number], b: readonly [number, number]): number {
  const [ra1, dec1] = a;
  const [ra2, dec2] = b;
  const dRa = Math.abs(((ra1 - ra2 + 540) % 360) - 180) * Math.cos((dec1 * Math.PI) / 180);
  const dDec = Math.abs(dec1 - dec2);
  return Math.hypot(dRa, dDec) * 3600;
}

/** A synthetic WCS the way `ssc.skymap.cutouts` builds one: `size` pixels
 * square, `scaleDeg` per pixel, tangent point at the pixel centre, rotated
 * `paDeg` (position angle, north through east) from "column axis west, row
 * axis north". */
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

describe("wcsPixelToWorld", () => {
  it("projects the image centre onto CRVAL when CRPIX sits at the image's own centre", () => {
    const size = 500;
    const wcs = syntheticWcs({ crval1: 283.396, crval2: -1.294, size, scaleDeg: 0.000381, paDeg: 0 });
    const [ra, dec] = wcsPixelToWorld(wcs, size / 2, size / 2);
    expect(ra).toBeCloseTo(283.396, 6);
    expect(dec).toBeCloseTo(-1.294, 6);
  });

  it("moves the expected direction for an unrotated, normally-oriented WCS", () => {
    // CD1_1 < 0 (RA decreases as pxCol increases, i.e. east is a negative
    // column step); CD2_2 > 0 (dec increases as pxRow increases).
    const size = 500;
    const scaleDeg = 0.0003;
    const wcs = syntheticWcs({ crval1: 200, crval2: 40, size, scaleDeg, paDeg: 0 });
    const [centerRa, centerDec] = wcsPixelToWorld(wcs, size / 2, size / 2);
    const [eastRa] = wcsPixelToWorld(wcs, 0, size / 2); // smaller pxCol -> east -> larger RA
    const [, northDec] = wcsPixelToWorld(wcs, size / 2, size); // larger pxRow -> north -> larger dec
    expect(eastRa).toBeGreaterThan(centerRa);
    expect(northDec).toBeGreaterThan(centerDec);
  });

  // Regression fixture for the "M31/SDSS cutout renders small and
  // off-centre" investigation (2026-09-07): the WCS below is a real piece
  // fetched live through `ssc-core` from SDSS DR17's SAS frame service for
  // M31 (`fits_url`/`display_png_url` under cutout `42ef2fc28d78`, band g).
  // The four corner RA/Dec values are not computed here — they are what
  // `@wwtelescope/engine`'s own `TangentTile.createGeometry` produced when
  // this exact WCS (via `cutoutImagePlacement`, since removed with WWT) was
  // fed into a live `Imageset` and rendered, captured via a temporary
  // console-log instrumentation and converted from the engine's own
  // `Vector3d` back to RA/Dec. This is the strongest test available for
  // this module: not "does our math agree with itself" but "does an
  // independently-implemented renderer, given the same WCS, land on the
  // same sky footprint this function predicts directly from the pixel
  // corners" — to within ~1 arcsecond (floating-point noise).
  it("matches an independent renderer's own corners for a real fetched SDSS piece (M31, 2026-09-07)", () => {
    const wcs: CutoutWcs = {
      crpix1: 1025.0,
      crpix2: 745.0,
      crval1: 10.7351360138,
      crval2: 41.1533591736,
      cd1_1: -8.57663006882e-5,
      cd1_2: 6.87688685965e-5,
      cd2_1: 6.86373592874e-5,
      cd2_2: 8.58844153068e-5,
      ctype1: "RA---TAN",
      ctype2: "DEC--TAN",
    };
    const width = 2048;
    const height = 1489;

    const engineCorners: Record<string, [ra: number, dec: number]> = {
      topLeft: [10.919827057128803, 41.14687017733013],
      bottomRight: [10.55052251392781, 41.15978016807484],
      topRight: [10.686303895511431, 41.28771168829349],
      bottomLeft: [10.783882213469559, 41.01921294556601],
    };
    const wcsCorners: [ra: number, dec: number][] = [
      wcsPixelToWorld(wcs, 0, 0),
      wcsPixelToWorld(wcs, width, 0),
      wcsPixelToWorld(wcs, 0, height),
      wcsPixelToWorld(wcs, width, height),
    ];

    for (const [label, engineCorner] of Object.entries(engineCorners)) {
      const nearest = Math.min(...wcsCorners.map((wcsCorner) => sepArcsec(engineCorner, wcsCorner)));
      expect(nearest, `${label} vs nearest WCS-predicted corner`).toBeLessThan(2);
    }
  });
});
