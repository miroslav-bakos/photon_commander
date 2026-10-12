// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Turns a `FramingResponse`/`MosaicResponse` (already-computed geometry from
 * `ssc.astro` — nothing here solves parallactic angle or a keep-region) into
 * the `FovShape[]` the map's `setOverlay` draws. This is the boundary
 * between "astro-math's answer" and "how the planner draws it" DESIGN.md
 * §3.2 describes: five shapes for an alt-az session, one rectangle for EQ.
 *
 * The only per-scrub-frame work is `interpolatePositionAngleDeg` plus
 * building one rotated rectangle (`buildFovShapes`'s `paDeg`/`primary`) —
 * everything else here (`keep`, `union`, `stack`, `ghost`) is a function of
 * the *session*, not the instant, and is unchanged by dragging the instant
 * handle. `PlanningTab` relies on this split to stay inside the 16ms budget:
 * see `frames/shared/overlayDiff.ts`'s per-id diffing of `setOverlay`, which
 * skips rebuilding any shape whose id+contents did not change.
 */
import type {
  FieldGeometryModel,
  MosaicPanelModel,
  MountMode,
  RotationSampleModel,
  SessionEnvelopeModel,
  ShapeModel,
} from "../api/astro";
import { tangentOffsetToWorld } from "./projection";
import type { FovShape } from "./types";

/** The union/keep polygons already carry exact coverage, while individual
 * frame outlines only communicate direction and scale of rotation. Nine is
 * enough to read that motion without turning the target into a wireframe
 * block on a phone. */
export const MAX_DISPLAY_FRAME_SAMPLES = 9;

/** `ShapeModel.points` are tangent-plane offsets from whatever centre the
 * server computed them against (the target for `FramingResponse`, a panel
 * centre for `MosaicResponse`) — never assume the overall session target. */
export function shapeModelToWorldPolygon(
  shape: ShapeModel,
  centerRaDeg: number,
  centerDecDeg: number,
): [ra: number, dec: number][] {
  return shape.points.map((p) => tangentOffsetToWorld(centerRaDeg, centerDecDeg, p.x_deg, p.y_deg));
}

/** Shortest signed delta from `fromDeg` to `toDeg`, in (-180, 180]. */
function shortestAngleDeltaDeg(fromDeg: number, toDeg: number): number {
  let d = (toDeg - fromDeg) % 360;
  if (d > 180) d -= 360;
  if (d <= -180) d += 360;
  return d;
}

/**
 * Piecewise-linear interpolation of one `RotationSampleModel` field between
 * the two adaptively-sampled points bracketing `atSeconds`. `samples` is
 * assumed sorted by `elapsed_hours` ascending (the API's own contract —
 * `sample_session`). Out-of-range instants clamp to the session's own
 * start/end value rather than extrapolate. `circular` takes the shorter way
 * round the +-180deg seam (`shortestAngleDeltaDeg`) — for an angle field
 * such as position angle or parallactic angle, never for altitude, which has
 * no seam to cross.
 *
 * Shared by `interpolatePositionAngleDeg` (the scrubbed "primary" rectangle),
 * `interpolateParallacticAngleDeg` and `interpolateAltitudeDeg` (the horizon
 * markers below) — one bracketing/clamping rule, not three copies of it.
 */
function interpolateSampleFieldDeg(
  samples: readonly RotationSampleModel[],
  atSeconds: number,
  valueOf: (sample: RotationSampleModel) => number,
  circular: boolean,
): number {
  const first = samples[0];
  if (first === undefined) return 0;
  const last = samples[samples.length - 1];
  if (last === undefined || last === first) return valueOf(first);

  const hours = atSeconds / 3600;
  if (hours <= first.elapsed_hours) return valueOf(first);
  if (hours >= last.elapsed_hours) return valueOf(last);

  let i = 0;
  while (i < samples.length - 2) {
    const next = samples[i + 1];
    if (next === undefined || next.elapsed_hours >= hours) break;
    i++;
  }
  const a = samples[i];
  const b = samples[i + 1];
  if (a === undefined || b === undefined) return valueOf(first);
  const span = b.elapsed_hours - a.elapsed_hours;
  const frac = span <= 0 ? 0 : (hours - a.elapsed_hours) / span;
  const va = valueOf(a);
  const vb = valueOf(b);
  return circular ? va + shortestAngleDeltaDeg(va, vb) * frac : va + (vb - va) * frac;
}

/**
 * `position_angle_deg` at `atSeconds`, taking the shorter way round the
 * circle so a PA crossing the +-180deg seam does not spin the rectangle the
 * long way for one scrub step.
 */
export function interpolatePositionAngleDeg(samples: readonly RotationSampleModel[], atSeconds: number): number {
  return interpolateSampleFieldDeg(samples, atSeconds, (s) => s.position_angle_deg, true);
}

/**
 * `parallactic_angle_deg` at `atSeconds` — the horizon markers' own rotation
 * input (`buildAltitudeLimitRingShape`/`buildHorizonProfileMarkerShape`),
 * kept distinct from `interpolatePositionAngleDeg` because a non-zero
 * `pa_offset_deg` would otherwise rotate a physical horizon marker by the
 * sensor's mounting offset, which has no meaning for where the sky actually
 * is. Circular for the same seam-crossing reason as position angle.
 */
export function interpolateParallacticAngleDeg(samples: readonly RotationSampleModel[], atSeconds: number): number {
  return interpolateSampleFieldDeg(samples, atSeconds, (s) => s.parallactic_angle_deg, true);
}

/**
 * `altitude_deg` at `atSeconds` — real ephemeris, from the same
 * once-per-session `sample_session` set `interpolatePositionAngleDeg`
 * interpolates, not a second fetch. Plain linear: altitude has no +-180deg
 * seam to cross.
 */
export function interpolateAltitudeDeg(samples: readonly RotationSampleModel[], atSeconds: number): number {
  return interpolateSampleFieldDeg(samples, atSeconds, (s) => s.altitude_deg, false);
}

/** Select a compact, chronological set of frame poses for display.
 *
 * Start/end and the minimum/maximum *unwrapped* position angles are mandatory,
 * so a reversal or a crossing of the ±180° seam cannot hide an extremum. The
 * remaining slots use farthest-point sampling along cumulative angular travel,
 * which keeps more poses where rotation actually happens. A near-stationary
 * session falls back to elapsed time. This affects drawing only: the server's
 * complete adaptive sample set still drives interpolation and exact swept/keep
 * geometry.
 */
export function selectRotationDisplaySamples(
  samples: readonly RotationSampleModel[],
  maximum = MAX_DISPLAY_FRAME_SAMPLES,
): readonly RotationSampleModel[] {
  if (maximum < 2) throw new Error("rotation display needs at least two frame slots");
  if (samples.length <= maximum) return samples;

  const unwrapped = [samples[0]?.position_angle_deg ?? 0];
  const travel = [0];
  for (let index = 1; index < samples.length; index += 1) {
    const previous = samples[index - 1];
    const current = samples[index];
    if (previous === undefined || current === undefined) continue;
    const delta = shortestAngleDeltaDeg(previous.position_angle_deg, current.position_angle_deg);
    unwrapped.push((unwrapped[index - 1] ?? 0) + delta);
    travel.push((travel[index - 1] ?? 0) + Math.abs(delta));
  }

  let minIndex = 0;
  let maxIndex = 0;
  for (let index = 1; index < unwrapped.length; index += 1) {
    if ((unwrapped[index] ?? 0) < (unwrapped[minIndex] ?? 0)) minIndex = index;
    if ((unwrapped[index] ?? 0) > (unwrapped[maxIndex] ?? 0)) maxIndex = index;
  }

  const lastIndex = samples.length - 1;
  const selected = new Set([0, lastIndex, minIndex, maxIndex]);
  const totalTravel = travel[lastIndex] ?? 0;
  const axis =
    totalTravel > 0.01
      ? travel
      : samples.map((sample) => sample.elapsed_hours - (samples[0]?.elapsed_hours ?? 0));

  while (selected.size < maximum) {
    let bestIndex = -1;
    let bestDistance = -1;
    for (let index = 1; index < lastIndex; index += 1) {
      if (selected.has(index)) continue;
      const distance = Math.min(
        ...Array.from(selected, (chosen) => Math.abs((axis[index] ?? 0) - (axis[chosen] ?? 0))),
      );
      if (distance > bestDistance) {
        bestIndex = index;
        bestDistance = distance;
      }
    }
    if (bestIndex < 0) break;
    selected.add(bestIndex);
  }

  return Array.from(selected)
    .sort((left, right) => left - right)
    .map((index) => samples[index])
    .filter((sample): sample is RotationSampleModel => sample !== undefined);
}

export interface OverlayInputs {
  mount: MountMode;
  geometry: FieldGeometryModel;
  envelope: SessionEnvelopeModel;
  targetRaDeg: number;
  targetDecDeg: number;
  /** Where the scrubber's "instant" handle currently sits, in seconds
   * elapsed since the session's own `start_time`. Ignored in EQ mode — PA is
   * constant for the whole session, so there is nothing to scrub through. */
  instantSeconds: number;
}

/**
 * DESIGN.md §3.2's five alt-az shapes (`keep`, `union`, `stack`, `ghost`,
 * `primary`) or EQ's single rectangle. EQ draws straight from
 * `envelope.frame_start` — `eq_session_envelope` (`ssc/astro/framing.py`)
 * already collapses keep/union/ghost onto the one frame, so drawing all four
 * would only redraw the same rectangle on top of itself.
 */
export function buildFovShapes(inputs: OverlayInputs): FovShape[] {
  const { mount, geometry, envelope, targetRaDeg, targetDecDeg, instantSeconds } = inputs;

  if (mount === "eq") {
    return [
      {
        kind: "polygon",
        id: "primary",
        verticesRaDec: shapeModelToWorldPolygon(envelope.frame_start, targetRaDeg, targetDecDeg),
        style: { role: "primary" },
        label: "Frame (EQ mode — fixed for the whole session)",
      },
    ];
  }

  const positionAngleDeg = interpolatePositionAngleDeg(envelope.samples, instantSeconds);

  return [
    {
      kind: "polygon",
      id: "keep",
      verticesRaDec: shapeModelToWorldPolygon(envelope.keep_region, targetRaDeg, targetDecDeg),
      style: { role: "keep", fillOpacity: 0.32 },
      label: "Guaranteed-keep region (survives stacking)",
    },
    {
      kind: "polygon",
      id: "union",
      verticesRaDec: shapeModelToWorldPolygon(envelope.swept_envelope, targetRaDeg, targetDecDeg),
      style: { role: "union", dashed: true, fillOpacity: 0 },
      label: "Swept envelope (sky touched over the session)",
    },
    {
      kind: "frameStack",
      id: "stack",
      // Unfilled. Each sample frame was filled at 0.12 independently, and a
      // session samples PA(t) many times over — so the fills compounded,
      // alpha over alpha, into a solid block that hid the target the frames
      // are centred on (owner, 2026-09-10: "filled blue rectangles which
      // completely covers target"). Rotation is carried by the outlines'
      // orientation, not by area, and the sky the session touches is already
      // drawn once as `union` — which is correctly filled 0 for the same
      // reason.
      style: { role: "stack", fillOpacity: 0 },
      frames: selectRotationDisplaySamples(envelope.samples).map((sample) => ({
        atSeconds: sample.elapsed_hours * 3600,
        centerRa: targetRaDeg,
        centerDec: targetDecDeg,
        widthDeg: geometry.width_deg,
        heightDeg: geometry.height_deg,
        positionAngleDeg: sample.position_angle_deg,
      })),
    },
    {
      kind: "polygon",
      id: "ghost",
      verticesRaDec: shapeModelToWorldPolygon(envelope.frame_end, targetRaDeg, targetDecDeg),
      style: { role: "ghost", dashed: true, fillOpacity: 0 },
      label: "Frame at session end",
    },
    {
      kind: "rect",
      id: "primary",
      centerRa: targetRaDeg,
      centerDec: targetDecDeg,
      widthDeg: geometry.width_deg,
      heightDeg: geometry.height_deg,
      positionAngleDeg,
      style: { role: "primary" },
      label: "Frame at the scrubbed instant",
    },
  ];
}

// --- horizon markers ---------------------------------------------------
//
// DESIGN.md §3.2's "altitude limit ring and the user's horizon profile
// drawn on the map." Both are alt-az only, and both are built from real,
// once-per-session `SessionEnvelopeModel.samples` — never a second server
// round trip per scrub frame, same rule as `buildFovShapes`'s `primary`.
//
// EQ mode has nothing meaningful to interpolate: `eq_session_envelope`
// (`ssc/astro/framing.py`) fills `samples` with one placeholder whose
// `altitude_deg`/`parallactic_angle_deg` are both hardcoded 0, not real
// ephemeris — there is no rotation to track because the sensor is fixed to
// the sky, and there is nothing physically below it to distinguish from a
// deceptively precise-looking flat marker. `PlanningTab` must not call
// either builder below for `mount === "eq"`.
//
// The map itself is RA/Dec (inertial) and a horizon is alt/az (rotating
// with the sky), so a literal skyline traced onto it would have to move
// continuously and would need the target's own azimuth at the scrubbed
// instant to know *which* stretch of the mask is even relevant. That
// azimuth is not data this overlay has: `RotationSampleModel` carries hour
// angle, altitude and parallactic angle — everything `PA(t)` needs — not
// azimuth, and computing it here from hour angle/declination/latitude would
// be exactly the duplicated `atan2` skymap-viz.md forbids ("that number
// belongs to astro-math"). So both markers below draw a short bar,
// perpendicular to the local zenith direction, whose *position* tracks the
// real, continuously-resampled `altitude_deg(t)` (and, for the ring, is
// fully accurate — a constant-altitude ring has no azimuth dependence at
// all) but whose *reference altitude* — what the bar is measured against —
// is held at one exact number from `ssc.astro` for the whole session. That
// is exact for the flat altitude limit (there is only one number, ever) and
// a stated approximation for the physical skyline (the mask altitude at the
// target's own bearing *at session start*,
// `FramingVisibilityModel.start_horizon_altitude_deg`): the marker moves
// correctly as the target's altitude changes, held against an assumed-fixed
// obstruction height. The exact, azimuth-correct answer for "is the target
// actually behind the mask right now" is never this shape — it is
// `NightResponse.visibility_intervals`, surfaced as text by `VisibilityPanel`,
// which this marker is explicitly labelled as not overriding.

const DEG2RAD = Math.PI / 180;

/**
 * Unit vector, in the tangent-plane (`+x` east, `+y` north) basis every
 * shape in this file projects through, toward the zenith as seen from the
 * target. Standard position-angle convention — measured from celestial
 * north, increasing through east, the same convention
 * `ssc.astro.framing.azimuth_deg` documents ("Azimuth in [0, 360) measured
 * from north through east") and the one `parallactic_angle_deg` (Meeus 14.1)
 * is defined by, since the parallactic angle *is* the position angle of the
 * zenith as seen from the object: a direction at position angle `a` is
 * `(sin a, cos a)` in this basis. This is deliberately *not*
 * `rotateTangentOffset` (`projection.ts`): that rotates an already-oriented
 * offset by a plain mathematical (counter-clockwise) angle, a different
 * operation with no reason to share this one's sign convention, and mixing
 * the two up here would draw the marker on the wrong side of the frame.
 */
function zenithDirection(parallacticAngleDeg: number): { east: number; north: number } {
  const a = parallacticAngleDeg * DEG2RAD;
  return { east: Math.sin(a), north: Math.cos(a) };
}

/** Degrees of altitude margin at or below which a horizon marker reads as
 * comfortably clear, amber, or red — the same three-tier severity language
 * `FramingStatsPanel` already uses for rotation, applied here to altitude
 * margin. Not an `astro-math` number (there is no fixture for it): a
 * presentation choice, the same category of decision as `roleStyle.ts`'s
 * fill-opacity floors. */
const MARGIN_SAFE_DEG = 5;

function roleForMarginDeg(marginDeg: number): "keep" | "warning" | "danger" {
  if (marginDeg <= 0) return "danger";
  if (marginDeg < MARGIN_SAFE_DEG) return "warning";
  return "keep";
}

/**
 * A short open path, perpendicular to the local zenith direction (i.e.
 * parallel to the local alt-az horizontal — what a constant-altitude
 * surface looks like from close up) and offset from the target by
 * `marginDeg` along that direction: negative (the marker sits between the
 * target and the ground, i.e. *below* it) while the target still has
 * `marginDeg` of altitude to spare, crossing to sit level with or above the
 * target once the margin runs out or goes negative. `halfWidthDeg` only
 * controls how wide the drawn bar is, not what it means, so it reads next
 * to the frame rather than as an unexplained short stroke.
 */
function horizonMarkerVertices(
  targetRaDeg: number,
  targetDecDeg: number,
  parallacticAngleDeg: number,
  marginDeg: number,
  halfWidthDeg: number,
): [ra: number, dec: number][] {
  const zenith = zenithDirection(parallacticAngleDeg);
  const horizontal = { east: zenith.north, north: -zenith.east }; // perpendicular to zenith
  const offset = -marginDeg;
  const localPoints: [number, number][] = [
    [horizontal.east * halfWidthDeg + zenith.east * offset, horizontal.north * halfWidthDeg + zenith.north * offset],
    [
      -horizontal.east * halfWidthDeg + zenith.east * offset,
      -horizontal.north * halfWidthDeg + zenith.north * offset,
    ],
  ];
  return localPoints.map(([x, y]) => tangentOffsetToWorld(targetRaDeg, targetDecDeg, x, y));
}

export interface HorizonMarkerInputs {
  /** Alt-az only — see this section's own doc comment on why EQ's samples
   * are not usable here. */
  envelope: SessionEnvelopeModel;
  geometry: FieldGeometryModel;
  targetRaDeg: number;
  targetDecDeg: number;
  instantSeconds: number;
}

/**
 * DESIGN.md §3.2's altitude-limit ring at the scrubbed instant: exact, not
 * approximate, because a flat limit (`flat_horizon(minAltitudeDeg)` —
 * `ssc.astro.horizon`'s own "same code path as a full mask" case) has no
 * azimuth dependence, so the target's real, resampled `altitude_deg(t)` is
 * the whole answer.
 */
export function buildAltitudeLimitRingShape(inputs: HorizonMarkerInputs, minAltitudeDeg: number): FovShape {
  const { envelope, geometry, targetRaDeg, targetDecDeg, instantSeconds } = inputs;
  const altitudeDeg = interpolateAltitudeDeg(envelope.samples, instantSeconds);
  const parallacticAngleDeg = interpolateParallacticAngleDeg(envelope.samples, instantSeconds);
  const marginDeg = altitudeDeg - minAltitudeDeg;
  return {
    kind: "polyline",
    id: "altitude-limit-ring",
    verticesRaDec: horizonMarkerVertices(targetRaDeg, targetDecDeg, parallacticAngleDeg, marginDeg, geometry.width_deg / 2),
    style: { role: roleForMarginDeg(marginDeg) },
    label:
      marginDeg > 0
        ? `${minAltitudeDeg.toFixed(0)}° altitude limit — ${marginDeg.toFixed(1)}° of margin`
        : `Below the ${minAltitudeDeg.toFixed(0)}° altitude limit by ${Math.abs(marginDeg).toFixed(1)}°`,
  };
}

/**
 * DESIGN.md §3.2's user horizon profile at the scrubbed instant — see this
 * section's own doc comment for what is exact here (the marker's position)
 * and what is a stated approximation (its reference altitude, held at the
 * mask's value at the target's session-start bearing,
 * `FramingVisibilityModel.start_horizon_altitude_deg`, for the whole
 * session). Dashed, distinct from the ring's solid stroke, precisely because
 * it is the less-certain of the two.
 */
export function buildHorizonProfileMarkerShape(
  inputs: HorizonMarkerInputs,
  horizonAltitudeAtStartBearingDeg: number,
): FovShape {
  const { envelope, geometry, targetRaDeg, targetDecDeg, instantSeconds } = inputs;
  const altitudeDeg = interpolateAltitudeDeg(envelope.samples, instantSeconds);
  const parallacticAngleDeg = interpolateParallacticAngleDeg(envelope.samples, instantSeconds);
  const marginDeg = altitudeDeg - horizonAltitudeAtStartBearingDeg;
  return {
    kind: "polyline",
    id: "horizon-profile-marker",
    verticesRaDec: horizonMarkerVertices(targetRaDeg, targetDecDeg, parallacticAngleDeg, marginDeg, geometry.width_deg / 2),
    style: { role: roleForMarginDeg(marginDeg), dashed: true },
    label: "Your horizon profile (approximate — held at the target's bearing at session start)",
  };
}

/** The outline per mosaic panel, at the scrubbed instant in alt-az and at
 * that panel's fixed `frame_start` in EQ, plus — in alt-az mode, where DESIGN.md §3.3
 * warns each panel rotates independently and mosaics-plus-rotation compound
 * badly — that panel's own `keep_region`, shaded, so the outline is not read
 * as "this whole frame survives" when it does not. Drawing all five alt-az
 * shapes per panel (as `buildFovShapes` does for the single-target case)
 * was tried and is unreadable at more than a couple of panels; two per panel
 * (outline + keep) still scales to 9-12 and is the pair that answers what a
 * user actually acts on. Both share the `grid` role (not `keep`, the
 * session-level role `buildFovShapes` uses) so a panel's own keep-region
 * reads as *that panel's*, never confusable with the whole-session shaded
 * region drawn alongside it. EQ mode skips the keep shape: `eq_session_envelope`
 * collapses keep onto the one frame (no rotation to lose anything to), so a
 * second identical polygon on top would only double-draw the outline. Each
 * panel's `ShapeModel`s are already tangent-plane offsets from *that panel's
 * own centre*, not the mosaic's overall target. */
export function buildMosaicOverlayShapes(
  panels: readonly MosaicPanelModel[],
  mount: MountMode,
  instantSeconds = 0,
): FovShape[] {
  return panels.flatMap((panel) => {
    const centerRaDeg = panel.center.ra_j2000_h * 15;
    const centerDecDeg = panel.center.dec_j2000_deg;
    const label = `Panel r${String(panel.center.row)}c${String(panel.center.col)}`;
    const id = `mosaic-${String(panel.center.row)}-${String(panel.center.col)}`;
    const startPositionAngleDeg = panel.envelope.samples[0]?.position_angle_deg ?? 0;
    const currentPositionAngleDeg = interpolatePositionAngleDeg(panel.envelope.samples, instantSeconds);
    const rotationDeg = mount === "altaz"
      ? shortestAngleDeltaDeg(startPositionAngleDeg, currentPositionAngleDeg)
      : 0;
    const radians = rotationDeg * Math.PI / 180;
    const cos = Math.cos(radians);
    const sin = Math.sin(radians);
    const frameAtInstant: ShapeModel = rotationDeg === 0
      ? panel.envelope.frame_start
      : {
          points: panel.envelope.frame_start.points.map((point) => {
            return {
              x_deg: point.x_deg * cos - point.y_deg * sin,
              y_deg: point.x_deg * sin + point.y_deg * cos,
            };
          }),
        };
    const outline: FovShape = {
      kind: "polygon",
      id,
      verticesRaDec: shapeModelToWorldPolygon(frameAtInstant, centerRaDeg, centerDecDeg),
      style: { role: "grid", fillOpacity: 0 },
      label: mount === "altaz" ? `${label} at the scrubbed instant` : label,
    };
    if (mount === "eq") return [outline];
    const keep: FovShape = {
      kind: "polygon",
      id: `${id}-keep`,
      verticesRaDec: shapeModelToWorldPolygon(panel.envelope.keep_region, centerRaDeg, centerDecDeg),
      style: { role: "grid", fillOpacity: 0.4 },
      label: `${label} guaranteed-keep region (this panel's own rotation, not the session's)`,
    };
    return [outline, keep];
  });
}
