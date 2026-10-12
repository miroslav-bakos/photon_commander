// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type {
  FieldGeometryModel,
  MosaicPanelModel,
  RotationSampleModel,
  SessionEnvelopeModel,
  ShapeModel,
} from "../api/astro";
import { tangentOffsetToWorld } from "./projection";
import {
  buildAltitudeLimitRingShape,
  buildFovShapes,
  MAX_DISPLAY_FRAME_SAMPLES,
  selectRotationDisplaySamples,
  buildHorizonProfileMarkerShape,
  buildMosaicOverlayShapes,
  interpolateAltitudeDeg,
  interpolateParallacticAngleDeg,
  interpolatePositionAngleDeg,
  shapeModelToWorldPolygon,
} from "./overlay";

/** `arr[i]`, but fails the test immediately with a useful message instead of
 * a `noUncheckedIndexedAccess` `| undefined` silently propagating (or a
 * `!` non-null assertion the lint config forbids project-wide). */
function at<T>(arr: readonly T[], index: number): T {
  const value = arr[index];
  if (value === undefined) throw new Error(`expected index ${String(index)} to exist in ${JSON.stringify(arr)}`);
  return value;
}

const GEOMETRY: FieldGeometryModel = {
  optic_id: "tele",
  label: "Telephoto",
  pixel_scale_arcsec: 3.74,
  width_deg: 3.99,
  height_deg: 2.24,
  diagonal_deg: 4.58,
  inscribed_circle_deg: 2.24,
  provenance: "datasheet",
  distortion: "rectilinear",
  caveats: [],
};

function rectShape(halfWDeg: number, halfHDeg: number): ShapeModel {
  return {
    points: [
      { x_deg: -halfWDeg, y_deg: -halfHDeg },
      { x_deg: halfWDeg, y_deg: -halfHDeg },
      { x_deg: halfWDeg, y_deg: halfHDeg },
      { x_deg: -halfWDeg, y_deg: halfHDeg },
    ],
  };
}

function sample(elapsedHours: number, positionAngleDeg: number): RotationSampleModel {
  return {
    when: new Date(Date.UTC(2026, 7, 28, 22, 0, 0) + elapsedHours * 3600_000).toISOString(),
    elapsed_hours: elapsedHours,
    hour_angle_deg: elapsedHours * 15,
    altitude_deg: 60,
    parallactic_angle_deg: positionAngleDeg,
    position_angle_deg: positionAngleDeg,
  };
}

/** Like `sample`, but with independently-controllable `altitude_deg` and
 * `parallactic_angle_deg` — the two fields `interpolateAltitudeDeg`/
 * `interpolateParallacticAngleDeg` and the horizon markers read, which
 * `sample` deliberately keeps equal to `position_angle_deg`/a constant 60
 * for the tests that only care about position angle. */
function rotationSample(
  elapsedHours: number,
  altitudeDeg: number,
  parallacticAngleDeg: number,
): RotationSampleModel {
  return {
    when: new Date(Date.UTC(2026, 7, 28, 22, 0, 0) + elapsedHours * 3600_000).toISOString(),
    elapsed_hours: elapsedHours,
    hour_angle_deg: elapsedHours * 15,
    altitude_deg: altitudeDeg,
    parallactic_angle_deg: parallacticAngleDeg,
    position_angle_deg: parallacticAngleDeg,
  };
}

/** A synthetic but shape-accurate `SessionEnvelopeModel`, with `sampleCount`
 * adaptively-flavoured samples sweeping through the +-180deg seam once — the
 * interpolation hazard `interpolatePositionAngleDeg` exists to handle. */
