// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { screenToWorld, worldToScreen } from "../../projection";
import { altAzToRaDec, type SiteInstant } from "./sidereal";
import { createHorizontalCamera, horizontalCameraViewport, panHorizontalCameraToSkyPoint, zoomHorizontalCamera, type HorizontalCamera } from "./horizontalCamera";

const site: SiteInstant = { latitudeDeg: 48.15, longitudeDeg: 17.1, instant: new Date("2026-10-14T20:00:00Z") };
const width = 800;
const height = 600;
function viewport(camera: HorizontalCamera, fovDeg = 60) {
  const [ra, dec] = altAzToRaDec(camera.altDeg, camera.azDeg, site);
  return horizontalCameraViewport({ ra, dec, fovDeg }, width, height, site, camera);
}
function expectAnchor(camera: HorizontalCamera, alt: number, az: number, x: number, y: number, fov = 60) {
  const [ra, dec] = altAzToRaDec(alt, az, site);
  const point = worldToScreen(viewport(camera, fov), ra, dec);
  expect(point[0]).toBeCloseTo(x, 6);
  expect(point[1]).toBeCloseTo(y, 6);
}

describe("horizontal camera zenith continuity", () => {
  it.each([120, 60, 2, 0.02])("round-trips drawing and picking at %s degrees through zenith", (fov) => {
    let camera = createHorizontalCamera(90, 37);
    const [anchorRa, anchorDec] = altAzToRaDec(90, 37, site);
    const [starRa, starDec] = altAzToRaDec(90 - fov / 10, 120, site);
    let previous = worldToScreen(viewport(camera, fov), starRa, starDec);
    for (let step = 1; step <= 80; step += 1) {
      const x = 400 + step / 4;
      const y = 300 + step / 3;
      camera = panHorizontalCameraToSkyPoint(camera, fov, width, height, site, anchorRa, anchorDec, x, y);
      const vp = viewport(camera, fov);
      const anchor = worldToScreen(vp, anchorRa, anchorDec);
      expect(anchor[0]).toBeCloseTo(x, 5);
      expect(anchor[1]).toBeCloseTo(y, 5);
      const star = worldToScreen(vp, starRa, starDec);
      expect(Math.hypot(star[0] - previous[0], star[1] - previous[1])).toBeLessThan(1);
      const picked = screenToWorld(vp, star[0], star[1]);
      expect(picked[0]).toBeCloseTo(starRa, 8);
      expect(picked[1]).toBeCloseTo(starDec, 8);
      previous = star;
    }
  });
  it("uses the same anchor geometry in a phone viewport", () => {
    const w = 360;
    const h = 720;
    const camera = createHorizontalCamera(90, 0);
    const [ra, dec] = altAzToRaDec(90, 0, site);
    const moved = panHorizontalCameraToSkyPoint(camera, 60, w, h, site, ra, dec, 170, 380);
    const vp = horizontalCameraViewport({ ra, dec, fovDeg: 60 }, w, h, site, moved);
    expect(worldToScreen(vp, ra, dec)[0]).toBeCloseTo(170, 6);
    expect(worldToScreen(vp, ra, dec)[1]).toBeCloseTo(380, 6);
  });
  it("keeps the grabbed sky point under the pointer when crossing zenith", () => {
    const [ra, dec] = altAzToRaDec(89.9, 0, site);
    const camera = panHorizontalCameraToSkyPoint(createHorizontalCamera(89.9, 0), 60, width, height, site, ra, dec, 400, 310);
    expectAnchor(camera, 89.9, 0, 400, 310);
  });
  it("supports a one-pixel sideways drag starting exactly at zenith", () => {
    const [ra, dec] = altAzToRaDec(90, 37, site);
    const camera = panHorizontalCameraToSkyPoint(createHorizontalCamera(90, 37), 60, width, height, site, ra, dec, 401, 300);
    expectAnchor(camera, 90, 37, 401, 300);
  });
  it("preserves cursor anchor while zooming near zenith", () => {
    const camera = createHorizontalCamera(89.9, 0);
    const [ra, dec] = screenToWorld(viewport(camera), 410, 310);
    const next = zoomHorizontalCamera(camera, 60, width, height, 410, 310, 0.5, 0.02, 120);
    const point = worldToScreen(viewport(next.camera, next.fovDeg), ra, dec);
    expect(point[0]).toBeCloseTo(410, 6);
    expect(point[1]).toBeCloseTo(310, 6);
  });
});
