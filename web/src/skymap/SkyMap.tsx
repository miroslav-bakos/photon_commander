// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { useLanguage } from "../i18n/locale";

/**
 * The Planning tab's sky map. Owns the `<iframe>` (the engine boundary,
 * `THIRD_PARTY_NOTICES.md` #1) and the `SkyMapBackend` instance; renders the
 * credit line from the backend's own `onCreditChange` — one credit per
 * visible imagery layer, in draw order, plus the catalogue sources while
 * outlines are on — never a static string (compliance requirement 3).
 *
 * The frame is API-free: this component fetches the survey registry and
 * the catalogue objects in the field, and forwards what the frame needs
 * over `postMessage` (`setSurveys`/`setLayers`/`setDisplay`, and the
 * catalogue outlines as ordinary `FovShape`s). The FOV overlay itself still
 * comes from `PlanningTab.tsx` through the `SkyMapBackend` handed to
 * `onBackendReady`; that handle is a thin composition so the tab's
 * `setOverlay` and this component's outlines share one overlay without the
 * tab knowing outlines exist.
 */
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAstroBodiesQuery } from "../api/astro";
import { useMountState } from "../api/mount";
import {
  useBrightStarsQuery,
  useCachedCutoutsQuery,
  useConstellationBoundariesQuery,
  useCutoutManifestsQuery,
  useFieldObjectsQuery,
  useSkymapSurveysQuery,
  type SurveyModel,
} from "../api/skymap";
import { Card } from "../components/Card";
import { useConnectionStore } from "../store/connectionStore";
import { useThemeColor } from "../theme/useThemeColor";
import { buildBrightStarShapes, STARS_MAX_FOV_DEG } from "./brightStars";
import { buildCatalogOutlineShapes, OUTLINES_MAX_FOV_DEG } from "./catalogOutlines";
import { buildConstellationBoundaryShapes, constellationRegionGeometry } from "./constellationBoundaries";
import { createSkyMapBackend, FRAME_SRC } from "./createSkyMapBackend";
import { CutoutFetchIndicator } from "./CutoutFetchIndicator";
import { findFieldCutoutIdBySource, mergeLiveCutouts, resolveCutoutRows } from "./cutoutLayers";
import { recenterFrameFollowers } from "./frames/shared/fovDrag";
// `sidereal.ts`'s own doc comment: this is "the one deliberate exception" to
// every sky number coming from `astro/` — reused here, not reimplemented a
// second time, for the same reason the frame needs it: the meridian's
// off-screen hint below must follow the scrubber instant without a round
// trip, and it is pure geometry (no engine import, so no boundary crossed).
import { circularDistanceDeg, localMeanSiderealTimeHours, raDecToAltAz } from "./frames/shared/sidereal";
import { LayersPanel } from "./LayersPanel";
import { CUTOUT_DEFAULT_OPACITY, type LayerPrefs, loadLayerPrefs, resolveLayerSelection, saveLayerPrefs } from "./layerPrefs";
import { buildMountPointingIndicator, describeMountPointingStatus, type MountFovDeg } from "./mountPointing";
import { ObjectInfoPanel } from "./ObjectInfoPanel";
import { useSkyMapCreditsStore } from "./skyMapCreditsStore";
import { buildSolarSystemShapes } from "./solarSystem";
import { DEFAULT_CUTOUT_SOURCE_ID } from "./surveys";
import type { CutoutLayerPiece, DisplayOptions, FovShape, SkyMapBackend, SkySurvey, SurveyCredit, ViewState } from "./types";

/** Whether resolving a target automatically fetches its unWISE cutout.
 *
 * `false` since 2026-09-09 — see the effect below for why and for how to
 * turn it back on. A manual "Fetch" on a cutout row is unaffected. */

/** How long the view must hold still before the field's objects are fetched. */
const VIEW_SETTLE_MS = 300;
/** `ssc.astro.catalog.MAX_CONE_RADIUS_DEG` — the server refuses more. Raised
 * with it from 10 to 60 on 2026-09-09: at 10 a 60-degree view asked for a
 * cone covering only its middle, so outlines stopped at an invisible circle
 * and the corners looked empty. */
const MAX_CONE_RADIUS_DEG = 90;
/** The cutout size a fetch requests when the framing response has not given
 * a lens-fitted number yet — M11 spec's own default. */
const DEFAULT_CUTOUT_SIZE_DEG = 1.5;
/** Every source but unWISE requests exactly the lens-fitted field — this is
 * their own cap, unchanged since M11. */
const MAX_CUTOUT_SIZE_DEG = 3;
/** unWISE alone always requests this exact size, regardless of the lens-fitted
 * field — a framing aid needs to show what is *around* the frame, not just
 * what is in it (reported live: "always fetch 6 degrees worth of unwise
 * tiles"). Not scaled by `targetFovFittedSizeDeg`: unWISE's context field is
 * a fixed size, the same for every optic, so there is nothing to compute
 * here. `ssc.skymap.cutouts._fetch_unwise` tiles this into four 3 deg
 * requests server-side (a resolution improvement, not a frontend concern —
 * the frontend still sees one cutout job at this one size). Not raised
 * without bound: the wide optic's own FOV diagonal (60+ deg) is far past
 * what cutout imagery can usefully cover at any sane resolution, so this
 * stays a practical "more context, not the whole sky" cap, the same
 * reasoning `MAX_CUTOUT_SIZE_DEG` already used at 3. */
const UNWISE_MAX_CUTOUT_SIZE_DEG = 6;
/** How close a cached cutout's own centre must be to the resolved target to
 * count as "the field's own cutout" — small enough that it never matches a
 * neighbouring target, generous enough to absorb float rounding between two
 * requests for the same resolved coordinate. */
const CUTOUT_POINT_RADIUS_DEG = 0.05;

let cachedSkyMapFrame: HTMLIFrameElement | null = null;
let cachedSkyMapFrameSrc: string | null = null;
let cachedSkyMapFrameReady = false;
let cachedSkyMapView: ViewState | null = null;
let cachedSkyMapParkingLot: HTMLDivElement | null = null;

function skyMapParkingLot(): HTMLDivElement {
  if (cachedSkyMapParkingLot === null) {
    cachedSkyMapParkingLot = document.createElement("div");
    cachedSkyMapParkingLot.setAttribute("aria-hidden", "true");
    cachedSkyMapParkingLot.style.position = "fixed";
    cachedSkyMapParkingLot.style.left = "-10000px";
    cachedSkyMapParkingLot.style.top = "0";
    cachedSkyMapParkingLot.style.width = "1px";
    cachedSkyMapParkingLot.style.height = "1px";
    cachedSkyMapParkingLot.style.overflow = "hidden";
    document.body.appendChild(cachedSkyMapParkingLot);
  }
  return cachedSkyMapParkingLot;
}

