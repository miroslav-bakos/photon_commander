// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `SkyMapBackend` — exactly the interface in `DESIGN.md` §3.1. Every sky-map
 * backend (M11's own vector renderer today; Aladin and then WWT before it,
 * both removed — `THIRD_PARTY_NOTICES.md` §9) implements this and nothing more
 * leaks past it: no engine type appears in any signature here.
 *
 * The overlay content model (`FovShape`, `CatalogLayer`) is intentionally
 * minimal in this pass — `setOverlay`/`addCatalogLayer` exist and are wired
 * end-to-end (host -> iframe -> library call), but the actual renderer lands
 * once `astro-math`'s API publishes `PA(t)` and keep-region geometry. Nothing
 * here computes parallactic angle or keep-region — that would duplicate
 * `astro/` and drift.
 *
 * `FovShape`'s vocabulary is already shaped for `DESIGN.md` §3.2's **five**
 * things an alt-az session renders (commit `e758485`, landed mid-M4): solid
 * rectangle (`kind: "rect"`, role `"primary"`), ghost rectangle (`"rect"`,
 * role `"ghost"`), the **frame stack** (`kind: "frameStack"` — the
 * individual frames as captured across the session, *not* just their
 * outline, so overlapping coverage is visible), the swept envelope
 * (`"polygon"`/`"rect"`, role `"union"`), and the guaranteed-keep region
 * (role `"keep"`). EQ mode renders one `"rect"`, role `"primary"`, no ghost.
 * The frame stack must be built from the *same* adaptively-sampled `PA(t)`
 * set the time scrubber interpolates — never a second series — which is why
 * each `FrameStackSample` below carries `atSeconds`, an offset into that one
 * set, rather than owning its own timing.
 */

export interface ViewState {
  ra: number;
  dec: number;
  fovDeg: number;
}

/** A single flat colour/style bucket, resolved to a real CSS token by the
 * iframe content — never a literal colour crossing the postMessage boundary.
 * `"stack"` is the frame stack's own role, kept distinct from `"union"` (the
 * envelope) and `"keep"` (the intersection) because it answers a different
 * question (DESIGN.md §3.2) and needs its own translucency, not theirs. */
export type ShapeRole =
  | "primary"
  | "ghost"
  | "stack"
  | "union"
  | "keep"
  | "grid"
  | "warning"
  | "danger"
  /** Catalogue outlines (`catalogOutlines.ts`): the deep-sky objects in the
   * field, drawn from their catalogued size and position angle. */
  | "catalog"
  /** Reference lines drawn by the frame itself (celestial equator, local
   * meridian, zenith) — muted, so they sit under the FOV overlay. */
  | "skyline"
  /** The horizon's four cardinal letters, N/E/S/W (`skyLines.ts`'s
   * `horizonShapes`). Its own role rather than `"skyline"`: these answer
   * "which way am I facing" at a glance, so they carry their own colour and
   * their own label weight, and must not drift when reference-line styling
   * changes. Owner: "N S W and E on skymap should be bigger bold darker
   * red". */
  | "cardinal"
  /** The equatorial/alt-az coordinate grids (`skyLines.ts`'s
   * `equatorialGridShapes`/`altAzGridShapes`) — deliberately a *different*
   * role from `"grid"` (the mosaic panel outline, themed `--ssc-warning`):
   * a coordinate grid is background reference geometry drawn over the sky,
   * not a warning colour, and shares `--ssc-sky-ink` with `"catalog"`,
   * `"skyline"` and `"constellation"` (`tokens.css`'s own comment on that
   * token) rather than the mosaic panels' warning colour. */
  | "coordGrid"
  /** The IAU constellation-boundary regions (`constellationBoundaries.ts`)
   * — background reference geometry, like `"coordGrid"`, sharing the same
   * `--ssc-sky-ink` token. */
  | "constellation"
  /** Named bright stars (`brightStars.ts`): point markers, its own
   * `--` token — distinct from `"catalog"` (extended deep-sky
   * outlines) and `"constellation"` (reference lines), since a star is
   * neither. */
  | "star"
  /** The cap of sky that never rises from the observer's latitude
   * (`skyLines.ts`'s `unobservableCapShape`). Themed `--ssc-bg`, which is
   * deliberate and unlike every other role: this shape exists to *subtract*
   * light, washing the region toward the page background rather than
   * drawing a coloured mark on it, and it therefore stays correct in both
   * the dark and the red-light themes without a token of its own. */
  | "unobservable"
  /** The Sun (`solarSystem.ts`), drawn at its true apparent size from
   * `GET /api/astro/bodies`. Its own role and token — distinct from
   * `"star"` (a point marker for a catalogued fixed star) since the Sun is
   * drawn to scale as a disc, and distinct from `"moon"` since the two need
   * independent Layers-panel toggles. */
  | "sun"
  /** The Moon, same source and to-scale treatment as `"sun"` — its own
   * token so it is never mistaken for it (a bright disc dimmed differently
   * from the Sun would otherwise share a colour with no way to tell them
   * apart at a glance). */
  | "moon"
  /** Mercury..Neptune, one role for all of them (the Layers panel's single
   * "Planets" toggle covers every planet, not one per body): drawn as a
   * fixed-size marker, never to scale — every planet's real angular size is
   * at most a couple of arcseconds, which would be an invisible speck at any
   * of this map's FOVs, so `solarSystem.ts` ignores `apparent_diameter_arcmin`
   * for this role entirely and draws a marker sized to be found instead. */
  | "planet"
  /** Where the telescope actually points, from `snapshot.mount`
   * (`mountPointing.ts`, DESIGN.md §3.5.1) — its own role, distinct from
   * every FOV-planning role (`primary`/`ghost`/`stack`/`union`/`keep`) and
   * from `danger`/`cardinal` (both already red): this answers a different
   * question ("where is the scope right now") from all of them, and a
   * stale or slewing reading is carried on the shape itself
   * (`dashed`/`fillOpacity`), never by borrowing another role's colour. */
  | "pointing";

