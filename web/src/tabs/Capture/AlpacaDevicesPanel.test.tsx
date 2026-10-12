// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pins the three honest states this panel must distinguish (mirroring
 * `ConnectionsFooter.tsx`'s own `DevicesEntries` tests, which this panel's
 * rendering logic is adapted from): no snapshot at all, a snapshot whose
 * device list has never been polled, and a real-but-empty list — plus that
 * a device's tri-state `connected` reads out in words, not just colour.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DevicesState } from "../../api/types";
import { useConnectionStore } from "../../store/connectionStore";
import { testSnapshot } from "../../test/fixtures";
import { AlpacaDevicesPanel } from "./AlpacaDevicesPanel";

afterEach(() => {
  cleanup();
  useConnectionStore.getState().reset();
});

function withDevices(devices: DevicesState | null): void {
  useConnectionStore.setState({ snapshot: testSnapshot({ devices }), seq: 1 });
}

describe("AlpacaDevicesPanel", () => {
  it("says unpolled when no snapshot has devices at all", () => {
    render(<AlpacaDevicesPanel />);
    expect(screen.getByText("Devices: unpolled.")).toBeInTheDocument();
  });

  it("flags a stale list with when it was last polled", () => {
    withDevices({
      polled_at: "2026-09-10T07:00:00Z",
      stale: true,
      items: [
        { id: "telescope-0", kind: "telescope", number: 0, label: "Seestar mount", connected: true, note: null, temperature_c: null, temperature_note: null },
      ],
    });
    const { container } = render(<AlpacaDevicesPanel />);
    expect(screen.getByText(/device list is stale/i)).toHaveTextContent("last polled");
    const dot = container.querySelector("li span[aria-hidden]");
    expect(dot?.className).toContain("bg-dot-unknown");
    expect(dot?.className).not.toContain("bg-dot-ok");
  });

  it("says never successfully polled when stale with no polled_at", () => {
    withDevices({ polled_at: null, stale: true, items: [] });
    render(<AlpacaDevicesPanel />);
    expect(screen.getByText(/never successfully polled/i)).toBeInTheDocument();
  });

  it("distinguishes a real empty list from unpolled", () => {
    withDevices({ polled_at: "2026-09-10T07:00:00Z", stale: false, items: [] });
    render(<AlpacaDevicesPanel />);
    expect(screen.getByText("No devices configured.")).toBeInTheDocument();
  });

  it("renders each device's kind, label and tri-state connection in words", () => {
    withDevices({
      polled_at: "2026-09-10T07:00:00Z",
      stale: false,
      items: [
        { id: "telescope-0", kind: "telescope", number: 0, label: "Seestar mount", connected: true, note: null, temperature_c: null, temperature_note: null },
        { id: "focuser-0", kind: "focuser", number: 0, label: "Seestar focuser", connected: false, note: null, temperature_c: null, temperature_note: null },
        { id: "filterwheel-0", kind: "filterwheel", number: 0, label: "Seestar wheel", connected: null, note: null, temperature_c: null, temperature_note: null },
      ],
    });
    render(<AlpacaDevicesPanel />);
    expect(screen.getByText("Seestar mount")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.getByText("not connected")).toBeInTheDocument();
    expect(screen.getByText("connection unknown")).toBeInTheDocument();
  });
});
