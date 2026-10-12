// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Greenwich / local mean sidereal time, and the alt/az <-> RA/Dec rotation
 * it feeds, for the frame's own reference lines and grids (the local
 * meridian and zenith marker, the alt-az grid and horizon line —
 * `skyLines.ts`).
 *
 * This is the one deliberate exception to "every sky number comes from
 * `astro/` through the API" (skymap-viz.md): the meridian and alt-az grid
 * must follow the scrubber instant inside the 16 ms budget, and a round trip
 * per scrub step is exactly what that budget rules out. The formula is the
 * standard IAU 1982 GMST polynomial (Aoki et al. 1982; Meeus, *Astronomical
 * Algorithms* 12.2), UT taken as UT1 — an error of under a second of time,
 * which is under 0.004 deg on the sky and invisible at any zoom this map
 * allows. It is not used for any planning answer: parallactic angle, the
 * keep-region and event times all still come from `ssc/astro`.
 *
 *   T   = (JD₀ − 2451545.0) / 36525        (JD₀: the preceding 0h UT)
 *   GMST₀ = 24110.54841 + 8640184.812866·T + 0.093104·T² − 6.2e-6·T³   [s]
 *   GMST  = GMST₀ + 1.00273790935 · UT                                  [s]
 *   LMST  = GMST + λ_east / 15                                          [h]
 *
 * `raDecToAltAz`/`altAzToRaDec` are the standard spherical-triangle
 * transform (Duffett-Smith & Zwart, *Practical Astronomy with your
 * Calculator or Spreadsheet*, ch. 25), with azimuth measured from North,
 * clockwise through East (0=N, 90=E, 180=S, 270=W). The two functions are
 * literally the same formula with (dec, H) swapped for (alt, az) — the
 * Pole/Zenith/Star triangle is the same triangle viewed from either vertex
 * — which is what makes the round trip exact to floating-point precision
 * away from the zenith/nadir, where azimuth is undefined by construction.
 */

const MS_PER_DAY = 86_400_000;
/** JD of the Unix epoch, 1970-01-01T00:00:00Z. */
const JD_UNIX_EPOCH = 2_440_587.5;
const JD_J2000 = 2_451_545.0;
/** Ratio of a sidereal day's worth of UT to a mean solar day. */
const SIDEREAL_RATE = 1.00273790935;

/** Greenwich mean sidereal time in hours, [0, 24). */
export function greenwichMeanSiderealTimeHours(instant: Date): number {
  const jd = instant.getTime() / MS_PER_DAY + JD_UNIX_EPOCH;
  const jd0 = Math.floor(jd - 0.5) + 0.5;
  const utHours = (jd - jd0) * 24;
  const t = (jd0 - JD_J2000) / 36525;
  const gmst0Seconds = 24110.54841 + 8640184.812866 * t + 0.093104 * t * t - 6.2e-6 * t * t * t;
  const hours = gmst0Seconds / 3600 + utHours * SIDEREAL_RATE;
  return ((hours % 24) + 24) % 24;
}

/** Local mean sidereal time in hours, [0, 24), for an east-positive longitude. */
export function localMeanSiderealTimeHours(instant: Date, longitudeDegEast: number): number {
  const hours = greenwichMeanSiderealTimeHours(instant) + longitudeDegEast / 15;
  return ((hours % 24) + 24) % 24;
}

/** The shorter-way-round distance between two RA-like values in degrees —
 * shared by `skyLines.ts` (ranking equatorial-grid label candidates by
 * distance from the view centre) and `SkyMap.tsx` (whether the meridian, at
 * RA = LST, is inside the current view or needs the off-screen hint). Lives
 * here rather than in either caller because it is the same kind of pure
 * angle arithmetic as the rest of this module, not because it needs LST. */
export function circularDistanceDeg(a: number, b: number): number {
  const d = Math.abs(((a - b) % 360) + 360) % 360;
  return d > 180 ? 360 - d : d;
}

import type { CoordTransform } from "../../projection";

const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;

/** Site coordinates for `raDecToAltAz`/`altAzToRaDec` — plain fields (not
 * `types.ts`'s `DisplayOptions["site"]`) so this module never imports the
 * shape vocabulary; `skyLines.ts` bridges the two. */
export interface SiteInstant {
  latitudeDeg: number;
  longitudeDeg: number;
  instant: Date;
}

/** Equatorial (RA, Dec, deg) -> horizontal (Alt, Az, deg) for a site and
 * instant. Az is North=0, East=90 — see the module doc comment. */