export interface ShapeStyle {
  role: ShapeRole;
  dashed?: boolean;
  /** Shorter dashes than `dashed`: the catalogue's "size only" glyph, for an
   * object whose orientation is unknown. Ignored when `dashed` is set. */
  dotted?: boolean;
  fillOpacity?: number;
}

/** One rectangle in the frame stack: the frame as captured at `atSeconds`
 * into the session, rotated to `positionAngleDeg`. `atSeconds` is an offset
 * into astro-math's adaptively-sampled `PA(t)` set (the same set the time
 * scrubber interpolates) — this type does not own or compute timing. */
export interface FrameStackSample {
  atSeconds: number;
  centerRa: number;
  centerDec: number;
  widthDeg: number;
  heightDeg: number;
  positionAngleDeg: number;
}

export type FovShape =
  | {
      kind: "rect";
      id: string;
      /** Rectangle centre, degrees. Rotation is applied about this centre in
       * world coordinates, then projected — never the reverse (skymap-viz.md). */
      centerRa: number;
      centerDec: number;
      widthDeg: number;
      heightDeg: number;
      positionAngleDeg: number;
      style: ShapeStyle;
      label?: string;
      labelAt?: [ra: number, dec: number];
    }
  | {
      kind: "circle";
      id: string;
      centerRa: number;
      centerDec: number;
      radiusDeg: number;
      style: ShapeStyle;
      label?: string;
      labelAt?: [ra: number, dec: number];
    }
  | {
      kind: "polygon";
      id: string;
      verticesRaDec: [ra: number, dec: number][];
      style: ShapeStyle;
      label?: string;
      /** Where the frame anchors `label`, in world coordinates — projected
       * by the engine's own projection at draw time, so the text stays on
       * the shape's edge as the view moves. Defaults to the first vertex. */
      labelAt?: [ra: number, dec: number];
    }
  | {
      /** An *open* path — no implied closing edge back to the first vertex,
       * unlike `polygon`. Exists for the altitude-limit ring and horizon
       * profile markers (`overlay.ts`'s `buildAltitudeLimitRingShape`/
       * `buildHorizonProfileMarkerShape`): a short bar tangent to a
       * constant-altitude surface reads as a line, not a filled shape, and
       * closing it into a degenerate two-point "polygon" drew a doubled
       * stroke back over itself in both backends. */
      kind: "polyline";
      id: string;
      verticesRaDec: [ra: number, dec: number][];
      style: ShapeStyle;
      label?: string;
      labelAt?: [ra: number, dec: number];
    }
  | {
      /** DESIGN.md §3.2 item 3, alt-az only: the individual captured frames
       * across the session, rendered so overlapping coverage reads as depth
       * (translucent fills that compound), not as N identical outlines. How
       * many samples land here is the presentation layer's bounded sampling
       * decision; exact coverage remains in the union/keep polygons. */
      kind: "frameStack";
      id: string;
      frames: FrameStackSample[];
      style: ShapeStyle;
    }
  | {
      kind: "grid";
      id: string;
      spacingDeg: number;
      style: ShapeStyle;
    };

