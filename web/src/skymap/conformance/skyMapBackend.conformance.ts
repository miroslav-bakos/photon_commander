// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The one adapter conformance suite, run against every `SkyMapBackend`
 * (skymap-viz.md: "every implementation must pass the same adapter
 * conformance test suite, so a fallback stays real rather than theoretical").
 * Aladin and then WWT each had their own call into
 * `describeSkyMapBackendConformance` before being removed in turn
 * (`THIRD_PARTY_NOTICES.md` §9); `VectorSkyMapBackend.test.ts` is today's only
 * caller — write it once, run it against whatever backends exist.
 */
import { describe, expect, it } from "vitest";
import { FakeSkyMapFrame, readySignal } from "../../test/fakeSkyMapFrame";
import { SKYMAP_CHANNEL } from "../protocol";
import type { SkyMapBackend } from "../types";

export interface ConformanceHarness {
  fake: FakeSkyMapFrame;
  backend: SkyMapBackend;
}

export function describeSkyMapBackendConformance(
  name: string,
  makeHarness: () => ConformanceHarness,
): void {
  describe(`SkyMapBackend conformance: ${name}`, () => {
    it("queues goto() until the frame signals ready, then flushes it", () => {
      const { fake, backend } = makeHarness();
      backend.goto(10, 20, 3);
      expect(fake.sentToFrame).toHaveLength(0);

      fake.simulateFrameMessage(readySignal());
      expect(fake.sentToFrame).toEqual([
        { channel: SKYMAP_CHANNEL, type: "goto", ra: 10, dec: 20, fovDeg: 3 },
      ]);
    });

    it("sends goto() immediately once ready", () => {
      const { fake, backend } = makeHarness();
      fake.simulateFrameMessage(readySignal());
      backend.goto(180, -45, 1.5);
      expect(fake.sentToFrame).toEqual([
        {
          channel: SKYMAP_CHANNEL,
          type: "goto",
          ra: 180,
          dec: -45,
          fovDeg: 1.5,
        },
      ]);
    });

    it("sends zoomBy() as a factor for the frame to apply to its own live camera", () => {
      const { fake, backend } = makeHarness();
      fake.simulateFrameMessage(readySignal());
      backend.zoomBy(1.6);
      expect(fake.sentToFrame).toEqual([
        { channel: SKYMAP_CHANNEL, type: "zoomBy", factor: 1.6 },
      ]);
    });

    it("delivers onViewChange callbacks for both the ready view and later viewChange messages", () => {
      const { fake, backend } = makeHarness();
      const seen: { ra: number; dec: number; fovDeg: number }[] = [];
      backend.onViewChange((v) => seen.push(v));

      fake.simulateFrameMessage(
        readySignal({ view: { ra: 1, dec: 2, fovDeg: 3 } }),
      );
      fake.simulateFrameMessage({
        channel: SKYMAP_CHANNEL,
        type: "viewChange",
        view: { ra: 4, dec: 5, fovDeg: 6 },
      });

      expect(seen).toEqual([
        { ra: 1, dec: 2, fovDeg: 3 },
        { ra: 4, dec: 5, fovDeg: 6 },
      ]);
    });

    it("delivers onCreditChange for the ready credits and later creditChange messages — one per visible layer, in draw order, never a hardcoded string", () => {
      const { fake, backend } = makeHarness();
      const seen: { label: string; url?: string }[][] = [];
      backend.onCreditChange((c) => seen.push(c));

      fake.simulateFrameMessage(
        readySignal({ credits: [{ label: "Pan-STARRS DR1" }] }),
      );
      fake.simulateFrameMessage({
        channel: SKYMAP_CHANNEL,
        type: "creditChange",
        credits: [
          { label: "Pan-STARRS DR1" },
          { label: "WHAM", url: "https://www.astro.wisc.edu/wham/" },
        ],
      });

      expect(seen).toEqual([
        [{ label: "Pan-STARRS DR1" }],
        [
          { label: "Pan-STARRS DR1" },
          { label: "WHAM", url: "https://www.astro.wisc.edu/wham/" },
        ],
      ]);
    });

    it("delivers onImageryLoadingChange for each imageryLoadingChange message, and invents no initial value", () => {
      const { fake, backend } = makeHarness();
      const seen: boolean[] = [];
      backend.onImageryLoadingChange((loading) => seen.push(loading));

      fake.simulateFrameMessage(readySignal());
      expect(seen).toEqual([]); // no imageryLoadingChange yet — nothing invented from "ready" alone

      fake.simulateFrameMessage({ channel: SKYMAP_CHANNEL, type: "imageryLoadingChange", loading: true });
      fake.simulateFrameMessage({ channel: SKYMAP_CHANNEL, type: "imageryLoadingChange", loading: false });

      expect(seen).toEqual([true, false]);
    });

    it("delivers onDigitalZoomChange for each digitalZoomChange message, and invents no initial value", () => {
      const { fake, backend } = makeHarness();
      const seen: (number | null)[] = [];
      backend.onDigitalZoomChange((factor) => seen.push(factor));

      fake.simulateFrameMessage(readySignal());
      expect(seen).toEqual([]); // no digitalZoomChange yet — nothing invented from "ready" alone

      fake.simulateFrameMessage({ channel: SKYMAP_CHANNEL, type: "digitalZoomChange", factor: 4 });
      fake.simulateFrameMessage({ channel: SKYMAP_CHANNEL, type: "digitalZoomChange", factor: null });

      expect(seen).toEqual([4, null]);
    });

    it("delivers onFovMoved when the frame reports the user dropped the dragged FOV shape, and never invents a message the frame did not send", () => {
      const { fake, backend } = makeHarness();
      const seen: { ra: number; dec: number }[] = [];
      backend.onFovMoved((ra, dec) => seen.push({ ra, dec }));

      fake.simulateFrameMessage(readySignal());
      expect(seen).toEqual([]);

      fake.simulateFrameMessage({
        channel: SKYMAP_CHANNEL,
        type: "fovMoved",
        ra: 83.822,
        dec: 21.97,
      });

      expect(seen).toEqual([{ ra: 83.822, dec: 21.97 }]);
    });

    it("delivers onObjectClick when the frame reports a catalogue-glyph click, and never invents one the frame did not send", () => {
      const { fake, backend } = makeHarness();
      const seen: string[] = [];
      backend.onObjectClick((designation) => seen.push(designation));

      fake.simulateFrameMessage(readySignal());
      expect(seen).toEqual([]);

      fake.simulateFrameMessage({
        channel: SKYMAP_CHANNEL,
        type: "objectClick",
        designation: "M31",
      });

      expect(seen).toEqual(["M31"]);
    });

    it("forwards setSurveys(), setLayers() and setDisplay() verbatim, so the frame draws only what the host's registry said", () => {
      const { fake, backend } = makeHarness();
      fake.simulateFrameMessage(readySignal());

      // A synthetic TOAST fixture, not a real registry id -- no survey uses
      // TOAST any more (Pan-STARRS DR1, removed 2026-09-08, was the one
      // that did), but the scheme itself stays and still needs conformance
      // coverage (`ssc.skymap.surveys.TileScheme.TOAST`'s own docstring).
      const survey = {
        id: "synthetic-toast",
        label: "Synthetic TOAST survey (test only)",
        kind: "optical" as const,
        scheme: "toast" as const,
        frame: "equatorial" as const,
        tileUrlTemplate: "/api/skymap/tiles/synthetic-toast/toast/{level}/{x}/{y}.png",
        tileFormat: "png",
        maxOrder: 3,
        credit: { label: "Test fixture", url: "https://example.org" },
        blendOnly: false,
        enabled: true,
        reason: null,
        productionCleared: true,
        rightsNotice: null,
      };
      const layers = { background: "synthetic-toast", backgroundOpacity: 1, overlays: [{ surveyId: "wham-halpha", visible: true, opacity: 0.6 }] };
      const display = {
        equatorialGrid: true,
        altAzGrid: false,
        ecliptic: false,
        celestialEquator: true,
        meridian: true,
        horizon: false,
        altitudeLimitDeg: 20,
        site: { latitudeDeg: 48.1, longitudeDeg: 17.1 },
        instant: "2026-09-07T21:00:00Z",
      };
      backend.setSurveys([survey]);
      backend.setLayers(layers);
      backend.setDisplay(display);

      expect(fake.sentToFrame).toEqual([
        { channel: SKYMAP_CHANNEL, type: "setSurveys", surveys: [survey] },
        { channel: SKYMAP_CHANNEL, type: "setLayers", layers },
        { channel: SKYMAP_CHANNEL, type: "setDisplay", display },
      ]);
    });

    it("forwards setCutoutLayers() verbatim, so the frame positions cutout pieces by their own WCS", () => {
      const { fake, backend } = makeHarness();
      fake.simulateFrameMessage(readySignal());

      const pieces = [
        {
          id: "wise-cutout-1:0",
          displayPngUrl: "/api/skymap/cutouts/wise-cutout-1/pieces/0.png",
          wcs: {
            crpix1: 250.5,
            crpix2: 250.5,
            crval1: 283.396,
            crval2: -1.294,
            cd1_1: -0.000381,
            cd1_2: 0,
            cd2_1: 0,
            cd2_2: 0.000381,
            ctype1: "RA---SIN",
            ctype2: "DEC--SIN",
          },
          widthPx: 500,
          heightPx: 500,
          opacity: 0.85,
          creditLabel: "AllWISE: NASA/JPL-Caltech/UCLA · NASA/IPAC IRSA",
          creditUrl: "https://irsa.ipac.caltech.edu/",
        },
      ];
      backend.setCutoutLayers(pieces);

      expect(fake.sentToFrame).toEqual([
        { channel: SKYMAP_CHANNEL, type: "setCutoutLayers", pieces },
      ]);
    });

    it("forwards setOverlay() and addCatalogLayer() as postMessage commands", () => {
      const { fake, backend } = makeHarness();
      fake.simulateFrameMessage(readySignal());

      backend.setOverlay([
        {
          kind: "circle",
          id: "keep-circle",
          centerRa: 10,
          centerDec: 20,
          radiusDeg: 1.12,
          style: { role: "keep" },
        },
      ]);
      backend.addCatalogLayer({
        id: "messier",
        label: "Messier",
        kind: "hips",
        url: "https://example.test/messier",
      });

      expect(fake.sentToFrame).toEqual([
        {
          channel: SKYMAP_CHANNEL,
          type: "setOverlay",
          shapes: [
            {
              kind: "circle",
              id: "keep-circle",
              centerRa: 10,
              centerDec: 20,
              radiusDeg: 1.12,
              style: { role: "keep" },
            },
          ],
        },
        {
          channel: SKYMAP_CHANNEL,
          type: "addCatalogLayer",
          layer: {
            id: "messier",
            label: "Messier",
            kind: "hips",
            url: "https://example.test/messier",
          },
        },
      ]);
    });

    it("worldToScreen(...) is the inverse of screenToWorld(...) after ready, near the field centre", () => {
      const { fake, backend } = makeHarness();
      fake.simulateFrameMessage(
        readySignal({
          view: { ra: 83.633, dec: 22.0145, fovDeg: 2 },
          viewportPx: { w: 800, h: 600 },
        }),
      );

      const [x, y] = backend.worldToScreen(83.7, 22.1);
      const [ra, dec] = backend.screenToWorld(x, y);
      expect(ra).toBeCloseTo(83.7, 6);
      expect(dec).toBeCloseTo(22.1, 6);
    });

    it("worldToScreen() maps the field centre to the viewport centre", () => {
      const { fake, backend } = makeHarness();
      fake.simulateFrameMessage(
        readySignal({
          view: { ra: 200, dec: -10, fovDeg: 4 },
          viewportPx: { w: 400, h: 200 },
        }),
      );
      const [x, y] = backend.worldToScreen(200, -10);
      expect(x).toBeCloseTo(200, 6);
      expect(y).toBeCloseTo(100, 6);
    });

    it("handles RA wrap at 0h/24h without a discontinuity", () => {
      const { fake, backend } = makeHarness();
      fake.simulateFrameMessage(
        readySignal({
          view: { ra: 0.2, dec: 0, fovDeg: 2 },
          viewportPx: { w: 800, h: 600 },
        }),
      );

      const [xNear] = backend.worldToScreen(359.9, 0);
      const [xModerate] = backend.worldToScreen(30, 0);
      // A point 0.3deg away across the wrap (300px/deg at this fov/viewport)
      // must land just off-centre, not thrown to the far edge by an
      // un-normalised 359.7deg delta — and a point ~30deg away (still within
      // the tangent plane's valid hemisphere, unlike a near-antipodal point
      // where gnomonic projection is degenerate) must land much further out.
      expect(Math.abs(xNear - 400)).toBeLessThan(150);
      expect(Math.abs(xModerate - 400)).toBeGreaterThan(
        Math.abs(xNear - 400) * 5,
      );
    });

    it("dispose() removes its message listener so it stops reacting to frame messages", () => {
      const { fake, backend } = makeHarness();
      const seen: unknown[] = [];
      backend.onViewChange((v) => seen.push(v));
      fake.simulateFrameMessage(readySignal());
      expect(fake.listenerCount()).toBeGreaterThan(0);

      backend.dispose();
      expect(fake.listenerCount()).toBe(0);

      fake.simulateFrameMessage({
        channel: SKYMAP_CHANNEL,
        type: "viewChange",
        view: { ra: 9, dec: 9, fovDeg: 9 },
      });
      expect(seen).toHaveLength(1); // only the "ready" delivery, nothing after dispose
    });
  });
}