function acquireSkyMapFrame(src: string): HTMLIFrameElement {
  if (cachedSkyMapFrame === null || cachedSkyMapFrameSrc !== src) {
    cachedSkyMapFrame?.remove();
    cachedSkyMapFrame = document.createElement("iframe");
    cachedSkyMapFrame.src = src;
    cachedSkyMapFrameSrc = src;
    cachedSkyMapFrameReady = false;
    cachedSkyMapView = null;
  }
  cachedSkyMapFrame.title = "Sky map";
  cachedSkyMapFrame.className = "h-full min-h-[65vh] w-full border-0 bg-bg sm:min-h-80";
  cachedSkyMapFrame.style.colorScheme = "dark";
  return cachedSkyMapFrame;
}

/** Preserve the browsing context where the browser supports atomic DOM moves.
 * Older browsers reload on appendChild; the host handshake replays their state. */
function moveSkyMapFrame(host: HTMLDivElement, frame: HTMLIFrameElement): void {
  if (frame.parentElement === host) return;
  const movable = host as HTMLDivElement & { moveBefore?: (node: Node, child: Node | null) => void };
  if (frame.isConnected && host.isConnected && movable.moveBefore) movable.moveBefore(frame, null);
  else {
    host.appendChild(frame);
    // A detached document may still be navigating when React reparents the
    // card. Explicitly navigate the connected frame after that cancellation.
    frame.src = cachedSkyMapFrameSrc ?? frame.src;
  }
}

function parkSkyMapFrame(host: HTMLDivElement, frame: HTMLIFrameElement): void {
  if (frame.parentElement === host) moveSkyMapFrame(skyMapParkingLot(), frame);
}

export interface SkyMapTarget {
  label: string;
  raDeg: number;
  decDeg: number;
  /** The lens-fitted field size, when framing geometry is known — the
   * cutout fetch's own default size (clamped to `MAX_CUTOUT_SIZE_DEG`);
   * `null` uses `DEFAULT_CUTOUT_SIZE_DEG`. */
  fovFittedSizeDeg: number | null;
}

