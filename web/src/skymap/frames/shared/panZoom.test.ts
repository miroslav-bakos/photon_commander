// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { screenToWorld } from "../../projection";
import { centerForAnchor, screenPositionOf, zoomAboutScreenPoint } from "./panZoom";

describe("centerForAnchor", () => {
  it("finds a centre that puts the anchor back at the target screen position", () => {
    const center = centerForAnchor(2, 800, 600, 83.7, 22.1, 500, 250);
    const [x, y] = screenPositionOf({ ...center, fovDeg: 2 }, 800, 600, 83.7, 22.1);
    expect(x).toBeCloseTo(500, 3);
    expect(y).toBeCloseTo(250, 3);
  });

  it("converges at the widest field of view this map allows (60deg)", () => {
    const center = centerForAnchor(60, 1000, 700, 10, -20, 200, 600);
    const [x, y] = screenPositionOf({ ...center, fovDeg: 60 }, 1000, 700, 10, -20);
    expect(x).toBeCloseTo(200, 1);
    expect(y).toBeCloseTo(600, 1);
  });

  it("handles an anchor near the RA 0h/24h wrap without a discontinuity", () => {
    const center = centerForAnchor(4, 800, 600, 0.05, 5, 300, 300);
    const [x, y] = screenPositionOf({ ...center, fovDeg: 4 }, 800, 600, 0.05, 5);
    expect(x).toBeCloseTo(300, 3);
    expect(y).toBeCloseTo(300, 3);
    // The resolved centre itself must also be a normal, wrapped RA.
    expect(center.ra).toBeGreaterThanOrEqual(0);
    expect(center.ra).toBeLessThan(360);
  });

  it("is a no-op when the anchor is already at the target's current centre", () => {
    const center = centerForAnchor(2, 800, 600, 83.633, 22.0145, 400, 300);
    expect(center.ra).toBeCloseTo(83.633, 6);
    expect(center.dec).toBeCloseTo(22.0145, 6);
  });
});

describe("zoomAboutScreenPoint", () => {
  it("keeps the sky point under the cursor fixed on screen after zooming in", () => {
    const view = { ra: 83.633, dec: 22.0145, fovDeg: 4 };
    const widthPx = 800;
    const heightPx = 600;
    const cursorX = 550;
    const cursorY = 200;
    const [beforeRa, beforeDec] = screenToWorld({ view, widthPx, heightPx }, cursorX, cursorY);

    const next = zoomAboutScreenPoint(view, widthPx, heightPx, cursorX, cursorY, 0.5, 0.02, 60);
    expect(next.fovDeg).toBeCloseTo(2, 6);

    const [x, y] = screenPositionOf(next, widthPx, heightPx, beforeRa, beforeDec);
    expect(x).toBeCloseTo(cursorX, 2);
    expect(y).toBeCloseTo(cursorY, 2);
  });

  it("clamps to the given min/max field of view", () => {
    const view = { ra: 0, dec: 0, fovDeg: 1 };
    const zoomedOutTooFar = zoomAboutScreenPoint(view, 800, 600, 400, 300, 1000, 0.02, 60);
    expect(zoomedOutTooFar.fovDeg).toBe(60);

    const zoomedInTooFar = zoomAboutScreenPoint(view, 800, 600, 400, 300, 0.0001, 0.02, 60);
    expect(zoomedInTooFar.fovDeg).toBe(0.02);
  });
});