/** Which position-angle model produced a `FovShape[]` — surfaced so the UI
 * can say plainly when the *planned* mode (chosen ahead of time, in
 * daylight) differs from the *device's* reported `mount.equ_mode`
 * (DESIGN.md §5), rather than silently following either one. Not consumed by
 * `SkyMapBackend` itself — it is state the caller building `FovShape[]`
 * needs, kept here so the type lands with the shapes it describes. */
export type MountMode = "altaz" | "eq";

export interface CatalogLayer {
  id: string;
  label: string;
  kind: "hips" | "votable";
  url: string;
  colorRole?: ShapeRole;
}

/** Attribution for one visible imagery layer, sourced from the survey
 * registry record at runtime — never a static string in JSX (skymap-viz.md,
 * DESIGN.md §3.1.2). The frame reports one per *visible* layer, in draw
 * order (THIRD_PARTY_NOTICES.md "Sky-survey layers", consequence 2). */
export interface SurveyCredit {
  label: string;
  url?: string;
}

/** A sky survey as `GET /api/skymap/surveys` describes it, reduced to what a
 * frame needs to draw it. The host maps the API model to this; the frame
 * never calls the API (it stays API-free behind `postMessage`). */
export interface SkySurvey {
  id: string;
  label: string;
  kind: "optical" | "infrared" | "halpha" | "oiii" | "sii";
  scheme: "hips" | "toast" | "mercator";
  /** Coordinate frame used by a HiPS pyramid's HEALPix indices. */
  frame: "equatorial" | "galactic";
  /** Same-origin tile template: `{level}`/`{x}`/`{y}` for TOAST and for Mercator
   * (Legacy Surveys' own XYZ scheme, a different pyramid from TOAST despite
   * sharing this placeholder shape — `mercatorTiles.ts`),
   * `{order}`/`{dir}`/`{npix}` for HiPS (`ssc.skymap.surveys.client_tile_template`). */
  tileUrlTemplate: string;
  tileFormat: string;
  maxOrder: number;
  credit: SurveyCredit;
  /** An overlay to blend over a background, never a background itself. */
  blendOnly: boolean;
  enabled: boolean;
  /** Why `enabled` is false; `null` when it is not. Shown, never hidden. */
  reason: string | null;
  /** False means the layer exists for evaluation only and must not ship. */
  productionCleared: boolean;
  rightsNotice: string | null;
}

/** One blend layer the user has switched on, with its opacity. */
export interface OverlayLayerSelection {
  surveyId: string;
  visible: boolean;
  /** 0-1. */
  opacity: number;
}

/** What the map draws from the survey registry: one background, any number
 * of blend overlays in draw order. `background: null` is an honest empty sky
 * (the frame says so in the credit line), never an engine default. */
export interface LayerSelection {
  background: string | null;
  /** 0..1, the viewer's own dimming of the background imagery. 1 means "draw
   * it exactly as before" and is the default; a frame must treat 1 as the
   * untouched path rather than a special case of blending, so nobody's map
   * changes appearance until they ask it to. */
  backgroundOpacity: number;
  overlays: OverlayLayerSelection[];
}

