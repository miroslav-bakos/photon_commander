// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { altAzToRaDec, horizontalCoordTransform, type SiteInstant } from "./frames/shared/sidereal";
import {
  circleWorldVertices,
  cosAngularDistanceFromViewCenter,
  FAR_HEMISPHERE_COSC_MARGIN,
  normalizeRaDeltaDeg,
  projectPolylineRuns,
  rectWorldCorners,
  rotateTangentOffset,
  screenToWorld,
  tangentOffsetToWorld,
  worldToScreen,
  type Viewport,
} from "./projection";

describe("normalizeRaDeltaDeg", () => {
  it("keeps small deltas unchanged", () => {
    expect(normalizeRaDeltaDeg(5)).toBeCloseTo(5, 9);
    expect(normalizeRaDeltaDeg(-5)).toBeCloseTo(-5, 9);
  });

  it("wraps a delta just past +180 to the short way round", () => {
    expect(normalizeRaDeltaDeg(190)).toBeCloseTo(-170, 9);
  });

  it("wraps a delta just past -180 to the short way round", () => {
    expect(normalizeRaDeltaDeg(-190)).toBeCloseTo(170, 9);
  });

  it("handles the 0h/24h seam: field near RA 0 seeing a target near RA 359.9", () => {
    // view at RA 0.5, target at RA 359.8 -> should be treated as -0.7deg away,
    // not -359.3deg away.
    expect(normalizeRaDeltaDeg(359.8 - 0.5)).toBeCloseTo(-0.7, 9);
  });
});

describe("worldToScreen / screenToWorld", () => {
  const viewport = { view: { ra: 100, dec: 40, fovDeg: 5 }, widthPx: 1000, heightPx: 500 };

  it("maps the field centre to the viewport centre", () => {
    const [x, y] = worldToScreen(viewport, 100, 40);
    expect(x).toBeCloseTo(500, 6);
    expect(y).toBeCloseTo(250, 6);
  });

  it("east (increasing RA) is left on screen (decreasing x), matching sky convention", () => {
    const [xEast] = worldToScreen(viewport, 100.5, 40);
    const [xWest] = worldToScreen(viewport, 99.5, 40);
    expect(xEast).toBeLessThan(500);
    expect(xWest).toBeGreaterThan(500);
  });

  it("north (increasing dec) is up on screen (decreasing y)", () => {
    const [, yNorth] = worldToScreen(viewport, 100, 40.5);
    const [, ySouth] = worldToScreen(viewport, 100, 39.5);
    expect(yNorth).toBeLessThan(250);
    expect(ySouth).toBeGreaterThan(250);
  });

  it("round-trips through screenToWorld across many points in the field", () => {
    const points: [number, number][] = [
      [100, 40],
      [101, 41],
      [99, 39],
      [102.3, 38.7],
    ];
    for (const [ra, dec] of points) {
      const [x, y] = worldToScreen(viewport, ra, dec);
      const [ra2, dec2] = screenToWorld(viewport, x, y);
      expect(ra2).toBeCloseTo(ra, 6);
      expect(dec2).toBeCloseTo(dec, 6);
    }
  });

  it("stays finite near the pole rather than producing NaN", () => {
    const polarViewport = { view: { ra: 0, dec: 89, fovDeg: 5 }, widthPx: 800, heightPx: 800 };
    const [x, y] = worldToScreen(polarViewport, 90, 89.9);
    expect(Number.isFinite(x)).toBe(true);
    expect(Number.isFinite(y)).toBe(true);
  });

  it("handles a field centred right at the RA 0h/24h seam", () => {
    const seamViewport = { view: { ra: 0.1, dec: 0, fovDeg: 2 }, widthPx: 800, heightPx: 800 };
    const [xJustBefore] = worldToScreen(seamViewport, 359.9, 0);
    const [xJustAfter] = worldToScreen(seamViewport, 0.3, 0);
    // Both are ~0.2-0.3deg from the centre; neither should be thrown to the
    // opposite edge of the frame by a naive un-normalised RA delta.
    expect(Math.abs(xJustBefore - 400)).toBeLessThan(100);
    expect(Math.abs(xJustAfter - 400)).toBeLessThan(100);
  });
});

