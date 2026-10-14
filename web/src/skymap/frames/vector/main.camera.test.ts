// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { afterEach, expect, it, vi } from "vitest";
import type { HostToFrameMessage } from "../../protocol";
import { screenToWorld, worldToScreen, type Viewport } from "../../projection";
import type { DisplayOptions } from "../../types";
import { altAzToRaDec, localMeanSiderealTimeHours } from "../shared/sidereal";

const bridge = vi.hoisted(() => ({ receive: (_message: HostToFrameMessage) => { throw new Error("Frame bridge not initialized"); }, draw: vi.fn<(viewport: Viewport) => number>(() => 0) }));
vi.mock("../shared/frameBridge", () => ({ listenForHostMessages: (handler: typeof bridge.receive) => { bridge.receive = handler; }, postToHost: vi.fn() }));
vi.mock("../shared/imageryLoadTracking", () => ({ trackImageryLoading: vi.fn() }));
vi.mock("./imageryLayer", () => ({ createImageryLayer: () => ({ draw: bridge.draw, resize: vi.fn() }) }));

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = ""; });

it("uses one continuous basis in the real frame's pointer, paint, wheel and timeline paths", async () => {
  document.body.innerHTML = '<div id="skymap-root"><canvas id="skymap-canvas"></canvas><div id="skymap-labels"></div></div>';
  const root = document.getElementById("skymap-root");
  if (root === null) throw new Error("Missing test root");
  Object.defineProperty(root, "setPointerCapture", { value: vi.fn() });
  const context = { setTransform: vi.fn(), save: vi.fn(), clearRect: vi.fn(), restore: vi.fn() };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as unknown as CanvasRenderingContext2D);
  const frames: FrameRequestCallback[] = [];
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.push(callback); return frames.length; });
  vi.stubGlobal("ResizeObserver", undefined);
  const paint = () => {
    for (const callback of frames.splice(0)) callback(0);
    const lastPaint = bridge.draw.mock.calls.at(-1);
    if (lastPaint === undefined) throw new Error("Frame did not paint");
    return lastPaint[0];
  };
  const pointer = (type: string, x: number, y: number) => {
    const event = new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true });
    Object.defineProperties(event, { isPrimary: { value: true }, pointerId: { value: 1 } });
    root.dispatchEvent(event);
  };
  await import("./main");
  const site = { latitudeDeg: 48.15, longitudeDeg: 17.1, instant: new Date("2026-10-14T20:00:00Z") };
  const display: DisplayOptions = { equatorialGrid: false, altAzGrid: false, ecliptic: false, celestialEquator: false, meridian: false, horizon: false, altitudeLimitDeg: null, site, instant: site.instant.toISOString() };
  const send = (message: HostToFrameMessage) => { bridge.receive(message); };
  const [ra, dec] = altAzToRaDec(89.9, 0, site);
  send({ channel: "ssc-skymap/v1", type: "setDisplay", display });
  send({ channel: "ssc-skymap/v1", type: "goto", ra, dec, fovDeg: 60 });
  paint();
  pointer("pointerdown", 400, 300);
  pointer("pointermove", 400, 310);
  pointer("pointerup", 400, 310);
  let viewport = paint();
  expect(worldToScreen(viewport, ra, dec)[1]).toBeCloseTo(310, 6);
  const [zoomRa, zoomDec] = screenToWorld(viewport, 410, 310);
  root.dispatchEvent(new WheelEvent("wheel", { clientX: 410, clientY: 310, deltaY: -100 }));
  viewport = paint();
  expect(worldToScreen(viewport, zoomRa, zoomDec)[0]).toBeCloseTo(410, 6);
  expect(worldToScreen(viewport, zoomRa, zoomDec)[1]).toBeCloseTo(310, 6);
  const beforeRefresh = worldToScreen(viewport, ra, dec);
  send({ channel: "ssc-skymap/v1", type: "setDisplay", display: { ...display, holdOnTarget: true } });
  expect(worldToScreen(paint(), ra, dec)).toEqual(beforeRefresh);

  // A held target transits the true zenith while time advances. Its
  // neighbouring reference star must move smoothly, rather than flip sides.
  const targetRa = localMeanSiderealTimeHours(site.instant, site.longitudeDeg) * 15;
  send({ channel: "ssc-skymap/v1", type: "goto", ra: targetRa, dec: site.latitudeDeg, fovDeg: 60 });
  let previous: [number, number] | undefined;
  for (let seconds = -5; seconds <= 5; seconds += 1) {
    send({ channel: "ssc-skymap/v1", type: "setDisplay", display: { ...display, holdOnTarget: true, instant: new Date(site.instant.getTime() + seconds * 1000).toISOString() } });
    viewport = paint();
    const target = worldToScreen(viewport, targetRa, site.latitudeDeg);
    expect(target[0]).toBeCloseTo(400, 6);
    expect(target[1]).toBeCloseTo(300, 6);
    const star = worldToScreen(viewport, targetRa + 1, site.latitudeDeg);
    if (previous !== undefined) expect(Math.hypot(star[0] - previous[0], star[1] - previous[1])).toBeLessThan(0.01);
    previous = star;
  }
});
