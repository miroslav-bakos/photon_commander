// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Anchor-preserving pan and zoom for a frame that owns its own projection
 * (M11's vector renderer) rather than delegating to a third-party engine's
 * camera. WWT (removed, `THIRD_PARTY_NOTICES.md` §9) needed an iterative,
 * *asynchronous* re-anchor (`zoomAbout`/`settleAnchor`) because its
 * `getCoordinatesForScreenPoint` only answered for the last *rendered* frame
 * — a pick right after a synchronous camera move still reflected the old
 * view. This frame has no such lag: `projection.ts`'s
 * `worldToScreen`/`screenToWorld` are pure functions of a `ViewState` the
 * caller already holds, so the same "hold a sky point fixed under the
 * pointer" problem is solvable synchronously, in one call.
 *
 * `centerForAnchor` is the one primitive both interactions need:
 *  - **Zoom about the cursor**: capture the world point under the cursor at
 *    the *old* view, decide the *new* `fovDeg`, then ask "what view centre
 *    puts that same world point back under the cursor at the new zoom?"
 *  - **Pan**: capture the world point under the pointer where a drag started
 *    (fixed for the whole gesture, never recomputed mid-drag — the same
 *    "always translate from the base, never compound a frame's already-moved
 *    state" rule `frames/shared/fovDrag.ts` uses for shape dragging), then
 *    ask the same question at the pointer's *current* position and the same
 *    `fovDeg`.
 *
 * Pure geometry, no `window`/canvas/engine import — unit-tested directly.
 */
import { type CoordTransform, normalizeRaDeltaDeg, screenToWorld, worldToScreen } from "../../projection";

export interface ViewCenter {
  ra: number;
  dec: number;
}

/** Iterations to converge. The correction each pass makes is exact in the
 * small-field limit and only mildly nonlinear otherwise (the same gnomonic
 * family `projection.ts`'s own doc comment discusses), so a handful of
 * fixed-point passes reaches sub-pixel agreement even at the widest FOV this
 * map allows (60deg) — proven in `panZoom.test.ts` at that width. */
const DEFAULT_ITERATIONS = 8;

/**
 * Carries a (ra, dec)-like pair whose `dec` component has drifted outside
 * [-90, 90] back onto the sphere by continuing *over* the pole, rather than
 * clamping it to the pole and discarding the rest of the motion.
 *
 * Clamping is what `centerForAnchor` did before this existed, and it is
 * wrong whenever the fixed-point loop's own correction asks for more than a
 * clamp can give: once `dec` saturates at exactly 90, every further
 * iteration (or every further caller pushing the anchor screen target
 * further still, e.g. a drag continuing past the point a camera reaches the
 * true zenith) computes the *same* zero-or-arbitrary correction, and which
 * `ra` the loop happens to be sitting on when that saturation hits becomes
 * the map's whole orientation — a lottery, not a limit. Continuing past the
 * pole instead (declination 91 is the same physical point as declination 89
 * on the *opposite* meridian, exactly like longitude over a geographic pole)
 * keeps the iteration's steps meaningful on both sides, which is what makes
 * dragging (or zooming) a horizontal view straight through the zenith settle
 * on the correct far-side answer instead of pinning at the near side with a
 * one-frame orientation jump. */
function wrapOverPole(raDeg: number, decDeg: number): ViewCenter {
  let dec = ((decDeg + 180) % 360 + 360) % 360 - 180; // (-180, 180]
  let ra = raDeg;
  if (dec > 90) {
    dec = 180 - dec;
    ra += 180;
  } else if (dec < -90) {
    dec = -180 - dec;
    ra += 180;
  }
  return { ra: ((ra % 360) + 360) % 360, dec };
}

/**
 * The view centre (RA/Dec, degrees) that puts world point
 * (`anchorRaDeg`, `anchorDecDeg`) at screen position (`screenX`, `screenY`)
 * for a viewport of the given `fovDeg`/`widthPx`/`heightPx`.
 *
 * Fixed-point iteration: at the current centre guess, find out what world
 * point is *currently* sitting at the target screen position, then nudge the
 * centre by the (wrap-safe) sky delta between that point and the real
 * anchor. Mirrors what WWT's own `settleAnchor` did before it was removed,
 * just run to convergence in one synchronous call instead of across
 * animation frames.
 *
 * `coordTransform`, when given, must be the *same* one the caller's own
 * drawing/hit-testing uses (`projection.ts`'s own doc comment on why) — a
 * true horizontal view's pan has to converge in the same alt-az-projected
 * space it is drawn in, or dragging drifts diagonally instead of tracking
 * the pointer. `ra`/`dec` in and out are always true equatorial regardless:
 * only the projection used to get from screen space to that sky point
 * changes, never what "sky point" means.
 *
 * `initialGuess`, when given, replaces the anchor as the fixed-point loop's
 * starting centre (still exact only when it is already screen-centre — the
 * loop corrects the rest regardless of where it started). This matters
 * exactly where the true answer is *underdetermined*: at a coordinate
 * system's own pole (dec/altitude = ±90), every "ra"/azimuth value describes
 * the same physical point, so which one 8 fixed-point passes converge to is
 * otherwise arbitrary — and restarting from the anchor's own (fixed)
 * position every call, as the default below does, can converge to a
 * *different* arbitrary value on consecutive calls even though the camera
 * barely moved, which is exactly the kind of one-frame jump
 * `frames/vector/main.ts`'s zenith-singularity fix exists to avoid. Seeding
 * from the caller's last known centre instead makes the pole a stable fixed
 * point of the iteration — it has nowhere to drift to — rather than a
 * lottery. */
export function centerForAnchor(
  fovDeg: number,
  widthPx: number,
  heightPx: number,
  anchorRaDeg: number,
  anchorDecDeg: number,
  screenX: number,
  screenY: number,
  coordTransform?: CoordTransform,
  initialGuess?: ViewCenter,
  iterations = DEFAULT_ITERATIONS,
): ViewCenter {
  // First guess: the caller's last known centre when given (see
  // `initialGuess`'s own doc comment above), else point the camera straight
  // at the anchor — exact only when the anchor is already screen-centre; the
  // loop below corrects the rest either way.
  let { ra, dec } = wrapOverPole(initialGuess?.ra ?? anchorRaDeg, initialGuess?.dec ?? anchorDecDeg);
  for (let i = 0; i < iterations; i += 1) {
    const viewport = { view: { ra, dec, fovDeg }, widthPx, heightPx, coordTransform };
    const [nowRa, nowDec] = screenToWorld(viewport, screenX, screenY);
    const dRa = normalizeRaDeltaDeg(anchorRaDeg - nowRa);
    const dDec = anchorDecDeg - nowDec;
    // `wrapOverPole`, not a clamp (see its own doc comment): a correction
    // that would push `dec` past ±90 continues over the pole onto the
    // opposite meridian instead of saturating there.
    ({ ra, dec } = wrapOverPole(ra + dRa, dec + dDec));
  }
  return { ra, dec };
}

/**
 * Zoom by `factor` (> 1 zooms out, matching `SkyMapBackend.zoomBy`) about
 * screen position (`screenX`, `screenY`), keeping the sky point currently
 * under it fixed on screen. Clamped to [`minFovDeg`, `maxFovDeg`].
 *
 * Seeded (`centerForAnchor`'s `initialGuess`) from `view` itself — the
 * camera's own last known centre — rather than left to default to the
 * anchor: see `centerForAnchor`'s own doc comment for why that matters right
 * at a coordinate pole (zooming while centred at the zenith of a horizontal
 * view, for instance).
 */
export function zoomAboutScreenPoint(
  view: { ra: number; dec: number; fovDeg: number },
  widthPx: number,
  heightPx: number,
  screenX: number,
  screenY: number,
  factor: number,
  minFovDeg: number,
  maxFovDeg: number,
  coordTransform?: CoordTransform,
): { ra: number; dec: number; fovDeg: number } {
  const [anchorRa, anchorDec] = screenToWorld({ view, widthPx, heightPx, coordTransform }, screenX, screenY);
  const fovDeg = Math.min(maxFovDeg, Math.max(minFovDeg, view.fovDeg * factor));
  const center = centerForAnchor(fovDeg, widthPx, heightPx, anchorRa, anchorDec, screenX, screenY, coordTransform, view);
  return { ra: center.ra, dec: center.dec, fovDeg };
}

/** Re-exported for callers that already have a `Viewport`-shaped value and
 * want the anchor world point without importing `projection.ts` separately. */
export function worldPointAt(
  view: { ra: number; dec: number; fovDeg: number },
  widthPx: number,
  heightPx: number,
  screenX: number,
  screenY: number,
): [ra: number, dec: number] {
  return screenToWorld({ view, widthPx, heightPx }, screenX, screenY);
}

/** Verifies a computed centre actually round-trips — used only by tests, but
 * exported so `panZoom.test.ts` shares the same tolerance logic other tests
 * in this directory use rather than reinventing it. */
export function screenPositionOf(
  view: { ra: number; dec: number; fovDeg: number },
  widthPx: number,
  heightPx: number,
  raDeg: number,
  decDeg: number,
): [x: number, y: number] {
  return worldToScreen({ view, widthPx, heightPx }, raDeg, decDeg);
}