function envelopeFixture(sampleCount: number): SessionEnvelopeModel {
  const samples: RotationSampleModel[] = [];
  const denom = Math.max(sampleCount - 1, 1);
  for (let i = 0; i < sampleCount; i++) {
    const elapsedHours = (4 * i) / denom;
    // Sweeps from -170 to +170deg and back, crossing the seam near the middle.
    const positionAngleDeg = ((-170 + (340 * i) / denom + 180) % 360) - 180;
    samples.push(sample(elapsedHours, positionAngleDeg));
  }
  return {
    samples,
    frame_start: rectShape(1.995, 1.12),
    frame_end: rectShape(1.995, 1.12),
    swept_envelope: rectShape(2.5, 1.6),
    keep_region: rectShape(1.0, 0.6),
    boundary_angles_deg: [],
    swept_radii_deg: [],
    keep_radii_deg: [],
    inscribed_circle_deg: 2.24,
    keep_region_min_diameter_deg: 1.2,
    keep_area_fraction: 0.62,
    keep_region_area_deg2: 5.4,
    swept_envelope_area_deg2: 12.1,
    total_rotation_deg: 42,
    max_abs_rotation_rate_deg_per_hour: 20,
    max_altitude_deg: 75,
    rotation_severity: "amber",
    high_altitude_transit: false,
    zenith_singularity: false,
    warnings: [],
  };
}

/** Great-circle separation between two RA/Dec points, degrees — used below
 * to check a projected polygon's actual size and position on the sphere
 * rather than trusting raw RA/Dec deltas, which are not degrees once
 * `cos(dec)` convergence is in play. */
function angularSepDeg(ra1: number, dec1: number, ra2: number, dec2: number): number {
  const d2r = Math.PI / 180;
  const [dec1R, dec2R, dRaR] = [dec1 * d2r, dec2 * d2r, (ra2 - ra1) * d2r];
  const cosc = Math.sin(dec1R) * Math.sin(dec2R) + Math.cos(dec1R) * Math.cos(dec2R) * Math.cos(dRaR);
  return (Math.acos(Math.min(1, Math.max(-1, cosc))) * 180) / Math.PI;
}

describe("shapeModelToWorldPolygon", () => {
  it("projects each tangent-plane offset relative to the given centre", () => {
    const shape = rectShape(1, 0.5);
    const points = shapeModelToWorldPolygon(shape, 200, 10);
    expect(points).toHaveLength(4);
    for (const [ra, dec] of points) {
      expect(Number.isFinite(ra)).toBe(true);
      expect(Number.isFinite(dec)).toBe(true);
    }
  });

  // The bug this guards against: a mosaic panel's shape centre is never the
  // overall session target (or any other single "view" point) — each panel
  // has its *own* centre, and a rectangle drawn wrong-sized/mispositioned
  // when that centre is away from the origin is exactly what "mosaic panels
  // render as tiny scattered squares instead of a grid" looked like. The
  // single-target overlay never exercised this because its one shape's
  // centre *is* the target, so a bug specific to an off-centre tangent
  // point could pass every existing test and still corrupt every panel.
  it("sizes and positions a rectangle correctly when its centre is far from another shape's centre (the mosaic case)", () => {
    // Same local offsets (a 3.99 x 2.24deg frame, astro-math's own S30 Pro
    // tele geometry), two different panel centres — as two adjacent mosaic
    // panels would be, not near RA 0 and not near a pole.
    const shape = rectShape(1.995, 1.12);
    const panelA = { ra: 250.4, dec: -35.2 };
    const panelB = { ra: 255.1, dec: -33.1 };

    for (const centre of [panelA, panelB]) {
      const poly = shapeModelToWorldPolygon(shape, centre.ra, centre.dec);
      expect(poly).toHaveLength(4);
      // Adjacent corners are a full side apart, not a fraction of one — this
      // is what would catch a scale bug (radians/degrees, or cos(dec)
      // applied at the wrong point): a 9x-too-small rectangle fails this
      // immediately rather than only being "off" in some subtle way.
      const [c0, c1, c2] = [at(poly, 0), at(poly, 1), at(poly, 2)];
      const side01 = angularSepDeg(c0[0], c0[1], c1[0], c1[1]);
      const side12 = angularSepDeg(c1[0], c1[1], c2[0], c2[1]);
      expect(side01).toBeCloseTo(3.99, 1);
      expect(side12).toBeCloseTo(2.24, 1);
      // The polygon's own centroid lands near *its* centre, not at the
      // origin and not at the other panel's centre — catches an offset
      // silently applied about (0,0), or a centre argument ignored.
      const centroidRa = poly.reduce((a, [ra]) => a + ra, 0) / poly.length;
      const centroidDec = poly.reduce((a, [, dec]) => a + dec, 0) / poly.length;
      expect(angularSepDeg(centroidRa, centroidDec, centre.ra, centre.dec)).toBeLessThan(0.05);
    }

    // The two panels' polygons must actually differ — a bug that ignores
    // `centerRaDeg`/`centerDecDeg` (e.g. always projecting about the first
    // shape it ever saw) would otherwise still pass every check above.
    const polyA = shapeModelToWorldPolygon(shape, panelA.ra, panelA.dec);
    const polyB = shapeModelToWorldPolygon(shape, panelB.ra, panelB.dec);
    const [a0, b0] = [at(polyA, 0), at(polyB, 0)];
    expect(angularSepDeg(a0[0], a0[1], b0[0], b0[1])).toBeGreaterThan(1);
  });
});

