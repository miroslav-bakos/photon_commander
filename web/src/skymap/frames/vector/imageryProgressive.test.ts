// @vitest-environment jsdom
// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { afterEach, describe, expect, it, vi } from "vitest";
import { createImageryLayer, type ImageryLayer } from "./imageryLayer";
import { buildTileMeshFracGrid, mercatorTileUrl, pickZoom, visibleTiles } from "./mercatorTiles";
import { buildHipsTileMeshGrid, hipsTileUrl, pickHipsOrder, visibleHipsTiles } from "./hipsTiles";
import { imageryTilePriority } from "./imageryPriority";
import { screenToWorld, type Viewport } from "../../projection";
import type { FovShape, SkySurvey } from "../../types";

const view: Viewport = { view: { ra: 10, dec: 35, fovDeg: 10 }, widthPx: 800, heightPx: 600 };
const focus: FovShape = { kind: "rect", id: "target", centerRa: 10, centerDec: 35, widthDeg: 1, heightDeg: 1, positionAngleDeg: 0, style: { role: "primary" } };
let layer: ImageryLayer | undefined;
afterEach(() => { layer?.destroy(); layer = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.replaceChildren(); });
function survey(id: string, scheme: "mercator" | "hips"): SkySurvey {
  return { id, label: id, kind: "infrared", scheme, frame: "equatorial", tileUrlTemplate: `/tiles/${id}/${scheme === "mercator" ? "{level}/{x}/{y}" : "{order}/{dir}/{npix}"}.jpg`, tileFormat: "jpg", maxOrder: 16, credit: { label: id, url: "https://example.com" }, blendOnly: false, enabled: true, reason: null, productionCleared: true, rightsNotice: null };
}
function setup(surveys: SkySurvey[]) {
  const images: HTMLImageElement[] = []; const starts: string[] = []; const settled = new Set<HTMLImageElement>();
  vi.stubGlobal("Image", class {
    onload: HTMLImageElement["onload"] = null; onerror: HTMLImageElement["onerror"] = null; private value = "";
    constructor() { images.push(this as unknown as HTMLImageElement); }
    get src() { return this.value; }
    set src(value: string) { this.value = value; if (value !== "") starts.push(value); }
  });
  const methods = new Map<string, ReturnType<typeof vi.fn>>();
  const gl = new Proxy({}, { get: (_target, name: string) => {
    if (name === name.toUpperCase()) return 1;
    let method = methods.get(name); if (method === undefined) { method = vi.fn(() => ({})); methods.set(name, method); } return method;
  } }) as WebGLRenderingContext;
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation((kind: string) => (kind === "webgl" ? gl : { clearRect: vi.fn(), drawImage: vi.fn() }) as never);
  const root = document.createElement("div"); const canvas = document.createElement("canvas"); root.append(canvas); document.body.append(root);
  layer = createImageryLayer(root, canvas, vi.fn()); layer.setSurveys(surveys);
  layer.setLayers({ background: surveys[0]?.id ?? null, backgroundOpacity: 1, overlays: surveys.slice(1).map((s) => ({ surveyId: s.id, visible: true, opacity: 1 })) }); layer.applyLayers(); layer.resize(800, 600, 1);
  const active = () => images.filter((image) => image.src !== "" && !settled.has(image));
  const finish = (image: HTMLImageElement, fail = false) => { settled.add(image); if (fail) image.onerror?.call(image, new Event("error")); else image.onload?.call(image, new Event("load")); };
  const drain = (fail = false) => { for (let count = 0; active().length > 0; count += 1) { if (count > 1000) throw new Error("Queue did not drain"); const image = active()[0]; if (image !== undefined) finish(image, fail); expect(active().length).toBeLessThanOrEqual(4); } };
  return { images, starts, active, finish, drain };
}
function previews(s: SkySurvey, viewport = view) {
  if (s.scheme === "mercator") {
    const z = Math.max(0, pickZoom(viewport.view.fovDeg, viewport.widthPx, viewport.heightPx) - 2);
    return visibleTiles(viewport, z, screenToWorld).map((tile) => ({ url: mercatorTileUrl(s.tileUrlTemplate, tile), priority: imageryTilePriority(viewport, buildTileMeshFracGrid(viewport, tile, 2), focus) }));
  }
  const order = Math.max(0, pickHipsOrder(viewport.view.fovDeg, viewport.widthPx, viewport.heightPx) - 2);
  return visibleHipsTiles(viewport, order, screenToWorld, 9, s.frame).map((tile) => ({ url: hipsTileUrl(s.tileUrlTemplate, tile), priority: imageryTilePriority(viewport, buildHipsTileMeshGrid(viewport, tile, 2, s.frame), focus) }));
}

