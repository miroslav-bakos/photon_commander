// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The 88 IAU constellation-boundary regions (Delporte 1930, via Pierre
 * Barbier, CC BY-SA 4.0 — `ssc.astro.constellations`), drawn the same way
 * catalogue outlines are (`catalogOutlines.ts`): thin closed polylines
 * (`FovShape` `"polygon"`s, unfilled — no `fillOpacity` set), through the
 * same overlay path as everything else. Background reference geometry, not
 * a target indicator — its own `"constellation"` role/token
 * (`roleStyle.ts`), distinct from both the coordinate grid (`"coordGrid"`)
 * and catalogue outlines (`"catalog"`).
 *
 * Nothing here is astronomy: the vertices arrive already precessed to
 * J2000 from `GET /api/astro/constellation-boundaries`
 * (`ssc.astro.constellations.boundaries_j2000`) — this file only turns them
 * into shapes and decides which of the 88 regions get a (full-name) label.
 *
 * Split in two for the 16ms scrubber budget (`skymap-viz.md`): this layer
 * never depends on the scrubber's `instant`, so nothing here should ever
 * run on a scrub tick.
 * `constellationRegionGeometry` does the only expensive part — walking all
 * 1562 vertices once, to find each region's centroid (a circular mean, so
 * a region straddling RA 0h/24h — CET, PSC, AQR, PEG, ... — gets a sane
 * answer instead of an average pulled toward RA 180°) and an approximate
 * angular size — and is only ever called again if the fetched region list
 * itself changes (`staleTime: Infinity`, so in practice: once per session).
 * `buildConstellationBoundaryShapes` is the cheap part: given that
 * precomputed geometry, it re-decides which regions' labels to show (the
 * near-view/FoV-gated rule below) and wraps everything in `FovShape`s,
 * reusing the same vertex arrays — safe to call again on a view settle,
 * never on a scrub tick.
 */
import type { ConstellationBoundaryRegionModel } from "../api/skymap";
import { circularDistanceDeg } from "./frames/shared/sidereal";
import { GRID_LABEL_MAX_FOV_DEG, nearestLabels } from "./frames/shared/skyLines";
import type { FovShape } from "./types";

export function constellationBoundaryShapeId(abbreviation: string, ringIndex: number): string {
  return `constellation:${abbreviation}:${String(ringIndex)}`;
}

export interface ConstellationRegionGeometry {
  abbreviation: string;
  name: string;
  rings: [ra: number, dec: number][][];
  centroidRaDeg: number;
  centroidDecDeg: number;
  /** Approximate angular diameter, degrees — twice the farthest vertex's
   * distance from the centroid. Only ever used to gate the label (the
   * "large enough on screen" rule below), never anything geometry-critical:
   * a rough size, not the region's exact extent. */
  extentDeg: number;
}

/** The expensive walk over every vertex, done once per fetched region list
 * (see the module doc comment). Pure — no view, no instant, nothing that
 * changes while the map is open. */
export function constellationRegionGeometry(
  regions: readonly ConstellationBoundaryRegionModel[],
): ConstellationRegionGeometry[] {
  return regions.map((region) => {
    const rings = region.rings.map((ring) => ring.map((v): [number, number] => [v.ra_j2000_deg, v.dec_j2000_deg]));
    const vertices = rings.flat();

    // Circular mean for RA: a plain average breaks for any region whose
    // boundary crosses RA 0h/24h (its vertices sit near both 359° and 1°,
    // averaging to ~180° — the opposite side of the sky).
    let sinSum = 0;
    let cosSum = 0;
    let decSum = 0;
    for (const [raDeg, decDeg] of vertices) {
      const raRad = (raDeg * Math.PI) / 180;
      sinSum += Math.sin(raRad);
      cosSum += Math.cos(raRad);
      decSum += decDeg;
    }
    const centroidRaDeg = ((Math.atan2(sinSum, cosSum) * 180) / Math.PI + 360) % 360;
    const centroidDecDeg = decSum / vertices.length;

    let maxDistDeg = 0;
    const cosDec = Math.cos((centroidDecDeg * Math.PI) / 180);
    for (const [raDeg, decDeg] of vertices) {
      const dRa = circularDistanceDeg(raDeg, centroidRaDeg) * cosDec;
      const dDec = decDeg - centroidDecDeg;
      maxDistDeg = Math.max(maxDistDeg, Math.hypot(dRa, dDec));
    }

    return {
      abbreviation: region.abbreviation,
      name: region.name,
      rings,
      centroidRaDeg,
      centroidDecDeg,
      extentDeg: maxDistDeg * 2,
    };
  });
}