describe("tangentOffsetToWorld", () => {
  it("maps a zero offset back to the centre", () => {
    const [ra, dec] = tangentOffsetToWorld(123, -12, 0, 0);
    expect(ra).toBeCloseTo(123, 9);
    expect(dec).toBeCloseTo(-12, 9);
  });

  it("+x is east (increasing RA) at dec 0", () => {
    const [ra] = tangentOffsetToWorld(180, 0, 1, 0);
    expect(ra).toBeGreaterThan(180);
  });

  it("+y is north (increasing dec)", () => {
    const [, dec] = tangentOffsetToWorld(180, 0, 0, 1);
    expect(dec).toBeGreaterThan(0);
  });

  it("stays finite right at the pole", () => {
    const [ra, dec] = tangentOffsetToWorld(0, 89.9, 0.05, 0.05);
    expect(Number.isFinite(ra)).toBe(true);
    expect(Number.isFinite(dec)).toBe(true);
  });

  it("round-trips with worldToScreen/screenToWorld's own gnomonic family: a small offset back-projects near the forward point", () => {
    // Cross-check against the *other* gnomonic in this module (view-relative)
    // at a matching small scale, to catch a sign convention drifting apart
    // between the two independently-justified projections.
    const viewport = { view: { ra: 50, dec: 20, fovDeg: 4 }, widthPx: 1000, heightPx: 1000 };
    const [ra, dec] = tangentOffsetToWorld(50, 20, 0.3, 0.2);
    const [x, y] = worldToScreen(viewport, ra, dec);
    const [ra2, dec2] = screenToWorld(viewport, x, y);
    expect(ra2).toBeCloseTo(ra, 6);
    expect(dec2).toBeCloseTo(dec, 6);
  });
});

describe("rotateTangentOffset", () => {
  it("leaves a zero offset at zero", () => {
    expect(rotateTangentOffset(0, 0, 37)).toEqual([0, 0]);
  });

  it("rotating north (0,1) by +90deg swings it to east (1,0): PA is measured from north through east", () => {
    const [x, y] = rotateTangentOffset(0, 1, 90);
    expect(x).toBeCloseTo(1, 9);
    expect(y).toBeCloseTo(0, 9);
  });

  it("matches ssc.astro.framing.frame_shape corner for corner at PA 30 (astro-math fixture)", () => {
    // `frame_shape(FieldGeometry(width 2, height 1), 30 deg)` — corners in
    // its own order, (+x east, +y north) degrees.
    const expected: [number, number][] = [
      [1.116025, -0.066987],
      [-0.616025, 0.933013],
      [-1.116025, 0.066987],
      [0.616025, -0.933013],
    ];
    const local: [number, number][] = [
      [1, 0.5],
      [-1, 0.5],
      [-1, -0.5],
      [1, -0.5],
    ];
    const rotated = local.map(([x, y]) => rotateTangentOffset(x, y, 30));
    expect(rotated.map(([x, y]) => [Number(x.toFixed(5)), Number(y.toFixed(5))])).toEqual(
      expected.map(([x, y]) => [Number(x.toFixed(5)), Number(y.toFixed(5))]),
    );
  });

  it("a full 360deg rotation is the identity", () => {
    const [x, y] = rotateTangentOffset(0.7, -0.4, 360);
    expect(x).toBeCloseTo(0.7, 9);
    expect(y).toBeCloseTo(-0.4, 9);
  });
});

