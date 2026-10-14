// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { StateEvent } from "../api/types";
import type { MountState } from "../api/mount";
import type { PolarAlignmentState } from "../api/polarAlignment";
import { testSnapshot } from "../test/fixtures";
import { reduce, replay } from "./reduce";


it("an unknown event leaves the snapshot alone instead of destroying it", () => {
  // This switch had no default, so an unrecognised type fell through and
  // `reduce` returned `undefined` — replacing the entire snapshot with
  // nothing. The UI rendered from the initial snapshot and then blanked on
  // the first unknown event, which reads as "reverts to no location set
  // after a second" (owner, 2026-09-10, the moment `devices.changed` shipped
  // ahead of this file). A client may lag a server deploy by design, so
  // ignoring an unknown event is correct; losing state over one never is.
  const snapshot = testSnapshot();
  const after = reduce(snapshot, { type: "something.from.a.newer.server" } as never);
  expect(after).toBe(snapshot);
  expect(after.location).toEqual(snapshot.location);
});

it("applies devices.changed without touching anything else", () => {
  const snapshot = testSnapshot();
  const devices = { polled_at: "2026-09-10T14:00:00Z", stale: false, items: [] };
  const after = reduce(snapshot, { type: "devices.changed", devices } as never);
  expect((after as { devices?: unknown }).devices).toEqual(devices);
  expect(after.location).toEqual(snapshot.location);
  expect(after.link).toEqual(snapshot.link);
});

it("applies camera_fov.changed so a plate solve can replace the map marker", () => {
  const snapshot = testSnapshot();
  const camera_fov = {
    width_deg: 2.24,
    height_deg: 3.99,
    rotation_deg: 93.5,
    center_ra_j2000_h: 10,
    center_dec_j2000_deg: 41.2,
    solved_at: "2026-09-12T21:00:00Z",
  };

  const after = reduce(snapshot, { type: "camera_fov.changed", camera_fov } as never);

  expect(after.camera_fov).toEqual(camera_fov);
  expect(after.mount).toEqual(snapshot.mount);
});


it("replaces the polar_alignment section on polar_alignment.changed, live not just on resnapshot", () => {
  const snapshot = testSnapshot();
  const polar_alignment: PolarAlignmentState = {
    status: "running",
    run_kind: "measure",
    started_at: "2026-09-11T20:00:00Z",
    ended_at: null,
    exposure_started_at: null,
    exposure_duration_s: null,
    current_point_index: 1,
    preflight_reason: null,
    points: [],
    result: null,
    start: null,
    assessment: null,
    error: null,
  };
  const after = reduce(snapshot, { type: "polar_alignment.changed", polar_alignment } as never);
  expect(after.polar_alignment).toEqual(polar_alignment);
  expect(after.location).toEqual(snapshot.location);
});

it("replaces the capture section on capture.changed so a completed frame appears live", () => {
  const snapshot = testSnapshot();
  const capture = {
    in_progress: false,
    last_frame_id: 135,
    last_captured_at: "2026-09-12T20:05:05Z",
    last_error: null,
    folder_configured: true,
  };
  const after = reduce(snapshot, { type: "capture.changed", capture } as never);
  expect(after.capture?.last_frame_id).toBe(135);
  expect(after.location).toEqual(snapshot.location);
});