export function SkyMap({
  onBackendReady,
  instant = null,
  target = null,
  altitudeLimitDeg = null,
  mountFovDeg = null,
  onResolveDesignation,
  onWarmViewRestore,
}: {
  onBackendReady?: (backend: SkyMapBackend | null) => void;
  /** A cached iframe was restored with its last known camera intact. Hosts
   * that also auto-frame a target can use this to skip their mount-time
   * recenter path, keeping the user's last map position. */
  onWarmViewRestore?: (view: ViewState) => void;
  /** Resolve a catalogue designation and make it the Planning target —
   * what the "Select target" button in the object-info popup calls. Omitted
   * by a host with nowhere to put a target, which hides the button. */
  onResolveDesignation?: (designation: string) => void;
  /** The scrubber's current UTC instant — what the meridian and alt-az grid
   * are drawn for. `null` when no session start resolves. */
  instant?: Date | null;
  /** The resolved Planning target — what the "Target field" cutout group
   * fetches imagery for. `null` when nothing is resolved yet. */
  target?: SkyMapTarget | null;
  /** The configured altitude limit (`planningUiStore`) — draws the full-sky
   * danger-coloured ring at exactly this altitude. `null` draws nothing
   * (no Planning session, or the caller does not track one). */
  altitudeLimitDeg?: number | null;
  /** The mount's real field (measured or profile-derived — see
   * `mountPointing.ts`'s own doc comment), drawn as a rotated rectangle at
   * the believed-pointing position instead of the fixed marker. `null`
   * (the default, and today the only value any host actually passes: no
   * caller yet has sensor geometry to give) falls back to that marker. */
  mountFovDeg?: MountFovDeg | null;
}) {
  const frameHostRef = useRef<HTMLDivElement | null>(null);
  const language = useLanguage();
  const [frameAttachment, setFrameAttachment] = useState<{ element: HTMLIFrameElement } | null>(null);
  const frameElement = frameAttachment?.element ?? null;
  const backendRef = useRef<SkyMapBackend | null>(null);
  const [frameCredits, setFrameCredits] = useState<SurveyCredit[] | null>(null);
  const [imageryLoading, setImageryLoading] = useState(false);
  // `null` = not currently overzoomed. Set only from a real tile failure
  // (`protocol.ts`'s `digitalZoomChange` doc comment) — never merely because
  // the view is deeper than some cached pack; real-time loading up to the
  // survey's own upstream depth is unaffected and unchanged by this.
  const [digitalZoomFactor, setDigitalZoomFactor] = useState<number | null>(null);
  // Tells `SkyMapCreditsFooter` (`Shell.tsx`) a map exists to report
  // credits for at all — cleared on unmount so a tab switch never leaves a
  // stale credit on screen for a map that is no longer showing.
  useEffect(() => {
    useSkyMapCreditsStore.getState().setMounted(true);
    return () => {
      useSkyMapCreditsStore.getState().setMounted(false);
    };
  }, []);
  // The catalogue object (if any) whose glyph was last clicked — drives
  // `ObjectInfoPanel` below.
  const [clickedDesignation, setClickedDesignation] = useState<string | null>(null);
  const [view, setView] = useState<ViewState | null>(() => cachedSkyMapView);
  const [settledView, setSettledView] = useState<ViewState | null>(null);
  // Debounced the same way `settledView` is (`VIEW_SETTLE_MS`, below): the
  // Sun/Moon/planets are the only shapes on the map that actually move with
  // the scrubber, and a drag can post a new `instant` many times a second —
  // fetching on every one of those would both blow the 16ms redraw budget
  // and hammer the server for no visible benefit between debounce windows.
  // Initialised to `instant` itself, not `null`: unlike `view` (which only
  // exists once the frame's first `onViewChange` fires), `instant` is
  // already known synchronously from props, so there is no reason to show
  // no bodies at all for the first `VIEW_SETTLE_MS` of a session.
  const [settledInstant, setSettledInstant] = useState<Date | null>(instant);
  const [prefs, setPrefsState] = useState<LayerPrefs>(() => loadLayerPrefs());
  const setPrefs = useCallback((next: LayerPrefs) => {
    setPrefsState(next);
    saveLayerPrefs(next);
  }, []);

  const location = useConnectionStore((s) => s.snapshot?.location ?? null);
  const surveysQuery = useSkymapSurveysQuery();
  const surveys = surveysQuery.data?.surveys;

  // Resolved token values, not literals. A separate document does not inherit
  // the host's CSS custom properties, and a canvas paints things no
  // stylesheet can reach — the "primary" FOV rectangle drawn there (the
  // sole, draggable position indicator) needs a real colour, not a
  // custom-property reference. So every colour the frame needs is resolved
  // here and threaded through the iframe URL.
  // Every token `frames/shared/roleStyle.ts` resolves a `ShapeRole` colour
  // from must be listed here too, or an overlay shape silently reads an empty
  // string forever — `applyHostTheme.ts`'s `URL_THEME_KEYS` is the
  // authoritative list this must match.
  const bg = useThemeColor("--ssc-bg");
  const fg = useThemeColor("--ssc-fg");
  const fgMuted = useThemeColor("--ssc-fg-muted");
  const surface = useThemeColor("--ssc-surface");
  const surfaceRaised = useThemeColor("--ssc-surface-raised");
  const border = useThemeColor("--ssc-border");
  const borderStrong = useThemeColor("--ssc-border-strong");
  const accent = useThemeColor("--ssc-accent");
  const accentMuted = useThemeColor("--ssc-accent-muted");
  const success = useThemeColor("--ssc-success");
  const warning = useThemeColor("--ssc-warning");
  const danger = useThemeColor("--ssc-danger");
  const cardinal = useThemeColor("--ssc-cardinal");
  const skyInk = useThemeColor("--ssc-sky-ink");
  const star = useThemeColor("--ssc-star");
  const sun = useThemeColor("--ssc-sun");
  const moon = useThemeColor("--ssc-moon");
  const planet = useThemeColor("--ssc-planet");
  const stack = useThemeColor("--ssc-stack");
  const envelope = useThemeColor("--ssc-envelope");
  const pointing = useThemeColor("--ssc-pointing");
  const src = `${FRAME_SRC}?${new URLSearchParams({
    bg,
    fg,
    fgMuted,
    surface,
    surfaceRaised,
    border,
    borderStrong,
    accent,
    accentMuted,
    success,
    warning,
    danger,
    cardinal,
    skyInk,
    star,
    sun,
    moon,
    planet,
    stack,
    envelope,
    pointing,
  }).toString()}`;

  const warmViewRestoreRef = useRef(onWarmViewRestore);
  useEffect(() => { warmViewRestoreRef.current = onWarmViewRestore; }, [onWarmViewRestore]);

  const attachFrameHost = useCallback((host: HTMLDivElement | null) => {
    const previousHost = frameHostRef.current;
    if (host === null) {
      if (previousHost !== null && cachedSkyMapFrame !== null) parkSkyMapFrame(previousHost, cachedSkyMapFrame);
      frameHostRef.current = null;
      setFrameAttachment(null);
      return;
    }
    const warmView =
      cachedSkyMapFrame !== null && cachedSkyMapFrameSrc === src && cachedSkyMapFrameReady ? cachedSkyMapView : null;
    frameHostRef.current = host;
    const frame = acquireSkyMapFrame(src);
    moveSkyMapFrame(host, frame);
    setFrameAttachment({ element: frame });
    if (warmView !== null) warmViewRestoreRef.current?.(warmView);
  }, [src]);

  // Six parts of one overlay: what the Planning tab draws (frames, mosaic,
  // horizon), what this component draws from the deep-sky catalogue
  // (catalogue outlines), the constellation-boundary regions, the named
  // bright stars, the Sun/Moon/planets, and the believed-pointing indicator.
  // Bodies are pushed before pointing, both after everything else: unlike
  // the background/catalogue layers they are real, bright/important things
  // the map is actually about, and the mount's own pointing is the single
  // most important of those — it must never be hidden under a catalogue
  // outline, a star dot or a body that happens to share its position.
  const hostShapesRef = useRef<FovShape[]>([]);
  const catalogShapesRef = useRef<FovShape[]>([]);
  const constellationShapesRef = useRef<FovShape[]>([]);
  const starShapesRef = useRef<FovShape[]>([]);
  const bodyShapesRef = useRef<FovShape[]>([]);
  const pointingShapesRef = useRef<FovShape[]>([]);
  const pushOverlay = useCallback(() => {
    backendRef.current?.setOverlay([
      ...hostShapesRef.current,
      ...catalogShapesRef.current,
      ...constellationShapesRef.current,
      ...starShapesRef.current,
      ...bodyShapesRef.current,
      ...pointingShapesRef.current,
    ]);
  }, []);
  const mirrorCompletedFrameDrag = useCallback((raDeg: number, decDeg: number) => {
    // This runs synchronously inside the iframe's fovMoved callback, before
    // Planning updates its target and before any view-driven auxiliary layer
    // can call pushOverlay. The host and iframe therefore agree on the moved
    // framing throughout the replacement framing/mosaic request.
    hostShapesRef.current = recenterFrameFollowers(hostShapesRef.current, raDeg, decDeg);
  }, []);

  useEffect(() => {
    const frame = frameElement;
    if (frame === null) return;

    const backend: SkyMapBackend = createSkyMapBackend(frame, { requestReady: true });
    backendRef.current = backend;
    backend.onCreditChange(setFrameCredits);
    backend.onImageryLoadingChange(setImageryLoading);
    backend.onDigitalZoomChange(setDigitalZoomFactor);
    backend.onViewChange((nextView) => {
      cachedSkyMapFrameReady = true;
      cachedSkyMapView = nextView;
      setView(nextView);
    });
    backend.onObjectClick(setClickedDesignation);
    // A layout card can move into a portal without remounting SkyMap. React
    // then replaces the iframe node while this component stays mounted. The
    // new frame starts empty, so restore its camera and all current layers.
    const previousView = view;
    if (target !== null && (previousView === null || previousView.fovDeg >= 60)) {
      backend.goto(target.raDeg, target.decDeg, target.fovFittedSizeDeg ?? 6);
    } else if (previousView !== null) {
      backend.goto(previousView.ra, previousView.dec, previousView.fovDeg);
    }
    pushOverlay();
    onBackendReady?.(composeBackend(backend, (shapes) => {
      hostShapesRef.current = shapes;
      pushOverlay();
    }, mirrorCompletedFrameDrag));

    return () => {
      onBackendReady?.(null);
      backendRef.current = null;
      backend.dispose();
    };
    // `onBackendReady`/`pushOverlay` are intentionally not deps: both are
    // effectively stable, and adding them would tear down and rebuild the
    // backend on every render rather than once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [frameAttachment, frameElement]);

  // The registry, forwarded as-is (reduced to what a frame needs).
  useEffect(() => {
    if (surveys === undefined) return;
    backendRef.current?.setSurveys(surveys.map(toSkySurvey));
  }, [surveys, frameAttachment]);

  // The viewer's choice, resolved against what the registry can actually show.
  useEffect(() => {
    if (surveys === undefined) return;
    backendRef.current?.setLayers(resolveLayerSelection(prefs, surveys));
  }, [prefs, surveys, frameAttachment]);

  const siteUnavailableReason = location === null ? "set a location in Settings" : null;
  const holdOnTarget = target !== null;
  useEffect(() => {
    const hasSitedInstant = location !== null && instant !== null;
    const display: DisplayOptions = {
      language,
      equatorialGrid: prefs.skyLines.equatorialGrid,
      altAzGrid: prefs.skyLines.altAzGrid && hasSitedInstant,
      ecliptic: prefs.skyLines.ecliptic,
      celestialEquator: prefs.skyLines.celestialEquator,
      meridian: prefs.skyLines.meridian && hasSitedInstant,
      horizon: prefs.skyLines.horizon && hasSitedInstant,
      // The ring is opt-in (`layerPrefs.ts`'s `altitudeLimitRing` doc
      // comment): while the toggle is off, the host simply does not supply
      // a value -- the limit itself (Planning's own field) is unaffected.
      // Resolved target => framing, so hold the sky still around it; no
      // target => hunting, so let the sky turn past the horizon.
      holdOnTarget,
      skyLinesOpacity: prefs.skyLinesOpacity,
      catalogueOpacity: prefs.catalogueOpacity,
      mosaicOpacity: prefs.mosaicOpacity,
      solarSystemOpacity: prefs.solarSystemOpacity,
      altitudeLimitDeg: prefs.altitudeLimitRing ? altitudeLimitDeg : null,
      site: location === null ? null : { latitudeDeg: location.latitude_deg, longitudeDeg: location.longitude_deg },
      instant: instant === null ? null : instant.toISOString(),
    };
    backendRef.current?.setDisplay(display);
    // Every field the body above actually reads: `prefs.skyLines` covered
    // the toggles but not the four opacity sliders or `target` (for
    // `holdOnTarget`), so changing Lines/Panels/Glyphs opacity did nothing
    // until some unrelated dependency happened to fire and flush the stale
    // value through as a surprise — reported live, "the latest sliders
    // introduced dont work for opacity" (owner, 2026-09-10). eslint's
    // `react-hooks/exhaustive-deps` had already flagged this exact set and
    // was wrongly waved through; `SkyMap.test.tsx` now asserts the update
    // path, not just the initial call, so a regression here fails a test.
  }, [
    language,
    prefs.skyLines,
    prefs.altitudeLimitRing,
    prefs.skyLinesOpacity,
    prefs.catalogueOpacity,
    prefs.mosaicOpacity,
    prefs.solarSystemOpacity,
    location,
    instant,
    holdOnTarget,
    altitudeLimitDeg,
    frameAttachment,
  ]);

  // Debounce the view: the catalogue cone is fetched once the map holds still.
  useEffect(() => {
    if (view === null) return;
    const handle = window.setTimeout(() => {
      setSettledView(view);
    }, VIEW_SETTLE_MS);
    return () => {
      window.clearTimeout(handle);
    };
  }, [view]);

  // Same debounce, for the scrubber's instant (see `settledInstant`'s own
  // doc comment above).
  useEffect(() => {
    const handle = window.setTimeout(() => {
      setSettledInstant(instant);
    }, VIEW_SETTLE_MS);
    return () => {
      window.clearTimeout(handle);
    };
  }, [instant]);

  // The map's aspect ratio, for the cone radius: `fovDeg` is the viewport
  // *height*, and a wide map shows more sky to the sides.
  const [aspect, setAspect] = useState(4 / 3);
  useEffect(() => {
    const frame = frameElement;
    if (frame === null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (height > 0) setAspect(round(width / height, 3));
      }
    });
    observer.observe(frame);
    return () => {
      observer.disconnect();
    };
  }, [frameAttachment, frameElement]);

  const fieldRequest = useMemo(() => {
    if (!prefs.catalogOutlines || settledView === null || settledView.fovDeg > OUTLINES_MAX_FOV_DEG) return null;
    // Half the field's diagonal, so the corners are covered too.
    const radius = Math.min(MAX_CONE_RADIUS_DEG, 0.5 * settledView.fovDeg * Math.hypot(1, aspect));
    return {
      ra_deg: round(settledView.ra, 3),
      dec_deg: round(settledView.dec, 3),
      radius_deg: round(radius, 3),
    };
  }, [prefs.catalogOutlines, settledView, aspect]);
  const objectsQuery = useFieldObjectsQuery(fieldRequest);
  const objects = fieldRequest === null ? undefined : objectsQuery.data;

  useEffect(() => {
    catalogShapesRef.current =
      objects === undefined || settledView === null ? [] : buildCatalogOutlineShapes(objects.objects, { fovDeg: settledView.fovDeg });
    pushOverlay();
  }, [objects, settledView, pushOverlay]);

  // -- constellation boundaries (the "Sky lines" group's own toggle) -------
  //
  // Gated behind the toggle itself (`useConstellationBoundariesQuery`'s own
  // `enabled`), not `settledView`: unlike the catalogue cone above, this
  // data does not depend on where the view is, so there is nothing to wait
  // for it to settle before fetching — only the *shapes and labels* need
  // the settled view, and only for label placement.
  const boundariesQuery = useConstellationBoundariesQuery(prefs.constellationBoundaries);
  // The expensive walk over all 1562 vertices, done once per fetch
  // (`staleTime: Infinity` keeps `boundariesQuery.data` referentially
  // stable across renders) — never repeated on a view settle or scrub tick.
  const constellationGeometry = useMemo(
    () => (boundariesQuery.data === undefined ? null : constellationRegionGeometry(boundariesQuery.data.regions)),
    [boundariesQuery.data],
  );

  useEffect(() => {
    constellationShapesRef.current =
      !prefs.constellationBoundaries || constellationGeometry === null || settledView === null
        ? []
        : buildConstellationBoundaryShapes(constellationGeometry, {
            fovDeg: settledView.fovDeg,
            viewRaDeg: settledView.ra,
            viewDecDeg: settledView.dec,
          });
    pushOverlay();
  }, [prefs.constellationBoundaries, constellationGeometry, settledView, pushOverlay]);

  // -- named bright stars (the "Catalogue" group's own toggle) -------------
  //
  // Same cone-on-the-settled-view shape as the catalogue-outline fetch
  // above, including the same `STARS_MAX_FOV_DEG` (= `OUTLINES_MAX_FOV_DEG`)
  // ceiling: past that width the requested cone cannot cover the field
  // either, so nothing is fetched rather than drawing a partial scatter.
  const starsRequest = useMemo(() => {
    if (!prefs.brightStars || settledView === null || settledView.fovDeg > STARS_MAX_FOV_DEG) return null;
    const radius = Math.min(MAX_CONE_RADIUS_DEG, 0.5 * settledView.fovDeg * Math.hypot(1, aspect));
    return {
      ra_deg: round(settledView.ra, 3),
      dec_deg: round(settledView.dec, 3),
      radius_deg: round(radius, 3),
    };
  }, [prefs.brightStars, settledView, aspect]);
  const starsQuery = useBrightStarsQuery(starsRequest);
  const stars = starsRequest === null ? undefined : starsQuery.data;

  useEffect(() => {
    starShapesRef.current =
      stars === undefined || settledView === null
        ? []
        : buildBrightStarShapes(stars.stars, {
            fovDeg: settledView.fovDeg,
            viewRaDeg: settledView.ra,
            viewDecDeg: settledView.dec,
          });
    pushOverlay();
  }, [stars, settledView, pushOverlay]);

  // -- Sun, Moon and planets (the "Solar system" group's own toggles) ------
  //
  // Unlike the catalogue/constellation/star fetches above, this is not a
  // cone on the settled *view* — the Sun, Moon and planets are the same
  // handful of bodies wherever the map is pointed, so the only thing that
  // changes what to fetch is the settled *instant*. Skipped entirely (no
  // fetch at all) when every toggle in the group is off, the same
  // "nothing to draw, so nothing to ask for" rule the catalogue and star
  // fetches use for their own toggles.
  //
  // Also skipped without a location: positions are topocentric (the
  // endpoint's own doc comment — a geocentric Moon is up to 1 deg wrong), so
  // it 409s without one, the same guard `/framing`/`/night` already use.
  // `null` here is the honest "nothing to draw" this component uses
  // everywhere else a prerequisite is missing (architecture policy invariant 7) —
  // never a request sent to fail, and the Layers toggles stay on screen
  // regardless (`LayersPanel`'s own `siteUnavailableReason` disables them
  // with a reason rather than hiding the group).
  const solarSystemEnabled = prefs.solarSystem.sun || prefs.solarSystem.moon || prefs.solarSystem.planets;
  const bodiesInstant =
    solarSystemEnabled && settledInstant !== null && location !== null ? settledInstant.toISOString() : null;
  const bodiesQuery = useAstroBodiesQuery(bodiesInstant);
  const bodies = bodiesInstant === null ? undefined : bodiesQuery.data;

  useEffect(() => {
    bodyShapesRef.current = bodies === undefined ? [] : buildSolarSystemShapes(bodies.bodies, prefs.solarSystem);
    pushOverlay();
  }, [bodies, prefs.solarSystem, pushOverlay]);

  // -- believed-pointing indicator (DESIGN.md §3.5.1) -----------------------
  //
  // `snapshot.mount` is polled, not pushed (the native Alpaca surface has no
  // event channel — `mount.ts`'s own doc comment), so staleness has to keep
  // advancing between polls on a wall clock of its own — the same reasoning
  // `MountTab/useNow.ts` documents (not imported from there: `web/src/tabs/**`
  // is `frontend-react`'s file, this component is `skymap-viz`'s).
  const mount = useMountState();
  const solvedCameraFov = useConnectionStore((s) => s.snapshot?.camera_fov ?? null);
  const liveMountFov = useMemo<MountFovDeg | null>(() => {
    // A completed solve is strongest because it carries measured centre and
    // rotation. Otherwise prefer the host's configured optic: Seestar 9.31
    // exposes sensor dimensions but does not implement FocalLength.
    if (solvedCameraFov?.solved_at === null || solvedCameraFov === null) {
      if (mountFovDeg !== null) return mountFovDeg;
    }
    if (solvedCameraFov === null) return null;
    const centerRaHours = solvedCameraFov.center_ra_j2000_h;
    const centerDecDeg = solvedCameraFov.center_dec_j2000_deg;
    return {
      widthDeg: solvedCameraFov.width_deg,
      heightDeg: solvedCameraFov.height_deg,
      // Sensor geometry is useful before the first plate solve: draw the
      // real rectangular field north-up at the mount-reported position
      // instead of discarding its width/height and falling back to a dot.
      // A solve replaces this neutral pose with measured rotation/centre.
      rotationDeg: solvedCameraFov.rotation_deg ?? 0,
      ...(centerRaHours !== null && centerDecDeg !== null
        ? {
            centerRaDeg: centerRaHours * 15,
            centerDecDeg,
          }
        : {}),
    };
  }, [mountFovDeg, solvedCameraFov]);
  const [pointingNowMs, setPointingNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      setPointingNowMs(Date.now());
    }, 1000);
    return () => {
      clearInterval(timer);
    };
  }, []);
  const pointingIndicator = useMemo(
    () => buildMountPointingIndicator({ mount: mount ?? null, nowMs: pointingNowMs, fov: liveMountFov }),
    [mount, pointingNowMs, liveMountFov],
  );
  useEffect(() => {
    pointingShapesRef.current = pointingIndicator.shape === null ? [] : [pointingIndicator.shape];
    pushOverlay();
  }, [pointingIndicator, pushOverlay]);

  // -- target-field cutouts ("Target field" group, cutoutLayers.ts) --------

  const skymapState = useConnectionStore((s) => s.snapshot?.skymap ?? null);
  const cutoutSources = surveysQuery.data?.cutout_sources;
  const targetKey = target === null ? null : `${target.raDeg.toFixed(4)}/${target.decDeg.toFixed(4)}`;

  // The cached-cutouts-at-point lookup: run *before* offering "Fetch for
  // this field" (deliverable 1) so an offline viewer with a cached cutout
  // sees it without needing the network.
  const cachedCutoutsRequest =
    target === null ? null : { ra_deg: target.raDeg, dec_deg: target.decDeg, radius_deg: CUTOUT_POINT_RADIUS_DEG };
  const cachedCutoutsQuery = useCachedCutoutsQuery(cachedCutoutsRequest);

  // Which cutout id counts as "the one for this field", per source: the
  // cached lookup above is the base, overridden by whatever this session
  // has explicitly fetched — reset whenever the target itself changes.
  //
  // Reset during render, not in a `useEffect`: "adjusting state when a prop
  // changes" (react.dev) is the pattern React itself recommends for this —
  // setting state directly in the render body is a supported escape hatch
  // that skips the extra commit an effect-based reset would cause, and this
  // component's own `iframeSkyMapBackend` render-vs-effect ordering already
  // depends on effects running once per *stable* value, not once more per
  // reset.
  const [sessionCutoutIds, setSessionCutoutIds] = useState<Map<string, string>>(new Map());
  const [cutoutResetForKey, setCutoutResetForKey] = useState(targetKey);
  if (targetKey !== cutoutResetForKey) {
    setCutoutResetForKey(targetKey);
    setSessionCutoutIds(new Map());
  }
  const targetFovFittedSizeDeg = target?.fovFittedSizeDeg ?? null;
  const defaultCutoutSizeDeg =
    targetFovFittedSizeDeg === null
      ? DEFAULT_CUTOUT_SIZE_DEG
      : Math.min(MAX_CUTOUT_SIZE_DEG, Math.max(0.1, round(targetFovFittedSizeDeg, 2)));
  // unWISE's own, larger default — always `UNWISE_MAX_CUTOUT_SIZE_DEG`
  // (6 deg), never scaled by `targetFovFittedSizeDeg`: unlike every other
  // source, unWISE is a fixed-size context field, not a lens-fitted one.
  const unwiseCutoutSizeDeg = UNWISE_MAX_CUTOUT_SIZE_DEG;
  const cutoutSizeDegForSource = useCallback(
    (sourceId: string) => (sourceId === DEFAULT_CUTOUT_SOURCE_ID ? unwiseCutoutSizeDeg : defaultCutoutSizeDeg),
    [unwiseCutoutSizeDeg, defaultCutoutSizeDeg],
  );

  // `skymapState.cutouts` alone is not a complete job history (capped at 50
  // entries server-side) — merged with the uncapped point lookup so a job
  // that has aged out of the snapshot still resolves correctly
  // (`mergeLiveCutouts`'s own doc comment; reported live: SDSS for M31 stuck
  // on "Fetch (3.00°)" after a real, successful fetch).
  const mergedLiveCutouts = useMemo(
    () => mergeLiveCutouts(skymapState?.cutouts ?? [], cachedCutoutsQuery.data?.cutouts ?? []),
    [skymapState, cachedCutoutsQuery.data],
  );

  // Which live job counts as "the one for this field", per source — matched
  // against `mergedLiveCutouts` (the live snapshot, not a one-shot REST
  // query) so a source the server autofetched with no explicit request from
  // this session still resolves the moment it lands, not only if the point
  // lookup happens to refire afterwards. `findFieldCutoutIdBySource`'s own
  // doc comment has the bug this replaced (reported live: IC 1805, third
  // resolve in one session, a cutout complete on disk and
  // the panel still showing nothing).
  const targetPoint = useMemo(() => (target === null ? null : { raDeg: target.raDeg, decDeg: target.decDeg }), [target]);
  const fieldCutoutIdBySource = useMemo(() => {
    if (targetPoint === null || cutoutSources === undefined) return new Map<string, string>();
    return findFieldCutoutIdBySource({
      target: targetPoint,
      sourceIds: cutoutSources.map((s) => s.id),
      liveCutouts: mergedLiveCutouts,
      sessionCutoutIdBySource: sessionCutoutIds,
      defaultSizeDeg: cutoutSizeDegForSource,
      matchRadiusDeg: CUTOUT_POINT_RADIUS_DEG,
    });
  }, [targetPoint, cutoutSources, mergedLiveCutouts, sessionCutoutIds, cutoutSizeDegForSource]);

  const cutoutRows = useMemo(() => {
    if (cutoutSources === undefined) return null;
    return resolveCutoutRows({
      target: targetPoint,
      sources: cutoutSources,
      currentCutoutIdBySource: fieldCutoutIdBySource,
      liveCutouts: mergedLiveCutouts,
      upstreamReachable: skymapState?.upstream_reachable ?? null,
      defaultSizeDeg: cutoutSizeDegForSource,
    });
  }, [cutoutSources, targetPoint, fieldCutoutIdBySource, mergedLiveCutouts, skymapState, cutoutSizeDegForSource]);

  // **Automatic fetching is off** (owner, 2026-09-09: "disable automated 6
  // degrees cutoffs overlay for testing. we might not need it anymore if
  // composite works now"). Once the NSNS composite drew correctly — the
  // doubly-applied UV flip in `imageryBatch.ts` was what made it look
  // broken — a 6-degree per-target cutout stopped being obviously worth
  // its cost: it is a second imagery source drawn over the background in a
  // different survey's colours and stretch, and it was itself mistaken for
  // a tiling fault more than once today.
  //
  // Automatic target cutout fetching remains deliberately off. If it is
  // restored, use a guarded event-driven request: an effect would repeatedly
  // refetch an in-flight row while its state still reads as "not-fetched".

  // Pieces for every *ready and visible* cutout, positioned by their own WCS.
  const visibleReadyRows = useMemo(
    () => (cutoutRows ?? []).filter((r) => r.state.kind === "ready" && (prefs.cutouts[r.sourceId]?.visible ?? false)),
    [cutoutRows, prefs.cutouts],
  );
  const visibleReadyCutouts = useMemo(() => {
    const byId = new Map<string, string>();
    for (const row of visibleReadyRows) {
      byId.set((row.state as { kind: "ready"; cutoutId: string }).cutoutId, row.sourceId);
    }
    return [...byId].map(([cutoutId, sourceId]) => ({ cutoutId, sourceId }));
  }, [visibleReadyRows]);
  const visibleReadyIds = useMemo(
    () => visibleReadyCutouts.map((cutout) => cutout.cutoutId),
    [visibleReadyCutouts],
  );
  const cutoutManifestsQuery = useCutoutManifestsQuery(visibleReadyIds);

  useEffect(() => {
    const backend = backendRef.current;
    if (backend === null) return;
    const manifests = cutoutManifestsQuery.data;
    const pieces: CutoutLayerPiece[] = [];
    if (manifests !== undefined) {
      for (const row of visibleReadyCutouts) {
        const cutoutId = row.cutoutId;
        const manifest = manifests.get(cutoutId);
        if (manifest === undefined) continue;
        const opacity = prefs.cutouts[row.sourceId]?.opacity ?? CUTOUT_DEFAULT_OPACITY;
        for (const piece of manifest.pieces) {
          if (piece.wcs === null || piece.display_png_url === null || piece.width === null || piece.height === null) {
            continue;
          }
          pieces.push({
            id: `${cutoutId}:${String(piece.idx)}`,
            displayPngUrl: piece.display_png_url,
            wcs: piece.wcs,
            widthPx: piece.width,
            heightPx: piece.height,
            opacity,
            creditLabel: manifest.credit_label,
            creditUrl: manifest.credit_url,
          });
        }
      }
    }
    backend.setCutoutLayers(pieces);
  }, [cutoutManifestsQuery.data, visibleReadyCutouts, prefs.cutouts, frameAttachment]);

  const credits = useMemo<SurveyCredit[] | null>(() => {
    if (frameCredits === null) return null;
    const outlineSources = objects === undefined || objects.objects.length === 0 ? [] : objects.sources;
    // Only when the layer is actually visible and loaded — no credit for a
    // source contributing nothing on screen.
    const constellationSource =
      prefs.constellationBoundaries && boundariesQuery.data !== undefined ? [boundariesQuery.data.credit] : [];
    // Same rule again: only when stars are actually drawn on screen.
    const starSource = stars !== undefined && stars.stars.length > 0 ? [stars.credit] : [];
    // Same rule again: only when at least one body actually drew a shape —
    // never a hardcoded string (compliance requirement 3), and never
    // credited for a fetch that came back with nothing to show.
    const bodySource = bodies !== undefined && bodies.bodies.length > 0 ? [bodies.credit] : [];
    return [
      ...frameCredits,
      ...outlineSources.map((label) => ({ label })),
      ...constellationSource.map((label) => ({ label })),
      ...starSource.map((label) => ({ label })),
      ...bodySource.map((label) => ({ label })),
    ];
  }, [frameCredits, objects, prefs.constellationBoundaries, boundariesQuery.data, stars, bodies]);

  // Handed to `SkyMapCreditsFooter` (`Shell.tsx`) — the footer that replaced
  // this component's own in-viewport credit bar. `credits` (content and
  // sourcing) is unchanged; only where it renders moved.
  useEffect(() => {
    useSkyMapCreditsStore.getState().setCredits(credits);
  }, [credits]);

  // The meridian is drawn (`applyDisplay`'s `skyLineOverlay`) whenever the
  // toggle is on, whether or not it is inside the current view — a scrubbed
  // instant moves it, and a viewer who has panned away should not have to
  // guess where it went. `meridianHint` is that guess, made for them: `null`
  // when the meridian is either off or already on screen.
  const meridianLstDeg =
    prefs.skyLines.meridian && location !== null && instant !== null
      ? localMeanSiderealTimeHours(instant, location.longitude_deg) * 15
      : null;
  const meridianHint =
    meridianLstDeg === null || view === null || circularDistanceDeg(meridianLstDeg, view.ra) <= view.fovDeg / 2
      ? null
      : { text: `at RA ${formatRaHm(meridianLstDeg / 15)} — off-screen`, lstDeg: meridianLstDeg };
  const handleGotoMeridian = useCallback(() => {
    if (meridianLstDeg === null || view === null) return;
    backendRef.current?.goto(meridianLstDeg, view.dec, view.fovDeg);
  }, [meridianLstDeg, view]);

  return (
    <Card layoutId="sky-map" layoutLabel="Sky map" padded={false} className="relative h-full min-h-[65vh] overflow-hidden sm:min-h-80">
      <div ref={attachFrameHost} className="h-full min-h-[65vh] w-full sm:min-h-80" />
      {imageryLoading && (
        // `onImageryLoadingChange` — a real `<img>`-load signal
        // (`imageryLoadTracking.ts`), not `credits === null` (this
        // component's own first attempt): credits resolve as soon as a
        // layer is *selected*, near-instantly, well before any pixel
        // exists, so that version of this indicator never actually showed
        // during a real 10-20s unWISE fetch (reported live). `animate-pulse`
        // (Tailwind's built-in opacity fade), not a hard on/off blink:
        // readable as "still loading" without the flicker a literal blink
        // would put on screen the whole time imagery loads.
        <span
          aria-live="polite"
          className="pointer-events-none absolute bottom-10 right-2 animate-pulse text-[10px] text-fg-muted"
        >
          Loading sky survey imagery…
        </span>
      )}
      {digitalZoomFactor !== null && (
        // Quiet, not a warning (architecture policy invariant 7's own phrasing): a
        // real tile could not be had right now (offline, an upstream gap,
        // outside the survey's footprint) and the engine is stretching the
        // last one that loaded — real-time loading up to the survey's own
        // depth is unaffected, this only says what is on screen *right
        // now*. `--ssc-*` tokens only, so it reads correctly in both dark
        // and red-light themes (no `text-warning`/`text-danger`: those read
        // as "something is wrong", which this is not).
        <span
          aria-live="polite"
          className="pointer-events-none absolute bottom-16 right-2 text-[10px] text-fg-muted"
        >
          Digital zoom ×{digitalZoomFactor}
        </span>
      )}
      {mount !== null && mount !== undefined && (
        // Invariant 7, applied to the believed-pointing indicator: a viewer
        // must be able to see *why* nothing red is drawn (no frame
        // conversion available — `mountFrame.ts` — or a stale/slewing
        // reading), not just notice its absence. Neutral `text-fg-muted`,
        // matching the imagery-loading/digital-zoom indicators above, not a
        // `--ssc-pointing` token: this is status text, not the indicator
        // shape itself.
        <span aria-live="polite" className="pointer-events-none absolute bottom-22 right-2 text-[10px] text-fg-muted">
          {describeMountPointingStatus(pointingIndicator)}
        </span>
      )}
      {/* Next slot in the same bottom-right stack (`bottom-10/16/22` above)
       * — see `CutoutFetchIndicator.tsx`'s own doc comment for what it
       * shows and why it is indeterminate rather than a percentage. */}
      <CutoutFetchIndicator rows={cutoutRows} />
      {view !== null && (
        // Regression (reported live: FOV drag "disappeared" on both mouse
        // and touch, desktop and phone): this column used to be
        // `top-2 right-2 bottom-10`, pinning both edges to force a
        // *definite* height nearly as tall as the whole card, regardless of
        // how little of it the visible controls actually used. `LayersPanel`
        // is the one child with `flex-1`, so — closed or open — it stretched
        // to fill essentially all of that reserved height, and with nothing
        // in the chain set to `pointer-events-none`, the empty area below
        // its own trigger button silently absorbed every pointerdown over
        // that stretch of the map, including the FOV-drag hit test in
        // `wireFovDrag`, which never even saw the event. Reproduced by
        // posting a synthetic pointerdown into that empty stretch and
        // observing neither a drag nor the frame's own pan fire underneath.
        //
        // Fixed two ways, kept together:
        //  1. `max-h-[calc(100%-3rem)]` (a cap) instead of `bottom-10` (a
        //     forced height) — an absolutely positioned box with only `top`
        //     pinned sizes to its own content, so this column is now only as
        //     tall as its actually-visible controls. `LayersPanel`'s own
        //     `flex-1 min-h-0`/`max-h-full` dropdown still shrinks to fit
        //     when *open* (verified live: it caps against this same
        //     available space rather than overflowing the card), so nothing
        //     in `LayersPanel.tsx` itself needed to change (another agent's
        //     concurrent work, left untouched).
        //  2. `pointer-events-none` on the column, `pointer-events-auto`
        //     restored on each control — belt and braces against any
        //     residual slack (rounding, a future child added here), rather
        //     than relying on (1) alone.
        <div className="pointer-events-none absolute top-2 right-2 flex max-h-[calc(100%-3rem)] flex-col items-end gap-1">
          <ZoomButton
            label="Zoom in"
            onClick={() => {
              backendRef.current?.zoomBy(1 / ZOOM_STEP);
            }}
          >
            +
          </ZoomButton>
          <ZoomButton
            label="Zoom out"
            onClick={() => {
              backendRef.current?.zoomBy(ZOOM_STEP);
            }}
          >
            −
          </ZoomButton>
          {/* `contents`: no box of its own, so it cannot reintroduce the
           * same dead-space problem this fix is for — `LayersPanel`'s own
           * `flex min-h-0 flex-1` (its own doc comment references this same
           * column) sizes it exactly as if it sat directly in the column,
           * unchanged. `pointer-events-auto` is still a real, inherited CSS
           * value on this element even though it has no box (spec: a
           * `display: contents` element's descendants inherit from it as if
           * it existed), so it restores `auto` for the whole subtree without
           * touching `LayersPanel.tsx` itself (another agent's concurrent
           * work). */}
          <div className="pointer-events-auto contents">
            <LayersPanel
              surveys={surveys}
              surveysError={surveysQuery.isError ? describe(surveysQuery.error) : null}
              prefs={prefs}
              onChange={setPrefs}
              siteUnavailableReason={siteUnavailableReason}
              meridianHint={meridianHint}
              onGotoMeridian={handleGotoMeridian}
              cutoutRows={cutoutRows}
            />
          </div>
        </div>
      )}
      {clickedDesignation !== null && (
        <ObjectInfoPanel
          designation={clickedDesignation}
          onClose={() => {
            setClickedDesignation(null);
          }}
          onResolve={
            onResolveDesignation === undefined
              ? undefined
              : (designation) => {
                  onResolveDesignation(designation);
                  // Close on pick: the panel has done its job, and leaving
                  // it open covers the field the viewer just chose to frame.
                  setClickedDesignation(null);
                }
          }
        />
      )}
      {view !== null && (
        // The survey/catalogue credit line used to render in a bar here
        // too — moved to `SkyMapCreditsFooter` (`Shell.tsx`), outside this
        // card's own bounding box, so it stops sitting over the imagery on
        // a small screen (reported live). This bar now carries only the
        // view readout.
        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex flex-wrap items-center justify-end gap-x-3 gap-y-0.5 bg-surface/80 px-3 py-1.5 text-xs text-fg-muted">
          <ViewReadout view={view} site={location} instant={instant} />
        </div>
      )}
    </Card>
  );
}

