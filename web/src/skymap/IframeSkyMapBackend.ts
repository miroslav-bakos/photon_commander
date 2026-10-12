// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Host-side half of the iframe + `postMessage` bridge (`DESIGN.md` §3.1.1,
 * `THIRD_PARTY_NOTICES.md` #2). This is the *only* class that talks to the
 * iframe's `contentWindow`, and it only ever sends/receives
 * `skymap/protocol.ts` messages — no engine type appears here.
 *
 * Used by the vector adapter (`VectorSkyMapBackend.ts`) and by any future
 * one, because the protocol is identical regardless of what a given frame
 * document draws with; what differs is entirely inside each
 * `frames/<backend>/main.ts`. WWT's own adapter (`WwtSkyMapBackend.ts`)
 * shared this same class before it was removed (`THIRD_PARTY_NOTICES.md` §9) —
 * this is why every backend can share one conformance suite: the suite
 * exercises this class.
 */
import type {
  CatalogLayer,
  CutoutLayerPiece,
  DisplayOptions,
  FovShape,
  LayerSelection,
  SkyMapBackend,
  SkySurvey,
  SurveyCredit,
  ViewState,
} from "./types";
import {
  type FrameToHostMessage,
  type HostToFrameMessage,
  isFrameToHostMessage,
} from "./protocol";
import { type Viewport, screenToWorld, worldToScreen } from "./projection";

/** The minimal surface this class needs from an `<iframe>` — deliberately
 * narrower than `HTMLIFrameElement` so tests can hand it a fake without
 * touching the DOM (see `src/test/fakeSkyMapFrame.ts`). */
export interface SkyMapFrameHandle {
  contentWindow: Pick<Window, "postMessage"> | null;
}

export interface IframeSkyMapBackendOptions {
  frame: SkyMapFrameHandle;
  /** The same iframe document may be detached and reattached across route
   * switches. In that case it already posted its one `ready` message before
   * this host adapter was constructed, so the adapter must send immediately
   * rather than queueing forever for a second ready that will never arrive. */
  initialReady?: boolean;
  /** Recover a ready message sent before this adapter subscribed. */
  requestReady?: boolean;
  /** `postMessage` target origin. Same-origin in production (the frame HTML
   * is served by this same Vite app); injectable for tests. */
  targetOrigin?: string;
  /** Where to listen for the frame's replies. Defaults to `window`; injectable
   * for tests so nothing here depends on a real global. */
  messageSource?: Pick<Window, "addEventListener" | "removeEventListener">;
}

export class IframeSkyMapBackend implements SkyMapBackend {
  private readonly frame: SkyMapFrameHandle;
  private readonly targetOrigin: string;
  private readonly messageSource: Pick<
    Window,
    "addEventListener" | "removeEventListener"
  >;
  private readonly listener: (event: MessageEvent) => void;

  private ready: boolean;
  private queue: HostToFrameMessage[] = [];
  private viewChangeCbs = new Set<(v: ViewState) => void>();
  private creditCbs = new Set<(c: SurveyCredit[]) => void>();
  private imageryLoadingCbs = new Set<(loading: boolean) => void>();
  private digitalZoomCbs = new Set<(factor: number | null) => void>();
  private fovMovedCbs = new Set<(ra: number, dec: number) => void>();
  private objectClickCbs = new Set<(designation: string) => void>();
  private viewport: Viewport | null = null;

  constructor(options: IframeSkyMapBackendOptions) {
    this.frame = options.frame;
    this.targetOrigin = options.targetOrigin ?? window.location.origin;
    this.messageSource = options.messageSource ?? window;
    this.ready = options.initialReady ?? false;

    this.listener = (event: MessageEvent) => {
      if (!isFrameToHostMessage(event.data)) return;
      this.handleFrameMessage(event.data);
    };
    this.messageSource.addEventListener(
      "message",
      this.listener as EventListener,
    );
    if (options.requestReady) {
      this.frame.contentWindow?.postMessage({ channel: "ssc-skymap/v1", type: "requestReady" }, this.targetOrigin);
    }
  }