describe("interpolatePositionAngleDeg", () => {
  it("clamps to the first sample before the session starts", () => {
    const samples = [sample(0, 10), sample(1, 40)];
    expect(interpolatePositionAngleDeg(samples, -100)).toBe(10);
  });

  it("clamps to the last sample after the session ends", () => {
    const samples = [sample(0, 10), sample(1, 40)];
    expect(interpolatePositionAngleDeg(samples, 999_999)).toBe(40);
  });

  it("interpolates linearly at the midpoint between two samples", () => {
    const samples = [sample(0, 10), sample(2, 30)];
    expect(interpolatePositionAngleDeg(samples, 3600)).toBeCloseTo(20, 6);
  });

  it("takes the short way round a +-180deg seam crossing", () => {
    const samples = [sample(0, 170), sample(2, -170)];
    // The short way from 170 to -170 is +20 (through 180/-180), not -340.
    const mid = interpolatePositionAngleDeg(samples, 3600);
    expect(mid).toBeCloseTo(180, 6);
  });

  it("returns the single sample's angle when there is only one (EQ-shaped input)", () => {
    expect(interpolatePositionAngleDeg([sample(0, 55)], 1234)).toBe(55);
  });
});

describe("interpolateAltitudeDeg", () => {
  it("interpolates linearly, with no seam to cross", () => {
    const samples = [rotationSample(0, 20, 0), rotationSample(2, 60, 0)];
    expect(interpolateAltitudeDeg(samples, 3600)).toBeCloseTo(40, 6);
  });

  it("clamps to the first/last sample outside the session", () => {
    const samples = [rotationSample(0, 20, 0), rotationSample(2, 60, 0)];
    expect(interpolateAltitudeDeg(samples, -100)).toBe(20);
    expect(interpolateAltitudeDeg(samples, 999_999)).toBe(60);
  });
});

describe("interpolateParallacticAngleDeg", () => {
  it("takes the short way round a +-180deg seam crossing, same as position angle", () => {
    const samples = [rotationSample(0, 60, 170), rotationSample(2, 60, -170)];
    expect(interpolateParallacticAngleDeg(samples, 3600)).toBeCloseTo(180, 6);
  });

  it("is read independently of position_angle_deg (non-zero pa_offset case)", () => {
    // A sensor rotation offset applied on top of the sky's own parallactic
    // angle: position_angle_deg = parallactic_angle_deg + 30 throughout.
    const samples = [
      { ...rotationSample(0, 60, 10), position_angle_deg: 40 },
      { ...rotationSample(2, 60, 50), position_angle_deg: 80 },
    ];
    expect(interpolateParallacticAngleDeg(samples, 3600)).toBeCloseTo(30, 6);
    expect(interpolatePositionAngleDeg(samples, 3600)).toBeCloseTo(60, 6);
  });
});

