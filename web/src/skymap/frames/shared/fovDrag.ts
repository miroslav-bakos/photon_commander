// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pure geometry for dragging the "primary" FOV shape (`overlay.ts`'s
 * `buildFovShapes` — the solid rectangle in alt-az, the one rectangle-shaped
 * polygon in EQ) directly on the map, instead of a separate crosshair the
 * user cannot move. No DOM, no engine import (Aladin, then WWT — both gone,
 * `THIRD_PARTY_NOTICES.md` §9), no `window` — the frame document (M11's own
 * vector renderer, `frames/vector/main.ts`; WWT wired the same pointer
 * events to these functions before it was removed) wires its pointer events
 * to these functions, which is why this lives in `frames/shared/` rather
 * than inside one frame's own directory.
 *
 * The drag keeps the shape centre's screen-space offset from the pointer
 * exact, then carries follower geometry through a rigid spherical rotation.
 * The centre never inherits the gnomonic scale at an
 * off-centre grab point. The host recomputes exact `astro-math` geometry for
 * that reported centre once `fovMoved` lands (`overlay.ts`/`PlanningTab.tsx`).
 */
import { normalizeRaDeltaDeg, rectWorldCorners, screenToWorld, worldToScreen, type Viewport } from "../../projection";
import type { FovShape } from "../../types";

const MOSAIC_ID_PREFIX = "mosaic-";

/** A mosaic panel's own outline shape (`overlay.ts`'s `buildMosaicOverlayShapes`),
 * as opposed to that same panel's `-keep` shading — used both by
 * `isFrameFollowerShape` below (every mosaic-id shape moves as one rigid
 * group during a drag) and by the mosaic fallback in `draggableShapeScreenPolygons`/
 * `primaryShapeCenter` (only the outlines, not the shading, describe the
 * grid's own footprint). */
function isMosaicOutlineId(id: string): boolean {
  return id.startsWith(MOSAIC_ID_PREFIX) && !id.endsWith("-keep");
}

/** Even-odd point-in-polygon test, screen-space pixels. */
export function pointInPolygon(px: number, py: number, poly: readonly [number, number][]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i];
    const b = poly[j];
    if (a === undefined || b === undefined) continue;
    const [xi, yi] = a;
    const [xj, yj] = b;
    const crosses = yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi;
    if (crosses) inside = !inside;
  }
  return inside;
}

function distanceToSegment(px: number, py: number, x1: number, y1: number, x2: number, y2: number): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  const t = lenSq === 0 ? 0 : Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lenSq));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

/** Shortest screen-space distance from `(px, py)` to any edge of `poly`
 * (closed — the last vertex connects back to the first). */
export function distanceToPolygonEdges(px: number, py: number, poly: readonly [number, number][]): number {
  let min = Number.POSITIVE_INFINITY;
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i];
    const b = poly[(i + 1) % poly.length];
    if (a === undefined || b === undefined) continue;
    min = Math.min(min, distanceToSegment(px, py, a[0], a[1], b[0], b[1]));
  }
  return min;
}

/**
 * Whether `(px, py)` should grab `poly`: inside it outright, or within
 * `padPx` of an edge. The pad exists because the shape's own stroke is a
 * couple of pixels wide — nowhere near a reliable touch target — and is
 * therefore a hit-test margin, never a change to the visible stroke width.
 */
export function hitTestShapeScreen(
  poly: readonly [number, number][],
  px: number,
  py: number,
  padPx: number,
): boolean {
  if (poly.length < 2) return false;
  return pointInPolygon(px, py, poly) || distanceToPolygonEdges(px, py, poly) <= padPx;
}

/** The "primary" shape's own outline in world (RA/Dec) coordinates — the
 * rotated rectangle in alt-az, the one rectangle-shaped polygon in EQ
 * (`overlay.ts`'s `buildFovShapes`). `null` for every other shape kind: the
 * ghost, swept envelope, keep-region and frame stack are all *derived from*
 * the primary shape, never draggable themselves — the whole point of
 * picking `id === "primary"` out of the set. */