describe("actual imagery layer progressive requests", () => {
  it.each(["mercator", "hips"] as const)("loads global FOV previews before fine %s tiles and keeps coverage while fine loads", (scheme) => {
    const surveys = [survey("background", scheme), survey("overlay", scheme)]; const io = setup(surveys);
    const expected = surveys.flatMap((s) => previews(s)); const previewUrls = new Set(expected.map((tile) => tile.url));
    expect(layer?.draw(view, focus)).toBe(0); expect(io.active()).toHaveLength(4);
    const ordered = expected.map((tile, index) => ({ ...tile, index })).sort((a, b) => a.priority - b.priority || b.index - a.index);
    expect(io.starts).toEqual(ordered.slice(0, 4).map((tile) => tile.url));
    const first = io.active()[0]; if (first === undefined) throw new Error("Missing preview");
    io.finish(first); expect(layer?.draw(view, focus)).toBeGreaterThan(0);
    expect(io.starts.every((url) => previewUrls.has(url))).toBe(true);
    io.drain(); expect(io.starts.every((url) => previewUrls.has(url))).toBe(true);
    const count = io.starts.length;
    expect(layer?.draw(view, focus)).toBeGreaterThan(0); expect(io.active()).toHaveLength(4);
    expect(io.starts.slice(count).every((url) => !previewUrls.has(url))).toBe(true);
    const countPending = io.starts.length;
    expect(layer?.draw(view, focus)).toBeGreaterThan(0); expect(io.starts).toHaveLength(countPending);
    io.drain(); layer?.draw(view, focus); expect(new Set(io.starts).size).toBe(io.starts.length);
  });

  it("releases the detail gate after previews fail", () => {
    const s = survey("background", "mercator"); const io = setup([s]); const previewUrls = new Set(previews(s).map((tile) => tile.url));
    layer?.draw(view, focus); io.drain(true); expect(io.starts.every((url) => previewUrls.has(url))).toBe(true);
    layer?.draw(view, focus); expect(io.active()).toHaveLength(4); expect(io.active().every((image) => !previewUrls.has(image.src))).toBe(true);
  });

  it("preserves useful active detail but cancels queued detail while new pan previews settle", () => {
    const s = survey("background", "mercator"); const io = setup([s]);
    layer?.draw(view, focus); io.drain(); layer?.draw(view, focus);
    const activeDetail = [...io.active()]; expect(activeDetail).toHaveLength(4);
    const oldQueued = io.images.filter((image) => image.src === ""); expect(oldQueued.length).toBeGreaterThan(0);
    const oldPreviewUrls = new Set(previews(s).map((tile) => tile.url));
    const candidates = [1, 2, 3, 4, 5, 6, 7, 8].map((delta) => ({ ...view, view: { ...view.view, ra: view.view.ra + delta } }));
    const moved = candidates.find((candidate) => {
      const fine = new Set(visibleTiles(candidate, pickZoom(candidate.view.fovDeg, candidate.widthPx, candidate.heightPx), screenToWorld).map((tile) => mercatorTileUrl(s.tileUrlTemplate, tile)));
      return previews(s, candidate).some((tile) => !oldPreviewUrls.has(tile.url)) && activeDetail.some((image) => fine.has(image.src));
    });
    if (moved === undefined) throw new Error("Expected overlapping detail and new preview footprint");
    const movedFine = new Set(visibleTiles(moved, pickZoom(moved.view.fovDeg, moved.widthPx, moved.heightPx), screenToWorld).map((tile) => mercatorTileUrl(s.tileUrlTemplate, tile)));
    const useful = activeDetail.filter((image) => movedFine.has(image.src));
    const beforePan = io.starts.length; layer?.draw(moved, focus);
    expect(useful.length).toBeGreaterThan(0); expect(useful.every((image) => io.active().includes(image))).toBe(true);
    const movedPreview = new Set(previews(s, moved).map((tile) => tile.url));
    // Completing retained detail must release capacity to previews, never to
    // fine work queued by the preceding viewport.
    for (const image of useful) { io.finish(image); expect(io.active().length).toBeLessThanOrEqual(4); }
    expect(io.starts.slice(beforePan).every((url) => movedPreview.has(url))).toBe(true);
    io.drain(); expect(io.starts.slice(beforePan).every((url) => movedPreview.has(url))).toBe(true);
    expect(oldQueued.every((image) => image.src === "")).toBe(true);
    layer?.draw(moved, focus); expect(io.active()).toHaveLength(4);
    expect(io.active().every((image) => movedFine.has(image.src))).toBe(true);
  });

  it("cancels old viewport work and all requests on survey teardown", () => {
    const s = survey("background", "mercator"); const io = setup([s]); layer?.draw(view, focus);
    const obsolete = [...io.images]; const oldStarts = new Set(io.starts);
    const moved = { ...view, view: { ...view.view, ra: 190, dec: -35 } }; layer?.draw(moved);
    expect(obsolete.every((image) => image.src === "")).toBe(true); expect(io.active().length).toBeLessThanOrEqual(4);
    expect(io.active().every((image) => !oldStarts.has(image.src))).toBe(true);
    layer?.setLayers({ background: null, backgroundOpacity: 1, overlays: [] }); layer?.applyLayers(); expect(io.active()).toHaveLength(0);
    const after = io.starts.length; layer?.draw(moved); expect(io.starts).toHaveLength(after);
  });
});
