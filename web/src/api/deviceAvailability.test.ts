// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";

import { isAlpacaDeviceConnected } from "./deviceAvailability";
import type { AlpacaDeviceStatus, DevicesState } from "./types";

const focuser: AlpacaDeviceStatus = {
  id: "focuser/0",
  kind: "focuser",
  number: 0,
  label: "Focuser",
  connected: true,
  note: null,
  temperature_c: null,
  temperature_note: null,
};

function devices(overrides: Partial<DevicesState> = {}): DevicesState {
  return {
    polled_at: "2026-09-22T19:40:00Z",
    stale: false,
    items: [focuser],
    ...overrides,
  };
}

describe("isAlpacaDeviceConnected", () => {
  it("requires a fresh connected device of the requested kind", () => {
    expect(isAlpacaDeviceConnected(devices(), "focuser")).toBe(true);
    expect(isAlpacaDeviceConnected(devices(), "filterwheel")).toBe(false);
  });

  it("treats stale, missing, false and unknown connection state as offline", () => {
    expect(isAlpacaDeviceConnected(null, "focuser")).toBe(false);
    expect(isAlpacaDeviceConnected(devices({ stale: true }), "focuser")).toBe(false);
    expect(isAlpacaDeviceConnected(devices({ items: [{ ...focuser, connected: false }] }), "focuser")).toBe(false);
    expect(isAlpacaDeviceConnected(devices({ items: [{ ...focuser, connected: null }] }), "focuser")).toBe(false);
  });
});