export function primaryShapeWorldPolygon(shapes: readonly FovShape[]): [ra: number, dec: number][] | null {
  const shape = shapes.find((s) => s.id === "primary");
  if (shape === undefined) return null;
  if (shape.kind === "rect") {
    return rectWorldCorners(shape.centerRa, shape.centerDec, shape.widthDeg, shape.heightDeg, shape.positionAngleDeg);
  }
  if (shape.kind === "polygon") return shape.verticesRaDec;
  return null;
}

/** Every mosaic panel *outline*'s own vertices (not its `-keep` shading),
 * world coordinates, concatenated — the fallback "thing to grab" once a
 * genuine multi-panel mosaic has replaced the single "primary" rectangle
 * (`PlanningTab.tsx` filters `"primary"` out of the overlay whenever
 * `mosaic.data` reports more than one panel — reported live: "impossible to
 * drag ... mosaic to narrow down the framing", because this file's own
 * hit-test and release-time centre lookups keyed on `id === "primary"`
 * alone and silently found nothing to grab. `isFrameFollowerShape` below
 * already treats every `"mosaic-*"` id as part of the one rigid group a
 * drag moves — only the *entry point* (starting the drag, and reporting
 * where it ended) needed the same fallback). */
function mosaicOutlineVertices(shapes: readonly FovShape[]): [ra: number, dec: number][] {
  return shapes
    .filter((s): s is Extract<FovShape, { kind: "polygon" }> => s.kind === "polygon" && isMosaicOutlineId(s.id))
    .flatMap((s) => s.verticesRaDec);
}

/** Every visible rectangle that is allowed to acquire a framing drag.
 *
 * A single FOV contributes its primary rectangle. A real mosaic contributes
 * one polygon per panel outline. Keeping those polygons separate matters:
 * their overall bounding box contains sky outside rotated corner panels (and
 * may contain gaps), which made a touch on empty sky steal the gesture from
 * map panning. The caller tests "inside any panel" instead. */
export function draggableShapeScreenPolygons(
  shapes: readonly FovShape[],
  viewport: Viewport,
): [x: number, y: number][][] {
  const world = primaryShapeWorldPolygon(shapes);
  if (world !== null) return [world.map(([ra, dec]) => worldToScreen(viewport, ra, dec))];
  return shapes
    .filter((shape): shape is Extract<FovShape, { kind: "polygon" }> =>
      shape.kind === "polygon" && isMosaicOutlineId(shape.id))
    .map((shape) => shape.verticesRaDec.map(([ra, dec]) => worldToScreen(viewport, ra, dec)));
}

type Vector3 = readonly [number, number, number];
const RADIANS = Math.PI / 180;

function skyVector(ra: number, dec: number): Vector3 {
  const r = ra * RADIANS; const d = dec * RADIANS;
  return [Math.cos(d) * Math.cos(r), Math.cos(d) * Math.sin(r), Math.sin(d)];
}

function vectorSky([x, y, z]: Vector3): [number, number] {
  return [((Math.atan2(y, x) / RADIANS) % 360 + 360) % 360,
    Math.atan2(z, Math.hypot(x, y)) / RADIANS];
}

function dot(a: Vector3, b: Vector3): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

/** Mean of unit sky vectors, including wrapped RA and polar footprints. */
export function polygonCentroidRaDec(vertices: readonly [ra: number, dec: number][]): [ra: number, dec: number] {
  if (vertices.length === 0) return [0, 0];
  const sum: [number, number, number] = [0, 0, 0];
  for (const [ra, dec] of vertices) {
    const v = skyVector(ra, dec);
    sum[0] += v[0]; sum[1] += v[1]; sum[2] += v[2];
  }
  return vectorSky(sum);
}