describe("rectWorldCorners", () => {
  it("produces four corners, each ~half-diagonal from the centre", () => {
    const corners = rectWorldCorners(200, 10, 4, 2, 0);
    expect(corners).toHaveLength(4);
    const halfDiagDeg = Math.hypot(2, 1);
    for (const [ra, dec] of corners) {
      const dRa = normalizeRaDeltaDeg(ra - 200) * Math.cos((10 * Math.PI) / 180);
      const dDec = dec - 10;
      expect(Math.hypot(dRa, dDec)).toBeCloseTo(halfDiagDeg, 1);
    }
  });

  it("stays finite for a rectangle centred near the pole (no bow-tie NaNs)", () => {
    const corners = rectWorldCorners(10, 89.5, 4, 2, 45);
    expect(corners).toHaveLength(4);
    for (const [ra, dec] of corners) {
      expect(Number.isFinite(ra)).toBe(true);
      expect(Number.isFinite(dec)).toBe(true);
      expect(dec).toBeLessThanOrEqual(90);
    }
  });

  it("rotating by 90deg swaps which axis the rectangle is long along", () => {
    // RA 100deg, well away from the 0h/24h seam a naive max-min spread
    // would mishandle (that wrap is `normalizeRaDeltaDeg`'s job, exercised
    // above, not this test's).
    const unrotated = rectWorldCorners(100, 0, 4, 2, 0);
    const rotated = rectWorldCorners(100, 0, 4, 2, 90);
    const raSpread = (pts: [number, number][]) => Math.max(...pts.map((p) => p[0])) - Math.min(...pts.map((p) => p[0]));
    const decSpread = (pts: [number, number][]) => Math.max(...pts.map((p) => p[1])) - Math.min(...pts.map((p) => p[1]));
    expect(raSpread(unrotated)).toBeGreaterThan(decSpread(unrotated));
    expect(decSpread(rotated)).toBeGreaterThan(raSpread(rotated));
  });
});

describe("circleWorldVertices", () => {
  it("produces vertices each exactly radiusDeg from the centre, away from the pole", () => {
    const vertices = circleWorldVertices(200, 10, 2, 36);
    expect(vertices).toHaveLength(36);
    for (const [ra, dec] of vertices) {
      const dRa = normalizeRaDeltaDeg(ra - 200) * Math.cos((10 * Math.PI) / 180);
      const dDec = dec - 10;
      // Small-angle approximation is fine for the test's own tolerance at a
      // 2deg radius, well away from the pole.
      expect(Math.hypot(dRa, dDec)).toBeCloseTo(2, 1);
    }
  });

  it("stays finite for a circle centred at the pole", () => {
    const vertices = circleWorldVertices(0, 90, 1, 24);
    expect(vertices).toHaveLength(24);
    for (const [ra, dec] of vertices) {
      expect(Number.isFinite(ra)).toBe(true);
      expect(Number.isFinite(dec)).toBe(true);
      expect(dec).toBeCloseTo(89, 0);
    }
  });

  it("wraps RA into [0, 360) for a circle straddling the 0h/24h seam", () => {
    const vertices = circleWorldVertices(0, 0, 2, 36);
    for (const [ra] of vertices) {
      expect(ra).toBeGreaterThanOrEqual(0);
      expect(ra).toBeLessThan(360);
    }
  });
});

