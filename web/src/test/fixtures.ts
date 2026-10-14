// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { AlpacaDeviceKind, DevicesState, StateSnapshot } from "../api/types";
import type { components } from "../api/schema";

export function testConditionFactors(): components["schemas"]["CaptureConditionFactorModel"][] {
  return [
    { name: "clouds", measurement: "avg total 40.0%, low 0.0%, high 0.0%", points_deducted: 20 },
    { name: "moon", measurement: "80% illuminated, above horizon 50% of dark window", points_deducted: 18 },
    { name: "dew_point", measurement: "avg temperature/dew-point margin 3.0°C", points_deducted: 3 },
    { name: "wind", measurement: "avg wind 5.3 m/s", points_deducted: 2 },
    { name: "precipitation", measurement: "max precipitation chance 42%", points_deducted: 5 },
    { name: "humidity", measurement: "avg humidity 70%", points_deducted: 0 },
  ];
}

export function clearConditionFactors(): components["schemas"]["CaptureConditionFactorModel"][] {
  return [
    { name: "clouds", measurement: "avg total 0.0%, low 0.0%, high 0.0%", points_deducted: 0 },
    { name: "moon", measurement: "0% illuminated, above horizon 0% of dark window", points_deducted: 0 },
    { name: "dew_point", measurement: "avg temperature/dew-point margin 8.0°C", points_deducted: 0 },
    { name: "wind", measurement: "avg wind 2.0 m/s", points_deducted: 0 },
    { name: "precipitation", measurement: "max precipitation chance 0%", points_deducted: 0 },
    { name: "humidity", measurement: "avg humidity 55%", points_deducted: 0 },
  ];
}

export function testConnectedDevices(...kinds: AlpacaDeviceKind[]): DevicesState {
  return {
    polled_at: "2026-08-28T00:00:00Z",
    stale: false,
    items: kinds.map((kind, index) => ({
      id: `${kind}-${String(index)}`,
      kind,
      number: 0,
      label: kind,
      connected: true,
      note: null,
      temperature_c: null,
      temperature_note: null,
    })),
  };
}

/** A fully-populated snapshot with every field the generated type requires
 * (several are nullable rather than optional, so a partial object does not
 * satisfy the type) — shared so `reduce.test.ts` and `connectionStore.test.ts`
 * do not each hand-roll their own, drifting copy. */
export function testSnapshot(overrides: Partial<StateSnapshot> = {}): StateSnapshot {
  return {
    schema_version: 1,
    server_started_at: "2026-08-28T00:00:00Z",
    link: {
      status: "disconnected",
      endpoint: null,
      reason: null,
      control_connected: false,
      events_connected: false,
      events_dropped: 0,
      connect_attempts: 0,
      changed_at: null,
    },
    device: null,
    mount: null,
    camera_fov: null,
    devices: null,
    discovery: {
      running: false,
      scan_id: null,
      started_at: null,
      finished_at: null,
      completed: 0,
      total: 0,
      found: [],
      cancelled: false,
      error: null,
    },
    location: null,
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
      theme: "dark",
      font_scale: "normal",
      accent: "blue",
      background_enabled: false,
      background_opacity_percent: 15,
      horizon_profile_set: false,
      workflow_guide_visible: true,
    },
    secrets: null,
    notices: [],
    last_device_event: null,
    device_events_seen: 0,
    run: null,
    capture: null,
    ...overrides,
  };
}
