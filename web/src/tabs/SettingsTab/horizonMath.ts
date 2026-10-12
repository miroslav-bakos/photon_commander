// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pure geometry for the horizon-profile editor — no rendering, no DOM. Kept
 * separate from `HorizonPanel.tsx` so the azimuth-wrap and stats math is
 * unit-testable without mounting anything.
 *
 * Mirrors the server's own conventions (`ssc/astro/horizon.py`, `HorizonRequest`
 * in `ssc/api/schemas.py`) rather than inventing new ones:
 *   - azimuth 0 = due north, increasing eastward, wrapped to [0, 360)
 *   - altitude in [-10, 90] — the same bound the server validates against
 *   - the mask is linearly interpolated between azimuth-sorted points, and
 *     cyclic: the segment from the last point back to the first runs the
 *     short way through north, not backwards through south.
 */
import type { HorizonPointRequest } from "../../api/types";

export const ALTITUDE_MIN_DEG = -10;
export const ALTITUDE_MAX_DEG = 90;

/** Normalises any real azimuth into [0, 360). */
export function wrapAzimuthDeg(deg: number): number {
  const wrapped = deg % 360;
  return wrapped < 0 ? wrapped + 360 : wrapped;
}

export function clampAltitudeDeg(deg: number): number {
  return Math.min(ALTITUDE_MAX_DEG, Math.max(ALTITUDE_MIN_DEG, deg));
}

/** Rounds to the nearest multiple of `step` — pointer input is continuous,
 * but a skyline drawn by hand is more usable (and easier to read back) at a
 * half-degree grid than at floating-point precision. */
export function roundToStep(value: number, step: number): number {
  return Math.round(value / step) * step;
}

export function sortByAzimuth(points: readonly HorizonPointRequest[]): HorizonPointRequest[] {
  return [...points].sort((a, b) => a.azimuth_deg - b.azimuth_deg);
}

/** True if `azimuthDeg` lands within `toleranceDeg` of a point already in the
 * list (after wrapping) — the client-side half of "no duplicate azimuths",
 * checked before ever sending a request so a tap next to an existing point
 * fails softly instead of round-tripping a 422. */
export function azimuthCollides(
  points: readonly HorizonPointRequest[],
  azimuthDeg: number,
  toleranceDeg: number,
  excludeIndex?: number,
): boolean {
  const target = wrapAzimuthDeg(azimuthDeg);
  return points.some((point, index) => {
    if (index === excludeIndex) return false;
    const delta = Math.abs(wrapAzimuthDeg(point.azimuth_deg) - target);
    return Math.min(delta, 360 - delta) < toleranceDeg;
  });
}

/** An azimuth in the middle of the widest gap between existing points — a
 * reasonable default position for a new point added via a button rather than
 * a tap on the plot, and never a duplicate of an existing one. Full circle
 * (0°) when the list is empty. */
export function widestGapMidpointDeg(points: readonly HorizonPointRequest[]): number {
  if (points.length === 0) return 0;
  const only = points[0];
  if (points.length === 1) return only === undefined ? 0 : wrapAzimuthDeg(only.azimuth_deg + 180);
  const sorted = sortByAzimuth(points);
  let bestMid = 0;
  let bestSpan = -1;
  for (let i = 0; i < sorted.length; i++) {
    const current = sorted[i];
    const next = sorted[(i + 1) % sorted.length];
    // Unreachable — both indices are within [0, sorted.length) by
    // construction — but `noUncheckedIndexedAccess` cannot see that.
    if (current === undefined || next === undefined) continue;
    const span = wrapAzimuthDeg(next.azimuth_deg - current.azimuth_deg) || 360;
    if (span > bestSpan) {
      bestSpan = span;
      bestMid = wrapAzimuthDeg(current.azimuth_deg + span / 2);
    }
  }
  return bestMid;
}

export interface HorizonStats {
  minAltitudeDeg: number;
  maxAltitudeDeg: number;
  /** Azimuth-weighted mean altitude around the whole 360°, not a plain
   * average of vertex altitudes — two points nine degrees apart and two
   * points ninety degrees apart should not count equally toward "typical"
   * altitude. Because the mask is piecewise-linear, a plain min/max of the
   * vertex altitudes is exact (a linear segment's extremes are its
   * endpoints), but the mean needs the trapezoid area under each segment. */
  meanAltitudeDeg: number;
}

export function horizonStats(points: readonly HorizonPointRequest[]): HorizonStats | null {
  if (points.length === 0) return null;
  const sorted = sortByAzimuth(points);
  let min = Infinity;
  let max = -Infinity;
  let weightedSum = 0;
  const n = sorted.length;
  for (let i = 0; i < n; i++) {
    const point = sorted[i];
    const next = sorted[(i + 1) % n];
    // Unreachable given the loop bounds; see the same note in
    // `widestGapMidpointDeg`.
    if (point === undefined || next === undefined) continue;
    min = Math.min(min, point.altitude_deg);
    max = Math.max(max, point.altitude_deg);
    const span = wrapAzimuthDeg(next.azimuth_deg - point.azimuth_deg) || 360;
    weightedSum += span * ((point.altitude_deg + next.altitude_deg) / 2);
  }
  return { minAltitudeDeg: min, maxAltitudeDeg: max, meanAltitudeDeg: weightedSum / 360 };
}