/** The bottom-right view readout: where the centre of the map is pointing,
 * and how wide the field is.
 *
 * It used to read `10.56° / 69.79° · 60.00° FoV` — the centre coordinates
 * were already there, but unlabelled and both in degrees, so they read as
 * two anonymous numbers rather than a position (owner: "to the bottom line
 * of skymap where fov is displayed, also include fov center coordinates").
 *
 * Now labelled, with RA in hours as every catalogue and every other astronomy
 * tool writes it, and **alt/az alongside it** whenever a site and an instant
 * are known. On a horizon-based map "where am I looking" is an altitude and a
 * bearing far more often than it is an RA — but the equatorial pair is what
 * you type into a catalogue, so both belong here. Alt/az is omitted rather
 * than faked when the site or the instant is unknown (`None` means unknown,
 * never zero). */
function ViewReadout({
  view,
  site,
  instant,
}: {
  view: ViewState;
  site: { latitude_deg: number; longitude_deg: number } | null;
  instant: Date | null;
}) {
  const horizontal =
    site === null || instant === null
      ? null
      : raDecToAltAz(view.ra, view.dec, {
          latitudeDeg: site.latitude_deg,
          longitudeDeg: site.longitude_deg,
          instant,
        });
  const raHours = ((view.ra % 360) + 360) % 360 / 15;
  const h = Math.floor(raHours);
  const m = (raHours - h) * 60;
  const decSign = view.dec < 0 ? "−" : "+";
  return (
    <>
      <span className="tabular-nums">
        RA {h.toString().padStart(2, "0")}h{m.toFixed(1).padStart(4, "0")}m · Dec {decSign}
        {Math.abs(view.dec).toFixed(2)}°
      </span>
      {horizontal !== null && (
        <span className="tabular-nums">
          Alt {horizontal[0].toFixed(1)}° · Az {horizontal[1].toFixed(1)}°
        </span>
      )}
      <span className="tabular-nums">{view.fovDeg.toFixed(2)}° FoV</span>
    </>
  );
}