/** The "primary" shape's own centre, in world coordinates, before any drag
 * is applied, or — when a multi-panel mosaic has replaced it (no
 * `"primary"` shape at all, see `mosaicOutlineVertices` above) — the
 * centroid of every panel outline's own vertices, which is exactly the
 * mosaic's own overall centre for a regular grid. `null` only when there is
 * truly nothing to drag (empty overlay, e.g. before a target is resolved).
 * `endDrag` (`wireFovDrag`/`frames/vector/main.ts`) reports whatever this
 * returns, post-translation, as the session's new target RA/Dec — for a
 * mosaic that re-centres the whole grid on the dragged-to position, the
 * same way it re-centres a single frame. */
export function primaryShapeCenter(shapes: readonly FovShape[]): [ra: number, dec: number] | null {
  const shape = shapes.find((s) => s.id === "primary");
  if (shape !== undefined) {
    if (shape.kind === "rect") return [shape.centerRa, shape.centerDec];
    if (shape.kind === "polygon") return polygonCentroidRaDec(shape.verticesRaDec);
    return null;
  }
  const mosaicVertices = mosaicOutlineVertices(shapes);
  return mosaicVertices.length > 0 ? polygonCentroidRaDec(mosaicVertices) : null;
}

/** The sky-space delta a screen drag from `(fromRaDeg, fromDecDeg)` to
 * `(toRaDeg, toDecDeg)` represents — both already-projected world points
 * (`screenToWorld`), so this is just the wrap-safe difference between them,
 * not a re-derivation of the projection itself. */
export function worldDelta(
  fromRaDeg: number,
  fromDecDeg: number,
  toRaDeg: number,
  toDecDeg: number,
): { dRaDeg: number; dDecDeg: number } {
  return { dRaDeg: normalizeRaDeltaDeg(toRaDeg - fromRaDeg), dDecDeg: toDecDeg - fromDecDeg };
}

/** The exact new sky position for a dragged shape's centre. The pointer may
 * grab any part of a frame or mosaic, so deriving a sky delta at the grab
 * point and applying it at the centre is wrong under a nonlinear gnomonic
 * projection: the two locations have different scale and distortion. Keep
 * the centre's original screen-space offset from the pointer instead, then
 * unproject that translated centre once. This makes a 20 px pointer move a
 * 20 px centre move regardless of where inside the shape the drag began. */
export function draggedShapeCenter(
  viewport: Viewport,
  baseCenterRaDeg: number,
  baseCenterDecDeg: number,
  pointerDownX: number,
  pointerDownY: number,
  pointerX: number,
  pointerY: number,
): [ra: number, dec: number] {
  const [centerX, centerY] = worldToScreen(viewport, baseCenterRaDeg, baseCenterDecDeg);
  return screenToWorld(
    viewport,
    centerX + pointerX - pointerDownX,
    centerY + pointerY - pointerDownY,
  );
}

function shiftPoint(ra: number, dec: number, dRaDeg: number, dDecDeg: number): [ra: number, dec: number] {
  return [((ra + dRaDeg) % 360 + 360) % 360, Math.max(-90, Math.min(90, dec + dDecDeg))];
}

/** Translates every RA/Dec coordinate `shape` carries by the same
 * `(dRaDeg, dDecDeg)` as a coordinate-offset utility. Live framing drag uses
 * `transportFollowers` instead, because coordinate offsets distort angular sizes. Every `FovShape` kind that carries a position is handled;
 * `"grid"` carries none (`spacingDeg` only) and passes through unchanged. */
export function translateFovShape(shape: FovShape, dRaDeg: number, dDecDeg: number): FovShape {
  switch (shape.kind) {
    case "rect":
    case "circle": {
      const [centerRa, centerDec] = shiftPoint(shape.centerRa, shape.centerDec, dRaDeg, dDecDeg);
      return { ...shape, centerRa, centerDec };
    }
    case "polygon":
    case "polyline": {
      return {
        ...shape,
        verticesRaDec: shape.verticesRaDec.map(([ra, dec]) => shiftPoint(ra, dec, dRaDeg, dDecDeg)),
      };
    }
    case "frameStack": {
      return {
        ...shape,
        frames: shape.frames.map((frame) => {
          const [centerRa, centerDec] = shiftPoint(frame.centerRa, frame.centerDec, dRaDeg, dDecDeg);
          return { ...frame, centerRa, centerDec };
        }),
      };
    }
    case "grid":
      return shape;
  }
}