export function raDecToAltAz(raDeg: number, decDeg: number, site: SiteInstant): [altDeg: number, azDeg: number] {
  const lstDeg = localMeanSiderealTimeHours(site.instant, site.longitudeDeg) * 15;
  const hourAngle = (((lstDeg - raDeg) % 360) + 360) % 360;
  return equatorialToHorizontal(hourAngle, decDeg, site.latitudeDeg);
}

/** The inverse of `raDecToAltAz`: horizontal (Alt, Az, deg) -> equatorial
 * (RA, Dec, deg) for the same site and instant. Undefined (any RA is as
 * valid as any other) exactly at the zenith/nadir, where `altDeg` is ±90 —
 * this returns the LST for RA there, an arbitrary but stable choice. */
export function altAzToRaDec(altDeg: number, azDeg: number, site: SiteInstant): [raDeg: number, decDeg: number] {
  const [decDeg, hourAngleDeg] = equatorialToHorizontal(azDeg, altDeg, site.latitudeDeg);
  const lstDeg = localMeanSiderealTimeHours(site.instant, site.longitudeDeg) * 15;
  const raDeg = (((lstDeg - hourAngleDeg) % 360) + 360) % 360;
  return [raDeg, decDeg];
}

/**
 * The shared spherical-triangle transform both directions reduce to: given
 * an hour-angle-like coordinate `hDeg` (hour angle for alt/az, azimuth for
 * ra/dec) and a declination-like coordinate `decLikeDeg` (declination for
 * alt/az, altitude for ra/dec), returns (altitude-like, azimuth-like) —
 * i.e. calling it once gives (Alt, Az) from (H, Dec), and calling it again
 * with (Az, Alt) gives back (Dec, H). Kept private: the public functions'
 * names say which direction and in which units, which a shared name cannot.
 */
function equatorialToHorizontal(hDeg: number, decLikeDeg: number, latitudeDeg: number): [number, number] {
  const h = hDeg * DEG2RAD;
  const decLike = decLikeDeg * DEG2RAD;
  const lat = latitudeDeg * DEG2RAD;
  const sinAltLike = Math.sin(decLike) * Math.sin(lat) + Math.cos(decLike) * Math.cos(lat) * Math.cos(h);
  const altLike = Math.asin(Math.max(-1, Math.min(1, sinAltLike)));
  const cosAltLike = Math.cos(altLike);
  // At the zenith/nadir (cosAltLike ~ 0) the azimuth-like output is
  // undefined; 0 is as valid as any other value and keeps this finite.
  const sinAzLike = cosAltLike < 1e-9 ? 0 : (-Math.sin(h) * Math.cos(decLike)) / cosAltLike;
  const cosAzLike = cosAltLike < 1e-9 ? 1 : (Math.sin(decLike) - Math.sin(altLike) * Math.sin(lat)) / (cosAltLike * Math.cos(lat));
  const azLike = (((Math.atan2(sinAzLike, cosAzLike) * RAD2DEG) % 360) + 360) % 360;
  return [altLike * RAD2DEG, azLike];
}

/**
 * The parallactic angle at (`raDeg`, `decDeg`) for this site and instant, in
 * degrees, wrapped to (-180, +180].
 *
 * This is the angle between "toward the north celestial pole" and "toward the
 * zenith" as seen at that point on the sky — which is exactly the roll a
 * pole-up map needs in order to become a zenith-up one. Rotating the view by
 * `-q` puts the local vertical on screen-up, so the horizon lies flat along
 * the bottom instead of cutting across as an inclined plate (owner, 2026-09-08:
 * "polaris is on top but on the sky it's north but not so much up at my
 * 48degs ... could we have toggle between equatorial and 'what i see in my
 * backyard'").
 *
 * Meeus 14.1, the same formula and the same wrap as
 * `ssc.astro.framing.parallactic_angle_deg`, which is the canonical
 * implementation — this exists as a local copy for the same reason the rest of
 * this module does: it is needed per view frame as the user pans, and a round
 * trip per frame is not affordable inside the scrubber's 16 ms budget. If the
 * two ever disagree, `ssc/astro/` is right.
 *
 * At the zenith itself (`dec == latitude` and the point on the meridian) both
 * `atan2` arguments are zero and the angle is genuinely undefined; `atan2`
 * returns 0 there, which is as good as any other value for a roll. The
 * geographic poles are rejected outright, matching the Python: `tan(±90°)`
 * does not exist, and a map centred exactly on the pole has no local vertical
 * to align to.
 */