describe("buildAltitudeLimitRingShape / buildHorizonProfileMarkerShape", () => {
  // RA 100, Dec 0: near-zero polar distortion, so a tangent-plane north
  // offset of `d` degrees lands at very nearly `dec = d` and an east offset
  // at very nearly `ra = 100 + d`, making the geometry easy to check without
  // re-deriving `tangentOffsetToWorld`'s own math (already covered by
  // `projection.test.ts`).
  const inputs = (parallacticAngleDeg: number, altitudeDeg: number) => ({
    envelope: { ...envelopeFixture(1), samples: [rotationSample(0, altitudeDeg, parallacticAngleDeg)] },
    geometry: GEOMETRY,
    targetRaDeg: 100,
    targetDecDeg: 0,
    instantSeconds: 0,
  });

  it("at parallactic angle 0 (zenith = north), a comfortable margin sits south of the target, spread east-west", () => {
    // altitude 50, limit 30 => 20deg of margin => the marker sits 20deg
    // toward the horizon, i.e. away from the zenith direction (north) => south.
    // Expected vertices computed independently via `tangentOffsetToWorld`
    // (already verified by `projection.test.ts`) rather than a hand-guessed
    // RA/Dec, so this checks the marker's own zenith-direction/offset math,
    // not the gnomonic projection's small-angle behaviour.
    const halfW = GEOMETRY.width_deg / 2;
    const expected = [
      tangentOffsetToWorld(100, 0, halfW, -20),
      tangentOffsetToWorld(100, 0, -halfW, -20),
    ];
    const shape = buildAltitudeLimitRingShape(inputs(0, 50), 30);
    expect(shape.kind).toBe("polyline");
    if (shape.kind !== "polyline") return;
    expect(shape.verticesRaDec).toHaveLength(2);
    for (const [i, [ra, dec]] of shape.verticesRaDec.entries()) {
      const [expectedRa, expectedDec] = at(expected, i);
      expect(ra).toBeCloseTo(expectedRa, 9);
      expect(dec).toBeCloseTo(expectedDec, 9);
    }
  });

  it("at parallactic angle 90 (zenith = east), the same margin sits west of the target, spread north-south", () => {
    const halfW = GEOMETRY.width_deg / 2;
    // Perpendicular to an east-pointing zenith is north-south, not
    // east-west — the bar's two endpoints differ in the *north* offset now.
    const expected = [
      tangentOffsetToWorld(100, 0, -20, -halfW),
      tangentOffsetToWorld(100, 0, -20, halfW),
    ];
    const shape = buildAltitudeLimitRingShape(inputs(90, 50), 30);
    if (shape.kind !== "polyline") throw new Error("expected a polyline");
    for (const [i, [ra, dec]] of shape.verticesRaDec.entries()) {
      const [expectedRa, expectedDec] = at(expected, i);
      expect(ra).toBeCloseTo(expectedRa, 9);
      expect(dec).toBeCloseTo(expectedDec, 9);
    }
  });

  it("colours the ring by margin: comfortable, close, and below the limit", () => {
    expect(buildAltitudeLimitRingShape(inputs(0, 50), 30).style.role).toBe("keep"); // 20deg margin
    expect(buildAltitudeLimitRingShape(inputs(0, 32), 30).style.role).toBe("warning"); // 2deg margin
    expect(buildAltitudeLimitRingShape(inputs(0, 25), 30).style.role).toBe("danger"); // -5deg margin
  });

  it("the horizon-profile marker is dashed and reads against the session-start bearing altitude, not the flat limit", () => {
    const shape = buildHorizonProfileMarkerShape(inputs(0, 44), /* horizonAltitudeAtStartBearingDeg */ 40);
    expect(shape.kind).toBe("polyline");
    expect(shape.id).toBe("horizon-profile-marker");
    expect(shape.style.dashed).toBe(true);
    expect(shape.style.role).toBe("warning"); // 4deg margin, under the 5deg amber boundary
  });
});