/** `translateFovShape` over a whole set of shapes — every one of `shapes`
 * moves together while the drag is live. Callers must pre-filter to
 * `isFrameFollowerShape` (`applyDrag`, below, does): this function itself
 * has no way to tell a shape that belongs to the dragged target's framing
 * from one that does not. */
export function translateFovShapes(shapes: readonly FovShape[], dRaDeg: number, dDecDeg: number): FovShape[] {
  return shapes.map((shape) => translateFovShape(shape, dRaDeg, dDecDeg));
}

/** `overlay.ts`'s `buildFovShapes` ids for the primary rectangle and every
 * framing shape derived from it in alt-az mode (sample stack, ghost end,
 * swept envelope, keep-region) — EQ mode sends only `"primary"`, so the
 * others are simply absent from `shapes`, not present-but-unmoving. */
const FRAME_FOLLOWER_IDS = new Set(["primary", "stack", "ghost", "union", "keep"]);

/**
 * Whether `shape` is part of the *dragged target's own framing* — the only
 * shapes a live drag preview may move (`wireFovDrag`, called from the frame
 * document, `frames/vector/main.ts`). `setOverlay`'s one message carries far
 * more than that: `SkyMap.tsx` merges
 * the host's FOV shapes with its own catalogue outlines
 * (`catalogOutlines.ts`'s `catalog:<designation>:<part>` ids — nearby
 * objects' *real*, fixed sky positions, never the dragged target's) into
 * one array before it ever reaches this frame, and `PlanningTab.tsx` adds
 * the horizon-margin markers (`altitude-limit-ring`/`horizon-profile-marker`
 * — anchored to the target, but not the target's *framing*, and explicitly
 * excluded from what a drag preview moves) alongside the FOV shapes proper.
 * Moving everything in that combined array during a drag was the bug this
 * function fixes: a companion galaxy's own outline is not "derived from"
 * the rectangle being dragged and must stay exactly where it really is on
 * the sky while the shape being dragged moves under the pointer. Sky lines
 * (grids, ecliptic, horizon, the altitude-limit circle) never reach this
 * array at all — a separate `setDisplay` channel (`shared/skyLines.ts`
 * builds the shapes, the frame document draws them) — so they need no
 * filtering here.
 */
export function isFrameFollowerShape(shape: FovShape): boolean {
  return FRAME_FOLLOWER_IDS.has(shape.id) || shape.id.startsWith(MOSAIC_ID_PREFIX);
}

function skyBasis(ra: number, dec: number): readonly [Vector3, Vector3, Vector3] {
  const r = ra * RADIANS; const d = dec * RADIANS;
  return [[-Math.sin(r), Math.cos(r), 0],
    [-Math.sin(d) * Math.cos(r), -Math.sin(d) * Math.sin(r), Math.cos(d)], skyVector(ra, dec)];
}

/** Carry the complete footprint between local east/north/center bases.
 * This is one orthogonal rotation for every vertex and panel center, so
 * angular sizes, diagonals and inter-panel separations remain unchanged. */
