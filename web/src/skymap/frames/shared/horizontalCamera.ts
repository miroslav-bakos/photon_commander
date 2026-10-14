// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { type CoordTransform, type Viewport } from "../../projection";
import type { ViewState } from "../../types";
import { localMeanSiderealTimeHours, type SiteInstant } from "./sidereal";

const RAD = Math.PI / 180;
type Vector = readonly [number, number, number];

/** An orthonormal camera frame in horizontal Cartesian space: x north,
 * y west (negated azimuth), z zenith. Unlike an altitude/azimuth pair this
 * retains screen orientation when the centre passes through the zenith. */
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

/** Seed once for a goto/first activation. Subsequent interactions rotate
 * this frame; recreating it from the centre would discard accumulated roll. */
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

/** Rotate the whole camera by the shortest rotation taking the target
 * pixel's current ray onto the anchor. Rodrigues' formula solves the
 * anchor in one step, with no longitude/latitude iteration or pole clamp. */
function anchorCamera(camera: HorizontalCamera, anchor: Vector, fovDeg: number, widthPx: number, heightPx: number, x: number, y: number): HorizontalCamera {
  const from = screenRay(camera, fovDeg, widthPx, heightPx, x, y);
  const to = unit(anchor);
  const cosine = Math.max(-1, Math.min(1, dot(from, to)));
  const axis = cross(from, to);
  function rotate(v: Vector): Vector {
    if (cosine < -1 + 1e-12) {
      // Antipodal rays have no unique shortest rotation. Choose a stable
      // perpendicular axis from the existing camera instead of dividing by 0.
      const perpendicular = unit(cross(from, Math.abs(dot(from, camera.up)) < 0.9 ? camera.up : camera.east));
      const along = dot(perpendicular, v);
      return [2 * along * perpendicular[0] - v[0], 2 * along * perpendicular[1] - v[1], 2 * along * perpendicular[2] - v[2]];
    }
    const first = cross(axis, v);
    const second = cross(axis, first);
    return [v[0] + first[0] + second[0] / (1 + cosine), v[1] + first[1] + second[1] / (1 + cosine), v[2] + first[2] + second[2] / (1 + cosine)];
  }
  return cameraFromBasis(rotate(camera.forward), rotate(camera.east));
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