describe("buildFovShapes", () => {
  it("EQ mode draws exactly one shape, from frame_start, role primary", () => {
    const shapes = buildFovShapes({
      mount: "eq",
      geometry: GEOMETRY,
      envelope: envelopeFixture(1),
      targetRaDeg: 90,
      targetDecDeg: 20,
      instantSeconds: 0,
    });
    expect(shapes).toHaveLength(1);
    expect(shapes[0]).toMatchObject({ kind: "polygon", id: "primary", style: { role: "primary" } });
  });

  it("the frame stack is unfilled, so many samples cannot bury the target", () => {
    // Each sample frame used to fill at 0.12 independently. A session samples
    // PA(t) dozens of times, so the fills compounded alpha-over-alpha into a
    // solid block covering the very target the frames are centred on (owner,
    // 2026-09-10). Rotation is carried by the outlines' orientation; the sky
    // the session touches is already drawn once, as `union`.
    const shapes = buildFovShapes({
      mount: "altaz",
      geometry: GEOMETRY,
      envelope: envelopeFixture(50),
      targetRaDeg: 90,
      targetDecDeg: 20,
      instantSeconds: 0,
    });
    const stack = shapes.find((s) => s.kind === "frameStack");
    expect(stack?.style.fillOpacity).toBe(0);
  });

  it("alt-az mode draws all five shapes with the roles DESIGN.md 3.2 specifies", () => {
    const shapes = buildFovShapes({
      mount: "altaz",
      geometry: GEOMETRY,
      envelope: envelopeFixture(50),
      targetRaDeg: 90,
      targetDecDeg: 20,
      instantSeconds: 0,
    });
    const roles = shapes.map((s) => s.style.role).sort();
    expect(roles).toEqual(["ghost", "keep", "primary", "stack", "union"]);
    const stack = shapes.find((s) => s.kind === "frameStack");
    expect(stack?.kind).toBe("frameStack");
    if (stack?.kind === "frameStack") {
      expect(stack.frames).toHaveLength(MAX_DISPLAY_FRAME_SAMPLES);
      expect(stack.frames[0]?.atSeconds).toBe(0);
      expect(stack.frames.at(-1)?.atSeconds).toBe(4 * 3600);
    }
  });

  it("keeps a reversal's interior PA extrema while bounding long sessions", () => {
    const samples = Array.from({ length: 21 }, (_, index) => {
      const angle = index <= 6 ? index * 10 : index <= 14 ? 60 - (index - 6) * 15 : -60 + (index - 14) * 5;
      return sample(index / 5, angle);
    });
    const selected = selectRotationDisplaySamples(samples);
    expect(selected).toHaveLength(MAX_DISPLAY_FRAME_SAMPLES);
    expect(selected[0]).toBe(samples[0]);
    expect(selected.at(-1)).toBe(samples.at(-1));
    expect(selected).toContain(samples[6]); // +60° interior maximum
    expect(selected).toContain(samples[14]); // -60° interior minimum
  });

  it("does not invent or discard poses in an already-short session", () => {
    const samples = [sample(0, 10), sample(1, 15), sample(2, 20)];
    expect(selectRotationDisplaySamples(samples)).toBe(samples);
  });

  it("the primary rectangle's position angle tracks the scrubbed instant", () => {
    const envelope = envelopeFixture(50);
    const early = buildFovShapes({
      mount: "altaz",
      geometry: GEOMETRY,
      envelope,
      targetRaDeg: 90,
      targetDecDeg: 20,
      instantSeconds: 0,
    }).find((s) => s.id === "primary");
    const late = buildFovShapes({
      mount: "altaz",
      geometry: GEOMETRY,
      envelope,
      targetRaDeg: 90,
      targetDecDeg: 20,
      instantSeconds: 4 * 3600,
    }).find((s) => s.id === "primary");
    expect(early?.kind).toBe("rect");
    expect(late?.kind).toBe("rect");
    if (early?.kind === "rect" && late?.kind === "rect") {
      expect(early.positionAngleDeg).not.toBeCloseTo(late.positionAngleDeg, 3);
    }
  });
});