describe("roll (the backyard view's screen rotation)", () => {
  const base = { view: { ra: 10.68, dec: 41.27, fovDeg: 20 }, widthPx: 800, heightPx: 600 };

  it("is identity at roll 0, and an absent roll matches an explicit 0", () => {
    const point: [number, number] = [14, 44];
    expect(worldToScreen(base, ...point)).toEqual(worldToScreen({ ...base, rollDeg: 0 }, ...point));
  });

  it("round-trips screen <-> world at every roll, which drag and click-picking rely on", () => {
    // If the forward and inverse rotations disagree even slightly, a drag
    // grabs the frame at one place and moves it to another, and a click
    // picks the wrong object — the failure this property exists to prevent.
    for (const rollDeg of [-135, -42, -1, 0, 1, 42, 90, 135, 179]) {
      const viewport = { ...base, rollDeg };
      for (const [ra, dec] of [
        [10.68, 41.27],
        [14, 44],
        [6, 38],
        [11, 49],
      ] as [number, number][]) {
        const [x, y] = worldToScreen(viewport, ra, dec);
        const [backRa, backDec] = screenToWorld(viewport, x, y);
        expect(backRa).toBeCloseTo(ra, 6);
        expect(backDec).toBeCloseTo(dec, 6);
      }
    }
  });

  it("rotates the screen position about the view centre, leaving the centre fixed", () => {
    const rolled = { ...base, rollDeg: 90 };
    // The view centre projects to the middle of the viewport at any roll —
    // rotation is about that point.
    const [cx, cy] = worldToScreen(rolled, base.view.ra, base.view.dec);
    expect(cx).toBeCloseTo(base.widthPx / 2, 6);
    expect(cy).toBeCloseTo(base.heightPx / 2, 6);

    // A point offset from the centre keeps its distance from it under roll.
    const offset: [number, number] = [14, 44];
    const straight = worldToScreen(base, ...offset);
    const turned = worldToScreen(rolled, ...offset);
    const radius = ([x, y]: [number, number]) => Math.hypot(x - base.widthPx / 2, y - base.heightPx / 2);
    expect(radius(turned)).toBeCloseTo(radius(straight), 6);
    // ...and actually moved: a 90 deg roll is not a no-op.
    expect(Math.hypot(turned[0] - straight[0], turned[1] - straight[1])).toBeGreaterThan(1);
  });
});

/** Wrap-safe RA-like comparison, degrees, for the round-trip assertions
 * below — the same reasoning `normalizeRaDeltaDeg` exists for. */
function assertCloseRaDec(actual: [number, number], expected: [number, number], toleranceDeg: number): void {
  const [actualRa, actualDec] = actual;
  const [expectedRa, expectedDec] = expected;
  const raDiff = (((actualRa - expectedRa + 540) % 360) + 360) % 360 - 180;
  expect(Math.abs(raDiff)).toBeLessThan(toleranceDeg);
  expect(Math.abs(actualDec - expectedDec)).toBeLessThan(toleranceDeg);
}

