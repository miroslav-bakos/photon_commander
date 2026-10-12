// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The boundary between the mount's own reported equatorial frame and the
 * J2000 frame everything else on the sky map uses (the bundled catalogue,
 * plate solves, the frame drawn in the `<iframe>`) — DESIGN.md §3.5.1's
 * "frame trap". The S30 Pro reports `EquatorialSystem: topocentric`
 * (coordinates *of date*); in 2026 that is ~20 arcminutes from J2000, the
 * same offset in every direction, so it reads as pointing error rather than
 * noise if drawn or sent unconverted.
 *
 * **This module does not perform the conversion.** `ssc/astro/frames.py`
 * (astro-math's module — `to_j2000`/`to_mount_frame`, a full skyfield
 * apparent-place chain: frame bias, precession, nutation, annual and
 * diurnal aberration, verified to 0.079 arcsec against JPL Horizons) is
 * where that belongs; architecture policy is explicit that a second implementation of
 * precession in the browser is exactly the divergence this project's
 * sky-map history warns about. The server now publishes converted J2000
 * mount coordinates and accepts J2000 slew/sync targets, converting those
 * outbound commands at the device boundary. This legacy fallback is only
 * for drawing snapshots from an older server that lack the J2000 fields.
 */

/** `MountState.equatorial_system` verbatim — `string | null`, never a
 * literal union, because the device can report any of ASCOM's five
 * `EquatorialCoordinateType` values (or none yet) and this module must not
 * silently mislabel one it does not recognise (mirrors `mount.ts`'s own
 * doc comment on the field). */
export type MountEquatorialFrame = string | null;

export type MountFrameConversion =
  | { status: "ok"; raHours: number; decDeg: number }
  | { status: "unavailable"; reason: string };

/** The only frame this module can pass through today: an exact (case-
 * insensitive) match for ASCOM's `equJ2000`. Not `startsWith`/`includes` —
 * a driver reporting something merely containing "J2000" in a longer label
 * is exactly the kind of near-miss `mountFrameFromAscom` (`frames.py`)
 * exists to name and refuse rather than pattern-match past. */
function isJ2000Label(equatorialSystem: string): boolean {
  return /^j2000$/i.test(equatorialSystem.trim());
}

/**
 * Of-date (or whatever `equatorialSystem` reports) -> J2000, for drawing.
 * `raHours`/`decDeg` are the mount's own reported position
 * (`MountState.ra_hours`/`dec_deg`) — pass `null` straight through from a
 * mount that has not reported a position yet, rather than a caller
 * inventing a 0.
 */
export function mountPositionToJ2000(params: {
  raHours: number | null;
  decDeg: number | null;
  equatorialSystem: MountEquatorialFrame;
}): MountFrameConversion {
  const { raHours, decDeg, equatorialSystem } = params;
  if (raHours === null || decDeg === null) {
    return { status: "unavailable", reason: "mount has not reported a position yet" };
  }
  if (equatorialSystem === null) {
    return {
      status: "unavailable",
      reason: "mount's equatorial frame is unknown; refusing to guess it is J2000",
    };
  }
  if (isJ2000Label(equatorialSystem)) {
    return { status: "ok", raHours, decDeg };
  }
  return {
    status: "unavailable",
    reason:
      `mount reports its position in its own "${equatorialSystem}" frame, not J2000, and Photon Commander does not ` +
      `yet convert it for drawing (ssc/astro/frames.py's to_j2000 is not wired to an endpoint yet)`,
  };
}