describe("reduce", () => {
  // `mount.changed` is not yet in the generated `StateEvent` union — see
  // this file's own `reduce.ts` header — so this is typed structurally
  // rather than via an import of `StateEvent`.
  it("replaces the mount section on mount.changed", () => {
    const snapshot = testSnapshot();
    const next = reduce(snapshot, {
      type: "mount.changed",
      mount: {
        polled_at: "2026-09-10T07:00:00Z",
        stale: false,
        ra_hours: 12.34,
        dec_deg: 45.6,
        alt_deg: 30.1,
        az_deg: 180,
        sidereal_time_hours: 8.06,
        tracking: true,
        tracking_rate: "sidereal",
        at_park: false,
        at_home: true,
        slewing: false,
        equatorial_system: "topocentric",
        capabilities: null,
      },
    });
    const mounted = next as typeof next & { mount: MountState | null };
    expect(mounted.mount?.ra_hours).toBe(12.34);
    // Immutable, like every other case here. `testSnapshot()`'s own `mount`
    // is explicitly `null` (the field is required, just nullable — architecture policy:
    // `None` means unknown, never guessed), so the pre-reduce value to check
    // against is `null`, not `undefined`.
    expect((snapshot as typeof snapshot & { mount: MountState | null }).mount).toBeNull();
  });

  it("replaces the link section on link.changed", () => {
    const snapshot = testSnapshot();
    const event: StateEvent = {
      type: "link.changed",
      link: {
        status: "ready",
        endpoint: "192.168.1.77:4701",
        reason: null,
        control_connected: true,
        events_connected: true,
        events_dropped: 0,
        connect_attempts: 1,
        changed_at: "2026-08-28T00:00:00Z",
      },
    };
    const next = reduce(snapshot, event);
    expect(next.link?.status).toBe("ready");
    // Immutable: the input snapshot is untouched.
    expect(snapshot.link?.status).toBe("disconnected");
  });

  it("appends a notice, deduping by id and trimming to MAX_NOTICES", () => {
    let snapshot = testSnapshot();
    for (let i = 0; i < 55; i++) {
      snapshot = reduce(snapshot, {
        type: "notice.raised",
        notice: {
          id: `n${String(i)}`,
          level: "info",
          code: "test",
          message: "hello",
          raised_at: "2026-08-28T00:00:00Z",
        },
      });
    }
    expect(snapshot.notices).toHaveLength(50);
    expect(snapshot.notices?.[0]?.id).toBe("n5");
    expect(snapshot.notices?.at(-1)?.id).toBe("n54");
  });

  it("replacing a notice with the same id does not grow the list", () => {
    let snapshot = testSnapshot();
    const notice = {
      id: "n1",
      level: "info" as const,
      code: "test",
      message: "first",
      raised_at: "2026-08-28T00:00:00Z",
    };
    snapshot = reduce(snapshot, { type: "notice.raised", notice });
    snapshot = reduce(snapshot, { type: "notice.raised", notice: { ...notice, message: "second" } });
    expect(snapshot.notices).toHaveLength(1);
    expect(snapshot.notices?.[0]?.message).toBe("second");
  });

  it("clears a notice by id", () => {
    let snapshot = testSnapshot();
    snapshot = reduce(snapshot, {
      type: "notice.raised",
      notice: { id: "n1", level: "info", code: "x", message: "m", raised_at: "2026-08-28T00:00:00Z" },
    });
    snapshot = reduce(snapshot, { type: "notice.cleared", notice_id: "n1" });
    expect(snapshot.notices).toHaveLength(0);
  });

  it("increments device_events_seen and records the last event", () => {
    const snapshot = testSnapshot();
    const record = {
      name: "battery",
      received_at: "2026-08-28T00:00:00Z",
      device_uptime_s: null,
      payload: {},
    };
    const next = reduce(snapshot, { type: "device.event", event: record });
    expect(next.device_events_seen).toBe(1);
    expect(next.last_device_event).toEqual(record);
  });

  it("replay folds a run of events in order", () => {
    const events: StateEvent[] = [
      {
        type: "settings.changed",
        settings: {
          location_source_preference: null,
          remembered_host: null,
          remembered_control_port: null,
          ethernet_host: null,
          ethernet_control_port: null,
          hardware_profile_id: null,
          hardware_profile_source: null,
          configured_secret_names: [],
          control_pin_set: false,
          auth_required: false,
          theme: "red",
          font_scale: "large",
          accent: "violet",
          background_enabled: true,
          background_opacity_percent: 24,
          horizon_profile_set: false,
          workflow_guide_visible: true,
        },
      },
      { type: "location.changed", location: null },
    ];
    const next = replay(testSnapshot(), events);
    expect(next.settings?.theme).toBe("red");
    expect(next.location).toBeNull();
  });
});