describe("worldToScreen / screenToWorld with a horizontal (alt-az) coordTransform", () => {
  const sites: SiteInstant[] = [
    { latitudeDeg: 48.1486, longitudeDeg: 17.1077, instant: new Date("2026-09-07T22:00:00Z") },
    { latitudeDeg: -33.8688, longitudeDeg: 151.2093, instant: new Date("2026-03-20T12:00:00Z") },
    { latitudeDeg: 66.5, longitudeDeg: -20, instant: new Date("2026-06-21T00:00:00Z") },
  ];

  it("round-trips ra/dec through screen space, across several sites, instants and view centres", () => {
    for (const site of sites) {
      const coordTransform = horizontalCoordTransform(site);
      const viewCenters: [alt: number, az: number][] = [
        [60, 30],
        [20, 200],
        [80, 10],
      ];
      for (const [viewAlt, viewAz] of viewCenters) {
        const [viewRa, viewDec] = altAzToRaDec(viewAlt, viewAz, site);
        const viewport = { view: { ra: viewRa, dec: viewDec, fovDeg: 30 }, widthPx: 1000, heightPx: 600, coordTransform };
        const points: [ra: number, dec: number][] = [
          [viewRa + 1, viewDec + 1],
          [viewRa - 2, viewDec],
          altAzToRaDec(viewAlt - 10, viewAz + 15, site),
          altAzToRaDec(viewAlt + 5, viewAz - 20, site),
        ];
        for (const point of points) {
          const [x, y] = worldToScreen(viewport, ...point);
          const back = screenToWorld(viewport, x, y);
          assertCloseRaDec(back, point, 1e-6);
        }
      }
    }
  });

  it("handedness: due east of a due-north-centred view lands on the right of the screen", () => {
    // The scenario `skymap-viz.md` spells out explicitly: "In a horizontal
    // view facing north, azimuth 90 (east) is to your right." Off the
    // zenith (alt 20, not 90) so azimuth is well-defined at both points.
    const site = sites[0];
    if (site === undefined) throw new Error("fixture is empty");
    const coordTransform = horizontalCoordTransform(site);
    const [northRa, northDec] = altAzToRaDec(20, 0, site);
    const viewport = { view: { ra: northRa, dec: northDec, fovDeg: 60 }, widthPx: 1000, heightPx: 600, coordTransform };
    const [eastRa, eastDec] = altAzToRaDec(20, 90, site);
    const [x] = worldToScreen(viewport, eastRa, eastDec);
    expect(x).toBeGreaterThan(500);
  });

  it("centres the zenith when the view is centred on the zenith, and puts a lower-altitude point below it", () => {
    const site = sites[0];
    if (site === undefined) throw new Error("fixture is empty");
    const coordTransform = horizontalCoordTransform(site);
    const [zenithRa, zenithDec] = altAzToRaDec(90, 0, site);
    const viewport = { view: { ra: zenithRa, dec: zenithDec, fovDeg: 120 }, widthPx: 1000, heightPx: 600, coordTransform };

    const [cx, cy] = worldToScreen(viewport, zenithRa, zenithDec);
    expect(cx).toBeCloseTo(500, 6);
    expect(cy).toBeCloseTo(300, 6);

    // Not exactly the true horizon (alt 0, 90deg from the zenith): that is
    // gnomonic projection's own asymptote (`projection.ts`'s far-hemisphere
    // clamp), not a meaningful screen position for any tangent-plane view.
    // A lower altitude — still comfortably inside the tangent plane — is
    // enough to show "up on screen" tracks "higher altitude", the horizontal
    // analogue of the existing "north is up" equatorial test above.
    const [lowerRa, lowerDec] = altAzToRaDec(30, 45, site);
    const [higherRa, higherDec] = altAzToRaDec(60, 45, site);
    const [, lowerY] = worldToScreen(viewport, lowerRa, lowerDec);
    const [, higherY] = worldToScreen(viewport, higherRa, higherDec);
    expect(lowerY).toBeGreaterThan(cy);
    expect(higherY).toBeGreaterThan(cy);
    expect(lowerY).toBeGreaterThan(higherY);
  });

  it("is byte-identical to the plain equatorial projection when coordTransform is absent", () => {
    const viewport = { view: { ra: 100, dec: 40, fovDeg: 5 }, widthPx: 1000, heightPx: 500 };
    const withExplicitUndefined = { ...viewport, coordTransform: undefined };
    expect(worldToScreen(withExplicitUndefined, 101, 41)).toEqual(worldToScreen(viewport, 101, 41));
    const [x, y] = worldToScreen(viewport, 101, 41);
    expect(screenToWorld(withExplicitUndefined, x, y)).toEqual(screenToWorld(viewport, x, y));
  });
});

