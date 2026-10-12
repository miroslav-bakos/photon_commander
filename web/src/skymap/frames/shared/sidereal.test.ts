// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import siderealAlmanac from "./__fixtures__/siderealAlmanac.json";
import {
  altAzToRaDec,
  greenwichMeanSiderealTimeHours,
  horizontalCoordTransform,
  localMeanSiderealTimeHours,
  parallacticAngleDeg,
  raDecToAltAz,
  type SiteInstant,
} from "./sidereal";

const SECONDS_OF_TIME = 1 / 3600;

describe("greenwichMeanSiderealTimeHours", () => {
  it("gives 18h41m50.55s at J2000.0 (2000-01-01 12:00 UTC) — the IAU 1982 reference value", () => {
    // 18h 41m 50.54841s: the constant term of the polynomial itself, plus
    // twelve hours of UT at the sidereal rate.
    const expectedHours = 18 + 41 / 60 + 50.54841 / 3600;
    const gmst = greenwichMeanSiderealTimeHours(new Date("2000-01-01T12:00:00Z"));
    expect(Math.abs(gmst - expectedHours)).toBeLessThan(0.1 * SECONDS_OF_TIME);
  });

  it("advances by 24h of sidereal time per sidereal day, wrapping at 24h", () => {
    const start = new Date("2026-09-07T21:00:00Z");
    const oneSiderealDayLater = new Date(start.getTime() + 86_164_090.5);
    const a = greenwichMeanSiderealTimeHours(start);
    const b = greenwichMeanSiderealTimeHours(oneSiderealDayLater);
    expect(Math.abs(a - b)).toBeLessThan(0.01 * SECONDS_OF_TIME);
    expect(a).toBeGreaterThanOrEqual(0);
    expect(a).toBeLessThan(24);
  });
});

describe("localMeanSiderealTimeHours", () => {
  it("adds east longitude at 15 deg per hour and wraps", () => {
    const instant = new Date("2000-01-01T12:00:00Z");
    const gmst = greenwichMeanSiderealTimeHours(instant);
    expect(localMeanSiderealTimeHours(instant, 15)).toBeCloseTo((gmst + 1) % 24, 9);
    expect(localMeanSiderealTimeHours(instant, -90)).toBeCloseTo((gmst - 6 + 24) % 24, 9);
    // 18.697h + 6h wraps past 24h.
    expect(localMeanSiderealTimeHours(instant, 90)).toBeCloseTo((gmst + 6) % 24, 9);
  });
});

/** Wrap-safe angle difference, degrees, in (-180, 180]. */
function angleDiffDeg(a: number, b: number): number {
  return (((a - b + 540) % 360) + 360) % 360 - 180;
}

describe("raDecToAltAz / altAzToRaDec", () => {
  const site = { latitudeDeg: 48.1486, longitudeDeg: 17.1077, instant: new Date("2026-09-07T22:00:00Z") };

  it("puts the zenith (alt=90) at RA = LST, Dec = latitude, both directions", () => {
    const lstDeg = localMeanSiderealTimeHours(site.instant, site.longitudeDeg) * 15;

    const [alt] = raDecToAltAz(lstDeg, site.latitudeDeg, site);
    expect(alt).toBeCloseTo(90, 6);

    const [raDeg, decDeg] = altAzToRaDec(90, 0, site);
    expect(Math.abs(angleDiffDeg(raDeg, lstDeg))).toBeLessThan(1e-6);
    expect(decDeg).toBeCloseTo(site.latitudeDeg, 6);
  });

  it("round-trips alt/az -> ra/dec -> alt/az to under 1e-6 deg, away from the zenith", () => {
    const cases: [alt: number, az: number][] = [
      [45, 30],
      [10, 200],
      [60, 350],
      [5, 90],
      [80, 270],
      [-20, 135],
    ];
    for (const [alt, az] of cases) {
      const [ra, dec] = altAzToRaDec(alt, az, site);
      const [alt2, az2] = raDecToAltAz(ra, dec, site);
      expect(Math.abs(alt2 - alt)).toBeLessThan(1e-6);
      expect(Math.abs(angleDiffDeg(az2, az))).toBeLessThan(1e-6);
    }
  });

  it("round-trips ra/dec -> alt/az -> ra/dec to under 1e-6 deg", () => {
    const cases: [ra: number, dec: number][] = [
      [10, 20],
      [200, -30],
      [350, 60],
      [90, 5],
      [270, -60],
    ];
    for (const [ra, dec] of cases) {
      const [alt, az] = raDecToAltAz(ra, dec, site);
      const [ra2, dec2] = altAzToRaDec(alt, az, site);
      expect(Math.abs(angleDiffDeg(ra2, ra))).toBeLessThan(1e-6);
      expect(Math.abs(dec2 - dec)).toBeLessThan(1e-6);
    }
  });
});