export interface ConstellationBoundaryOptions {
  /** Current field of view (viewport height), degrees — `null` before the
   * frame's first view report, same convention as `CatalogOutlineOptions`
   * and `EquatorialGridOptions`. */
  fovDeg: number | null;
  viewRaDeg: number;
  viewDecDeg: number;
}

/** Five, down from twelve (owner, 2026-09-08: "make labels less
 * cluttered", after full names replaced the three-letter abbreviations).
 * Twelve short codes and twelve words like "Camelopardalis" are very
 * different amounts of ink, and a constellation label names a whole region
 * of sky rather than a point — a handful is orientation, a dozen is noise.
 * Deliberately this layer's own constant, not `skyLines.ts`'s
 * `MAX_GRID_LABELS`: the two counts were always free to differ. */
const MAX_CONSTELLATION_LABELS = 5;

/** Constellations need a far stricter "large enough on screen" bar than
 * catalogue objects do, which is why this is no longer
 * `catalogOutlines.ts`'s `LABEL_MIN_FOV_FRACTION` (0.04).
 *
 * That fraction is tuned for *objects*: a galaxy covering 4% of the field
 * is worth naming. Constellations are tens of degrees across, so at the
 * 30° field where labels switch on at all, 4% is a 1.2° bar that every
 * region in view clears trivially — the size gate did nothing for this
 * layer and the count cap was doing all the work alone. Requiring a fifth
 * of the field means a name appears for regions the view is actually
 * *inside*, not every region clipping a corner of it. */
export const CONSTELLATION_LABEL_MIN_FOV_FRACTION = 0.2;

/**
 * One `FovShape` polygon per ring (unfilled, `"constellation"` role), plus
 * a label at the region's own centroid for regions that are both large
 * enough on screen (`CONSTELLATION_LABEL_MIN_FOV_FRACTION` — this layer's
 * own bar, see why it is not the catalogue's) and near the current view
 * (`nearestLabels`/`GRID_LABEL_MAX_FOV_DEG`, `skyLines.ts`'s own rule): the
 * same two *gates* the catalogue-outline and coordinate-grid labels use,
 * with this layer's own threshold for the first. Serpens (`"SER"`, two
 * disjoint rings) gets at most one label, on its first ring.
 */
export function buildConstellationBoundaryShapes(
  geometry: readonly ConstellationRegionGeometry[],
  options: ConstellationBoundaryOptions,
): FovShape[] {
  const fovDeg = options.fovDeg;
  const labelsOn = fovDeg !== null && Number.isFinite(fovDeg) && fovDeg <= GRID_LABEL_MAX_FOV_DEG;
  const sizeQualified = labelsOn ? geometry.filter((region) => region.extentDeg >= fovDeg * CONSTELLATION_LABEL_MIN_FOV_FRACTION) : [];
  const keep = labelsOn
    ? nearestLabels(
        sizeQualified.map((region) => ({
          id: region.abbreviation,
          dist: Math.hypot(
            circularDistanceDeg(region.centroidRaDeg, options.viewRaDeg),
            region.centroidDecDeg - options.viewDecDeg,
          ),
        })),
        MAX_CONSTELLATION_LABELS,
      )
    : null;

  const shapes: FovShape[] = [];
  for (const region of geometry) {
    const labelled = keep?.has(region.abbreviation) === true;
    region.rings.forEach((ring, ringIndex) => {
      const showLabel = labelled && ringIndex === 0;
      shapes.push({
        kind: "polygon",
        id: constellationBoundaryShapeId(region.abbreviation, ringIndex),
        verticesRaDec: ring,
        style: { role: "constellation" },
        // The full name ("Andromeda"), not the IAU abbreviation ("AND") —
        // owner's request, 2026-09-08: "show full names of constellations".
        // Both already travel with the region (`/api/astro/constellation-
        // boundaries` returns `abbreviation` and `name`; the bundled
        // boundaries.csv carries both), so this is a display choice, not new
        // data. The abbreviation stays the identity key below and in
        // `constellationBoundaryShapeId`, where a stable short id is what is
        // actually wanted.
        label: showLabel ? region.name : undefined,
        labelAt: showLabel ? [region.centroidRaDeg, region.centroidDecDeg] : undefined,
      });
    });
  }
  return shapes;
}
