// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Small, local formatting + sign-convention helpers for the Polar align
 * tab's result readout. The sign convention below is not exposed anywhere
 * the frontend can read it — `PolarAlignmentResult`'s OpenAPI schema only
 * carries a one-line field description — so it is transcribed by hand from
 * `ssc/astro/polar.py`'s `PolarAlignmentModel` docstring (its own "stated
 * once and used everywhere" convention) rather than a generated alias.
 * If that convention ever changes, this file has to change with it by
 * hand; there is nothing to regenerate.
 *
 * Every formatter returns `UNKNOWN` for `null`, matching `MountTab/format.ts`'s
 * own rule (invariant 7: `null` means unknown, never a numeric-looking zero).
 */
export const UNKNOWN = "unknown";

export function formatArcsec(value: number | null): string {
  if (value === null) return UNKNOWN;
  return `${value.toFixed(1)}″`;
}

export function formatDeg2(value: number): string {
  return `${value.toFixed(2)}°`;
}

/** PA readouts use degrees with two decimals; calculations retain full precision. */
export function formatAngleMagnitude(valueDeg: number): string {
  return formatDeg2(Math.abs(valueDeg));
}

/** Worst-case PA-only center-field drift budget for the S30 Pro tele camera.
 * Sony IMX585 pixels are 2.9 um; the Seestar tele focal length is 160 mm.
 * Half a native pixel is a conservative round-star threshold, not a promise
 * about tracking, seeing, wind, focus, or field rotation. */
export function s30ProPaSubLimitSeconds(errorDeg: number, deviceModel: string | null): number | null {
  if (deviceModel?.trim().toLowerCase() !== "seestar s30 pro" || !Number.isFinite(errorDeg)) return null;
  const pixelScaleArcsec = 206.265 * 2.9 / 160;
  const driftArcsecPerSecond = 15.041067 * Math.sin(Math.abs(errorDeg) * Math.PI / 180);
  return driftArcsecPerSecond === 0 ? Number.POSITIVE_INFINITY : 0.5 * pixelScaleArcsec / driftArcsecPerSecond;
}

/** The correction's size, in arcminutes — the scale a tripod knob is
 * actually turned by, and legible at arm's length where a fraction of a
 * degree would read as "0.1°" and look like nothing. */
export function formatCorrectionMagnitudeArcmin(correctionDeg: number): string {
  return `${(Math.abs(correctionDeg) * 60).toFixed(1)}′`;
}

/**
 * `altitude_correction_deg > 0` means move the wedge axis upward —
 * the axis is too low (`ssc.astro.polar.PolarAlignmentModel.altitude_correction_deg`'s
 * own docstring: "Positive means raise the front of the wedge.").
 */
export function altitudeInstruction(correctionDeg: number): string {
  return correctionDeg >= 0 ? "UP" : "DOWN";
}

/**
 * The backend sign is clockwise/counter-clockwise as seen from above. The
 * UI has the observing latitude, so translate that sign into the user's
 * requested EAST/WEST instruction without losing the hemisphere reversal.
 */
export function azimuthInstruction(correctionDeg: number, latitudeDeg: number): string {
  const positiveIsEast = latitudeDeg >= 0;
  const east = correctionDeg >= 0 ? positiveIsEast : !positiveIsEast;
  return east ? "EAST" : "WEST";
}

export function formatCorrectionMagnitudeArcsec(correctionDeg: number): string {
  return `${String(Math.round(Math.abs(correctionDeg) * 3600))}″`;
}

/** Minutes since `epochIso`, or `null` if there is no result yet or the
 * timestamp cannot be parsed. */
export function minutesSince(epochIso: string, nowMs: number): number | null {
  const then = Date.parse(epochIso);
  if (Number.isNaN(then)) return null;
  return Math.max(0, (nowMs - then) / 60_000);
}

/** DESIGN.md §4.5: "if you do PA for more than a few minutes, its good to
 * rerun whole procedure again" (owner, 2026-09-10) — the sky has moved on
 * from the moment the three-point model was fitted. */
export const RESULT_STALE_AFTER_MINUTES = 5;

/** Sexagesimal RA/Dec, local to this tab per this file's own module
 * comment (no cross-tab reuse of `MountTab/format.ts`'s copy) — same
 * implementation, kept independent on purpose. */
export function formatRaHours(hours: number | null): string {
  if (hours === null) return UNKNOWN;
  const h = ((hours % 24) + 24) % 24;
  const totalSeconds = Math.round(h * 3600);
  const hh = Math.floor(totalSeconds / 3600);
  const mm = Math.floor((totalSeconds % 3600) / 60);
  const ss = totalSeconds % 60;
  return `${String(hh).padStart(2, "0")}h ${String(mm).padStart(2, "0")}m ${String(ss).padStart(2, "0")}s`;
}

export function formatDecDeg(deg: number | null): string {
  if (deg === null) return UNKNOWN;
  const sign = deg < 0 ? "-" : "+";
  const abs = Math.abs(deg);
  const totalArcsec = Math.round(abs * 3600);
  const dd = Math.floor(totalArcsec / 3600);
  const mm = Math.floor((totalArcsec % 3600) / 60);
  const ss = totalArcsec % 60;
  return `${sign}${String(dd).padStart(2, "0")}° ${String(mm).padStart(2, "0")}′ ${String(ss).padStart(2, "0")}″`;
}

/** Great-circle separation between two J2000 RA/Dec points, in arcminutes —
 * "the difference between the two is the measurement" (the solve-here
 * route's own docstring). Exact spherical law of cosines, not a small-angle
 * approximation: nothing here bounds the disagreement to a few arcseconds,
 * and a mispointed preset is exactly the case this exists to catch. */
export function angularSeparationArcmin(
  raHours1: number,
  decDeg1: number,
  raHours2: number,
  decDeg2: number,
): number {
  const toRad = Math.PI / 180;
  const ra1 = raHours1 * 15 * toRad;
  const ra2 = raHours2 * 15 * toRad;
  const dec1 = decDeg1 * toRad;
  const dec2 = decDeg2 * toRad;
  const cosSeparation =
    Math.sin(dec1) * Math.sin(dec2) + Math.cos(dec1) * Math.cos(dec2) * Math.cos(ra1 - ra2);
  const separationRad = Math.acos(Math.min(1, Math.max(-1, cosSeparation)));
  return (separationRad / toRad) * 60;
}
