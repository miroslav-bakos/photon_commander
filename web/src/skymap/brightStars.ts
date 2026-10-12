// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Named bright stars: the ones the sky map draws and labels, from `GET
 * /api/astro/stars` (`ssc.astro.stars`), through the same overlay path as
 * catalogue outlines and constellation boundaries (`SkyMap.tsx` merges
 * them). A star is a point, not an extended object: it draws as a small
 * dot (`kind: "circle"`, its own `"star"` role/token, `roleStyle.ts`),
 * never an ellipse — distinct from both catalogue outlines (`"catalog"`,
 * extended deep-sky objects) and constellation boundaries (`"constellation"`,
 * reference lines).
 *
 * Nothing here is astronomy: name, position and magnitude come straight
 * from the endpoint; this file only turns a magnitude into a dot size and
 * decides which stars get a label, the same near-view/FoV-gated rule
 * `constellationBoundaries.ts` and `frames/shared/skyLines.ts`'s coordinate
 * grid already use (`nearestLabels`/`GRID_LABEL_MAX_FOV_DEG`) — reused
 * here, not reinvented a third time.
 *
 * Fetched the same way catalogue outlines are: a cone on the settled view,
 * skipped above `OUTLINES_MAX_FOV_DEG` (`catalogOutlines.ts`'s own
 * constant, reused rather than duplicated — the reasoning is identical:
 * beyond that width the requested cone could no longer cover the visible
 * field, so a partial scatter of dots would read as broken coverage, not a
 * sparser field).
 *
 * That constant went 20 -> 60 on 2026-09-09 together with a raised
 * `MAX_CONE_RADIUS_DEG`, which is what removed the coverage objection: the
 * cone now spans the whole visible field at any zoom this map allows. Stars
 * follow outlines to 60 degrees deliberately — a wide view without them is a
 * few lone ellipses on black, and the naked-eye stars are what make it
 * legible as sky. Their own magnitude cut and `nearestLabels` budget keep
 * the count sane; nothing here scales with the width of the field.
 */
import type { BrightStarModel } from "../api/skymap";
import { OUTLINES_MAX_FOV_DEG } from "./catalogOutlines";
import { circularDistanceDeg } from "./frames/shared/sidereal";
import { GRID_LABEL_MAX_FOV_DEG, nearestLabels } from "./frames/shared/skyLines";
import type { FovShape } from "./types";

export { OUTLINES_MAX_FOV_DEG as STARS_MAX_FOV_DEG };

export function starShapeId(name: string): string {
  return `star:${name}`;
}

/** A 20-star cap — deliberately more generous than the 12-region/12-line
 * caps `constellationBoundaries.ts`/`skyLines.ts` use: a field can
 * legitimately hold this many recognizable named stars at once (Orion
 * alone has eight in the bundled table), and unlike a boundary label a
 * star label does not compete with a large filled region for space. */
const MAX_STAR_LABELS = 20;

/** Magnitude range the bundled table actually spans (Sirius at the bright
 * end, the build's own Vmag 4.0 cutoff at the faint end) — the fixed input
 * range `starDotRadiusDeg` interpolates over, not derived per-call from
 * whatever stars happen to be in one field. */
const BRIGHTEST_MAGNITUDE = -1.5;
const FAINTEST_MAGNITUDE = 4.0;
const MIN_DOT_RADIUS_DEG = 0.02;
const MAX_DOT_RADIUS_DEG = 0.09;

/** A small sky-relative dot, larger for a brighter (lower-magnitude) star —
 * the same "brighter reads as bigger" convention every star chart uses.
 * Clamped at both ends: a star outside the table's own magnitude range
 * (should not happen; the bundled cutoff enforces it) never overflows or
 * inverts the scale. */
export function starDotRadiusDeg(magnitude: number): number {
  const t = (FAINTEST_MAGNITUDE - magnitude) / (FAINTEST_MAGNITUDE - BRIGHTEST_MAGNITUDE);
  const clamped = Math.min(1, Math.max(0, t));
  return MIN_DOT_RADIUS_DEG + clamped * (MAX_DOT_RADIUS_DEG - MIN_DOT_RADIUS_DEG);
}

export interface BrightStarOptions {
  /** Current field of view (viewport height), degrees — `null` before the
   * frame's first view report, same convention as `CatalogOutlineOptions`
   * and `ConstellationBoundaryOptions`. */
  fovDeg: number | null;
  viewRaDeg: number;
  viewDecDeg: number;
}

/** One dot per star, plus a name label for the `MAX_STAR_LABELS` nearest
 * the current view when the view is close enough in (`nearestLabels`,
 * `GRID_LABEL_MAX_FOV_DEG` — the same rule the coordinate grid and
 * constellation-boundary labels use). Below that width every star gets a
 * dot but no label, the same "draw it, do not caption it" degradation
 * `catalogOutlines.ts` uses for an object too small to label. */
export function buildBrightStarShapes(
  stars: readonly BrightStarModel[],
  options: BrightStarOptions,
): FovShape[] {
  const fovDeg = options.fovDeg;
  const labelsOn = fovDeg !== null && Number.isFinite(fovDeg) && fovDeg <= GRID_LABEL_MAX_FOV_DEG;
  const keep = labelsOn
    ? nearestLabels(
        stars.map((star) => ({
          id: star.name,
          dist: Math.hypot(
            circularDistanceDeg(star.ra_j2000_h * 15, options.viewRaDeg),
            star.dec_j2000_deg - options.viewDecDeg,
          ),
        })),
        MAX_STAR_LABELS,
      )
    : null;

  return stars.map((star): FovShape => {
    const raDeg = star.ra_j2000_h * 15;
    const decDeg = star.dec_j2000_deg;
    const labelled = keep?.has(star.name) === true;
    return {
      kind: "circle",
      id: starShapeId(star.name),
      centerRa: raDeg,
      centerDec: decDeg,
      radiusDeg: starDotRadiusDeg(star.magnitude),
      style: { role: "star", fillOpacity: 1 },
      label: labelled ? star.name : undefined,
    };
  });
}