describe("buildMosaicOverlayShapes", () => {
  const panels: MosaicPanelModel[] = [
    { center: { row: 0, col: 0, ra_j2000_h: 6, dec_j2000_deg: 5 }, envelope: envelopeFixture(1) },
    { center: { row: 0, col: 1, ra_j2000_h: 6.1, dec_j2000_deg: 5 }, envelope: envelopeFixture(1) },
  ];

  it("EQ mode: one outline per panel, no keep-region duplicate (eq_session_envelope already collapses keep onto the frame)", () => {
    const shapes = buildMosaicOverlayShapes(panels, "eq");
    expect(shapes).toHaveLength(2);
    expect(new Set(shapes.map((s) => s.id)).size).toBe(2);
    for (const shape of shapes) {
      expect(shape.style.role).toBe("grid");
    }
  });

  it("alt-az mode: outline plus that panel's own shaded keep-region, distinct ids, both role grid (not the session-level keep role)", () => {
    const shapes = buildMosaicOverlayShapes(panels, "altaz");
    expect(shapes).toHaveLength(4);
    expect(new Set(shapes.map((s) => s.id)).size).toBe(4);
    for (const shape of shapes) {
      expect(shape.style.role).toBe("grid");
    }
    const keepShapes = shapes.filter((s) => s.id.endsWith("-keep"));
    expect(keepShapes).toHaveLength(2);
    // The keep shape is visibly more filled than the outline (DESIGN.md
    // 3.3: "how much of each panel survives" is the number that matters),
    // and importantly still `grid`, not `keep` — reusing `keep` (the
    // session-level role `buildFovShapes` already draws in green) would
    // make a panel's own keep-region indistinguishable from the
    // whole-session one on screen.
    for (const keep of keepShapes) {
      expect(keep.style.fillOpacity).toBeGreaterThan(0.3);
    }
  });

  it("rotates every alt-az panel outline to the scrubbed instant while EQ stays fixed", () => {
    const rotatingPanels = panels.map((panel) => ({ ...panel, envelope: envelopeFixture(50) }));
    const earlyAltAz = buildMosaicOverlayShapes(rotatingPanels, "altaz", 0).filter((shape) => !shape.id.endsWith("-keep"));
    const lateAltAz = buildMosaicOverlayShapes(rotatingPanels, "altaz", 4 * 3600).filter((shape) => !shape.id.endsWith("-keep"));
    const earlyEq = buildMosaicOverlayShapes(rotatingPanels, "eq", 0);
    const lateEq = buildMosaicOverlayShapes(rotatingPanels, "eq", 4 * 3600);

    expect(lateAltAz.map((shape) => shape.kind === "polygon" ? shape.verticesRaDec : [])).not.toEqual(
      earlyAltAz.map((shape) => shape.kind === "polygon" ? shape.verticesRaDec : []),
    );
    expect(lateEq).toEqual(earlyEq);
    expect(lateAltAz.every((shape) => shape.kind === "polygon" && shape.label?.includes("scrubbed instant") === true)).toBe(true);
  });

  it("a panel's keep-region is projected around that panel's own centre, not the mosaic target", () => {
    const [outline, keep] = buildMosaicOverlayShapes([at(panels, 0)], "altaz");
    expect(outline?.kind).toBe("polygon");
    expect(keep?.kind).toBe("polygon");
    if (outline?.kind !== "polygon" || keep?.kind !== "polygon") return;
    // keep_region (fixture: 1.0 x 0.6) is smaller than frame_start (fixture:
    // 3.99 x 2.24) but shares the same centre — every keep vertex should sit
    // well inside the outline's bounding span, not off at some other point.
    const raSpread = (pts: [number, number][]) => Math.max(...pts.map((p) => p[0])) - Math.min(...pts.map((p) => p[0]));
    const decSpread = (pts: [number, number][]) => Math.max(...pts.map((p) => p[1])) - Math.min(...pts.map((p) => p[1]));
    expect(raSpread(keep.verticesRaDec)).toBeLessThan(raSpread(outline.verticesRaDec));
    expect(decSpread(keep.verticesRaDec)).toBeLessThan(decSpread(outline.verticesRaDec));
  });
});

describe("performance budget (skymap-viz.md: scrubber redraw <= 16ms)", () => {
  it("interpolating + rebuilding the full alt-az shape set (five shapes plus both horizon markers) for one scrub step is well under 16ms", () => {
    // 500 samples: denser than a real 4h session's adaptive sampling at the
    // default max_pa_step_deg=0.5 is likely to produce even through a
    // meridian rotation spike, so this over-estimates the real cost.
    const envelope = envelopeFixture(500);
    const iterations = 200;
    const start = performance.now();
    for (let i = 0; i < iterations; i++) {
      const instantSeconds = (i / iterations) * 4 * 3600;
      buildFovShapes({
        mount: "altaz",
        geometry: GEOMETRY,
        envelope,
        targetRaDeg: 90,
        targetDecDeg: 20,
        instantSeconds,
      });
      const markerInputs = { envelope, geometry: GEOMETRY, targetRaDeg: 90, targetDecDeg: 20, instantSeconds };
      buildAltitudeLimitRingShape(markerInputs, 20);
      buildHorizonProfileMarkerShape(markerInputs, 15);
    }
    const totalMs = performance.now() - start;
    const perFrameMs = totalMs / iterations;
    console.log(`[overlay perf] ${perFrameMs.toFixed(3)}ms/frame over ${String(iterations)} scrub steps, 500 samples`);
    expect(perFrameMs).toBeLessThan(16);
  });
});