/** A linear (TAN, or `RA---SIN`/`DEC--SIN` — AllWISE's own `CTYPE`s — treated
 * as TAN: SIN and TAN agree to first order at the tangent point and diverge
 * from each other as roughly `(field radius)^3` in the small-field limit; at
 * the 1.5 deg default cutout size that is on the order of 1e-3 deg, real but
 * small even at the 3 deg cap, and not corrected since AllWISE cutouts this
 * size are always near their own centre) tangent-plane WCS as plain numbers,
 * mirroring `ssc.api.schemas.CutoutWcsModel` exactly — this is
 * `astro-math`'s own cutout-piece geometry, forwarded verbatim, never
 * recomputed from a FITS header the frame does not have. */
export interface CutoutWcs {
  crpix1: number;
  crpix2: number;
  crval1: number;
  crval2: number;
  cd1_1: number;
  cd1_2: number;
  cd2_1: number;
  cd2_2: number;
  ctype1: string;
  ctype2: string;
}

/** One target-field cutout piece to draw as an image layer, positioned by
 * its own WCS rather than `goto`/a `FovShape` — a cutout is real imagery,
 * not an outline. `id` is unique per piece (survey + band), so the frame can
 * diff additions/removals the same way `DiffedOverlay` does for shapes. */
export interface CutoutLayerPiece {
  id: string;
  displayPngUrl: string;
  wcs: CutoutWcs;
  widthPx: number;
  heightPx: number;
  /** 0-1. */
  opacity: number;
  creditLabel: string;
  creditUrl: string;
}

/** Reference lines and grids the frame draws for the planned site and
 * instant (`LayersPanel`'s "Sky lines"). `site`/`instant` feed the alt-az
 * grid and the meridian; when either is `null` the frame reports the
 * dependent lines unavailable rather than using the wall clock or 0/0. */
export interface DisplayOptions {
  /** Hold the view on a fixed RA/Dec as the instant changes, instead of
   * holding a fixed alt/az.
   *
   * Two different jobs for one view, and the owner named both. **Hunting**
   * for tonight's target, nothing resolved: the sky should turn past a
   * static horizon as the slider moves, so you can see what clears your own
   * horizon and when — that is `false`, and it is what the alt/az anchoring
   * below does. **Framing** a target you have resolved: the object must
   * stay put while the horizon and grid sweep around it, so you can watch
   * it rise, transit and set without it wandering off screen — that is
   * `true`.
   *
   * Implemented by simply *not* re-anchoring: the camera already lives in
   * RA/Dec, so leaving it alone is exactly "keep the object where it is".
   * Any panning the viewer has done is preserved rather than being yanked
   * back to centre on every tick. */
  holdOnTarget?: boolean;
  /** 0..1 multiplier on the frame's reference geometry (grids, equator,
   * meridian, ecliptic, horizon, cardinals, constellation boundaries) and on
   * catalogue outlines and stars respectively. Defaults to 0.5 when a host
   * does not send them, so an older host loses nothing. */
  skyLinesOpacity?: number;
  catalogueOpacity?: number;
  /** 0..1 on the mosaic panel grid. Defaults to 0.5 when unsent. */
  mosaicOpacity?: number;
  /** 0..1 on the Sun/Moon/planets group (`solarSystem.ts`), its own slider
   * rather than folded into `catalogueOpacity`: the Moon at full brightness
   * over a narrowband composite is exactly the kind of thing worth dimming
   * on its own, independent of the catalogue-outline/star glyphs
   * `catalogueOpacity` already governs. Defaults to 0.5 when unsent. */
  solarSystemOpacity?: number;
  equatorialGrid: boolean;
  altAzGrid: boolean;
  ecliptic: boolean;
  celestialEquator: boolean;
  meridian: boolean;
  /** The alt = 0 line by itself, dashed — the alt-az analogue of
   * `celestialEquator`: a dedicated single-circle toggle, independent of
   * `altAzGrid`, needing the same site + instant. */
  horizon: boolean;
  /** The user's altitude-limit ring across the whole sky
   * (`frames/shared/skyLines.ts`'s `altitudeLimitCircleShape`), always the
   * fixed danger/red token — `null` when no limit is configured. Unlike
   * `horizon`/`altAzGrid`/`meridian`, there is no separate on/off toggle:
   * an altitude limit is not optional chrome the way a grid is, so it draws
   * whenever it is known and `site`/`instant` are too, silently not drawn
   * otherwise (no error — a convenience overlay, not a required feature). */
  altitudeLimitDeg: number | null;
  site: { latitudeDeg: number; longitudeDeg: number } | null;
  /** UTC instant, ISO 8601 — the scrubber's current moment. */
  instant: string | null;
}

