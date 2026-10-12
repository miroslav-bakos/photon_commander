// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { BodyId } from "../../api/plans";

export interface PlanningComet {
  spkId: number;
  designation: string;
  resolvedAt: string;
  elementEpochJdTdb: number;
  elementAgeDays: number;
  solutionDate: string | null;
  source: string;
  sourceUrl: string;
  snapshotFetchedAt: string;
  snapshotAgeDays: number;
  stale: boolean;
  accuracyNote: string;
}

/** The one target shape every `PlanningTab` child agrees on — resolved from
 * the offline catalogue (`TargetPicker`) or typed in by hand. `raH` is
 * always hours (`ssc.astro`'s own convention, `FramingRequest.ra_j2000_h`),
 * never degrees, so nothing downstream has to remember which. */
export interface PlanningTarget {
  name: string;
  raH: number;
  decDeg: number;
  magnitude: number | null;
  sizeMajorArcmin: number | null;
  /** Major-axis position angle, degrees north through east, [0, 180) — the
   * same field `GET /api/astro/objects`' own outline is oriented by
   * (`catalogOutlines.ts`); `null` when the catalogue does not give one, or
   * the target has no catalogue identity at all. */
  positionAngleDeg: number | null;
  objectType: string | null;
  /** Present only for a moving solar-system target. Its coordinates are a
   * preview at the selected planning instant; this identity, not those
   * coordinates, is what gets persisted into a scheduled plan. */
  bodyId?: BodyId;
  /** Present only for a comet. Scheduling persists this JPL identity and
   * provenance, never the time-resolved preview coordinates. */
  comet?: PlanningComet;
}

/** A `PlanningTarget` for a bare RA/Dec with no catalogue identity —
 * `TargetPicker`'s "Or enter RA/Dec directly" and dragging the map's FOV
 * rectangle off a resolved object (`PlanningTab.tsx`'s `onFovMoved`) both
 * produce exactly this: the pointing is no longer that object's own
 * catalogued position, so its name/magnitude/size/type are not carried
 * forward rather than left stale next to coordinates that no longer match
 * it. One function so both call sites stay in sync on what "no catalogue
 * identity" looks like, instead of two copies of the same object literal. */
export function targetFromCoordinates(raH: number, decDeg: number): PlanningTarget {
  return {
    name: `RA ${raH.toFixed(3)}h / Dec ${decDeg.toFixed(3)}°`,
    raH,
    decDeg,
    magnitude: null,
    sizeMajorArcmin: null,
    positionAngleDeg: null,
    objectType: null,
  };
}
