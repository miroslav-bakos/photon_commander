// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The vector iframe document (`web/skymap-vector.html`'s script entry) —
 * M11's own `SkyMapBackend`. No engine, no third-party vector-map library
 * (`docs/IMPLEMENTATION.md`'s M11 section: "Ours, not d3-celestial — a
 * third-party vector library is one more licence to audit"), no network:
 * everything drawn here comes from data the host already fetched and pushed
 * over `postMessage` (`setOverlay`, `setDisplay`), and every colour is a
 * `--ssc-*` token (`roleStyle.ts`) — the whole point being that red-light
 * mode is exact here in a way a tile image can never be
 * (`docs/IMPLEMENTATION.md`'s "Red-light mode" bullet).
 *
 * What this frame draws:
 *  - The host page's own themed background (`applyHostTheme.ts` sets it on
 *    `<body>` directly; this document paints nothing over it, it just clears
 *    the canvas each frame).
 *  - Every `FovShape` the host sends via `setOverlay` — the FOV rectangle(s),
 *    mosaic panels, catalogue outlines (`catalogOutlines.ts`'s DSO glyphs,
 *    already oriented by major/minor/PA — this frame is a renderer for them,
 *    not a second shape-generation layer), constellation boundaries and
 *    named bright stars. `SkyMap.tsx` merges all of these into one array
 *    before it ever reaches either backend; this frame does not know or care
 *    which builder produced a given shape.
 *  - Grids and reference lines from `setDisplay`, built by
 *    `shared/skyLines.ts` — pure `FovShape` builders, no engine dependency.
 *
 * What this frame also draws, since 2026-09-09: background/overlay survey
 * imagery (`imageryLayer.ts`), reached the ordinary way — `setSurveys`/
 * `setLayers`, the same messages `SkyMap.tsx` sends every backend — rather
 * than a URL flag, as it was while this was `imagerySpike`/
 * `imagerySpikeRenderer` (`1eb2807`). **Mercator-scheme** surveys (`unwise`,
 * `legacy-surveys-dr11`) and **HiPS-scheme** surveys (`hipsTiles.ts`) can
 * both be drawn here — HiPS support (`b6955f4`) is what left WWT with no
 * remaining capability and cleared the way to remove it
 * (`THIRD_PARTY_NOTICES.md` §9); this frame now draws every survey the registry
 * carries. A TOAST-scheme survey (none in today's registry, but the type
 * allows one) is refused with a stated reason (`imageryLayer.ts`'s
 * `refusalForImagery`) that reaches the credit line (architecture policy invariant 7
 * — no silent degradation).
 * `onCreditChange` reports the active layer's own registry metadata, never a
 * hardcoded string (`THIRD_PARTY_NOTICES.md` requirement 4); `SkyMap.tsx`
 * separately appends the constellation/star/catalogue credits, host-side,
 * regardless of backend. `onImageryLoadingChange` reports real loading via
 * the generic `<img>`-watching tracker (`imageryLoadTracking.ts`) — every
 * tile this layer fetches is a plain `new Image()` with its `src` set once,
 * so the same document-wide observer sees it with no extra wiring.
 * `setCutoutLayers` (fixed 2026-09-09: this frame accepted and silently
 * dropped every piece from when it became the default, `afeb4fc`, until
 * this fix — the live regression that left a whole day's cutout-fetch work
 * fetched, cached and never drawn) is forwarded straight to
 * `imageryLayer.ts`, which draws each piece via its own WCS-built mesh
 * (`frames/vector/cutoutMesh.ts`), above the background/overlay imagery and
 * below every `FovShape`/grid this file draws on its own 2D canvas.
 * `digitalZoomChange` is never sent — this layer never stretches an
 * ancestor tile to fill a gap; a tile that cannot be had simply is not
 * drawn.
 *
 * Projection: `projection.ts`'s gnomonic `worldToScreen`/`screenToWorld`,
 * the same functions the host uses for its own `worldToScreen`/
 * `screenToWorld` regardless of backend — reused directly here for the
 * canvas's own drawing math rather than re-derived. Whenever a site and
 * instant are known, `currentViewport()` also attaches a `CoordTransform`
 * from `shared/horizontalCamera.ts` that rotates RA/Dec into a persistent
 * horizontal camera frame before the same gnomonic maths runs. Its basis
 * remains continuous through zenith. Drawing, labels, FOV hit-testing and
 * catalogue picking all use this same viewport.
 *
 * Pan/zoom: horizontal gestures rotate the whole camera frame to preserve
 * the pointer anchor, without iterative longitude/latitude corrections.
 * Unsited equatorial views retain `shared/panZoom.ts`'s existing solver.
 */
import {
  circleWorldVertices,
  cosAngularDistanceFromViewCenter,
  projectPolylineRuns,
  rectWorldCorners,
  screenToWorld,
  worldToScreen,
  type Viewport,
} from "../../projection";
import type { SurveyCredit,
  ShapeRole, DisplayOptions, FovShape, ShapeStyle, ViewState } from "../../types";
import { applyHostTheme } from "../shared/applyHostTheme";
import {
  applyDrag,
  draggableShapeScreenPolygons,
  draggedShapeCenter,
  hitTestShapeScreen,
  primaryShapeCenter,
  worldDelta,
} from "../shared/fovDrag";
import { listenForHostMessages, postToHost } from "../shared/frameBridge";
import { trackImageryLoading } from "../shared/imageryLoadTracking";
import { pickCatalogObjectId } from "../shared/objectPick";
import { centerForAnchor, zoomAboutScreenPoint } from "../shared/panZoom";
import { makeRoleColorResolver, resolveShapeStyle } from "../shared/roleStyle";
import {
  raDecToAltAz,
  type SiteInstant,
} from "../shared/sidereal";
import { heldTargetNeedsReanchor } from "./cameraContinuity";
import {
  createHorizontalCamera,
  horizontalCameraViewport,
  horizontalCameraCenter,
  panHorizontalCameraToSkyPoint,
  zoomHorizontalCamera,
  type HorizontalCamera,
} from "../shared/horizontalCamera";
import {
  altAzGridShapes,
  altitudeLimitCircleShape,
  celestialEquatorShape,
  eclipticShape,
  equatorialGridShapes,
  horizonShapes,
  meridianShapes,
} from "../shared/skyLines";
import { createImageryLayer, type ImageryLayer } from "./imageryLayer";

applyHostTheme();

function requireElement(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (element === null) throw new Error(`[skymap] #${id} missing from skymap-vector.html`);
  return element;
}

const root = requireElement("skymap-root");
const canvas = requireElement("skymap-canvas") as HTMLCanvasElement;
const labelLayer = requireElement("skymap-labels");

/** Returns the type without the `| null` TypeScript's control-flow narrowing
 * cannot carry across a closure boundary (`ctx` below is read from inside
 * several `function` declarations further down this module, not just in
 * straight-line code after the check) — the same reason `requireElement`
 * above is a function returning `HTMLElement`, not an inline `if`. */
function require2dContext(c: HTMLCanvasElement): CanvasRenderingContext2D {
  const context = c.getContext("2d");
  if (context === null) throw new Error("[skymap] 2D canvas context unavailable");
  return context;
}
const ctx = require2dContext(canvas);

/**
 * The vector frame's own background/overlay survey imagery — always
 * created, unlike its `imagerySpike`/`imagerySpikeRenderer` predecessor
 * (which only existed behind `?imagerySpike=1`): whether anything is
 * actually drawn depends entirely on `setSurveys`/`setLayers`, the same
 * host-driven state every other feature here reads. `imageryLayer.ts`'s own
 * doc comment has the full scope (Mercator-scheme surveys only) and licence
 * boundary (same-origin tile proxy only). `scheduleRedraw` (declared further
 * down, but a hoisted `function` so this forward reference is safe) is the
 * "a tile just finished loading" callback — the same coalesced-into-one-rAF
 * redraw every other host message uses.
 */
const imageryLayer: ImageryLayer = createImageryLayer(root, canvas, scheduleRedraw);

/** Real, `<img>`-load-observed loading state (`protocol.ts`'s own doc
 * comment on why this must be a real signal, not a proxy for "a layer was
 * selected") — a generic, engine-version-proof tracker (WWT installed the
 * same one before it was removed, `THIRD_PARTY_NOTICES.md` §9): every tile
 * `imageryLayer.ts` fetches is a plain `new Image()` with its `src` set
 * once, so this document-wide observer sees it with no extra wiring. */
trackImageryLoading((loading) => {
  postToHost({ channel: "ssc-skymap/v1", type: "imageryLoadingChange", loading });
});

/** Exposed for a manual frame-time/draw-call measurement harness to read — a
 * bounded ring buffer only when the harness has already created it
 * (`undefined` by default costs one property check per frame and nothing
 * else); `__skymapImageryDrawCallsLastFrame` is a single number, always
 * written (`imageryLayer.ts`'s own doc comment on why that one is cheap
 * enough to leave unconditional). Neither is referenced by production code. */
declare global {
  interface Window {
    __skymapFrameTimesMs?: number[];
    __skymapImageryDrawCallsLastFrame?: number;
  }
}

const INITIAL_FOV_DEG = 60;
const MIN_FOV_DEG = 0.02;
/** How far out the view can zoom, in degrees of field height.
 *
 * 120 since 2026-09-09, up from 60, at the owner's request. They asked for
 * 200 first; that is not reachable with a gnomonic projection, which maps
 * one hemisphere and diverges as a point approaches 90 degrees from the
 * view centre — a 180-degree field is infinitely large and anything beyond
 * it does not exist to project. At 120 the frame edge sits 60 degrees out,
 * where the tangent-plane stretch is about 2x: visibly wide-angle, still
 * readable, and well short of where it runs away.
 *
 * A genuine all-sky view means a different projection — stereographic or
 * orthographic — which is a real piece of work rather than a constant. */
const MAX_FOV_DEG = 120;
const WHEEL_NOTCH_FACTOR = 1.25;

let view: ViewState = { ra: 0, dec: 0, fovDeg: INITIAL_FOV_DEG };
let widthPx = 800;
let heightPx = 600;

/**
 * The last `setDisplay` site/instant, kept so `currentViewport()` can
 * rebuild the horizontal `CoordTransform` for every draw, hit-test and
 * pan/zoom in between `setDisplay` calls too — the scrubber ticks these on
 * every frame (`applyDisplay` below), but a pointer drag or click can land
 * between ticks and must see the same projection the last paint used.
 */
let horizonSite: { latitudeDeg: number; longitudeDeg: number } | null = null;
let horizonInstant: Date | null = null;

/** The horizontal camera owns a continuous Cartesian screen basis as well
 * as its altitude/azimuth centre. A centre alone loses screen orientation
 * at the zenith: normalizing across the pole flips the tangent plane, and
 * recreating azimuth from equatorial coordinates makes it arbitrary there.
 * Pan and zoom rotate this whole basis; draw and pick use it unchanged.
 * Only a goto or first activation seeds a fresh horizon-up orientation. */
let hzCamera: HorizontalCamera | null = null;

/** `null`/`undefined` when `site`/`instant` are not both known (no Planning
 * location, or no resolved session instant yet) — the horizontal view is not
 * a toggle, it needs a horizon to level against, and without a site there is
 * none. WWT's own `applyZenithUp` made the same "fall back to equatorial"
 * decision for its roll-only approximation of this, before it was removed. */
function currentSiteInstant(): SiteInstant | null {
  if (horizonSite === null || horizonInstant === null) return null;
  return { ...horizonSite, instant: horizonInstant };
}

/** Refresh the equatorial mirror from the camera's forward vector. The
 * full screen basis remains authoritative in `hzCamera`. */
function syncViewFromHzCamera(site: SiteInstant): void {
  if (hzCamera === null) return;
  const [ra, dec] = horizontalCameraCenter(hzCamera, site);
  view = { ...view, ra, dec };
}

/** Zooms by `factor` about (`screenX`, `screenY`), updating `view`/`hzCamera`
 * and publishing the result — the one path `wheel`, `zoomBy` (host message)
 * share, so the horizontal-vs-equatorial branch (`hzCamera`'s doc comment)
 * is written once. */
function zoomBy(screenX: number, screenY: number, factor: number): void {
  const siteInstant = currentSiteInstant();
  if (hzCamera !== null && siteInstant !== null) {
    const zoomed = zoomHorizontalCamera(
      hzCamera,
      view.fovDeg,
      widthPx,
      heightPx,
      screenX,
      screenY,
      factor,
      MIN_FOV_DEG,
      MAX_FOV_DEG,
    );
    hzCamera = zoomed.camera;
    syncViewFromHzCamera(siteInstant);
    view = { ...view, fovDeg: zoomed.fovDeg };
  } else {
    view = zoomAboutScreenPoint(view, widthPx, heightPx, screenX, screenY, factor, MIN_FOV_DEG, MAX_FOV_DEG, undefined);
  }
  publishViewChange();
}

/** Keep the gesture's fixed sky point under the current pointer. The
 * horizontal path rotates its full camera basis, including at zenith. */
function panTo(anchorRaDeg: number, anchorDecDeg: number, fovDeg: number, screenX: number, screenY: number): void {
  const siteInstant = currentSiteInstant();
  if (hzCamera !== null && siteInstant !== null) {
    hzCamera = panHorizontalCameraToSkyPoint(
      hzCamera, fovDeg, widthPx, heightPx, siteInstant,
      anchorRaDeg, anchorDecDeg, screenX, screenY,
    );
    syncViewFromHzCamera(siteInstant);
    view = { ...view, fovDeg };
  } else {
    const center = centerForAnchor(fovDeg, widthPx, heightPx, anchorRaDeg, anchorDecDeg, screenX, screenY, undefined);
    view = { ra: center.ra, dec: center.dec, fovDeg };
  }
  publishViewChange();
}

/** Seeds `hzCamera` from `view.ra`/`view.dec` — the one place this file
 * still derives alt/az from equatorial coordinates via the ill-conditioned
 * inverse (`hzCamera`'s own doc comment), used only where that is genuinely
 * correct: a `goto` (an external, discontinuous "point somewhere new"
 * command) and the horizontal view's first activation this session, neither
 * of which has continuous camera motion to preserve in the first place. */
function seedHzCameraFromView(site: SiteInstant): void {
  const [altDeg, azDeg] = raDecToAltAz(view.ra, view.dec, site);
  hzCamera = createHorizontalCamera(altDeg, azDeg);
}

/** One viewport for drawing, labels, FOV hit-testing and catalogue
 * picking. The horizontal transform uses the camera's persistent basis,
 * never an azimuth reconstructed from the equatorial centre. */
function currentViewport(): Viewport {
  const site = currentSiteInstant();
  if (site !== null && hzCamera !== null) {
    return horizontalCameraViewport(view, widthPx, heightPx, site, hzCamera);
  }
  return { view, widthPx, heightPx };
}

/** 0..1 multipliers the host sets through `setDisplay` — see
 * `layerPrefs.ts`'s `skyLinesOpacity`. `0.5` until a host says otherwise, so
 * nothing changes for a caller that never sends them. */
let skyLinesOpacity = 0.5;
let catalogueOpacity = 0.5;
let mosaicOpacity = 1;
let solarSystemOpacity = 0.5;

/** Which group a role's opacity slider governs. The altitude-limit ring
 * (`danger`) is deliberately absent: it marks sky the mount will refuse, a
 * safety fact rather than chrome, so it is not dimmable. FOV shapes
 * (`primary`, `ghost`, `stack`, `union`, `keep`, `grid`) are absent for the
 * same reason — they are the thing being planned, not background. */
function groupOpacityFor(role: ShapeRole): number {
  switch (role) {
    case "coordGrid":
    case "skyline":
    case "cardinal":
    case "constellation":
    case "unobservable":
      return skyLinesOpacity;
    case "catalog":
    case "star":
      return catalogueOpacity;
    // The mosaic panel outline and its fill. Not lumped in with the other
    // FOV roles: a mosaic can be dozens of panels covering the whole field,
    // which is a different visual weight from one frame rectangle.
    case "grid":
      return mosaicOpacity;
    // The Sun, Moon and planets (solarSystem.ts) get their own slider,
    // deliberately not folded into `catalogueOpacity` — see
    // `DisplayOptions.solarSystemOpacity`'s own doc comment for why.
    case "sun":
    case "moon":
    case "planet":
      return solarSystemOpacity;
    default:
      return 1;
  }
}

let colorOf = makeRoleColorResolver();
let overlayShapes: FovShape[] = [];
let displayShapes: FovShape[] = [];

// --- resize -----------------------------------------------------------------

function resizeCanvas(): void {
  const rect = root.getBoundingClientRect();
  widthPx = Math.round(rect.width) || 800;
  heightPx = Math.round(rect.height) || 600;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(widthPx * dpr);
  canvas.height = Math.round(heightPx * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  imageryLayer.resize(widthPx, heightPx, dpr);
  scheduleRedraw();
}

// --- redraw scheduling (skymap-viz.md's 16ms scrubber budget) --------------
//
// Immediate-mode redraw, not WWT's diff-and-mutate approach (removed,
// `THIRD_PARTY_NOTICES.md` §9): there is no persistent annotation object here to
// diff against, only a
// canvas that gets cleared and repainted. `setOverlay` fires once per scrub
// tick with the full, already-interpolated shape set (`skymap-viz.md`: fetch
// or compute `PA(t)` once per session, interpolate, never solve per frame —
// that discipline lives in `PlanningTab.tsx`, not here); this file's own job
// is only to make sure one such call costs at most one paint, coalesced
// through `requestAnimationFrame` so a burst of `postMessage`s (e.g. a fast
// drag) never queues more paints than frames.
let redrawScheduled = false;
function scheduleRedraw(): void {
  if (redrawScheduled) return;
  redrawScheduled = true;
  window.requestAnimationFrame(() => {
    redrawScheduled = false;
    render();
  });
}

// --- drawing ------------------------------------------------------------

/** How far outside the viewport (in multiples of the larger dimension) a
 * projected point may land before `placeLabels` stops trusting it as an
 * on-screen label anchor — a cheap screen-space sanity check, not a
 * hemisphere test (a label anchor is always a single point, never a
 * segment, so it has no "spurious chord" failure mode to guard against). */
const FAR_POINT_VIEWPORT_MULTIPLE = 25;

function isOnScreenSide(point: readonly [number, number]): boolean {
  const span = Math.max(widthPx, heightPx) * FAR_POINT_VIEWPORT_MULTIPLE;
  return Math.abs(point[0] - widthPx / 2) < span && Math.abs(point[1] - heightPx / 2) < span;
}

/** Projects `verticesRaDec` and splits the result into contiguous
 * near-hemisphere runs, clipped at the 90deg boundary rather than drawn
 * through it — `projection.ts`'s `projectPolylineRuns`, this frame's own
 * call site for it. `closed` appends the first vertex again so a single
 * unbroken run still draws the closing edge.
 *
 * This used to be a screen-space-only heuristic (drop any point landing
 * further than `FAR_POINT_VIEWPORT_MULTIPLE` viewport-spans from centre),
 * which missed the actual failure mode: a segment whose two endpoints
 * straddle the 90deg great circle can have *both* project to positions well
 * inside that generous threshold (their gnomonic magnitude depends on how
 * close each is to the boundary, not on how far past it they are), and a
 * chord between them still crosses the entire visible field — the "strange
 * lines crossing in the center of view" reported live against `b0e9366`.
 * `projectPolylineRuns` fixes this at the source (per-segment, angular, not
 * screen-space), so it is the only test needed here now. */
function projectRuns(verticesRaDec: readonly [number, number][], closed: boolean): [number, number][][] {
  return projectPolylineRuns(currentViewport(), verticesRaDec, closed);
}

function setDash(ctx2d: CanvasRenderingContext2D, style: ShapeStyle): void {
  if (style.dashed === true) ctx2d.setLineDash([7, 5]);
  else if (style.dotted === true) ctx2d.setLineDash([1.5, 4]);
  else ctx2d.setLineDash([]);
}

function strokeAndMaybeFillRuns(
  runs: readonly [number, number][][],
  style: ShapeStyle,
  closed: boolean,
): void {
  const { color, fillColor, fillOpacity, lineWidth } = resolveShapeStyle(style, colorOf);
  if (color === "") return; // missing theme token — reported once, at setOverlay/setDisplay time, not per shape.
  const groupAlpha = groupOpacityFor(style.role);
  if (groupAlpha <= 0) return; // fully hidden by its slider — nothing to draw.
  ctx.lineWidth = lineWidth;
  ctx.strokeStyle = color;
  setDash(ctx, style);
  // A fill only makes sense for a single, unbroken closed run — a shape
  // split by the far-hemisphere cull (above) has no well-defined interior
  // left to fill, so it draws as open strokes only, same as an intentionally
  // open `"polyline"`.
  if (closed && fillOpacity > 0 && runs.length === 1) {
    const run = runs[0];
    if (run !== undefined) {
      ctx.beginPath();
      const first = run[0];
      if (first !== undefined) ctx.moveTo(first[0], first[1]);
      for (const [x, y] of run.slice(1)) ctx.lineTo(x, y);
      ctx.fillStyle = fillColor;
      ctx.globalAlpha = fillOpacity * groupAlpha;
      ctx.fill();
      ctx.globalAlpha = 1;
    }
  }
  ctx.globalAlpha = groupAlpha;
  for (const run of runs) {
    ctx.beginPath();
    const first = run[0];
    if (first === undefined) continue;
    ctx.moveTo(first[0], first[1]);
    for (const [x, y] of run.slice(1)) ctx.lineTo(x, y);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);
}

function drawShape(shape: FovShape): void {
  switch (shape.kind) {
    case "rect": {
      const corners = rectWorldCorners(shape.centerRa, shape.centerDec, shape.widthDeg, shape.heightDeg, shape.positionAngleDeg);
      strokeAndMaybeFillRuns(projectRuns(corners, true), shape.style, true);
      return;
    }
    case "circle": {
      const vertices = circleWorldVertices(shape.centerRa, shape.centerDec, shape.radiusDeg);
      strokeAndMaybeFillRuns(projectRuns(vertices, true), shape.style, true);
      return;
    }
    case "polygon": {
      strokeAndMaybeFillRuns(projectRuns(shape.verticesRaDec, true), shape.style, true);
      return;
    }
    case "polyline": {
      strokeAndMaybeFillRuns(projectRuns(shape.verticesRaDec, false), shape.style, false);
      return;
    }
    case "frameStack": {
      for (const frame of shape.frames) {
        const corners = rectWorldCorners(frame.centerRa, frame.centerDec, frame.widthDeg, frame.heightDeg, frame.positionAngleDeg);
        strokeAndMaybeFillRuns(projectRuns(corners, true), shape.style, true);
      }
      return;
    }
    case "grid": {
      // No builder in this codebase emits `"grid"` today (mosaic panels are
      // `"polygon"`s) — matched here rather than guessed at, drawing nothing.
      return;
    }
  }
}

function render(): void {
  const frameStart = window.__skymapFrameTimesMs !== undefined ? performance.now() : null;
  window.__skymapImageryDrawCallsLastFrame = imageryLayer.draw(currentViewport());
  ctx.save();
  // The themed background is the host page's own `<body>` colour
  // (`applyHostTheme.ts` sets it directly from the same `bg` query param);
  // this canvas only ever clears to transparent so that shows through,
  // never paints a colour of its own — one less place a literal or a missed
  // token could hide (`eslint-rules/no-literal-color.js`).
  ctx.clearRect(0, 0, widthPx, heightPx);
  for (const shape of displayShapes) drawShape(shape);
  for (const shape of overlayShapes) drawShape(shape);
  ctx.restore();
  if (frameStart !== null) window.__skymapFrameTimesMs?.push(performance.now() - frameStart);
  placeLabels();
}

// --- labels -------------------------------------------------------------
//
// Labels are DOM text over the canvas (this frame's own `worldToScreen`
// places them each redraw) since a canvas path has no text of its own — WWT
// needed the same approach for the same reason, before it was removed.

interface LabelEntry {
  element: HTMLSpanElement;
  ra: number;
  dec: number;
}
const labels = new Map<string, Map<string, LabelEntry>>();

function labelAnchor(shape: FovShape): [ra: number, dec: number] | null {
  switch (shape.kind) {
    case "rect":
      return shape.labelAt ?? [shape.centerRa, shape.centerDec];
    case "circle":
      return shape.labelAt ?? [shape.centerRa, shape.centerDec + shape.radiusDeg];
    case "polygon":
    case "polyline":
      return shape.labelAt ?? shape.verticesRaDec[0] ?? null;
    default:
      return null;
  }
}

function syncLabels(owner: string, shapes: readonly FovShape[]): void {
  let mine = labels.get(owner);
  if (mine === undefined) {
    mine = new Map();
    labels.set(owner, mine);
  }
  const seen = new Set<string>();
  for (const shape of shapes) {
    const label = "label" in shape ? shape.label : undefined;
    const labelledRole =
      shape.style.role === "catalog" ||
      shape.style.role === "skyline" ||
      shape.style.role === "coordGrid" ||
      shape.style.role === "constellation" ||
      // Added 2026-09-09 with the `cardinal` role itself. Giving N/E/S/W
      // their own role (bigger, bold, dark red) silently dropped their
      // letters, because this list is what decides whether a shape's label
      // is drawn at all — so the horizon showed four unexplained red circles.
      // A new role needs an entry here or it is mute.
      shape.style.role === "cardinal" ||
      shape.style.role === "star" ||
      // Sun/Moon/planets (solarSystem.ts) — added with the roles themselves,
      // the same lesson the `cardinal` comment above already learned once.
      shape.style.role === "sun" ||
      shape.style.role === "moon" ||
      shape.style.role === "planet";
    if (label === undefined || !labelledRole) continue;
    const anchor = labelAnchor(shape);
    if (anchor === null) continue;
    seen.add(shape.id);
    let entry = mine.get(shape.id);
    if (entry === undefined) {
      const element = document.createElement("span");
      element.className = "skymap-label";
      labelLayer.appendChild(element);
      entry = { element, ra: 0, dec: 0 };
      mine.set(shape.id, entry);
    }
    entry.element.textContent = label;
    // The cardinals are read at arm's length to answer "which way am I
    // facing", so they get their own weight and size rather than the 11px
    // every other label uses (owner: "N S W and E on skymap should be bigger
    // bold darker red"). Set as a class, not inline styles, so the sizing
    // lives with the rest of the label CSS in `skymap-vector.html`.
    // Intercardinals (NE/SE/SW/NW) are two letters and are drawn smaller
    // than the four cardinals: they are orientation *help*, not the primary
    // reference, and at the cardinals' weight eight bold red labels round
    // the horizon is a lot of shouting.
    entry.element.className =
      shape.style.role === "cardinal"
        ? `skymap-label skymap-label-cardinal${label.length > 1 ? " skymap-label-cardinal-minor" : ""}`
        : "skymap-label";
    entry.element.style.color = colorOf(shape.style.role);
    entry.ra = anchor[0];
    entry.dec = anchor[1];
  }
  for (const [id, entry] of mine) {
    if (seen.has(id)) continue;
    entry.element.remove();
    mine.delete(id);
  }
}

function placeLabels(): void {
  const viewport = currentViewport();
  for (const mine of labels.values()) {
    for (const entry of mine.values()) {
      const [x, y] = worldToScreen(viewport, entry.ra, entry.dec);
      // Reject the far hemisphere before believing the screen position at
      // all. `worldToScreen` deliberately *clamps* a point behind the viewer
      // to a large but finite position rather than returning NaN, so a label
      // for something genuinely behind you gets a plausible-looking
      // coordinate — and `isOnScreenSide` is a screen-space test that cannot
      // tell the difference.
      //
      // This is not hypothetical. Reported 2026-09-09: with the view on M31
      // (azimuth 66.8, east-north-east) the "W" cardinal — 203 degrees away,
      // behind the viewer — was drawn near the horizon exactly where "E"
      // belonged, so the map read as mirrored. It was not: the sky was
      // correct and one antipodal label was being painted into it.
      const onNearHemisphere = cosAngularDistanceFromViewCenter(viewport, entry.ra, entry.dec) > 0;
      const visible =
        onNearHemisphere && isOnScreenSide([x, y]) && x >= -40 && x <= widthPx + 40 && y >= -20 && y <= heightPx + 20;
      entry.element.style.display = visible ? "" : "none";
      if (visible) entry.element.style.transform = `translate(${String(Math.round(x))}px, ${String(Math.round(y))}px)`;
    }
  }
}

// --- overlay / display messages ------------------------------------------

function applyOverlay(shapes: FovShape[]): void {
  colorOf = makeRoleColorResolver();
  overlayShapes = shapes;
  syncLabels("host", shapes);
  scheduleRedraw();
}

/** Builds shapes from `shared/skyLines.ts` — reused, not reimplemented, the
 * same path WWT's own `applyDisplay` used before it was removed, so a grid/
 * horizon/meridian's geometry never had to be derived twice. */
function applyDisplay(display: DisplayOptions): void {
  // Absent means 1, so a host that predates these sliders is unaffected.
  skyLinesOpacity = Math.min(1, Math.max(0, display.skyLinesOpacity ?? 0.5));
  catalogueOpacity = Math.min(1, Math.max(0, display.catalogueOpacity ?? 0.5));
  mosaicOpacity = Math.min(1, Math.max(0, display.mosaicOpacity ?? 1));
  solarSystemOpacity = Math.min(1, Math.max(0, display.solarSystemOpacity ?? 0.5));
  const site = display.site;
  const instant = display.instant === null ? null : new Date(display.instant);
  const instantValid = instant !== null && Number.isFinite(instant.getTime());
  const canDrawSited = site !== null && instantValid;
  // Hold a fixed alt/az as time advances, so the *sky* drifts past a static
  // horizon rather than the horizon sweeping across fixed stars (owner: "new
  // timeline moves the model but sky is static, it should be the oposite
  // way"). The horizontal projection alone only levels the horizon at one
  // instant; without this the vector frame would still look frozen while
  // scrubbing, which is the half of the complaint the projection does not
  // answer.
  //
  // Only when the instant actually changed and a site is known: panning must
  // never be fought by this, and with no site there is no horizon to hold
  // fixed in the first place.
  const previousInstant = horizonInstant;
  horizonSite = site;
  horizonInstant = instantValid ? instant : null;
  const siteInstant = currentSiteInstant();
  if (siteInstant === null) {
    // No horizon to hold anything fixed against — nothing to track between
    // activations either (`hzCamera`'s own doc comment: it is authoritative
    // *only* while a horizontal view exists).
    hzCamera = null;
  } else if (hzCamera === null) {
    // First activation this session (or reactivation after the site/instant
    // were lost): there is no continuous camera history yet to preserve, so
    // seed once from the current equatorial view and leave it there — same
    // as the old code's behaviour of not moving the view on activation
    // (the reanchor below only ever fired on an actual instant *change*).
    seedHzCameraFromView(siteInstant);
  } else if (heldTargetNeedsReanchor(previousInstant, siteInstant.instant, display.holdOnTarget === true)) {
    // Follow the fixed equatorial target at the new instant by rotating
    // the existing basis. Re-seeding a pole-up frame here would restore the
    // zenith flip during timeline scrubbing even though pointer pans work.
    // Same-instant refreshes retain the drag-established basis unchanged.
    hzCamera = panHorizontalCameraToSkyPoint(
      hzCamera, view.fovDeg, widthPx, heightPx, siteInstant,
      view.ra, view.dec, widthPx / 2, heightPx / 2,
    );
  } else if (previousInstant !== null && previousInstant.getTime() !== siteInstant.instant.getTime()) {
    // Time moved on: hold the *same* alt/az and re-derive `view.ra`/
    // `view.dec` at the new instant — `hzCamera` itself is never touched
    // here, which is what keeps this continuous through the zenith. The old
    // code instead re-derived alt/az from `view.ra`/`view.dec` at the old
    // instant on every tick via `raDecToAltAz` — the same ill-conditioned
    // inverse `hzCamera`'s doc comment describes — so a target scrubbed
    // through the zenith over the course of a night hit the exact same
    // jump this file's camera-state rework fixes for dragging.
    syncViewFromHzCamera(siteInstant);
    publishViewChange();
  }
  for (const [needsSite, label] of [
    [display.altAzGrid, "alt-az grid"],
    [display.horizon, "horizon"],
    [display.meridian, "meridian"],
  ] as const) {
    if (needsSite && !canDrawSited) {
      postToHost({
        channel: "ssc-skymap/v1",
        type: "error",
        message: `${label} needs a site and an instant (site ${site === null ? "missing" : "set"}, instant ${instantValid ? "set" : "missing"})`,
      });
    }
  }

  const currentView = view;
  const shapes: FovShape[] = [];
  if (display.equatorialGrid) {
    shapes.push(
      ...equatorialGridShapes({
        fovDeg: Number.isFinite(currentView.fovDeg) ? currentView.fovDeg : null,
        viewRaDeg: currentView.ra,
        viewDecDeg: currentView.dec,
        strengthenEquator: !display.celestialEquator,
      }),
    );
  }
  if (display.celestialEquator) shapes.push(celestialEquatorShape(display.language));
  if (display.ecliptic) shapes.push(eclipticShape(Number.isFinite(currentView.fovDeg) ? currentView.fovDeg : null));
  if (display.altAzGrid && site !== null && instantValid) {
    shapes.push(
      ...altAzGridShapes({
        latitudeDeg: site.latitudeDeg,
        longitudeDeg: site.longitudeDeg,
        instant,
        fovDeg: Number.isFinite(currentView.fovDeg) ? currentView.fovDeg : null,
        viewRaDeg: currentView.ra,
        viewDecDeg: currentView.dec,
        strengthenHorizon: !display.horizon,
      }),
    );
  }
  if (display.horizon && site !== null && instantValid) {
    shapes.push(
      ...horizonShapes({
        latitudeDeg: site.latitudeDeg,
        longitudeDeg: site.longitudeDeg,
        instant,
        fovDeg: Number.isFinite(currentView.fovDeg) ? currentView.fovDeg : null,
      }),
    );
  }
  if (display.meridian && site !== null && instantValid) {
    shapes.push(
      ...meridianShapes({ latitudeDeg: site.latitudeDeg, longitudeDeg: site.longitudeDeg, instant }, display.language),
    );
  }
  if (display.altitudeLimitDeg !== null && site !== null && instantValid) {
    shapes.push(
      altitudeLimitCircleShape({
        latitudeDeg: site.latitudeDeg,
        longitudeDeg: site.longitudeDeg,
        instant,
        fovDeg: Number.isFinite(currentView.fovDeg) ? currentView.fovDeg : null,
        altitudeLimitDeg: display.altitudeLimitDeg,
      }),
    );
  }
  colorOf = makeRoleColorResolver();
  displayShapes = shapes;
  syncLabels("skyline", shapes);
  scheduleRedraw();
}

// --- imagery --------------------------------------------------------------
//
// `setSurveys`/`setLayers` arrive from the host regardless of backend
// (`SkyMap.tsx` always calls them once the registry loads); this frame
// forwards both straight to `imageryLayer.ts` — "host owns the selection,
// the frame owns drawing it".

let currentImageryCredits: SurveyCredit[] = [];

function publishCredits(): void {
  // `SkyMap.tsx` separately appends the catalogue/constellation/star
  // credits, host-side, from its own already-fetched queries, regardless of
  // which backend is mounted — this only ever reports imagery-layer
  // credits.
  currentImageryCredits = imageryLayer.applyLayers();
  postToHost({ channel: "ssc-skymap/v1", type: "creditChange", credits: currentImageryCredits });
}

// --- start-up -------------------------------------------------------------

resizeCanvas();
window.addEventListener("resize", () => {
  resizeCanvas();
  postToHost({ channel: "ssc-skymap/v1", type: "viewportResize", viewportPx: { w: widthPx, h: heightPx } });
});
if (typeof ResizeObserver !== "undefined") {
  new ResizeObserver(() => {
    resizeCanvas();
    postToHost({ channel: "ssc-skymap/v1", type: "viewportResize", viewportPx: { w: widthPx, h: heightPx } });
  }).observe(root);
}

function publishReady(): void {
  postToHost({
    channel: "ssc-skymap/v1",
    type: "ready",
    view,
    credits: currentImageryCredits,
    viewportPx: { w: widthPx, h: heightPx },
  });
}
publishReady();

function publishViewChange(): void {
  postToHost({ channel: "ssc-skymap/v1", type: "viewChange", view });
  scheduleRedraw();
}

// --- wheel zoom, about the cursor -------------------------------------------

for (const legacy of ["mousewheel", "DOMMouseScroll"]) {
  root.addEventListener(
    legacy,
    (e) => {
      e.preventDefault();
      e.stopPropagation();
    },
    { capture: true, passive: false },
  );
}
root.addEventListener(
  "wheel",
  (e: WheelEvent) => {
    e.preventDefault();
    const r = root.getBoundingClientRect();
    const notches = Math.max(-3, Math.min(3, e.deltaY / 100));
    const factor = WHEEL_NOTCH_FACTOR ** notches;
    zoomBy(e.clientX - r.left, e.clientY - r.top, factor);
  },
  { capture: true, passive: false },
);

// --- pointer interaction: drag the primary FOV shape, or pan the camera ----
//
// A pointerdown geometrically inside the "primary" FOV or any visible mosaic
// panel drags that framing group; otherwise the pointer pans
// the camera. Both share one active-drag slot — exactly one of the two can
// be happening at a time.

let lastHostShapes: FovShape[] = [];

type PointerMode =
  | {
      kind: "shapeDrag";
      pointerId: number;
      baseShapes: FovShape[];
      baseCenterRaDeg: number;
      baseCenterDecDeg: number;
      viewport: Viewport;
      pointerDownX: number;
      pointerDownY: number;
      lastPreviewCenterRaDeg: number;
      lastPreviewCenterDecDeg: number;
      lastPreviewShapes: FovShape[];
    }
  | { kind: "pan"; pointerId: number; anchorRaDeg: number; anchorDecDeg: number; startFovDeg: number };
let pointerMode: PointerMode | null = null;

/** Where the pointer went down, screen-space — `click`'s own handler (below)
 * compares against this to tell a plain click on a DSO glyph apart from a
 * drag-then-release over one, since a "pan" that happened to end on top of
 * a glyph must not also post an `objectClick` message. */
let pointerDownAt: { x: number; y: number } | null = null;

root.addEventListener(
  "pointerdown",
  (e: PointerEvent) => {
    if (!e.isPrimary || e.button !== 0) return;
    pointerDownAt = { x: e.clientX, y: e.clientY };
    const r = root.getBoundingClientRect();
    const px = e.clientX - r.left;
    const py = e.clientY - r.top;
    const viewport = currentViewport();
    root.setPointerCapture(e.pointerId);
    const hitsFrame = draggableShapeScreenPolygons(lastHostShapes, viewport)
      .some((polygon) => hitTestShapeScreen(polygon, px, py, 0));
    if (hitsFrame) {
      const center = primaryShapeCenter(lastHostShapes);
      if (center !== null) {
        pointerMode = {
          kind: "shapeDrag",
          pointerId: e.pointerId,
          baseShapes: lastHostShapes,
          baseCenterRaDeg: center[0],
          baseCenterDecDeg: center[1],
          viewport,
          pointerDownX: px,
          pointerDownY: py,
          lastPreviewCenterRaDeg: center[0],
          lastPreviewCenterDecDeg: center[1],
          lastPreviewShapes: lastHostShapes,
        };
        return;
      }
    }
    const [anchorRaDeg, anchorDecDeg] = screenToWorld(viewport, px, py);
    pointerMode = { kind: "pan", pointerId: e.pointerId, anchorRaDeg, anchorDecDeg, startFovDeg: view.fovDeg };
  },
  { capture: true, passive: false },
);

root.addEventListener(
  "pointermove",
  (e: PointerEvent) => {
    const active = pointerMode;
    if (active?.pointerId !== e.pointerId) return;
    e.preventDefault();
    const r = root.getBoundingClientRect();
    const px = e.clientX - r.left;
    const py = e.clientY - r.top;
    if (active.kind === "shapeDrag") {
      const [nowRa, nowDec] = draggedShapeCenter(
        active.viewport,
        active.baseCenterRaDeg,
        active.baseCenterDecDeg,
        active.pointerDownX,
        active.pointerDownY,
        px,
        py,
      );
      const { dRaDeg, dDecDeg } = worldDelta(active.baseCenterRaDeg, active.baseCenterDecDeg, nowRa, nowDec);
      const movedShapes = applyDrag(active.baseShapes, dRaDeg, dDecDeg);
      active.lastPreviewCenterRaDeg = nowRa;
      active.lastPreviewCenterDecDeg = nowDec;
      active.lastPreviewShapes = movedShapes;
      applyOverlay(movedShapes);
      return;
    }
    panTo(active.anchorRaDeg, active.anchorDecDeg, active.startFovDeg, px, py);
  },
  { capture: true, passive: false },
);

function endPointer(e: PointerEvent): void {
  const active = pointerMode;
  if (active?.pointerId !== e.pointerId) return;
  e.preventDefault();
  if (active.kind === "shapeDrag") {
    // Commit exactly the last preview that was painted. Recomputing from a
    // release/cancel event or from a viewport that resized during the drag
    // can produce a different sky coordinate and visibly jump the frame.
    applyOverlay(active.lastPreviewShapes);
    lastHostShapes = active.lastPreviewShapes;
    // Recenter through the same continuous camera path as an ordinary pan.
    // The host mirror is synchronised by SkyMap before any auxiliary overlay
    // refresh can run, so this camera move can no longer resurrect pre-drag
    // frame geometry and produce the apparent ALT/AZ/EQ flip.
    panTo(
      active.lastPreviewCenterRaDeg,
      active.lastPreviewCenterDecDeg,
      view.fovDeg,
      widthPx / 2,
      heightPx / 2,
    );
    pointerMode = null;
    postToHost({
      channel: "ssc-skymap/v1",
      type: "fovMoved",
      ra: active.lastPreviewCenterRaDeg,
      dec: active.lastPreviewCenterDecDeg,
    });
    return;
  }
  pointerMode = null;
}
root.addEventListener("pointerup", endPointer, { capture: true, passive: false });
root.addEventListener("pointercancel", endPointer, { capture: true, passive: false });
root.addEventListener("lostpointercapture", endPointer, { capture: true, passive: false });

/** A plain click (no drag) that never moved the pointer far enough to count
 * as a pan or a shape drag. */
const CLICK_MOVE_TOLERANCE_PX = 4;
root.addEventListener("click", (e: MouseEvent) => {
  const start = pointerDownAt;
  pointerDownAt = null;
  if (start !== null && Math.hypot(e.clientX - start.x, e.clientY - start.y) > CLICK_MOVE_TOLERANCE_PX) return;
  const r = root.getBoundingClientRect();
  const px = e.clientX - r.left;
  const py = e.clientY - r.top;
  const designation = pickCatalogObjectId(overlayShapes, currentViewport(), px, py);
  if (designation !== null) postToHost({ channel: "ssc-skymap/v1", type: "objectClick", designation });
});

listenForHostMessages((message) => {
  switch (message.type) {
    case "requestReady":
      publishReady();
      return;
    case "goto": {
      // An external "point somewhere new" command — discontinuous by nature
      // (see `seedHzCameraFromView`'s own doc comment), so re-deriving
      // `hzCamera` from the new `view.ra`/`view.dec` here is correct, not a
      // reintroduction of the zenith bug.
      const fovDeg = Math.min(MAX_FOV_DEG, Math.max(MIN_FOV_DEG, message.fovDeg));
      view = { ra: message.ra, dec: message.dec, fovDeg };
      const siteInstant = currentSiteInstant();
      if (siteInstant !== null) seedHzCameraFromView(siteInstant);
      publishViewChange();
      return;
    }
    case "zoomBy": {
      zoomBy(widthPx / 2, heightPx / 2, message.factor);
      return;
    }
    case "setOverlay": {
      // Planning refreshes the combined overlay as time-dependent catalogue
      // and Alt/Az data change. Applying one of those snapshots during a FOV
      // drag overwrites the live preview with the pre-drag framing, producing
      // the delayed jump most easily seen when holding a frame near an edge.
      // The completed drag posts its new centre and the host then supplies a
      // fresh authoritative overlay normally.
      if (pointerMode?.kind === "shapeDrag") return;
      lastHostShapes = message.shapes;
      applyOverlay(message.shapes);
      return;
    }
    case "setSurveys": {
      imageryLayer.setSurveys(message.surveys);
      publishCredits();
      scheduleRedraw();
      return;
    }
    case "setLayers": {
      imageryLayer.setLayers(message.layers);
      publishCredits();
      scheduleRedraw();
      return;
    }
    case "setDisplay": {
      applyDisplay(message.display);
      return;
    }
    case "setCutoutLayers": {
      imageryLayer.setCutoutLayers(message.pieces);
      // A cutout carries its own credit (`CutoutLayerPiece.creditLabel`)
      // that `applyLayers()` now
      // folds in — republish so turning a cutout row on/off in
      // `LayersPanel` updates the credit line the same way a background/
      // overlay toggle already does, not only on the next `setSurveys`/
      // `setLayers` message.
      publishCredits();
      scheduleRedraw();
      return;
    }
    case "addCatalogLayer": {
      postToHost({
        channel: "ssc-skymap/v1",
        type: "error",
        message: `addCatalogLayer(${message.layer.id}) is not implemented for the vector backend`,
      });
      return;
    }
  }
});
