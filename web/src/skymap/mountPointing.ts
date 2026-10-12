// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The believed-pointing indicator (DESIGN.md §3.5.1): where `snapshot.mount`
 * says the telescope is, drawn as its own `FovShape` through the same
 * overlay path as catalogue outlines, constellation boundaries, bright
 * stars and solar-system bodies (`SkyMap.tsx` merges all of these into one
 * `setOverlay` call). Frame conversion is `mountFrame.ts`'s job, never
 * repeated here.
 *
 * **A marker, not a rotated rectangle, was a deliberate scope cut — and it
 * cost the owner a real observing session.** DESIGN.md §3.5.1 calls this a
 * "red FOV indicator", and the natural reading is the telescope's actual
 * field footprint — the same rotated rectangle `overlay.ts` draws for the
 * *planned* session. That rectangle needed the active optic's field
 * geometry (`FieldGeometryModel`, from `POST /api/astro/framing`, which
 * itself needs a profile/optic id and a session length/start —
 * `PlanningTab`'s own state, not `snapshot.mount` and not anything
 * `SkyMap.tsx` received as a prop at the time). Building that dependency was
 * out of scope for that dispatch — so it drew a fixed-size marker at the
 * (converted) position instead, honestly labelled, and named its own
 * upgrade path: a `mountFovDeg` prop `SkyMap.tsx` would forward.
 *
 * That upgrade path is this one, taken 2026-09-10 after the owner reported
 * it live, at the telescope, under clear sky ("i expected red fov
 * rectangle") — but not by the route the note above predicted. Rather than
 * waiting on `FieldGeometryModel`/an optic profile, the same night's plate
 * solves gave a *measured* field (pixel scale, sensor size, rotation) that
 * needs no profile lookup at all — a measured field is arguably more honest
 * than a profile's, since it shows where the instrument actually is. So
 * `MountFovDeg` below is a plain (width, height, rotation) triple, owned by
 * whoever computes it — measured or profile-derived, this file does not
 * care — and **optional**. Planning now supplies it from the selected
 * configured optic when the driver cannot publish a complete geometry; a
 * completed plate solve is stronger and replaces that neutral field with
 * measured centre and rotation. The fixed marker remains the fallback for
 * hosts with neither source.
 *
 * **Provenance is part of the shape, not decoration, whichever shape it
 * is.** The native Alpaca surface has no event channel (`mount.ts`'s own
 * doc comment), so `snapshot.mount` is polled, and `polled_at`/`stale` say
 * how old a reading is. `buildMountPointingIndicator` never draws a shape as
 * simply "here": a stale reading is dashed, a slewing mount is dimmed and
 * separately flagged, and when the frame cannot be resolved at all (this
 * device's real answer today — see `mountFrame.ts`) no shape is drawn, with
 * the reason surfaced instead of a wrong position. A rectangle is a
 * stronger claim than a marker (it claims an orientation, not just a point),
 * so it gets no less care than the marker did.
 */
import { mountPositionToJ2000, type MountEquatorialFrame } from "./mountFrame";
import type { FovShape } from "./types";

export const MOUNT_POINTING_SHAPE_ID = "mount-pointing";

/** Matches `PointingReadout.tsx`'s own `STALE_AFTER_SECONDS` (Mount tab) —
 * kept in sync by hand for the same reason that file gives: the server's
 * `mount.stale` is correct only at the instant it was serialised, so a
 * client-side backstop has to keep ticking after the last message. Not
 * imported from there: `web/src/tabs/**` is `frontend-react`'s file, this
 * is `skymap-viz`'s (architecture policy's file-ownership table). */
export const MOUNT_POINTING_STALE_AFTER_SECONDS = 5;

/** The fallback shape, drawn only when the caller has no `MountFovDeg` to
 * give `buildMountPointingIndicator` (see this file's own doc comment) — a
 * fixed marker, deliberately larger than a planet's (`solarSystem.ts`'s
 * `PLANET_MARKER_RADIUS_DEG`, 0.12): this is the instrument the map is
 * about, not one more point of light on it. */
export const MOUNT_POINTING_MARKER_RADIUS_DEG = 0.18;

/** The mount's real field, in whatever way the caller obtained it (measured
 * from plate solves, or read from a configured optic profile — this file
 * takes no position on which). `rotationDeg` is the field's position angle,
 * same convention as `FovShape`'s `rect.positionAngleDeg`/`ssc.astro`'s
 * `position_angle_deg`: degrees, rotating a rectangle whose sides are
 * `widthDeg`/`heightDeg` before north-up is applied. Optional everywhere it
 * is consumed — see this file's own doc comment on why there is, as yet, no
 * source that always has one. */
export interface MountFovDeg {
  widthDeg: number;
  heightDeg: number;
  rotationDeg: number;
  centerRaDeg?: number;
  centerDecDeg?: number;
}

/**
 * Fill opacity for the mount-field rectangle. Deliberately light — the
 * measured field is nearly 4deg on its long side, wide enough that the
 * circle marker's own 0.6/0.25 (tuned for a sub-degree dot) would flood a
 * meaningful patch of the visible sky solid red, the same "filled rectangles
 * completely cover the target" failure `overlay.ts`'s `stack` role was
 * fixed for on 2026-09-10 (see that file's own comment on `buildFovShapes`).
 * Slewing still dims relative to settled, same direction as the marker's.
 */
const MOUNT_FOV_FILL_OPACITY = 0.14;
const MOUNT_FOV_FILL_OPACITY_SLEWING = 0.05;

export interface MountPointingInput {
  ra_hours: number | null;
  dec_deg: number | null;
  ra_j2000_h?: number | null;
  dec_j2000_deg?: number | null;
  equatorial_system: MountEquatorialFrame;
  polled_at: string | null;
  stale: boolean;
  slewing: boolean | null;
}

export interface MountPointingIndicator {
  /** `null` when nothing can be honestly drawn — no mount, no position yet,
   * or (today, on real hardware) no frame conversion available. */
  shape: FovShape | null;
  /** Why `shape` is `null`; always `null` when `shape` is not. */
  reason: string | null;
  /** `true` once `MOUNT_POINTING_STALE_AFTER_SECONDS` have passed since
   * `polled_at`, OR the server's own `stale` said so already — the same OR
   * `PointingReadout.tsx` uses, kept in sync by hand (see that constant's
   * doc comment). `false` when there is no shape to judge staleness of. */
  stale: boolean;
  slewing: boolean;
  /** Seconds since `polled_at`, or `null` when `polled_at` itself is
   * `null` (a mount that exists but has never been successfully polled —
   * `mount.ts`'s own doc comment on why `polled_at` is nullable). */
  ageSeconds: number | null;
}

/**
 * `mount === null` covers both "no mount attached" and "no snapshot yet"
 * (`useMountState()`'s own `null | undefined` contract, collapsed by the
 * caller — see `SkyMap.tsx`) as one case: either way there is nothing to
 * draw and nothing to explain further than "not connected".
 *
 * `fov` is optional. When present, it draws the mount's real field as a
 * rotated `rect`, reusing the exact `FovShape` variant `overlay.ts`'s
 * planned-session rectangle draws; when absent, it falls back to the fixed
 * marker unchanged from before this field existed. Every other rule —
 * staleness, slewing, "no shape when the frame cannot be resolved" — applies
 * identically to both.
 */
export function buildMountPointingIndicator(params: {
  mount: MountPointingInput | null;
  nowMs: number;
  fov?: MountFovDeg | null;
}): MountPointingIndicator {
  const { mount, nowMs, fov = null } = params;
  if (mount === null) {
    return { shape: null, reason: "mount not connected", stale: false, slewing: false, ageSeconds: null };
  }

  const polledAtMs = mount.polled_at === null ? null : Date.parse(mount.polled_at);
  const ageSeconds = polledAtMs === null || Number.isNaN(polledAtMs) ? null : (nowMs - polledAtMs) / 1000;
  const stale = mount.stale || ageSeconds === null || ageSeconds > MOUNT_POINTING_STALE_AFTER_SECONDS;
  const slewing = mount.slewing === true;

  const conversion =
    mount.ra_j2000_h !== undefined && mount.dec_j2000_deg !== undefined &&
    mount.ra_j2000_h !== null && mount.dec_j2000_deg !== null
      ? { status: "ok" as const, raHours: mount.ra_j2000_h, decDeg: mount.dec_j2000_deg }
      : mountPositionToJ2000({
          raHours: mount.ra_hours,
          decDeg: mount.dec_deg,
          equatorialSystem: mount.equatorial_system,
        });
  if (conversion.status === "unavailable") {
    return { shape: null, reason: conversion.reason, stale, slewing, ageSeconds };
  }

  const centerRa = fov?.centerRaDeg ?? conversion.raHours * 15;
  const centerDec = fov?.centerDecDeg ?? conversion.decDeg;
  const label = slewing ? "Mount (slewing)" : stale ? "Mount (stale)" : "Mount";
  // Dimmer while slewing — a moving reading, not a settled one — and dashed
  // while stale, the same "this is not current" language `ghost`/`stack`
  // already use elsewhere on this map. Shared between both shape kinds; only
  // the opacity numbers themselves differ (a big rectangle needs far less
  // fill than a sub-degree dot to still read as "dimmer" — see
  // `MOUNT_FOV_FILL_OPACITY`'s own comment).
  const shape: FovShape =
    fov === null
      ? {
          kind: "circle",
          id: MOUNT_POINTING_SHAPE_ID,
          centerRa,
          centerDec,
          radiusDeg: MOUNT_POINTING_MARKER_RADIUS_DEG,
          style: {
            role: "pointing",
            fillOpacity: slewing ? 0.25 : 0.6,
            dashed: stale,
          },
          label,
        }
      : {
          kind: "rect",
          id: MOUNT_POINTING_SHAPE_ID,
          centerRa,
          centerDec,
          widthDeg: fov.widthDeg,
          heightDeg: fov.heightDeg,
          positionAngleDeg: fov.rotationDeg,
          style: {
            role: "pointing",
            fillOpacity: slewing ? MOUNT_FOV_FILL_OPACITY_SLEWING : MOUNT_FOV_FILL_OPACITY,
            dashed: stale,
          },
          label,
        };
  return { shape, reason: null, stale, slewing, ageSeconds };
}

/** A one-line, human-readable status for the small text indicator
 * `SkyMap.tsx` renders beside the map (the same treatment as the imagery-
 * loading/digital-zoom indicators there) — invariant 7's "surfaced, not
 * buried" applied to this shape specifically: a viewer must be able to see
 * *why* nothing red is drawn, not just notice its absence. `null` when there
 * is nothing worth saying (no mount attached at all). */
export function describeMountPointingStatus(indicator: MountPointingIndicator): string | null {
  if (indicator.reason !== null) return `Mount pointing not shown: ${indicator.reason}`;
  if (indicator.slewing) return "Mount: slewing";
  if (indicator.stale) {
    const age = indicator.ageSeconds === null ? "unknown age" : `${String(Math.round(indicator.ageSeconds))}s old`;
    return `Mount: stale (${age})`;
  }
  return "Mount: live";
}
