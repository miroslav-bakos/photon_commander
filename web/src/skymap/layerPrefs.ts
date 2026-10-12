// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The viewer's durable layer choices use `localStorage` as a synchronous UI
 * cache and are mirrored into backend client configuration by
 * `ClientConfigurationSync`. Pointer gestures and transient pan/zoom never
 * enter this object and remain frontend-only. Every access is wrapped — a
 * locked-down browser throws on `localStorage` and the map must still draw.
 *
 * The default is Legacy Surveys DR11 alone, outlines on, no grids or lines. A stored
 * background that the fetched registry no longer carries, or carries
 * disabled, is resolved at use (`SkyMap.tsx`), not here: this module does
 * not know the registry.
 */
import type { SurveyModel } from "../api/skymap";
import { DEFAULT_BACKGROUND_SURVEY_ID } from "./surveys";
import type { LayerSelection } from "./types";

export interface SkyLinePrefs {
  equatorialGrid: boolean;
  altAzGrid: boolean;
  ecliptic: boolean;
  celestialEquator: boolean;
  meridian: boolean;
  /** The alt = 0 line by itself — the alt-az grid's analogue of
   * `celestialEquator`, independent of `altAzGrid`. */
  horizon: boolean;
}

/** The "Solar system" group's three toggles (`solarSystem.ts`). Grouped
 * under one key, like `SkyLinePrefs`, rather than three top-level booleans —
 * it is one Layers-panel section and reads more clearly as one thing. */
export interface SolarSystemPrefs {
  sun: boolean;
  moon: boolean;
  planets: boolean;
}

export interface LayerPrefs {
  background: string | null;
  /** Explicit acknowledgements for remote imagery whose copyright holder has
   * not granted Photon Commander redistribution or commercial-use rights.
   * The image bytes are never bundled; this records only the viewer's
   * one-time personal-use acknowledgement for the named source. */
  restrictedImageryConsents: Record<string, boolean>;
  /** 0..1. Dims the background survey *without touching the overlay*, which
   * is the point: a blindingly bright field (unWISE along the galactic
   * plane) can drown the grid, outlines and labels, and the viewer is the
   * one who can see how much is too much. Defaults to 1, so an untouched
   * install behaves exactly as before. */
  backgroundOpacity: number;
  overlays: Record<string, { visible: boolean; opacity: number }>;
  catalogOutlines: boolean;
  /** The 88 IAU constellation-boundary regions (`constellationBoundaries.ts`)
   * — off by default, like `skyLines` below: background reference geometry
   * drawn over imagery, which the M11 design notes are wary of visually
   * competing with (unlike `catalogOutlines`, which *is* the imaging
   * target data a Planning-tab viewer came here for, and defaults on). */
  constellationBoundaries: boolean;
  /** Whether the altitude-limit ring (`buildAltitudeLimitRingShape`) is
   * drawn on the map — off by default: the owner's own ask was "don't show
   * horizon + alt limit line if horizon / alt limit not set", i.e. the ring
   * must not appear until the viewer explicitly turns it on. This governs
   * *only* whether the ring is drawn; the limit's actual value (Planning's
   * own "Altitude limit" field, `planningUiStore`'s `altitudeLimitDeg`) is
   * unaffected and keeps applying to visibility warnings and the
   * night-altitude chart regardless of this toggle — `SkyMap.tsx` passes
   * `null` for `DisplayOptions.altitudeLimitDeg` while this is off, rather
   * than changing the limit itself. */
  altitudeLimitRing: boolean;
  /** Named bright stars (`brightStars.ts`) — on by default, unlike
   * `constellationBoundaries`: those are reference geometry the design
   * notes are wary of visually competing with imagery; named stars are the
   * basic missing content a Planning-tab viewer explicitly wants (star
   * names), the same "on by default" case `catalogOutlines` already is. */
  brightStars: boolean;
  skyLines: SkyLinePrefs;
  /** The Sun, Moon and planets (`solarSystem.ts`) — on by default, like
   * `catalogOutlines`/`brightStars`: basic content a Planning-tab viewer
   * wants, not reference geometry the design notes are wary of competing
   * with imagery. */
  solarSystem: SolarSystemPrefs;
  /** How strongly the frame's reference geometry is drawn — the grids,
   * equator, meridian, ecliptic, horizon, cardinals and constellation
   * boundaries. 0 hides them, 1 is full strength.
   *
   * Wanted because chrome that is useful over a black sky is loud over a
   * bright narrowband composite, and the honest answer to "how much" is a
   * judgement only the person looking can make (owner: "add opacity sliders
   * for all sky lines and catalogue items"). The altitude-limit ring is
   * deliberately *not* dimmed by this: it marks sky the mount will refuse,
   * which is a safety fact rather than decoration. */
  skyLinesOpacity: number;
  /** The same, for catalogue outlines and named bright stars. */
  catalogueOpacity: number;
  /** The same, for the Sun, Moon and planets — its own slider rather than
   * folded into `catalogueOpacity`: the Moon at full brightness over a
   * narrowband composite is exactly the kind of thing worth dimming on its
   * own (`types.ts`'s `DisplayOptions.solarSystemOpacity` doc comment). */
  solarSystemOpacity: number;
  /** How strongly the mosaic panel grid is drawn — the yellow rectangles a
   * multi-panel plan lays over the target.
   *
   * Defaults to 0.5 rather than 1 (owner: "make mosaic yellow fill half the
   * opacity as it's now - even better - add slider for it"). The fill was
   * given a deliberate floor once because thin panels vanished over a dense
   * star field (`roleStyle.ts`'s `ROLE_MIN_FILL_OPACITY`); over a bright
   * narrowband composite the same floor is too much. This scales the whole
   * thing, outline included, so the panels stay findable without covering
   * the object they frame. */
  mosaicOpacity: number;
  /** The "Target field" group's per-cutout-source choice — keyed by
   * `CutoutSourceModel.id`, same shape as `overlays` above. Which *cutout*
   * (which job id, for which field) is not stored here: that is
   * session-only bookkeeping (`SkyMap.tsx`), not a viewer preference. */
  cutouts: Record<string, { visible: boolean; opacity: number }>;
}