export interface SkyMapBackend {
  goto(ra: number, dec: number, fovDeg: number): void;
  /** Scale the field of view about its centre by `factor` (> 1 zooms out).
   * The frame applies it to its own live camera, so a button press never
   * depends on the host's possibly-stale copy of the view. */
  zoomBy(factor: number): void;
  onViewChange(cb: (v: ViewState) => void): void;
  /** Fired once, on release, when the user drags the "primary" FOV shape
   * (`frames/shared/fovDrag.ts`) to a new position directly on the map — the
   * frame does the live dragging itself and reports only the final centre,
   * never a message per pointer-move frame (skymap-viz.md's 16ms budget).
   * `ra`/`dec` are degrees, same convention as `goto`. */
  onFovMoved(cb: (ra: number, dec: number) => void): void;
  /** Fired when the user clicks a catalogue-object glyph — M11's
   * click-through info panel's entry point (`protocol.ts`'s `objectClick`
   * message). `designation` matches whatever `GET /api/astro/objects`
   * already gave the host for that shape. The vector frame implements this
   * with the shared hit-testing `frames/shared/objectPick.ts` provides, on
   * its own `click` listener (a `pointerdown` that misses the FOV shape's
   * polygon falls through to the frame's own pan handling; the object-click
   * check rides the trailing `click` event instead). */
  onObjectClick(cb: (designation: string) => void): void;
  setOverlay(shapes: FovShape[]): void;
  addCatalogLayer(layer: CatalogLayer): void;
  worldToScreen(ra: number, dec: number): [number, number];
  screenToWorld(x: number, y: number): [number, number];

  /** Not in the DESIGN.md §3.1 signature but required to satisfy compliance
   * requirement 4 (credit line driven by active-layer metadata, never a
   * hardcoded string) without smuggling a colour or a library type through
   * the six core methods. */
  onCreditChange(cb: (credits: SurveyCredit[]) => void): void;

  /** Whether any imagery tile or cutout piece is currently being fetched —
   * `protocol.ts`'s own doc comment on why this is a real image-load
   * signal, not `onCreditChange` repurposed. For a small "still loading"
   * indicator, not for anything that gates correctness. */
  onImageryLoadingChange(cb: (loading: boolean) => void): void;

  /** The background survey is showing a stretched ancestor tile rather than
   * the exact depth the view calls for, because a deeper tile genuinely
   * could not be had right now — `protocol.ts`'s own doc comment on
   * `digitalZoomChange`. `factor` is the approximate linear stretch (2, 4,
   * 8, …); `null` means "not currently overzoomed". WWT implemented this
   * before it was removed (`THIRD_PARTY_NOTICES.md` §9); the vector backend's
   * own imagery layer (`frames/vector/imageryLayer.ts`) never stretches a
   * tile to fill a gap — a tile it cannot have simply is not drawn — so it
   * never reports one here. */
  onDigitalZoomChange(cb: (factor: number | null) => void): void;

  /** The survey registry, as fetched by the host. Sent once per session
   * (and again if the registry changes); the frame constructs its own
   * imagesets from it and never contacts anything but these same-origin
   * tile URLs. */
  setSurveys(surveys: SkySurvey[]): void;
  /** Which of those surveys to draw, and how. */
  setLayers(layers: LayerSelection): void;
  /** Grids and reference lines. */
  setDisplay(display: DisplayOptions): void;
  /** Target-field cutout pieces to draw as WCS-positioned image layers —
   * distinct from `setOverlay`'s outlines and from `setLayers`' tile
   * pyramids, because a cutout is neither (`skymap-viz.md` deliverable 2).
   * The full set to show, every call: the frame reconciles by `id`. */
  setCutoutLayers(pieces: CutoutLayerPiece[]): void;

  /** Tears down the iframe and its listeners. React's effect cleanup calls
   * this on unmount; also what lets the conformance suite build a fresh
   * backend per test without leaking `window` listeners across tests. */
  dispose(): void;
}