/** One button press changes the field of view by this factor — a wheel notch
 * on a mouse is 1.1, which is too fine for a thumb on a phone. */
const ZOOM_STEP = 1.6;

function ZoomButton({ label, onClick, children }: { label: string; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className="pointer-events-auto flex h-11 w-11 items-center justify-center rounded-md border border-border-strong bg-surface/80 text-lg leading-none text-fg backdrop-blur-sm hover:bg-surface-raised"
    >
      {children}
    </button>
  );
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : "survey registry request failed";
}

function round(value: number, places: number): number {
  const f = 10 ** places;
  return Math.round(value * f) / f;
}

/** "21h14m" — whole minutes, the resolution the meridian hint needs. */
function formatRaHm(raH: number): string {
  const totalMinutes = Math.round(((raH % 24) + 24) % 24 * 60);
  const h = Math.floor(totalMinutes / 60) % 24;
  const m = totalMinutes % 60;
  return `${String(h)}h${String(m).padStart(2, "0")}m`;
}

function toSkySurvey(survey: SurveyModel): SkySurvey {
  return {
    id: survey.id,
    label: survey.label,
    kind: survey.kind as SkySurvey["kind"],
    scheme: survey.scheme as SkySurvey["scheme"],
    frame: survey.frame as SkySurvey["frame"],
    tileUrlTemplate: survey.tile_url_template,
    tileFormat: survey.tile_format,
    maxOrder: survey.max_order,
    credit: survey.credit.url === "" ? { label: survey.credit.label } : { label: survey.credit.label, url: survey.credit.url },
    blendOnly: survey.blend_only,
    enabled: survey.enabled,
    reason: survey.reason,
    productionCleared: survey.production_cleared,
    rightsNotice: survey.rights_notice,
  };
}