  private handleFrameMessage(message: FrameToHostMessage): void {
    switch (message.type) {
      case "ready": {
        this.viewport = {
          view: message.view,
          widthPx: message.viewportPx.w,
          heightPx: message.viewportPx.h,
        };
        this.ready = true;
        for (const cb of this.creditCbs) cb(message.credits);
        for (const cb of this.viewChangeCbs) cb(message.view);
        this.flushQueue();
        return;
      }
      case "viewChange": {
        if (this.viewport !== null)
          this.viewport = { ...this.viewport, view: message.view };
        for (const cb of this.viewChangeCbs) cb(message.view);
        return;
      }
      case "creditChange": {
        for (const cb of this.creditCbs) cb(message.credits);
        return;
      }
      case "imageryLoadingChange": {
        for (const cb of this.imageryLoadingCbs) cb(message.loading);
        return;
      }
      case "digitalZoomChange": {
        for (const cb of this.digitalZoomCbs) cb(message.factor);
        return;
      }
      case "viewportResize": {
        if (this.viewport !== null) {
          this.viewport = {
            ...this.viewport,
            widthPx: message.viewportPx.w,
            heightPx: message.viewportPx.h,
          };
        }
        return;
      }
      case "fovMoved": {
        for (const cb of this.fovMovedCbs) cb(message.ra, message.dec);
        return;
      }
      case "objectClick": {
        for (const cb of this.objectClickCbs) cb(message.designation);
        return;
      }
      case "error": {
        // Surfaced by the caller: this class does not own UI. Logged here so
        // a swallowed frame-side failure is never silent (architecture policy invariant 7).
        console.error(`[skymap] frame error: ${message.message}`);
        return;
      }
    }
  }

  private send(message: HostToFrameMessage): void {
    if (!this.ready) {
      this.queue.push(message);
      return;
    }
    this.frame.contentWindow?.postMessage(message, this.targetOrigin);
  }

  private flushQueue(): void {
    const pending = this.queue;
    this.queue = [];
    for (const message of pending) {
      this.frame.contentWindow?.postMessage(message, this.targetOrigin);
    }
  }

  goto(ra: number, dec: number, fovDeg: number): void {
    this.send({ channel: "ssc-skymap/v1", type: "goto", ra, dec, fovDeg });
  }

  zoomBy(factor: number): void {
    this.send({ channel: "ssc-skymap/v1", type: "zoomBy", factor });
  }

  onViewChange(cb: (v: ViewState) => void): void {
    this.viewChangeCbs.add(cb);
  }

  onCreditChange(cb: (credits: SurveyCredit[]) => void): void {
    this.creditCbs.add(cb);
  }

  onImageryLoadingChange(cb: (loading: boolean) => void): void {
    this.imageryLoadingCbs.add(cb);
  }

  onDigitalZoomChange(cb: (factor: number | null) => void): void {
    this.digitalZoomCbs.add(cb);
  }

  onFovMoved(cb: (ra: number, dec: number) => void): void {
    this.fovMovedCbs.add(cb);
  }

  onObjectClick(cb: (designation: string) => void): void {
    this.objectClickCbs.add(cb);
  }

  setSurveys(surveys: SkySurvey[]): void {
    this.send({ channel: "ssc-skymap/v1", type: "setSurveys", surveys });
  }

  setLayers(layers: LayerSelection): void {
    this.send({ channel: "ssc-skymap/v1", type: "setLayers", layers });
  }

  setDisplay(display: DisplayOptions): void {
    this.send({ channel: "ssc-skymap/v1", type: "setDisplay", display });
  }

  setCutoutLayers(pieces: CutoutLayerPiece[]): void {
    this.send({ channel: "ssc-skymap/v1", type: "setCutoutLayers", pieces });
  }

  setOverlay(shapes: FovShape[]): void {
    this.send({ channel: "ssc-skymap/v1", type: "setOverlay", shapes });
  }

  addCatalogLayer(layer: CatalogLayer): void {
    this.send({ channel: "ssc-skymap/v1", type: "addCatalogLayer", layer });
  }

  worldToScreen(ra: number, dec: number): [number, number] {
    if (this.viewport === null) return [NaN, NaN];
    return worldToScreen(this.viewport, ra, dec);
  }

  screenToWorld(x: number, y: number): [number, number] {
    if (this.viewport === null) return [NaN, NaN];
    return screenToWorld(this.viewport, x, y);
  }

  dispose(): void {
    this.messageSource.removeEventListener(
      "message",
      this.listener as EventListener,
    );
    this.viewChangeCbs.clear();
    this.creditCbs.clear();
    this.imageryLoadingCbs.clear();
    this.fovMovedCbs.clear();
    this.objectClickCbs.clear();
    this.queue = [];
  }
}
