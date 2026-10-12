// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import {
  buildMountPointingIndicator,
  describeMountPointingStatus,
  MOUNT_POINTING_STALE_AFTER_SECONDS,
  type MountFovDeg,
  type MountPointingInput,
} from "./mountPointing";
import { rectWorldCorners } from "./projection";

const NOW_MS = Date.parse("2026-09-10T22:00:00Z");

function mount(overrides: Partial<MountPointingInput>): MountPointingInput {
  return {
    ra_hours: 10.5,
    dec_deg: 41.2,
    equatorial_system: "J2000",
    polled_at: "2026-09-10T22:00:00Z",
    stale: false,
    slewing: false,
    ...overrides,
  };
}

describe("buildMountPointingIndicator", () => {
  it("draws nothing when there is no mount", () => {
    const result = buildMountPointingIndicator({ mount: null, nowMs: NOW_MS });
    expect(result.shape).toBeNull();
    expect(result.reason).toMatch(/not connected/);
  });

  it("draws a fresh, settled reading as a solid, undashed marker at the converted position", () => {
    const result = buildMountPointingIndicator({ mount: mount({}), nowMs: NOW_MS });
    expect(result.shape).not.toBeNull();
    expect(result.reason).toBeNull();
    expect(result.stale).toBe(false);
    expect(result.slewing).toBe(false);
    if (result.shape?.kind === "circle") {
      expect(result.shape.centerRa).toBeCloseTo(10.5 * 15);
      expect(result.shape.centerDec).toBeCloseTo(41.2);
      expect(result.shape.style.role).toBe("pointing");
      expect(result.shape.style.dashed).toBe(false);
    } else {
      throw new Error("expected a circle shape");
    }
  });

  // The frame trap (DESIGN.md §3.5.1): this device's real answer today.
  // Refusing to draw here — rather than plotting the of-date position as if
  // it were J2000 — is the whole point of this indicator existing.
  it("draws nothing for a topocentric (of-date) mount, and says why", () => {
    const result = buildMountPointingIndicator({
      mount: mount({ equatorial_system: "topocentric" }),
      nowMs: NOW_MS,
    });
    expect(result.shape).toBeNull();
    expect(result.reason).toMatch(/topocentric/);
  });

  it("uses the server-provided J2000 position for a topocentric mount", () => {
    const result = buildMountPointingIndicator({
      mount: mount({ equatorial_system: "topocentric", ra_j2000_h: 11.25, dec_j2000_deg: -12.5 }),
      nowMs: NOW_MS,
    });
    expect(result.shape).not.toBeNull();
    expect(result.reason).toBeNull();
    if (result.shape?.kind !== "circle") throw new Error("expected a circle shape");
    expect(result.shape.centerRa).toBeCloseTo(168.75);
    expect(result.shape.centerDec).toBeCloseTo(-12.5);
  });

  it("is stale once older than MOUNT_POINTING_STALE_AFTER_SECONDS, even if the server's own flag says otherwise", () => {
    const polledAtMs = NOW_MS - (MOUNT_POINTING_STALE_AFTER_SECONDS + 1) * 1000;
    const result = buildMountPointingIndicator({
      mount: mount({ polled_at: new Date(polledAtMs).toISOString(), stale: false }),
      nowMs: NOW_MS,
    });
    expect(result.stale).toBe(true);
    if (result.shape?.kind === "circle") {
      expect(result.shape.style.dashed).toBe(true);
      expect(result.shape.label).toMatch(/stale/i);
    } else {
      throw new Error("expected a circle shape");
    }
  });

  it("honours the server's own stale flag even inside the fresh window", () => {
    const result = buildMountPointingIndicator({ mount: mount({ stale: true }), nowMs: NOW_MS });
    expect(result.stale).toBe(true);
  });

  it("treats a never-polled mount (polled_at null) as stale, never as fresh", () => {
    const result = buildMountPointingIndicator({ mount: mount({ polled_at: null }), nowMs: NOW_MS });
    expect(result.stale).toBe(true);
    expect(result.ageSeconds).toBeNull();
  });

  it("draws a slewing mount visibly differently from a settled one", () => {
    const settled = buildMountPointingIndicator({ mount: mount({}), nowMs: NOW_MS });
    const slewing = buildMountPointingIndicator({ mount: mount({ slewing: true }), nowMs: NOW_MS });
    expect(slewing.slewing).toBe(true);
    if (settled.shape?.kind === "circle" && slewing.shape?.kind === "circle") {
      expect(slewing.shape.style.fillOpacity).not.toBe(settled.shape.style.fillOpacity);
      expect(slewing.shape.label).not.toBe(settled.shape.label);
    } else {
      throw new Error("expected circle shapes");
    }
  });
});