/** The Planning tab's handle: the real backend, except that its
 * `setOverlay` contributes to a shared overlay rather than owning it. */
function composeBackend(
  backend: SkyMapBackend,
  setHostShapes: (shapes: FovShape[]) => void,
  mirrorCompletedFrameDrag: (raDeg: number, decDeg: number) => void,
): SkyMapBackend {
  return {
    goto: (ra, dec, fovDeg) => { backend.goto(ra, dec, fovDeg); },
    zoomBy: (factor) => { backend.zoomBy(factor); },
    onViewChange: (cb) => { backend.onViewChange(cb); },
    onFovMoved: (cb) => {
      backend.onFovMoved((ra, dec) => {
        mirrorCompletedFrameDrag(ra, dec);
        cb(ra, dec);
      });
    },
    onObjectClick: (cb) => { backend.onObjectClick(cb); },
    setOverlay: setHostShapes,
    addCatalogLayer: (layer) => { backend.addCatalogLayer(layer); },
    worldToScreen: (ra, dec) => backend.worldToScreen(ra, dec),
    screenToWorld: (x, y) => backend.screenToWorld(x, y),
    onCreditChange: (cb) => { backend.onCreditChange(cb); },
    onImageryLoadingChange: (cb) => { backend.onImageryLoadingChange(cb); },
    onDigitalZoomChange: (cb) => { backend.onDigitalZoomChange(cb); },
    setSurveys: (surveys) => { backend.setSurveys(surveys); },
    setLayers: (layers) => { backend.setLayers(layers); },
    setDisplay: (display) => { backend.setDisplay(display); },
    setCutoutLayers: (pieces) => { backend.setCutoutLayers(pieces); },
    dispose: () => { backend.dispose(); },
  };
}
