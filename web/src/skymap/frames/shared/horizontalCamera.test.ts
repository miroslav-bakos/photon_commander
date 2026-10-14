// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { screenToWorld, worldToScreen } from "../../projection";
import { altAzToRaDec, type SiteInstant } from "./sidereal";
import { createHorizontalCamera, horizontalCameraViewport, panHorizontalCameraToSkyPoint, zoomHorizontalCamera, horizontalCameraCenter as horizontalCenter, type HorizontalCamera } from "./horizontalCamera";

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

describe("horizon-constrained navigation", () => {
  it.each([120, 60, 2, 0.02])("keeps a feasible cursor anchor at %s degrees", (fov) => {
    const camera = createHorizontalCamera(40, 37);
    const [ra, dec] = altAzToRaDec(40, 37, site);
    const moved = panHorizontalCameraToSkyPoint(camera, fov, width, height, site, ra, dec, 410, 310);
    expectAnchor(moved, 40, 37, 410, 310, fov);
    const next = zoomHorizontalCamera(moved, fov, width, height, 410, 310, 0.5, 0.02, 120);
    expectAnchor(next.camera, 40, 37, 410, 310, next.fovDeg);
  });
  it.each([90, -90])("preserves heading rather than rolling for a sideways pole anchor at %s", (altitude) => {
    const camera = createHorizontalCamera(altitude, 37);
    const [ra, dec] = altAzToRaDec(altitude, 37, site);
    const moved = panHorizontalCameraToSkyPoint(camera, 60, width, height, site, ra, dec, 401, 300);
    expect(moved.azDeg).toBeCloseTo(37, 10);
    expect(moved.altDeg).toBeCloseTo(altitude, 10);
    const anchor = worldToScreen(viewport(moved), ra, dec);
    expect(anchor[0]).toBeCloseTo(400, 8); // Horizon priority: sideways anchor is impossible.
    expect(moved.east[2]).toBeCloseTo(0, 12);
    expect(moved.up[2]).toBeGreaterThanOrEqual(0);
  });
  it("does not reverse heading when an anchor crosses the zenith", () => {
    const [ra, dec] = altAzToRaDec(89.9, 0, site);
    let camera = createHorizontalCamera(89.9, 0);
    for (let y = 300; y <= 340; y += 1) {
      camera = panHorizontalCameraToSkyPoint(camera, 60, width, height, site, ra, dec, 400, y);
      expect(camera.azDeg).toBeCloseTo(0, 8);
      expect(camera.altDeg).toBeLessThanOrEqual(90);
      expect(camera.east[2]).toBeCloseTo(0, 12);
    }
  });
  it("uses the same feasible geometry in a phone viewport", () => {
    const camera = createHorizontalCamera(45, 0);
    const [ra, dec] = altAzToRaDec(45, 0, site);
    const moved = panHorizontalCameraToSkyPoint(camera, 60, 360, 720, site, ra, dec, 170, 380);
    const vp = horizontalCameraViewport({ ra, dec, fovDeg: 60 }, 360, 720, site, moved);
    expect(worldToScreen(vp, ra, dec)[0]).toBeCloseTo(170, 6);
    expect(worldToScreen(vp, ra, dec)[1]).toBeCloseTo(380, 6);
  });
});

describe("local horizon orientation", () => {
  it("keeps the horizon level after a diagonal pan", () => {
    const camera = createHorizontalCamera(50, 30);
    const [ra, dec] = altAzToRaDec(50, 30, site);
    const moved = panHorizontalCameraToSkyPoint(camera, 60, width, height, site, ra, dec, 470, 340);
    expect(moved.east[2]).toBeCloseTo(0, 12);
    expect(moved.up[2]).toBeGreaterThanOrEqual(0);
  });
});


describe("site-independent horizon basis", () => {
  it.each([-90, -48.15, 0, 48.15, 90])("keeps local horizon level at latitude %s", (latitudeDeg) => {
    for (const longitudeDeg of [-170, 17.1, 170]) {
      for (const instant of [new Date("2026-01-01T00:00:00Z"), new Date("2026-10-14T20:00:00Z")]) {
        const location = { latitudeDeg, longitudeDeg, instant };
        let camera = createHorizontalCamera(35, 120);
        for (let step = 0; step < 20; step += 1) {
          const [ra, dec] = horizontalCenter(camera, location);
          camera = panHorizontalCameraToSkyPoint(camera, 60, width, height, location, ra, dec, 410, 305);
          camera = zoomHorizontalCamera(camera, 60, width, height, 420, 310, 0.9, 0.02, 120).camera;
          expect(camera.east[2]).toBeCloseTo(0, 12);
          expect(camera.up[2]).toBeGreaterThanOrEqual(0);
          const vp = horizontalCameraViewport({ ra, dec, fovDeg: 120 }, width, height, location, camera);
          const points = [-15, 0, 15].map((offset) => {
            const [hRa, hDec] = altAzToRaDec(0, camera.azDeg + offset, location);
            return worldToScreen(vp, hRa, hDec);
          });
          const [left, center, right] = points;
          if (left === undefined || center === undefined || right === undefined) throw new Error("Missing horizon samples");
          expect(left[1]).toBeCloseTo(center[1], 7);
          expect(right[1]).toBeCloseTo(center[1], 7);
          const picked = screenToWorld(vp, 410, 310);
          const roundTrip = worldToScreen(vp, ...picked);
          expect(roundTrip[0]).toBeCloseTo(410, 7);
          expect(roundTrip[1]).toBeCloseTo(310, 7);
        }
      }
    }
  });
});