/** Great-circle separation between two RA/Dec points, in degrees — the only
 * honest way to compare against the almanac fixture: a per-axis RA/Dec diff
 * is meaningless near the pole (RA is degenerate there) and wrong across the
 * 0h/24h RA wrap, which several fixture rows sit near by construction. */
function angularSeparationDeg(ra1: number, dec1: number, ra2: number, dec2: number): number {
  const d2r = Math.PI / 180;
  const cosSep =
    Math.sin(dec1 * d2r) * Math.sin(dec2 * d2r) +
    Math.cos(dec1 * d2r) * Math.cos(dec2 * d2r) * Math.cos((ra1 - ra2) * d2r);
  return Math.acos(Math.max(-1, Math.min(1, cosSep))) * (180 / Math.PI);
}

interface AlmanacRow {
  site: string;
  latitudeDeg: number;
  longitudeDeg: number;
  instant: string;
  altDeg: number;
  azDeg: number;
  raDeg: number;
  decDeg: number;
}

/**
 * Cross-validation against skyfield (`__fixtures__/siderealAlmanac.json`,
 * generated per its own README) — 7 sites (Arctic Circle to sub-Antarctic,
 * both signs of longitude, near +-180) x 6 instants across a year (both
 * solstices, the equinox, both 2026 DST transition dates) x 11 alt/az cases
 * (the horizon at all four cardinal azimuths, mid/high altitude, near-zenith
 * and the zenith singularity) = 462 rows.
 *
 * Tolerance is 20 arcsec, not sub-arcsecond: `sidereal.ts`'s own doc comment
 * chooses *mean* sidereal time deliberately (skyfield's `radec(epoch='date')`
 * is apparent, i.e. true-equinox-of-date), so the equation of the equinoxes
 * (nutation in RA) is an expected, accepted gap between the two, not a bug —
 * it can reach ~1s of time (~15 arcsec of RA) at worst across a year. The
 * measured worst case over this fixture's 462 rows is 10.2 arcsec (see this
 * file's own worst-case assertion below, which prints the actual number
 * rather than only pass/fail), comfortably inside 20 arcsec with headroom for
 * a slightly larger nutation excursion on a date this fixture does not
 * sample, while still an order of magnitude tighter than a real wrap, sign
 * or pole bug would produce.
 */
describe("altAzToRaDec vs skyfield (independent almanac)", () => {
  const rows = siderealAlmanac.rows as AlmanacRow[];
  const TOLERANCE_DEG = 20 / 3600;

  it("has the expected fixture shape (7 sites x 6 instants x 11 alt/az cases)", () => {
    expect(rows.length).toBe(462);
  });

  it.each(rows.map((row, i) => [i, row] as const))("row %i: %s", (_i, row) => {
    const [raDeg, decDeg] = altAzToRaDec(row.altDeg, row.azDeg, {
      latitudeDeg: row.latitudeDeg,
      longitudeDeg: row.longitudeDeg,
      instant: new Date(row.instant),
    });
    const sepDeg = angularSeparationDeg(raDeg, decDeg, row.raDeg, row.decDeg);
    expect(sepDeg).toBeLessThan(TOLERANCE_DEG);
  });

  it("reports the actual worst-case deviation across the whole fixture (not just pass/fail)", () => {
    const firstRow = rows[0];
    if (firstRow === undefined) throw new Error("fixture is empty");
    let worst: { sepDeg: number; row: AlmanacRow } = { sepDeg: -1, row: firstRow };
    for (const row of rows) {
      const [raDeg, decDeg] = altAzToRaDec(row.altDeg, row.azDeg, {
        latitudeDeg: row.latitudeDeg,
        longitudeDeg: row.longitudeDeg,
        instant: new Date(row.instant),
      });
      const sepDeg = angularSeparationDeg(raDeg, decDeg, row.raDeg, row.decDeg);
      if (sepDeg > worst.sepDeg) worst = { sepDeg, row };
    }
    // Deliberate: the worst-case deviation is the number this whole test
    // exists to report, not just whether it passed (astro-math.md's
    // verification standard).
    console.log(
      `sidereal.ts vs skyfield worst case: ${(worst.sepDeg * 3600).toFixed(2)} arcsec ` +
        `(${worst.row.site}, ${worst.row.instant}, alt=${String(worst.row.altDeg)} az=${String(worst.row.azDeg)})`,
    );
    expect(worst.sepDeg).toBeLessThan(TOLERANCE_DEG);
  });
});

