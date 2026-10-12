// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pure geometry + naming for the moon glyph. No I/O, no theme, no React —
 * kept separate from `MoonGlyph.tsx` so the trap this module exists to avoid
 * is testable without rendering anything.
 *
 * THE TRAP (architecture policy): illuminated fraction alone cannot tell first quarter
 * apart from last quarter — both are 50% illuminated and are mirror images
 * of each other. The limb has to be drawn from the waxing/waning direction,
 * not guessed from the fraction. This module takes `waxing` as a required
 * input for exactly that reason; there is no overload that omits it.
 *
 * Southern-hemisphere flip: a waxing moon viewed from the southern
 * hemisphere is lit on the *left*, the same shape a waning moon shows in the
 * north — so hemisphere and waxing/waning combine by XOR into one
 * `litOnRight` boolean that the drawing code treats identically either way.
 */

/** Which side of the disc carries the fixed, always-present outer limb —
 * the boundary that stays on the same side of the disc for the whole
 * waxing (or whole waning) half of the cycle, with only the terminator
 * (the other edge) sweeping across as the fraction changes. */
export function isLitOnRight(waxing: boolean, hemisphere: "north" | "south"): boolean {
  // North: waxing lights up from the right (first quarter shows the right
  // half lit); waning mirrors it. South: everything is mirrored again.
  const northLitRight = waxing;
  return hemisphere === "north" ? northLitRight : !northLitRight;
}

export interface MoonPathOptions {
  /** Illuminated fraction, 0 (new) – 1 (full). Clamped to [0, 1]. */
  illuminatedFraction: number;
  waxing: boolean;
  hemisphere: "north" | "south";
  /** Disc radius in the same units as the returned path's coordinates. */
  radius: number;
  /** Centre of the disc. Defaults to (radius, radius) — i.e. a viewBox
   * exactly `2*radius` square with no margin. */
  cx?: number;
  cy?: number;
}

/**
 * The SVG path `d` for the *lit* region of the disc, as two arcs: a fixed
 * semicircle on the limb side (`isLitOnRight`) and a terminator arc whose
 * horizontal radius shrinks to 0 at exactly half-illumination (a straight
 * terminator — first/last quarter) and grows back out to the far side at
 * f=0 or f=1 (coinciding with the fixed arc — zero-area at new moon, a full
 * circle at full moon).
 *
 * Derivation lives in the frontend-react agent's working notes for this
 * ticket; the boundary cases below are what's actually load-bearing and are
 * pinned by `moonPhase.test.ts`:
 *   f=0   -> zero-area path (new moon: nothing lit)
 *   f=0.5 -> terminator radius 0 (a straight edge: exact quarter)
 *   f=1   -> terminator radius = radius, opposite side (full disc lit)
 */
export function moonPath({
  illuminatedFraction,
  waxing,
  hemisphere,
  radius: r,
  cx = r,
  cy = r,
}: MoonPathOptions): string {
  const f = Math.min(1, Math.max(0, illuminatedFraction));
  const litRight = isLitOnRight(waxing, hemisphere);

  // arc1 (top -> bottom) sweep flag for "right": 1. For "left": 0.
  const arc1Sweep = litRight ? 1 : 0;

  // The terminator sits on the lit side while crescent (f < 0.5, a thin
  // lens against the limb) and flips to the dark side once gibbous
  // (f >= 0.5, the limb is now the far edge and the terminator carves the
  // remaining dark sliver out of the opposite side).
  const terminatorOnRight = f < 0.5 ? litRight : !litRight;
  // arc2 (bottom -> top) sweep flag for "right": 0. For "left": 1.
  const arc2Sweep = terminatorOnRight ? 0 : 1;

  const rx = Math.abs(1 - 2 * f) * r;
  const top = `${cx.toString()} ${(cy - r).toString()}`;
  const bottom = `${cx.toString()} ${(cy + r).toString()}`;
  const rStr = r.toString();

  return (
    `M ${top} A ${rStr} ${rStr} 0 0 ${arc1Sweep.toString()} ${bottom} ` +
    `A ${rx.toString()} ${rStr} 0 0 ${arc2Sweep.toString()} ${top} Z`
  );
}

export type PhaseName =
  | "New moon"
  | "Waxing crescent"
  | "First quarter"
  | "Waxing gibbous"
  | "Full moon"
  | "Waning gibbous"
  | "Last quarter"
  | "Waning crescent";

/** Named phase from fraction + direction, for the accessible label and the
 * expanded detail — never derived from fraction alone (same trap as the
 * glyph itself: 50% is ambiguous without `waxing`). */
export function phaseName(illuminatedFraction: number, waxing: boolean): PhaseName {
  const f = Math.min(1, Math.max(0, illuminatedFraction));
  if (f <= 0.02) return "New moon";
  if (f >= 0.98) return "Full moon";
  if (Math.abs(f - 0.5) <= 0.02) return waxing ? "First quarter" : "Last quarter";
  if (f < 0.5) return waxing ? "Waxing crescent" : "Waning crescent";
  return waxing ? "Waxing gibbous" : "Waning gibbous";
}
