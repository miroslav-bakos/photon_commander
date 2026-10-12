// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The wire format of the one coupling allowed between the app and a sky-map
 * iframe: `postMessage`. Both `IframeSkyMapBackend` (host side) and
 * each `frames/<backend>/main.ts` (iframe side) import only this file to agree on
 * shapes — neither side imports the other's library types.
 *
 * Every message carries `channel` so the listener can ignore unrelated
 * `postMessage` traffic (React devtools, browser extensions, a future
 * second iframe) without guessing from shape alone.
 */
import type {
  CatalogLayer,
  CutoutLayerPiece,
  DisplayOptions,
  FovShape,
  LayerSelection,
  SkySurvey,
  SurveyCredit,
  ViewState,
} from "./types";

export const SKYMAP_CHANNEL = "ssc-skymap/v1";

export type HostToFrameMessage =
  | { channel: typeof SKYMAP_CHANNEL; type: "requestReady" }
  | {
      channel: typeof SKYMAP_CHANNEL;
      type: "goto";
      ra: number;
      dec: number;
      fovDeg: number;
    }
  | { channel: typeof SKYMAP_CHANNEL; type: "zoomBy"; factor: number }
  | { channel: typeof SKYMAP_CHANNEL; type: "setOverlay"; shapes: FovShape[] }
  | {
      channel: typeof SKYMAP_CHANNEL;
      type: "addCatalogLayer";
      layer: CatalogLayer;
    }
  | { channel: typeof SKYMAP_CHANNEL; type: "setSurveys"; surveys: SkySurvey[] }
  | { channel: typeof SKYMAP_CHANNEL; type: "setLayers"; layers: LayerSelection }
  | { channel: typeof SKYMAP_CHANNEL; type: "setDisplay"; display: DisplayOptions }
  | {
      channel: typeof SKYMAP_CHANNEL;
      type: "setCutoutLayers";
      pieces: CutoutLayerPiece[];
    };

export type FrameToHostMessage =
  | {
      channel: typeof SKYMAP_CHANNEL;
      type: "ready";
      view: ViewState;
      credits: SurveyCredit[];
      viewportPx: { w: number; h: number };
    }
  | { channel: typeof SKYMAP_CHANNEL; type: "viewChange"; view: ViewState }
  | {
      /** One credit per visible imagery layer, in draw order. */
      channel: typeof SKYMAP_CHANNEL;
      type: "creditChange";
      credits: SurveyCredit[];
    }
  | {
      /** Whether the frame currently has any imagery tile or cutout piece
       * still being fetched — a real, `<img>`-load-observed signal, not a
       * proxy for "the currently selected layer's credit metadata has
       * arrived" (which `creditChange` reports near-instantly, well before
       * any pixel exists — reported live: a "loading" indicator keyed on
       * `creditChange` never actually showed while unWISE spent 10-20s
       * fetching a real field). Debounced on the frame side so panning
       * across many small tiles does not flicker true/false every few ms. */
      channel: typeof SKYMAP_CHANNEL;
      type: "imageryLoadingChange";
      loading: boolean;
    }
  | {
      channel: typeof SKYMAP_CHANNEL;
      type: "viewportResize";
      viewportPx: { w: number; h: number };
    }
  | {
      /** The background survey is genuinely stretching an ancestor tile
       * into the current view rather than the exact depth requested — real
       * detail was requested (this never caps how deep the engine may
       * subdivide, architecture policy invariant 7) but could not be had right now
       * (offline, an upstream gap, outside the survey's footprint), so the
       * frame is honest about it (`frames/shared/digitalZoomTracking.ts`).
       * `factor` is the approximate linear stretch (2, 4, 8, …); `null`
       * clears the indicator. Never sent merely because the view is deeper
       * than some cached pack — only when a tile request actually failed. */
      channel: typeof SKYMAP_CHANNEL;
      type: "digitalZoomChange";
      factor: number | null;
    }
  | {
      /** The user dragged the "primary" FOV shape (`frames/shared/fovDrag.ts`)
       * to a new position and released it — sent once, on release, never per
       * pointer-move frame. `ra`/`dec` are the shape's new centre, degrees,
       * same convention as `goto`. Frame -> host only: there is no reverse
       * message, since the host already has `goto` for repositioning the
       * view from its own side. */
      channel: typeof SKYMAP_CHANNEL;
      type: "fovMoved";
      ra: number;
      dec: number;
    }
  | {
      /** The user clicked a catalogue-object glyph
       * (`frames/shared/objectPick.ts`'s `pickCatalogObjectId`) — M11's
       * click-through info panel's own entry point. `designation` is
       * exactly `catalogDesignationFromShapeId`'s result, the same id
       * `GET /api/astro/objects` already gave the host for this shape,
       * never a new id scheme. Frame -> host only: the host owns fetching
       * `/api/astro/objects/{designation}/info` and rendering the panel, so
       * there is nothing for the reverse direction to say. */
      channel: typeof SKYMAP_CHANNEL;
      type: "objectClick";
      designation: string;
    }
  | { channel: typeof SKYMAP_CHANNEL; type: "error"; message: string };

export function isFrameToHostMessage(
  data: unknown,
): data is FrameToHostMessage {
  return (
    typeof data === "object" &&
    data !== null &&
    "channel" in data &&
    data.channel === SKYMAP_CHANNEL
  );
}

export function isHostToFrameMessage(
  data: unknown,
): data is HostToFrameMessage {
  return (
    typeof data === "object" &&
    data !== null &&
    "channel" in data &&
    data.channel === SKYMAP_CHANNEL
  );
}