describe("describeMountPointingStatus", () => {
  it("reads the refusal reason verbatim when nothing is drawn", () => {
    const indicator = buildMountPointingIndicator({
      mount: mount({ equatorial_system: "topocentric" }),
      nowMs: NOW_MS,
    });
    expect(describeMountPointingStatus(indicator)).toMatch(/^Mount pointing not shown: .*topocentric/);
  });

  it("says slewing before it says stale", () => {
    const indicator = buildMountPointingIndicator({
      mount: mount({ slewing: true, stale: true }),
      nowMs: NOW_MS,
    });
    expect(describeMountPointingStatus(indicator)).toBe("Mount: slewing");
  });

  it("reports live for a fresh, settled reading", () => {
    const indicator = buildMountPointingIndicator({ mount: mount({}), nowMs: NOW_MS });
    expect(describeMountPointingStatus(indicator)).toBe("Mount: live");
  });
});

// The measured S30 Pro field from the night this was added (2026-09-10):
// 3.92 x 2.20 deg, rotation varying 87.3-105.4 deg across a sweep of sky.
const MEASURED_FOV: MountFovDeg = { widthDeg: 3.92, heightDeg: 2.2, rotationDeg: 95 };

describe("buildMountPointingIndicator with a MountFovDeg", () => {
  it("draws the real field as a rotated rect at the converted position, matching rectWorldCorners", () => {
    const result = buildMountPointingIndicator({ mount: mount({}), nowMs: NOW_MS, fov: MEASURED_FOV });
    expect(result.shape).not.toBeNull();
    if (result.shape?.kind !== "rect") throw new Error("expected a rect shape");
    expect(result.shape.style.role).toBe("pointing");
    expect(result.shape.widthDeg).toBe(3.92);
    expect(result.shape.heightDeg).toBe(2.2);
    expect(result.shape.positionAngleDeg).toBe(95);
    expect(result.shape.centerRa).toBeCloseTo(10.5 * 15);
    expect(result.shape.centerDec).toBeCloseTo(41.2);

    // Assert against the same projection the map itself draws rectangles
    // through (`rectWorldCorners`, also used by `overlay.ts`'s planned-
    // session rectangle and by `fovDrag.ts`), not a snapshot of the shape.
    const expectedCorners = rectWorldCorners(10.5 * 15, 41.2, 3.92, 2.2, 95);
    const actualCorners = rectWorldCorners(
      result.shape.centerRa,
      result.shape.centerDec,
      result.shape.widthDeg,
      result.shape.heightDeg,
      result.shape.positionAngleDeg,
    );
    expect(actualCorners).toEqual(expectedCorners);
  });

  it("uses the plate-solved center instead of the mount's reported coordinates", () => {
    const result = buildMountPointingIndicator({
      mount: mount({}),
      nowMs: NOW_MS,
      fov: { ...MEASURED_FOV, centerRaDeg: 150.25, centerDecDeg: 40.75 },
    });

    if (result.shape?.kind !== "rect") throw new Error("expected a rect shape");
    expect(result.shape.centerRa).toBe(150.25);
    expect(result.shape.centerDec).toBe(40.75);
  });

  it("falls back to the fixed marker when no fov is given — nothing regresses", () => {
    const withFov = buildMountPointingIndicator({ mount: mount({}), nowMs: NOW_MS, fov: MEASURED_FOV });
    const withoutFov = buildMountPointingIndicator({ mount: mount({}), nowMs: NOW_MS });
    const withNullFov = buildMountPointingIndicator({ mount: mount({}), nowMs: NOW_MS, fov: null });
    expect(withFov.shape?.kind).toBe("rect");
    expect(withoutFov.shape?.kind).toBe("circle");
    expect(withNullFov.shape?.kind).toBe("circle");
  });

  it("dashes a stale reading and dims a slewing one for the rect, same as the marker", () => {
    const settled = buildMountPointingIndicator({ mount: mount({}), nowMs: NOW_MS, fov: MEASURED_FOV });
    const slewing = buildMountPointingIndicator({ mount: mount({ slewing: true }), nowMs: NOW_MS, fov: MEASURED_FOV });
    const stalePolledAtMs = NOW_MS - (MOUNT_POINTING_STALE_AFTER_SECONDS + 1) * 1000;
    const stale = buildMountPointingIndicator({
      mount: mount({ polled_at: new Date(stalePolledAtMs).toISOString() }),
      nowMs: NOW_MS,
      fov: MEASURED_FOV,
    });
    if (settled.shape?.kind !== "rect" || slewing.shape?.kind !== "rect" || stale.shape?.kind !== "rect") {
      throw new Error("expected rect shapes");
    }
    expect(settled.shape.style.dashed).toBe(false);
    expect(slewing.shape.style.fillOpacity).not.toBe(settled.shape.style.fillOpacity);
    expect(slewing.shape.label).not.toBe(settled.shape.label);
    expect(stale.shape.style.dashed).toBe(true);
    expect(stale.shape.label).toMatch(/stale/i);
  });

  it("still draws nothing for an unresolvable (topocentric) frame, even with a fov given", () => {
    const result = buildMountPointingIndicator({
      mount: mount({ equatorial_system: "topocentric" }),
      nowMs: NOW_MS,
      fov: MEASURED_FOV,
    });
    expect(result.shape).toBeNull();
    expect(result.reason).toMatch(/topocentric/);
  });
});