export const STORAGE_KEY = "ssc.skymap.layers.v1";
/** Initial opacity for new cutout layers, independent of saved preferences. */
export const CUTOUT_DEFAULT_OPACITY = 0.5;

export const DEFAULT_LAYER_PREFS: LayerPrefs = {
  background: DEFAULT_BACKGROUND_SURVEY_ID,
  restrictedImageryConsents: {},
  backgroundOpacity: 0.5,
  overlays: {},
  catalogOutlines: true,
  constellationBoundaries: false,
  altitudeLimitRing: false,
  brightStars: true,
  skyLines: { equatorialGrid: false, altAzGrid: false, ecliptic: false, celestialEquator: false, meridian: false, horizon: false },
  solarSystem: { sun: true, moon: true, planets: true },
  skyLinesOpacity: 0.5,
  catalogueOpacity: 0.5,
  solarSystemOpacity: 0.5,
  mosaicOpacity: 0.5,
  cutouts: {},
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** A stored opacity, clamped, falling back when absent or nonsense — the
 * same tolerance every other stored preference here uses. */
function opacityOr(raw: unknown, fallback: number): number {
  return typeof raw === "number" && Number.isFinite(raw) ? Math.min(1, Math.max(0, raw)) : fallback;
}

/** Shared by `overlays` and `cutouts`: both are "id -> visible + opacity". */
function coerceOpacityRecord(
  raw: unknown,
  defaultOpacity: number,
): Record<string, { visible: boolean; opacity: number }> {
  const out: Record<string, { visible: boolean; opacity: number }> = {};
  if (!isRecord(raw)) return out;
  for (const [id, entry] of Object.entries(raw)) {
    if (!isRecord(entry)) continue;
    const visible = typeof entry.visible === "boolean" ? entry.visible : false;
    const opacity =
      typeof entry.opacity === "number" && entry.opacity >= 0 && entry.opacity <= 1 ? entry.opacity : defaultOpacity;
    out[id] = { visible, opacity };
  }
  return out;
}

/** Shape-checked, field by field: a stale or hand-edited entry falls back
 * to the default for that field alone. */
function coerce(raw: unknown): LayerPrefs {
  if (!isRecord(raw)) return DEFAULT_LAYER_PREFS;
  const lines = isRecord(raw.skyLines) ? raw.skyLines : {};
  const solarSystem = isRecord(raw.solarSystem) ? raw.solarSystem : {};
  const bool = (value: unknown, fallback: boolean) => (typeof value === "boolean" ? value : fallback);
  return {
    background:
      typeof raw.background === "string" || raw.background === null ? raw.background : DEFAULT_LAYER_PREFS.background,
    restrictedImageryConsents: Object.fromEntries(
      Object.entries(isRecord(raw.restrictedImageryConsents) ? raw.restrictedImageryConsents : {})
        .filter((entry): entry is [string, boolean] => entry[1] === true),
    ),
    skyLinesOpacity: opacityOr(raw.skyLinesOpacity, 0.5),
    catalogueOpacity: opacityOr(raw.catalogueOpacity, 0.5),
    solarSystemOpacity: opacityOr(raw.solarSystemOpacity, 0.5),
    mosaicOpacity: opacityOr(raw.mosaicOpacity, 0.5),
    backgroundOpacity:
      typeof raw.backgroundOpacity === "number" && Number.isFinite(raw.backgroundOpacity)
        ? Math.min(1, Math.max(0, raw.backgroundOpacity))
        : DEFAULT_LAYER_PREFS.backgroundOpacity,
    overlays: coerceOpacityRecord(raw.overlays, 0.5),
    catalogOutlines: bool(raw.catalogOutlines, DEFAULT_LAYER_PREFS.catalogOutlines),
    constellationBoundaries: bool(raw.constellationBoundaries, DEFAULT_LAYER_PREFS.constellationBoundaries),
    altitudeLimitRing: bool(raw.altitudeLimitRing, DEFAULT_LAYER_PREFS.altitudeLimitRing),
    brightStars: bool(raw.brightStars, DEFAULT_LAYER_PREFS.brightStars),
    skyLines: {
      equatorialGrid: bool(lines.equatorialGrid, false),
      altAzGrid: bool(lines.altAzGrid, false),
      ecliptic: bool(lines.ecliptic, false),
      celestialEquator: bool(lines.celestialEquator, false),
      meridian: bool(lines.meridian, false),
      horizon: bool(lines.horizon, false),
    },
    solarSystem: {
      sun: bool(solarSystem.sun, true),
      moon: bool(solarSystem.moon, true),
      planets: bool(solarSystem.planets, true),
    },
    cutouts: coerceOpacityRecord(raw.cutouts, CUTOUT_DEFAULT_OPACITY),
    // A stored `mapKind` from before the Imagery/Vector toggle was removed
    // (`"wwt"` or `"vector"`) is simply not read here any more — `coerce`
    // only copies fields `LayerPrefs` still has, so it neither throws nor
    // resolves to a dead code path; the one remaining frame is the only
    // outcome regardless of what an old install has saved.
  };
}

export function loadLayerPrefs(storage: Pick<Storage, "getItem"> | null = safeStorage()): LayerPrefs {
  if (storage === null) return DEFAULT_LAYER_PREFS;
  try {
    const text = storage.getItem(STORAGE_KEY);
    if (text === null) return DEFAULT_LAYER_PREFS;
    return coerce(JSON.parse(text));
  } catch {
    return DEFAULT_LAYER_PREFS;
  }
}

export function saveLayerPrefs(prefs: LayerPrefs, storage: Pick<Storage, "setItem"> | null = safeStorage()): void {
  if (storage === null) return;
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Quota, private mode, a policy that disables storage: the choice
    // still applies for this session; it just will not be remembered.
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/**
 * The stored choice against the live registry. A stored background the
 * registry no longer offers (or offers disabled) is replaced by the default
 * when *that* is available, else by none — the frame then reports an empty
 * sky, which is honest; it never picks an engine default of its own.
 */
export function resolveLayerSelection(prefs: LayerPrefs, surveys: readonly SurveyModel[]): LayerSelection {
  const usable = (id: string | null) => {
    if (id === null) return false;
    const survey = surveys.find((candidate) => candidate.id === id);
    if (survey === undefined || !survey.enabled || survey.blend_only) return false;
    if (!survey.production_cleared && prefs.restrictedImageryConsents[id] !== true) return false;
    return true;
  };
  const background = prefs.background === null ? null : usable(prefs.background) ? prefs.background : usable(DEFAULT_BACKGROUND_SURVEY_ID) ? DEFAULT_BACKGROUND_SURVEY_ID : null;
  const overlays = surveys
    .filter((s) => s.blend_only && s.enabled)
    .map((s) => {
      const entry = prefs.overlays[s.id];
      return { surveyId: s.id, visible: entry?.visible ?? false, opacity: entry?.opacity ?? 0.5 };
    });
  return { background, backgroundOpacity: prefs.backgroundOpacity, overlays };
}