export function parallacticAngleDeg(raDeg: number, decDeg: number, site: SiteInstant): number {
  if (Math.abs(site.latitudeDeg) >= 90) {
    throw new Error(
      `parallactic angle is undefined at latitude ${String(site.latitudeDeg)}: tan(phi) diverges at the poles`,
    );
  }
  const lstDeg = localMeanSiderealTimeHours(site.instant, site.longitudeDeg) * 15;
  // Hour angle negative east of the meridian, positive west — the convention
  // the Python's own docstring states, so the two agree in sign.
  const hourAngle = (((lstDeg - raDeg + 180) % 360) + 360) % 360 - 180;
  const h = hourAngle * DEG2RAD;
  const dec = decDeg * DEG2RAD;
  const lat = site.latitudeDeg * DEG2RAD;
  const q = Math.atan2(Math.sin(h), Math.tan(lat) * Math.cos(dec) - Math.sin(dec) * Math.cos(h));
  const deg = q * RAD2DEG;
  return deg <= -180 ? deg + 360 : deg;
}

/**
 * (Altitude, azimuth) -> the projected pair `horizontalCoordTransform` feeds
 * `worldToScreen`/`screenToWorld` (negated azimuth first — see that
 * function's own doc comment for why). Exported on its own, not just inlined
 * inside the transform below, so `frames/vector/main.ts` can convert its own
 * *already-known* alt/az camera state into projected coordinates directly —
 * without a round trip through `raDecToAltAz`, which is exactly the
 * ill-conditioned inverse that produced the zenith-crossing jump this pair
 * of functions exists to let a caller avoid (see that file's own top note).
 */
export function projectedFromAltAz(altDeg: number, azDeg: number): [projRaDeg: number, projDecDeg: number] {
  return [normalizeAzDeg(-azDeg), altDeg];
}

/** The inverse of `projectedFromAltAz` above. */
export function altAzFromProjected(projRaDeg: number, projDecDeg: number): [altDeg: number, azDeg: number] {
  return [projDecDeg, normalizeAzDeg(-projRaDeg)];
}

/**
 * The `projection.ts` `CoordTransform` for a *true* horizontal (alt-az)
 * view: `worldToScreen`/`screenToWorld` are a generic gnomonic projection of
 * whatever coordinate pair they are handed, so feeding them (-azimuth,
 * altitude) instead of (RA, Dec) — for both the point being projected and
 * the view centre — projects in alt-az instead, with no second projection
 * implementation needed.
 *
 * Azimuth is negated (and re-negated on the way back) because `worldToScreen`
 * puts increasing RA on the *left* — correct for the sky, since RA increases
 * eastward and east is left there. Azimuth also increases through east, so
 * feeding it in unchanged would put increasing azimuth — east — on the left
 * too: facing local north, east (Az 90) would land on the wrong side of the
 * screen (it belongs on the *right*, over your right shoulder). Negating
 * azimuth first restores the correct handedness — verified directly in
 * `sidereal.test.ts` rather than trusted from this reasoning alone, since a
 * mirrored sky is the kind of bug that ships unnoticed.
 *
 * `raDecToAltAz`/`altAzToRaDec` above are exact inverses of each other (this
 * module's own doc comment), so `toProjected`/`fromProjected` round-trip to
 * floating-point precision away from the zenith/nadir, the one property
 * `screenToWorld(worldToScreen(p)) === p` depends on.
 */
export function horizontalCoordTransform(site: SiteInstant): CoordTransform {
  return {
    toProjected(raDeg: number, decDeg: number): [number, number] {
      const [altDeg, azDeg] = raDecToAltAz(raDeg, decDeg, site);
      return projectedFromAltAz(altDeg, azDeg);
    },
    fromProjected(negAzDeg: number, altDeg: number): [number, number] {
      const [alt, az] = altAzFromProjected(negAzDeg, altDeg);
      return altAzToRaDec(alt, az, site);
    },
  };
}

/** Azimuth wrapped into [0, 360) — `raDecToAltAz`/`altAzToRaDec`'s own
 * convention, which negating for `projectedFromAltAz`/`horizontalCoordTransform`
 * above can carry outside that range. */
function normalizeAzDeg(azDeg: number): number {
  return ((azDeg % 360) + 360) % 360;
}