describe("projectPolylineRuns", () => {
  // The owner's exact reported view (az 236.03 / alt -7.82 treated as an
  // equatorial ra/dec here — the bug is in the gnomonic maths shared by both
  // the plain equatorial projection and the alt-az `coordTransform`, so it
  // reproduces without needing `horizontalCoordTransform` and a site/instant
  // fixture too). 60deg FOV matches the screenshot.
  const viewport: Viewport = { view: { ra: 236.03, dec: -7.82, fovDeg: 60 }, widthPx: 1200, heightPx: 800 };

  /** A full parallel of declination, one vertex every 2deg of RA — the same
   * shape `equatorialGridShapes` builds for a Dec line, sized to definitely
   * cross the far hemisphere from `viewport` above (confirmed against the
   * live view: several of this map's dec parallels cross twice). */
  function decParallel(decDeg: number): [number, number][] {
    const vertices: [number, number][] = [];
    for (let ra = 0; ra < 360; ra += 2) vertices.push([ra, decDeg]);
    vertices.push([0, decDeg]);
    return vertices;
  }

  it("reproduces the bug: naively joining every vertex draws a chord between opposite hemispheres", () => {
    // This is deliberately the *old*, pre-fix behaviour (`b0e9366`): project
    // every vertex and join them all in one path, the way this frame's own
    // `projectRuns` did before `projectPolylineRuns` existed. It must find at
    // least one adjacent pair straddling the 90deg boundary, or this test is
    // not actually exercising the reported defect.
    const vertices = decParallel(-40);
    let foundHemisphereCrossingChord = false;
    for (let i = 1; i < vertices.length; i += 1) {
      const a = vertices[i - 1];
      const b = vertices[i];
      if (a === undefined || b === undefined) continue;
      const coscA = cosAngularDistanceFromViewCenter(viewport, a[0], a[1]);
      const coscB = cosAngularDistanceFromViewCenter(viewport, b[0], b[1]);
      if (Math.sign(coscA) !== Math.sign(coscB)) foundHemisphereCrossingChord = true;
    }
    expect(foundHemisphereCrossingChord).toBe(true);
  });

  it("emits no run whose consecutive vertices straddle the far hemisphere, at the owner's exact view", () => {
    const decsToCheck = [-80, -70, -60, -50, -40, -30, -20, -10, 0, 10, 20, 30, 40, 50, 60, 70, 80];
    for (const dec of decsToCheck) {
      const runs = projectPolylineRuns(viewport, decParallel(dec), false);
      for (const run of runs) {
        expect(run.length).toBeGreaterThanOrEqual(2);
        for (const [x, y] of run) {
          const [ra, backDec] = screenToWorld(viewport, x, y);
          // Every emitted point — including a clipped boundary point —
          // round-trips to somewhere on the near hemisphere, comfortably
          // past the clamp's own singularity at cosc = 0.
          expect(cosAngularDistanceFromViewCenter(viewport, ra, backDec)).toBeGreaterThan(0);
        }
      }
    }
  });

  it("clips at the boundary rather than dropping the vertex outright: the clipped point stays on the near side of the margin", () => {
    const runs = projectPolylineRuns(viewport, decParallel(-40), false);
    expect(runs.length).toBeGreaterThan(0);
    for (const run of runs) {
      const last = run[run.length - 1];
      if (last === undefined) continue;
      const [ra, dec] = screenToWorld(viewport, last[0], last[1]);
      expect(cosAngularDistanceFromViewCenter(viewport, ra, dec)).toBeGreaterThanOrEqual(FAR_HEMISPHERE_COSC_MARGIN - 1e-6);
    }
  });

  it("keeps an entirely near-hemisphere shape (a FOV-sized rectangle-like path) as one unbroken run", () => {
    const vertices: [number, number][] = [
      [235, -8],
      [237, -8],
      [237, -7],
      [235, -7],
    ];
    const runs = projectPolylineRuns(viewport, vertices, true);
    expect(runs).toHaveLength(1);
    expect(runs[0]).toHaveLength(5); // closed: first vertex repeated.
  });

  it("returns no runs for a shape entirely on the far hemisphere", () => {
    // Antipodal-ish declination band relative to the view centre — confirmed
    // (by direct computation) to have no near-hemisphere vertex at all from
    // this view, so the correct output is zero runs, not an empty run or one
    // stretching across the clamp.
    const vertices = decParallel(85);
    expect(vertices.every(([ra, dec]) => cosAngularDistanceFromViewCenter(viewport, ra, dec) <= 0)).toBe(true);
    const runs = projectPolylineRuns(viewport, vertices, false);
    expect(runs).toHaveLength(0);
  });
});