describe("parallacticAngleDeg", () => {
  // 48.15N: the owner's own latitude, and the one that prompted this — a
  // pole-up map there tilts the horizon by 90 - 48.15 = 41.85 deg.
  const site = { latitudeDeg: 48.15, longitudeDeg: 17.11, instant: new Date("2026-09-07T22:00:00Z") };
  const lstDeg = localMeanSiderealTimeHours(site.instant, site.longitudeDeg) * 15;

  it("is zero on the meridian for a target south of the zenith", () => {
    // On the meridian sin(H) = 0, so q is 0 or 180 depending only on the sign
    // of the denominator: 0 when dec < latitude (the target is between the
    // zenith and the southern horizon, pole and zenith on the same side).
    expect(parallacticAngleDeg(lstDeg, 10, site)).toBeCloseTo(0, 9);
  });

  it("is 180 on the meridian for a target between the zenith and the pole", () => {
    // dec > latitude: the zenith is now south of the target, so "toward the
    // pole" and "toward the zenith" point opposite ways. Not a discontinuity
    // bug — the flip is real, and it is why a zenith-up roll must be taken
    // from the view centre rather than assumed continuous across the zenith.
    expect(Math.abs(parallacticAngleDeg(lstDeg, 70, site))).toBeCloseTo(180, 9);
  });

  it("changes sign either side of the meridian", () => {
    // Hour angle negative east, positive west (matching
    // ssc.astro.framing.parallactic_angle_deg's stated convention), so q runs
    // negative before transit and positive after.
    const east = parallacticAngleDeg(lstDeg + 30, 10, site);
    const west = parallacticAngleDeg(lstDeg - 30, 10, site);
    expect(east).toBeLessThan(0);
    expect(west).toBeGreaterThan(0);
    expect(east).toBeCloseTo(-west, 9);
  });

  it("stays inside (-180, +180]", () => {
    for (let raDeg = 0; raDeg < 360; raDeg += 7) {
      for (const decDeg of [-80, -20, 0, 20, 48.15, 80]) {
        const q = parallacticAngleDeg(raDeg, decDeg, site);
        expect(q).toBeGreaterThan(-180);
        expect(q).toBeLessThanOrEqual(180);
      }
    }
  });

  it("refuses the geographic poles rather than returning a number", () => {
    // tan(+-90) does not exist and a map centred there has no local vertical
    // to align to — the same refusal ssc/astro/framing.py makes.
    expect(() => parallacticAngleDeg(0, 0, { ...site, latitudeDeg: 90 })).toThrow(/undefined at latitude/);
  });
});

describe("horizontalCoordTransform", () => {
  const sites: SiteInstant[] = [
    { latitudeDeg: 48.1486, longitudeDeg: 17.1077, instant: new Date("2026-09-07T22:00:00Z") },
    { latitudeDeg: -33.8688, longitudeDeg: 151.2093, instant: new Date("2026-03-20T12:00:00Z") },
    { latitudeDeg: 66.5, longitudeDeg: -20, instant: new Date("2026-06-21T00:00:00Z") },
  ];

  it("round-trips ra/dec -> projected -> ra/dec across several sites, instants and points", () => {
    const points: [ra: number, dec: number][] = [
      [10, 20],
      [200, -30],
      [350, 60],
      [90, 5],
      [0.2, -80],
    ];
    for (const site of sites) {
      const transform = horizontalCoordTransform(site);
      for (const [ra, dec] of points) {
        const [pRa, pDec] = transform.toProjected(ra, dec);
        const [ra2, dec2] = transform.fromProjected(pRa, pDec);
        // Wrap-safe RA comparison: fine at these test points, none of which
        // sit at the zenith/nadir where azimuth (and so the round trip) is
        // undefined by construction (this module's own doc comment).
        const raDiff = (((ra2 - ra + 540) % 360) + 360) % 360 - 180;
        expect(Math.abs(raDiff)).toBeLessThan(1e-6);
        expect(dec2).toBeCloseTo(dec, 6);
      }
    }
  });

  it("negates azimuth: due-east (Az 90) maps to RA-like -90 (wrapped to 270)", () => {
    const site = sites[0];
    if (site === undefined) throw new Error("fixture is empty");
    const transform = horizontalCoordTransform(site);
    const [eastRa, eastDec] = altAzToRaDec(20, 90, site);
    const [pRa, pDec] = transform.toProjected(eastRa, eastDec);
    expect(pRa).toBeCloseTo(270, 6);
    expect(pDec).toBeCloseTo(20, 6);
  });
});
