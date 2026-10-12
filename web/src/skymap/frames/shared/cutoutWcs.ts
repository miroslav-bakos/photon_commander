// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * A cutout piece's WCS (`CutoutWcs`, plain numbers straight off
 * `ssc.skymap.cutouts`' FITS header — never re-derived from a file the
 * frame does not have) turned into a pixel -> sky lookup, so a cutout mesh
 * can be built vertex-by-vertex against the piece's own true projection
 * (`frames/vector/cutoutMesh.ts`), the same way `mercatorTiles.ts`/
 * `hipsTiles.ts` build a tile's mesh from its own tile math — never a
 * single centre/scale/rotation approximation, since this frame owns its
 * drawing outright and has no reason to flatten a WCS down to what an
 * external engine's "place one image" API could take.
 *
 * Recovered from `cd169b9` (WWT's removal deleted this file as having no
 * callers) rather than re-derived: only `wcsPixelToWorld` is kept —
 * `cutoutImagePlacement`, the single-placement decomposition WWT's own
 * `skyImage` projection needed, has no caller once the frame projects each
 * mesh vertex through the WCS directly.
 *
 * Pure and engine-free, testable in plain Vitest (`cutoutWcs.test.ts`).
 *
 * The WCS pixel -> sky projection reuses `projection.ts`'s
 * `tangentOffsetToWorld`, which already implements the standard inverse
 * gnomonic (TAN) deprojection in the `(east, north)` tangent-offset
 * convention. A FITS WCS's own intermediate world coordinates (the CD
 * matrix applied to a pixel offset) are in exactly that convention for a
 * normally-oriented image (`CD1_1 < 0`: RA decreases as the pixel column
 * increases, i.e. east is a *negative* column step) with `LONPOLE = 180`
 * (the default, and the only case any of `ssc.skymap.cutouts`' three
 * sources produce) — so no separate WCS deprojection formula is needed
 * here; feeding the CD-matrix intermediate coordinates straight into
 * `tangentOffsetToWorld` *is* the standard TAN deprojection.
 *
 * `RA---SIN`/`DEC--SIN` (AllWISE's own `CTYPE`s) are treated as TAN. SIN and
 * TAN agree to first order at the tangent point and diverge from each other
 * as roughly `(field radius)^3` in the small-field limit; at the 1.5 deg
 * default cutout size (0.75 deg radius) that is on the order of 1e-3 deg —
 * far under the 0.1 deg test tolerance this module is held to, but real
 * for a field pushed toward the 3 deg cap. Not corrected because AllWISE
 * cutouts this size are always near their own centre; a wider mosaic of
 * cutouts would need the real SIN deprojection.
 */
import { tangentOffsetToWorld } from "../../projection";
import type { CutoutWcs } from "../../types";

/** The CD-matrix intermediate world coordinates (degrees) for pixel
 * `(pxCol, pxRow)` in **corner space**: 0 is the outer edge of the first
 * pixel, `width`/`height` the outer edge of the last. This is also the
 * pixel-index convention `piece.widthPx`/`heightPx` and the decoded
 * `displayPngUrl` image share directly: `ssc/skymap/png.py` writes each
 * FITS data row straight through with no `[::-1]`, so image row 0 (the
 * first decoded scanline) *is* corner-space `pxRow = 0` — no separate row
 * flip is ever applied between the FITS pixel grid, this function's own
 * indexing and the texture a frame samples from the same image.
 *
 * FITS pixel coordinates (`CRPIX`) are 1-indexed with pixel *centres* at
 * integers; a FITS pixel index `i` is corner-space `i - 0.5`, so a
 * corner-space coordinate `p` is FITS pixel index `p + 0.5` — that is the
 * only conversion here.
 */
function tangentOffsetForPixel(wcs: CutoutWcs, pxCol: number, pxRow: number): [xiDeg: number, etaDeg: number] {
  const dx = pxCol + 0.5 - wcs.crpix1;
  const dy = pxRow + 0.5 - wcs.crpix2;
  const xi = wcs.cd1_1 * dx + wcs.cd1_2 * dy;
  const eta = wcs.cd2_1 * dx + wcs.cd2_2 * dy;
  return [xi, eta];
}

/** Pixel `(pxCol, pxRow)` (corner-space — see `tangentOffsetForPixel`) ->
 * sky, through this piece's own WCS. */
export function wcsPixelToWorld(wcs: CutoutWcs, pxCol: number, pxRow: number): [ra: number, dec: number] {
  const [xi, eta] = tangentOffsetForPixel(wcs, pxCol, pxRow);
  return tangentOffsetToWorld(wcs.crval1, wcs.crval2, xi, eta);
}