function transportFollowers(
  shapes: readonly FovShape[], from: readonly [number, number], to: readonly [number, number],
): FovShape[] {
  const oldBasis = skyBasis(from[0], from[1]);
  const newBasis = skyBasis(to[0], to[1]);
  const rotate = (v: Vector3): Vector3 => {
    const x = dot(v, oldBasis[0]); const y = dot(v, oldBasis[1]); const z = dot(v, oldBasis[2]);
    return [x * newBasis[0][0] + y * newBasis[1][0] + z * newBasis[2][0],
      x * newBasis[0][1] + y * newBasis[1][1] + z * newBasis[2][1],
      x * newBasis[0][2] + y * newBasis[1][2] + z * newBasis[2][2]];
  };
  const point = (ra: number, dec: number): [number, number] =>
    ra === from[0] && dec === from[1] ? [to[0], to[1]] : vectorSky(rotate(skyVector(ra, dec)));
  const frame = <T extends { centerRa: number; centerDec: number; positionAngleDeg: number }>(value: T): T => {
    const [centerRa, centerDec] = point(value.centerRa, value.centerDec);
    if (value.centerRa === from[0] && value.centerDec === from[1]) {
      return { ...value, centerRa, centerDec };
    }
    const [east, north] = skyBasis(value.centerRa, value.centerDec);
    const angle = value.positionAngleDeg * RADIANS;
    const heightAxis = rotate([east[0] * Math.sin(angle) + north[0] * Math.cos(angle),
      east[1] * Math.sin(angle) + north[1] * Math.cos(angle), east[2] * Math.sin(angle) + north[2] * Math.cos(angle)]);
    const [newEast, newNorth] = skyBasis(centerRa, centerDec);
    const positionAngleDeg = ((Math.atan2(dot(heightAxis, newEast), dot(heightAxis, newNorth)) / RADIANS) % 360 + 360) % 360;
    return { ...value, centerRa, centerDec, positionAngleDeg };
  };
  return shapes.map((shape) => {
    if (!isFrameFollowerShape(shape)) return shape;
    switch (shape.kind) {
      case "rect": return frame(shape);
      case "circle": { const [centerRa, centerDec] = point(shape.centerRa, shape.centerDec); return { ...shape, centerRa, centerDec }; }
      case "polygon": case "polyline": return { ...shape, verticesRaDec: shape.verticesRaDec.map(([ra, dec]) => point(ra, dec)) };
      case "frameStack": return { ...shape, frames: shape.frames.map(frame) };
      case "grid": return shape;
    }
  });
}

/**
 * The full shape set to draw for a live drag preview at `(dRaDeg, dDecDeg)`
 * away from `baseShapes` (the overlay as it stood right before the drag
 * started): every follower shape carried by one spherical rotation, every other
 * shape passed through completely unchanged — same objects, so
 * `DiffedOverlay`'s signature diff is a no-op for them rather than a
 * pointless remove+re-add every pointer-move frame. */
export function applyDrag(baseShapes: readonly FovShape[], dRaDeg: number, dDecDeg: number): FovShape[] {
  const center = primaryShapeCenter(baseShapes);
  if (center === null) return [...baseShapes];
  if (dRaDeg === 0 && dDecDeg === 0) return [...baseShapes];
  return transportFollowers(baseShapes, center, shiftPoint(center[0], center[1], dRaDeg, dDecDeg));
}

/**
 * Mirror a renderer-completed frame drag in the host's overlay snapshot.
 *
 * The iframe previews and commits the moved followers locally, but SkyMap
 * also retains its own host-only copy so catalogue, star, body, and mount
 * refreshes can compose a complete overlay. Leaving that copy at the old
 * centre lets the first such refresh after pointer-up resurrect pre-drag
 * geometry until the replacement framing request completes — the transient
 * rotation/jump seen on the live Planning map.
 */
export function recenterFrameFollowers(
  shapes: readonly FovShape[],
  targetRaDeg: number,
  targetDecDeg: number,
): FovShape[] {
  const center = primaryShapeCenter(shapes);
  if (center === null) return [...shapes];
  const ra = targetRaDeg >= 0 && targetRaDeg < 360 ? targetRaDeg : ((targetRaDeg % 360 + 360) % 360);
  const dec = Math.max(-90, Math.min(90, targetDecDeg));
  if (center[0] === ra && center[1] === dec) return [...shapes];
  return transportFollowers(shapes, center, [ra, dec]);
}
