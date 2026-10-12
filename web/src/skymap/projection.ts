// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Screen <-> world projection used by `worldToScreen`/`screenToWorld`.
 *
 * `DESIGN.md` §3.1 specifies these two methods as *synchronous*, but the
 * licence boundary specifies `postMessage` as the *only* coupling to the
 * iframe (skymap-viz.md) — a synchronous cross-frame call is not on the
 * table. The resolution: the host caches the last `ViewState` the iframe
 * reported (via `viewChange`/`ready`) and projects locally with a standard
 * gnomonic (tangent-plane) projection, the same family of projection every
 * engine tried here (Aladin, then WWT, both removed — `THIRD_PARTY_NOTICES.md`
 * §9) used internally for a "north up, no rotation" view. `postMessage`
 * remains the only *coupling* — this is computed from data the iframe already
 * pushed over it, not a new channel, and no per-call round trip.
 *
 * This is presentation math for the map widget's own screen space, not
 * astronomy: it duplicates nothing from `astro/` (no parallactic angle, no
 * keep-region). It also does not attempt to match any prior engine's own
 * north-orientation or rotation handling pixel-for-pixel — screen-space
 * click targeting only needs to be close.
 *
 * RA wrap at 0h/24h (skymap-viz.md's spherical-geometry hazard): the
 * delta-RA below is normalised into (-180, 180] degrees before use, so a
 * field centred near RA 0 does not blow up.
 */
import type { ViewState } from "./types";

const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;

/** Normalise a signed RA delta (degrees) into (-180, 180]. */
export function normalizeRaDeltaDeg(deltaDeg: number): number {
  let d = deltaDeg % 360;
  if (d <= -180) d += 360;
  if (d > 180) d -= 360;
  return d;
}

/**
 * A pair of pure, mutually-inverse coordinate remaps applied *before* the
 * gnomonic maths in `worldToScreen`/`screenToWorld` runs, and to `view.ra`/
 * `view.dec` on every call too (the tangent-plane centre needs the same
 * remap as the point being projected, or the two disagree about what
 * "centre" means). This is what lets a *true horizontal* projection reuse
 * this file's one gnomonic implementation instead of a second one
 * (`skymap-viz.md`'s "the vector frame owns its projection outright... you
 * do not need a second projection: convert each point to alt/az and feed
 * the same functions, with the view centre also expressed in alt/az"):
 * `frames/shared/sidereal.ts`'s `horizontalCoordTransform` is the concrete
 * instance, mapping (ra, dec) to (-azimuth, altitude) and back — negated
 * azimuth because `worldToScreen` puts east left (right for RA, where
 * increasing RA is east; wrong for azimuth, which increases through east),
 * so azimuth must be negated first or a horizontal view comes out mirrored.
 * Absent entirely, projection is the ordinary equatorial one every caller
 * has always got — this is a pre-processing step, not a variant of the
 * maths itself, so it stays generic here and knows nothing about alt/az. */
export interface CoordTransform {
  toProjected(raDeg: number, decDeg: number): [number, number];
  fromProjected(projectedRaDeg: number, projectedDecDeg: number): [number, number];
}

export interface Viewport {
  view: ViewState;
  widthPx: number;
  heightPx: number;
  /** Screen roll, degrees, counter-clockwise. Absent or 0 is the ordinary
   * pole-up (equatorial) map every caller has always got.
   *
   * Non-zero is what the "backyard" view needs: rolling by the negative of
   * the parallactic angle at the view centre (`sidereal.ts`'s
   * `parallacticAngleDeg`) puts the *local vertical* on screen-up, so the
   * horizon lies flat along the bottom instead of cutting across as an
   * inclined plate. It lives here rather than in each frame because label
   * placement, drag hit-testing and click-picking all project through these
   * two functions — a roll applied to the camera but not to these would put
   * every label and every hit-test in the wrong place. */
  rollDeg?: number;
  /** See `CoordTransform` above. Also applied through `rollDeg` (order:
   * remap coordinates, project, then roll the tangent-plane result) though
   * the two are never expected to be set together in practice — the vector
   * frame's true horizontal projection needs no additional roll, unlike
   * WWT's roll-only approximation of the same view (removed, `THIRD_PARTY_NOTICES.md` §9). */
  coordTransform?: CoordTransform;
}

/** Gnomonic projection of (ra, dec) onto the tangent plane at `view`'s
 * centre, in pixels, with north up and east to the left (the standard sky
 * convention) — `fovDeg` is the viewport *height* in degrees. */
/** Rotates a tangent-plane offset by `rollDeg`. Its own inverse at
 * `-rollDeg`, which is what keeps `screenToWorld(worldToScreen(p)) === p`
 * true under roll — the property drag and click-picking depend on. */
function rollTangentPlane(xi: number, eta: number, rollDeg: number): [number, number] {
  if (rollDeg === 0) return [xi, eta];
  const r = rollDeg * DEG2RAD;
  const cos = Math.cos(r);
  const sin = Math.sin(r);
  return [xi * cos - eta * sin, xi * sin + eta * cos];
}

/**
 * cos(angular distance) from `viewport`'s view centre to (`ra`, `dec`),
 * after any `coordTransform` remap — the same `cosc` `worldToScreen` needs
 * internally to decide whether to clamp. Exposed because `worldToScreen`
 * *clamps* a far-hemisphere point to a finite, meaningless screen position
 * rather than rejecting it (this function's own call site below has the
 * reasoning) — a caller that draws its own mesh instead of a small
 * `FovShape` (a tile, not a FOV rectangle or a catalogue glyph — see
 * `frames/vector/imagerySpike.ts`) can reach far enough from the view
 * centre that this distinction matters, and must ask this separately
 * before drawing a triangle rather than trusting whatever `worldToScreen`
 * clamped it to.
 */
export function cosAngularDistanceFromViewCenter(viewport: Viewport, ra: number, dec: number): number {
  const { view, coordTransform } = viewport;
  const [pRa, pDec] = coordTransform ? coordTransform.toProjected(ra, dec) : [ra, dec];
  const [pViewRa, pViewDec] = coordTransform ? coordTransform.toProjected(view.ra, view.dec) : [view.ra, view.dec];
  const dec0 = pViewDec * DEG2RAD;
  const decR = pDec * DEG2RAD;
  const dRa = normalizeRaDeltaDeg(pRa - pViewRa) * DEG2RAD;
  return Math.sin(dec0) * Math.sin(decR) + Math.cos(dec0) * Math.cos(decR) * Math.cos(dRa);
}

/**
 * Below this, a point counts as "far" for `projectPolylineRuns` below — a
 * small positive margin, not exactly 0, so a vertex exactly grazing the
 * 90deg great circle does not flicker in and out one pixel of camera motion
 * at a time. Same value and the same reasoning as
 * `frames/vector/imagerySpike.ts`'s `FAR_HEMISPHERE_COSC_MARGIN` (kept as a
 * separate constant here rather than imported: that module is a spike for
 * tile meshes, this one is shipped code for every polyline/polygon shape,
 * and the two should not gain a load-bearing coupling just because they
 * currently agree on a number). At this margin the clipped boundary point
 * (see `projectPolylineRuns`) is already ~20x the viewport's own tangent
 * radius from the view centre — far enough off-canvas that canvas2d's own
 * clip crops it at the *true* field edge with no visible difference from
 * clipping at the exact 90deg point, which projects to infinity and cannot
 * be drawn at all.
 */
export const FAR_HEMISPHERE_COSC_MARGIN = 0.05;

/** Bisection steps `projectPolylineRuns` uses to locate the hemisphere
 * boundary along a crossing segment — 24 halvings of even a full RA/Dec
 * span (360deg) locate the crossing to a fraction of an arcsecond, far
 * finer than this map ever needs. */
const HEMISPHERE_BISECTION_STEPS = 24;

/** Linear interpolation between two (ra, dec) points, RA-wrap aware
 * (`normalizeRaDeltaDeg`) — an approximation of the great-circle arc between
 * them, not the arc itself, but grid/skyline vertices are already sampled
 * every few degrees (`skyLines.ts`'s `gridStepDeg`) so the approximation
 * error over one segment is negligible next to what `HEMISPHERE_BISECTION_STEPS`
 * needs to resolve. */
function interpolateRaDec(a: readonly [number, number], b: readonly [number, number], t: number): [number, number] {
  const dRa = normalizeRaDeltaDeg(b[0] - a[0]);
  const ra = (((a[0] + dRa * t) % 360) + 360) % 360;
  const dec = a[1] + (b[1] - a[1]) * t;
  return [ra, dec];
}

/** The point along the segment from `nearRaDec` (on the near hemisphere) to
 * `farRaDec` (on the far one) where `cosAngularDistanceFromViewCenter`
 * crosses `FAR_HEMISPHERE_COSC_MARGIN`, biased to stay on the near side —
 * see `projectPolylineRuns`'s own doc comment for why this is a clip point,
 * not a rejection. */
function hemisphereBoundaryPoint(
  viewport: Viewport,
  nearRaDec: readonly [number, number],
  farRaDec: readonly [number, number],
): [number, number] {
  let nearT = 0;
  let farT = 1;
  for (let i = 0; i < HEMISPHERE_BISECTION_STEPS; i += 1) {
    const midT = (nearT + farT) / 2;
    const [ra, dec] = interpolateRaDec(nearRaDec, farRaDec, midT);
    if (cosAngularDistanceFromViewCenter(viewport, ra, dec) > FAR_HEMISPHERE_COSC_MARGIN) nearT = midT;
    else farT = midT;
  }
  return interpolateRaDec(nearRaDec, farRaDec, nearT);
}

/**
 * Projects a polyline/polygon's `verticesRaDec` and splits the result into
 * contiguous **near-hemisphere** runs — the general fix for the class of bug
 * `757788c` fixed for imagery-mesh triangles (`imagerySpike.ts`'s
 * `filteredTileMeshTriangleIndices`) but never for polylines: a full-sky
 * reference line (`shared/skyLines.ts`'s equatorial grid, alt-az grid,
 * ecliptic, celestial equator, meridian, horizon) spans the whole sky, so a
 * consecutive pair of its own vertices routinely straddles the 90deg great
 * circle from the view centre. `worldToScreen` deliberately *clamps* a
 * far-hemisphere point to a large, finite, meaningless screen position
 * rather than rejecting it (that function's own doc comment) — drawing a
 * straight line to that clamped position draws a spurious chord clean across
 * the visible field, which is what the owner reported as "strange lines
 * crossing in the center of the view" against `b0e9366`, and is very likely
 * also the earlier, unreproduced "blinking additional lines": as the camera
 * moves, which vertex pairs straddle the boundary changes, so the spurious
 * chords appear and vanish.
 *
 * A crossing segment is **clipped**, not dropped: `hemisphereBoundaryPoint`
 * finds where the segment crosses `FAR_HEMISPHERE_COSC_MARGIN` and the run
 * ends (or a new one starts) there, instead of omitting both endpoints
 * outright, which would leave a visible gap well short of where the line
 * should actually end — at 60deg FOV, a noticeable truncation. The clipped
 * point still projects to a screen position far outside the canvas, in the
 * *correct* direction; canvas2d clips any stroke to the canvas itself, so
 * the line simply runs off the true edge of the frame instead of stopping
 * short of it or swinging across to the opposite, sign-flipped clamp value.
 *
 * `closed` appends the first vertex again first (before any clipping), so a
 * single unbroken run still draws the closing edge, matching the previous
 * `projectRuns` behaviour this replaces.
 */
export function projectPolylineRuns(
  viewport: Viewport,
  verticesRaDec: readonly [number, number][],
  closed: boolean,
): [number, number][][] {
  const first = verticesRaDec[0];
  if (first === undefined) return [];
  const points = closed ? [...verticesRaDec, first] : verticesRaDec;

  const runs: [number, number][][] = [];
  let current: [number, number][] = [];

  let prev = first;
  let prevCosc = cosAngularDistanceFromViewCenter(viewport, prev[0], prev[1]);
  if (prevCosc > FAR_HEMISPHERE_COSC_MARGIN) current.push(worldToScreen(viewport, prev[0], prev[1]));

  for (let i = 1; i < points.length; i += 1) {
    const curr = points[i];
    if (curr === undefined) break;
    const currCosc = cosAngularDistanceFromViewCenter(viewport, curr[0], curr[1]);
    const prevNear = prevCosc > FAR_HEMISPHERE_COSC_MARGIN;
    const currNear = currCosc > FAR_HEMISPHERE_COSC_MARGIN;

    if (prevNear && currNear) {
      current.push(worldToScreen(viewport, curr[0], curr[1]));
    } else if (prevNear) {
      // Crossing near -> far: clip and end the run here.
      const boundary = hemisphereBoundaryPoint(viewport, prev, curr);
      current.push(worldToScreen(viewport, boundary[0], boundary[1]));
      if (current.length >= 2) runs.push(current);
      current = [];
    } else if (currNear) {
      // Crossing far -> near: clip and start a new run here.
      const boundary = hemisphereBoundaryPoint(viewport, curr, prev);
      current = [worldToScreen(viewport, boundary[0], boundary[1]), worldToScreen(viewport, curr[0], curr[1])];
    }
    // far -> far: nothing to draw, `current` is already empty.

    prev = curr;
    prevCosc = currCosc;
  }
  if (current.length >= 2) runs.push(current);
  return runs;
}

export function worldToScreen(viewport: Viewport, ra: number, dec: number): [number, number] {
  const { view, widthPx, heightPx, coordTransform } = viewport;
  const [pRa, pDec] = coordTransform ? coordTransform.toProjected(ra, dec) : [ra, dec];
  const [pViewRa, pViewDec] = coordTransform ? coordTransform.toProjected(view.ra, view.dec) : [view.ra, view.dec];
  const dec0 = pViewDec * DEG2RAD;
  const decR = pDec * DEG2RAD;
  const dRa = normalizeRaDeltaDeg(pRa - pViewRa) * DEG2RAD;

  const cosc = cosAngularDistanceFromViewCenter(viewport, ra, dec);
  // A point on the far hemisphere has no finite tangent-plane position;
  // clamp rather than divide toward zero/negative so callers get a large,
  // finite (if meaningless) value instead of NaN or a sign flip. This is
  // *not* a visibility test — a caller that needs to know whether a point
  // is actually on the near hemisphere (to decide whether to draw it at
  // all, not just where it would land if it were) must call
  // `cosAngularDistanceFromViewCenter` itself; see that function's own
  // doc comment. `frames/vector/main.ts`'s `isOnScreenSide` is a *screen*-
  // space safety net for exactly the same problem, sized for small
  // `FovShape`s; a tile mesh is not always small enough for that net to
  // catch reliably, which is why this function exists as a separate,
  // angular (not screen-space) test.
  const safeCosc = Math.abs(cosc) < 1e-9 ? 1e-9 * Math.sign(cosc || 1) : cosc;

  const xi = (Math.cos(decR) * Math.sin(dRa)) / safeCosc;
  const eta = (Math.cos(dec0) * Math.sin(decR) - Math.sin(dec0) * Math.cos(decR) * Math.cos(dRa)) / safeCosc;

  const pxPerRad = heightPx / (view.fovDeg * DEG2RAD);
  const [rxi, reta] = rollTangentPlane(xi, eta, viewport.rollDeg ?? 0);
  // East is left on sky: increasing RA -> decreasing screen x.
  const x = widthPx / 2 - rxi * pxPerRad;
  const y = heightPx / 2 - reta * pxPerRad;
  return [x, y];
}

/** Inverse gnomonic projection: screen pixel -> (ra, dec) degrees. */
export function screenToWorld(viewport: Viewport, x: number, y: number): [number, number] {
  const { view, widthPx, heightPx, coordTransform } = viewport;
  const pxPerRad = heightPx / (view.fovDeg * DEG2RAD);
  const [xi, eta] = rollTangentPlane(
    (widthPx / 2 - x) / pxPerRad,
    (heightPx / 2 - y) / pxPerRad,
    -(viewport.rollDeg ?? 0),
  );

  const [pViewRa, pViewDec] = coordTransform ? coordTransform.toProjected(view.ra, view.dec) : [view.ra, view.dec];
  const dec0 = pViewDec * DEG2RAD;
  const rho = Math.hypot(xi, eta);
  const c = Math.atan(rho);
  const sinC = Math.sin(c);
  const cosC = Math.cos(c);

  if (rho < 1e-12) {
    return coordTransform ? coordTransform.fromProjected(pViewRa, pViewDec) : [view.ra, view.dec];
  }

  const decR = Math.asin(cosC * Math.sin(dec0) + (eta * sinC * Math.cos(dec0)) / rho);
  const dRa = Math.atan2(xi * sinC, rho * Math.cos(dec0) * cosC - eta * Math.sin(dec0) * sinC);

  const pRa = ((pViewRa + dRa * RAD2DEG) % 360 + 360) % 360;
  const pDec = decR * RAD2DEG;
  return coordTransform ? coordTransform.fromProjected(pRa, pDec) : [pRa, pDec];
}

/**
 * Inverse gnomonic projection of a *standard-coordinates* tangent-plane
 * offset (`+x` east, `+y` north, degrees — `OffsetModel`'s own convention,
 * `ssc/astro/framing.py`) back onto the sphere, centred at `(centerRa,
 * centerDec)`. This is the one place `ShapeModel`/`RotationSampleModel`
 * geometry — already computed server-side by `astro-math`, never
 * re-derived here — gets turned into RA/Dec for the overlay to draw.
 *
 * Distinct from `worldToScreen`/`screenToWorld` above (those project onto
 * *this widget's own current view*, in pixels); this one projects onto the
 * *target's* tangent plane, in degrees, independent of what is currently on
 * screen — the frame stack must still be correct after the user pans away.
 */
export function tangentOffsetToWorld(
  centerRaDeg: number,
  centerDecDeg: number,
  xDeg: number,
  yDeg: number,
): [ra: number, dec: number] {
  const dec0 = centerDecDeg * DEG2RAD;
  const xi = xDeg * DEG2RAD;
  const eta = yDeg * DEG2RAD;
  const rho = Math.hypot(xi, eta);
  if (rho < 1e-12) return [centerRaDeg, centerDecDeg];

  const c = Math.atan(rho);
  const sinC = Math.sin(c);
  const cosC = Math.cos(c);

  const decR = Math.asin(cosC * Math.sin(dec0) + (eta * sinC * Math.cos(dec0)) / rho);
  const dRa = Math.atan2(xi * sinC, rho * Math.cos(dec0) * cosC - eta * Math.sin(dec0) * sinC);

  const ra = ((centerRaDeg + dRa * RAD2DEG) % 360 + 360) % 360;
  return [ra, decR * RAD2DEG];
}

/** Rotate a tangent-plane offset by `angleDeg` — a position angle, measured
 * from north *through east*, exactly as `ssc.astro.framing.frame_shape`
 * rotates its corners (`x' = x cos + y sin`, `y' = -x sin + y cos`), so a
 * rectangle drawn here at `PA(t)` lands on the server's own `frame_start`
 * polygon at `t_start`. With +x east and +y north that is a *clockwise*
 * turn in the plane; the mathematically-positive (counter-clockwise) turn
 * this used to apply sent north toward west, mirroring every rotated frame
 * against the keep-region and envelope polygons `astro-math` computed.
 * Kept separate from `rectWorldCorners` so a caller rotating a whole
 * `ShapeModel`'s points can reuse it. */
export function rotateTangentOffset(xDeg: number, yDeg: number, angleDeg: number): [x: number, y: number] {
  const a = angleDeg * DEG2RAD;
  const cosA = Math.cos(a);
  const sinA = Math.sin(a);
  return [xDeg * cosA + yDeg * sinA, -xDeg * sinA + yDeg * cosA];
}

/**
 * The four corners of a `widthDeg` x `heightDeg` rectangle centred on
 * `(centerRa, centerDec)`, rotated by `positionAngleDeg` and projected onto
 * the sphere — "rotate about the frame centre in world coordinates and
 * project, not the other way round" (skymap-viz.md's spherical-geometry
 * hazard). Rotating in the tangent plane first and projecting each corner
 * afterwards (rather than scaling RA deltas by `cos(dec)`, the crude
 * small-angle shortcut this replaces) is what keeps a rectangle looking like
 * a rectangle near the pole, not a bow-tie.
 */
/**
 * `segments` vertices around the true small circle of angular radius
 * `radiusDeg` centred on (`centerRaDeg`, `centerDecDeg`) — the standard
 * "destination point given start point, bearing and distance" great-circle
 * formula, walked through every bearing. This is what a `kind: "circle"`
 * `FovShape` actually is on the sky (WWT's own `Circle` annotation draws it
 * `set_skyRelative(true)`, the same shape): a screen-space ellipse from
 * projecting only the north/south/east/west points would be wrong away from
 * the view centre, the same "rectangle is not a rectangle on screen" hazard
 * `rectWorldCorners` above exists to avoid for rectangles.
 */
export function circleWorldVertices(
  centerRaDeg: number,
  centerDecDeg: number,
  radiusDeg: number,
  segments = 48,
): [ra: number, dec: number][] {
  const dec0 = centerDecDeg * DEG2RAD;
  const r = radiusDeg * DEG2RAD;
  const vertices: [number, number][] = [];
  for (let i = 0; i < segments; i += 1) {
    const bearing = (i / segments) * 2 * Math.PI;
    const decR = Math.asin(Math.sin(dec0) * Math.cos(r) + Math.cos(dec0) * Math.sin(r) * Math.cos(bearing));
    const dRa = Math.atan2(
      Math.sin(bearing) * Math.sin(r) * Math.cos(dec0),
      Math.cos(r) - Math.sin(dec0) * Math.sin(decR),
    );
    const ra = (((centerRaDeg + dRa * RAD2DEG) % 360) + 360) % 360;
    vertices.push([ra, decR * RAD2DEG]);
  }
  return vertices;
}

export function rectWorldCorners(
  centerRaDeg: number,
  centerDecDeg: number,
  widthDeg: number,
  heightDeg: number,
  positionAngleDeg: number,
): [ra: number, dec: number][] {
  const halfW = widthDeg / 2;
  const halfH = heightDeg / 2;
  const localCorners: [number, number][] = [
    [-halfW, -halfH],
    [halfW, -halfH],
    [halfW, halfH],
    [-halfW, halfH],
  ];
  return localCorners.map(([x, y]) => {
    const [rx, ry] = rotateTangentOffset(x, y, positionAngleDeg);
    return tangentOffsetToWorld(centerRaDeg, centerDecDeg, rx, ry);
  });
}
