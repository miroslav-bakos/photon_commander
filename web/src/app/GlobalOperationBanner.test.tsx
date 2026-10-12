// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { testSnapshot } from "../test/fixtures";
import { useConnectionStore } from "../store/connectionStore";
import { GlobalOperationBanner } from "./GlobalOperationBanner";

const readyLink = {
  status: "ready" as const, endpoint: null, reason: null,
  control_connected: true, events_connected: true,
  events_dropped: 0, connect_attempts: 0, changed_at: null,
};

let captureState: { in_progress: boolean; stage: string | null; object_name: string | null } | null = null;

vi.mock("../api/capture", () => ({
  useCaptureState: () => captureState,
}));

afterEach(() => {
  captureState = null;
  useConnectionStore.getState().reset();
});

describe("GlobalOperationBanner", () => {
  it("shows reconnection impact globally", () => {
    useConnectionStore.setState({ status: "connecting", snapshot: null });
    render(<GlobalOperationBanner />);
    expect(screen.getByRole("alert")).toHaveTextContent("Connecting to Photon Commander");
  });

  it("keeps the active exposure visible outside the Capture screen", () => {
    captureState = { in_progress: true, stage: "exposing", object_name: "M31" };
    const snapshot = testSnapshot();
    snapshot.link = { ...readyLink };
    useConnectionStore.setState({ status: "open", snapshot });
    render(<GlobalOperationBanner />);
    expect(screen.getByRole("status")).toHaveTextContent("Exposing");
    expect(screen.getByRole("status")).toHaveTextContent("M31");
  });

  it("uses the ready control link when device polls have not arrived", () => {
    const snapshot = testSnapshot();
    snapshot.link = { ...readyLink };
    snapshot.devices = { items: [], stale: false, polled_at: null };
    captureState = { in_progress: true, stage: "exposing", object_name: "M31" };
    useConnectionStore.setState({ status: "open", snapshot });
    render(<GlobalOperationBanner />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Exposing");
  });

  it("reports a disconnected rig even before any device poll", () => {
    useConnectionStore.setState({ status: "open", snapshot: testSnapshot() });
    render(<GlobalOperationBanner />);
    expect(screen.getByRole("alert")).toHaveTextContent("Rig offline");
  });

  it("retains stale telemetry warnings for a connected control link", () => {
    const snapshot = testSnapshot();
    snapshot.link = { ...readyLink };
    snapshot.devices = { items: [], stale: true, polled_at: null };
    useConnectionStore.setState({ status: "open", snapshot });
    render(<GlobalOperationBanner />);
    expect(screen.getByRole("alert")).toHaveTextContent("telemetry is stale");
    expect(screen.getByRole("alert")).not.toHaveTextContent("Rig offline");
  });

  it("allows healthy native Alpaca polling without an event connection", () => {
    const snapshot = testSnapshot();
    snapshot.link = { ...readyLink, events_connected: false };
    useConnectionStore.setState({ status: "open", snapshot });
    render(<GlobalOperationBanner />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps long device errors collapsed behind an immediately visible warning", () => {
    const snapshot = testSnapshot();
    snapshot.link = { ...readyLink, status: "degraded", reason: "Device poll failed: " + "diagnostic ".repeat(100) };
    useConnectionStore.setState({ status: "open", snapshot });
    render(<GlobalOperationBanner />);
    const alert = screen.getByRole("alert");
    expect(alert).not.toHaveAttribute("open");
    expect(alert.querySelector("summary")).toHaveTextContent("Live device updates delayed");
    expect(alert.querySelector("summary")).not.toHaveTextContent("Device poll failed");
    expect(alert).toHaveTextContent("Device poll failed");
  });

  it("distinguishes missing events from a disconnected control link", () => {
    const snapshot = testSnapshot();
    snapshot.link = { ...readyLink, status: "degraded", events_connected: false };
    useConnectionStore.setState({ status: "open", snapshot });
    render(<GlobalOperationBanner />);
    expect(screen.getByRole("alert")).toHaveTextContent("event connection is unavailable");
    expect(screen.getByRole("alert")).not.toHaveTextContent("Rig offline");
  });
});
