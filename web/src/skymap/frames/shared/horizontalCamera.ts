// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { type CoordTransform, type Viewport } from "../../projection";
import type { ViewState } from "../../types";
import { localMeanSiderealTimeHours, type SiteInstant } from "./sidereal";

const RAD = Math.PI / 180;
type Vector = readonly [number, number, number];

/** A horizon-aligned camera in local Cartesian space: x north, y west,
 * z zenith. Screen right/left stays parallel to the local horizon. */
export interface HorizontalCamera {
  readonly forward: Vector;
  readonly east: Vector;
  readonly up: Vector;
  readonly altDeg: number;
  readonly azDeg: number;
}

function dot(a: Vector, b: Vector): number { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
function cross(a: Vector, b: Vector): Vector {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}
function unit(a: Vector): Vector {
  const length = Math.hypot(...a);
  return [a[0] / length, a[1] / length, a[2] / length];
}
function vector(lonDeg: number, latDeg: number): Vector {
  const lon = lonDeg * RAD;
  const lat = latDeg * RAD;
  return [Math.cos(lat) * Math.cos(lon), Math.cos(lat) * Math.sin(lon), Math.sin(lat)];
}
function angles(v: Vector): [number, number] {
  return [Math.atan2(v[1], v[0]) / RAD, Math.atan2(v[2], Math.hypot(v[0], v[1])) / RAD];
}
function cameraFromBasis(forward: Vector, east: Vector): HorizontalCamera {
  const f = unit(forward);
  const along = dot(east, f);
  const e = unit([east[0] - along * f[0], east[1] - along * f[1], east[2] - along * f[2]]);
  const [negAz, altDeg] = angles(f);
  return { forward: f, east: e, up: cross(f, e), altDeg, azDeg: ((-negAz % 360) + 360) % 360 };
}

/** Rebuild the local horizon basis; navigation must never accumulate roll. */
export function createHorizontalCamera(altDeg: number, azDeg: number): HorizontalCamera {
  const lon = -azDeg * RAD;
  return cameraFromBasis(vector(-azDeg, altDeg), [-Math.sin(lon), Math.cos(lon), 0]);
}

/** The same site rotation as sidereal.ts, in Cartesian form so neither
 * direction divides by cos(altitude) or needs an azimuth at the zenith. */
function siteRotation(site: SiteInstant) {
  const lat = site.latitudeDeg * RAD;
  const sinLat = Math.sin(lat);
  const cosLat = Math.cos(lat);
  const lst = localMeanSiderealTimeHours(site.instant, site.longitudeDeg) * 15;
  return {
    toHorizontal(raDeg: number, decDeg: number): Vector {
      const [x, y, z] = vector(lst - raDeg, decDeg);
      return [-sinLat * x + cosLat * z, y, cosLat * x + sinLat * z];
    },
    fromHorizontal(v: Vector): [number, number] {
      const [hourAngle, dec] = angles([-sinLat * v[0] + cosLat * v[2], v[1], cosLat * v[0] + sinLat * v[2]]);
      return [((lst - hourAngle) % 360 + 360) % 360, dec];
    },
  };
}
/** Explicit target/time/observer changes keep that equatorial field centred.
 * This is not a pointer gesture: a zenith transit may legitimately reverse
 * heading while the local horizon remains level. At the true pole only,
 * preserve the existing heading rather than infer one from numerical noise. */
export function horizontalCameraAtSkyPoint(site: SiteInstant, raDeg: number, decDeg: number, previous?: HorizontalCamera | null): HorizontalCamera {
  const direction = siteRotation(site).toHorizontal(raDeg, decDeg);
  const [longitude, altitude] = angles(direction);
  const azimuth = Math.hypot(direction[0], direction[1]) < 1e-12 ? previous?.azDeg ?? 0 : -longitude;
  return createHorizontalCamera(altitude, azimuth);
}

function localVector(camera: HorizontalCamera, v: Vector): Vector {
  return [dot(v, camera.forward), dot(v, camera.east), dot(v, camera.up)];
}
function horizontalVector(camera: HorizontalCamera, v: Vector): Vector {
  const { forward: f, east: e, up: u } = camera;
  return [f[0] * v[0] + e[0] * v[1] + u[0] * v[2], f[1] * v[0] + e[1] * v[1] + u[1] * v[2], f[2] * v[0] + e[2] * v[1] + u[2] * v[2]];
}

/** Shared by the renderer, labels, picking and gestures. The projected
 * centre is at local (0,0); the full basis supplies the orientation instead
 * of reconstructing a pole-up frame from an unstable centre azimuth. */
export function horizontalCameraViewport(view: ViewState, widthPx: number, heightPx: number, site: SiteInstant, camera: HorizontalCamera): Viewport {
  const rotation = siteRotation(site);
  const coordTransform: CoordTransform = {
    toProjected(ra, dec) { return angles(localVector(camera, rotation.toHorizontal(ra, dec))); },
    fromProjected(lon, lat) { return rotation.fromHorizontal(horizontalVector(camera, vector(lon, lat))); },
  };
  const [ra, dec] = rotation.fromHorizontal(camera.forward);
  return { view: { ...view, ra, dec }, widthPx, heightPx, coordTransform };
}

/** Equatorial mirror of the centre, without recovering a pole azimuth. */
export function horizontalCameraCenter(camera: HorizontalCamera, site: SiteInstant): [number, number] {
  return siteRotation(site).fromHorizontal(camera.forward);
}

function screenRay(camera: HorizontalCamera, fovDeg: number, widthPx: number, heightPx: number, x: number, y: number): Vector {
  const scale = fovDeg * RAD / heightPx;
  return unit(horizontalVector(camera, [1, (widthPx / 2 - x) * scale, (heightPx / 2 - y) * scale]));
}

/** Solve a cursor anchor with the camera constrained to the local horizon.
 * With local pixel ray [1,u,v], its zenith component is
 * (sin(alt)+v*cos(alt))/sqrt(1+u²+v²). The two analytic altitude
 * roots replace an unstable iterative longitude/latitude solve.
 *
 * A zenith/nadir anchor cannot reach an arbitrary sideways pixel without
 * rolling the horizon. At that singularity, or when reaching an anchor would
 * reverse the heading by over 90 degrees, preserve the previous azimuth and
 * choose the closest achievable ray in altitude. This explicitly prioritises
 * horizon-up continuity over exact cursor anchoring at a pole.
 */
function anchorCamera(camera: HorizontalCamera, anchor: Vector, fovDeg: number, widthPx: number, heightPx: number, x: number, y: number): HorizontalCamera {
  const to = unit(anchor);
  const scale = fovDeg * RAD / heightPx;
  const u = (widthPx / 2 - x) * scale;
  const v = (heightPx / 2 - y) * scale;
  const norm = Math.hypot(1, u, v);
  const rhs = to[2] * norm / Math.hypot(1, v);
  const currentLon = -camera.azDeg * RAD;
  const currentAlt = camera.altDeg * RAD;
  const wrap = (angle: number) => Math.atan2(Math.sin(angle), Math.cos(angle));
  let best: { altitude: number; longitude: number; distance: number } | undefined;
  // atan2 of a vanishing horizontal vector would invent an arbitrary heading.
  if (Math.hypot(to[0], to[1]) > 1e-12 && Math.abs(rhs) <= 1 + 1e-12) {
    const root = Math.asin(Math.max(-1, Math.min(1, rhs)));
    const offset = Math.atan(v);
    for (const raw of [root - offset, Math.PI - root - offset]) {
      const altitude = wrap(raw);
      if (Math.abs(altitude) > Math.PI / 2 + 1e-12) continue;
      const longitude = Math.atan2(to[1], to[0]) - Math.atan2(u, Math.cos(altitude) - v * Math.sin(altitude));
      const headingDelta = wrap(longitude - currentLon);
      if (Math.abs(headingDelta) > Math.PI / 2 + 1e-12) continue;
      const distance = Math.hypot(altitude - currentAlt, headingDelta);
      if (!best || distance < best.distance) best = { altitude, longitude, distance };
    }
  }
  if (best) return createHorizontalCamera(Math.max(-90, Math.min(90, best.altitude / RAD)), -best.longitude / RAD);
  // At fixed heading, maximise anchor dot ray: A*cos(alt)+B*sin(alt).
  const radial = to[0] * Math.cos(currentLon) + to[1] * Math.sin(currentLon);
  const a = radial + v * to[2];
  const b = to[2] - v * radial;
  const altitude = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, Math.atan2(b, a)));
  return createHorizontalCamera(altitude / RAD, camera.azDeg);
}

/** The live pointer path keeps its equatorial anchor as a vector throughout
 * the site rotation; even an exact-zenith anchor needs no inverse azimuth. */
export function panHorizontalCameraToSkyPoint(camera: HorizontalCamera, fovDeg: number, widthPx: number, heightPx: number, site: SiteInstant, anchorRaDeg: number, anchorDecDeg: number, screenX: number, screenY: number): HorizontalCamera {
  return anchorCamera(camera, siteRotation(site).toHorizontal(anchorRaDeg, anchorDecDeg), fovDeg, widthPx, heightPx, screenX, screenY);
}

export function zoomHorizontalCamera(camera: HorizontalCamera, fovDeg: number, widthPx: number, heightPx: number, screenX: number, screenY: number, factor: number, minFovDeg: number, maxFovDeg: number): { camera: HorizontalCamera; fovDeg: number } {
  const anchor = screenRay(camera, fovDeg, widthPx, heightPx, screenX, screenY);
  const nextFov = Math.min(maxFovDeg, Math.max(minFovDeg, fovDeg * factor));
  return { camera: anchorCamera(camera, anchor, nextFov, widthPx, heightPx, screenX, screenY), fovDeg: nextFov };
}
