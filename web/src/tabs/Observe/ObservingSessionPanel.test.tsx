// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useConnectionStore } from "../../store/connectionStore";
import { testSnapshot } from "../../test/fixtures";
import { ObservingSessionPanel } from "./ObservingSessionPanel";

const telemetry = vi.hoisted(() => ({ missing: false }));

const mutations = vi.hoisted(() => ({ pause: vi.fn(), resume: vi.fn(), stop: vi.fn() }));

vi.mock("../../api/capture", () => ({
  useCaptureState: () => telemetry.missing ? null : ({
    in_progress: true,
    stage: "exposing",
    object_name: "M31",
    last_frame_id: 42,
  }),
  useLiveStackQuery: () => telemetry.missing ? ({ data: undefined, isError: true }) : ({ data: { active: true, object_name: "M31", frame_count: 12, rejected_count: 2 } }),
  useCaptureFrameQualityQuery: () => ({ data: { hfr_px: 1.23, fwhm_px: 2.89 } }),
}));

vi.mock("../../api/mount", () => ({
  useMountState: () => telemetry.missing ? null : ({ stale: false, polled_at: new Date().toISOString(), tracking: true, slewing: false, at_park: false }),
}));

vi.mock("../../api/runs", () => ({
  usePauseRun: () => ({ isPending: false, mutate: mutations.pause }),
  useResumeRun: () => ({ isPending: false, mutate: mutations.resume }),
  useStopRun: () => ({ isPending: false, mutate: mutations.stop }),
}));

afterEach(() => {
  useConnectionStore.getState().reset();
  vi.clearAllMocks();
  telemetry.missing = false;
});

describe("ObservingSessionPanel", () => {
  it("does not turn missing telemetry into Idle, Off or an available control", () => {
    telemetry.missing = true;
    useConnectionStore.setState({ status: "open", snapshot: testSnapshot() });
    render(<MemoryRouter><ObservingSessionPanel /></MemoryRouter>);
    expect(screen.queryByText("Idle")).not.toBeInTheDocument();
    expect(screen.queryByText("Off")).not.toBeInTheDocument();
    expect(screen.getAllByText(/Unknown|Disconnected/).length).toBeGreaterThan(0);
  });

  it("keeps exposure, stack, last-frame quality, and next plan action together", () => {
    useConnectionStore.setState({
      status: "open",
      snapshot: testSnapshot({
        link: { endpoint: null, reason: null, events_dropped: 0, connect_attempts: 0, changed_at: null, events_connected: true, status: "ready", control_connected: true },
        run: {
          run_id: "run-1",
          plan_id: "plan-1",
          plan_name: "Andromeda mosaic",
          plan_revision: 1,
          night_date: "2026-09-22",
          status: "running",
          started_at: "2026-09-22T21:00:00Z",
          ended_at: null,
          ended_reason: null,
          current_item_id: "expose-1",
          items: [
            { item_id: "expose-1", kind: "exposure", enabled: true, status: "running", attempt: 1, panel_index: 0, held: false, reason: "", start_utc: null, end_utc: null },
            { item_id: "dither-1", kind: "dither", enabled: true, status: "waiting", attempt: 0, panel_index: 0, held: false, reason: "", start_utc: null, end_utc: null },
          ],
          gate_held: false,
          gate_reason: null,
          last_decision: null,
          last_decision_at: null,
          journal_length: 1,
          available_actions: ["pause", "stop"],
        },
      }),
    });

    render(<MemoryRouter><ObservingSessionPanel /></MemoryRouter>);

    expect(screen.getByRole("heading", { name: /observing now/i }).closest(".mobile-preview-card")).toHaveClass("mobile-preview-card");
    expect(screen.getByRole("button", { name: "Observing image viewer" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Capture" })).toHaveAttribute("href", "/live");
    expect(screen.getByText("M31")).toBeInTheDocument();
    expect(screen.getByText("Andromeda mosaic")).toBeInTheDocument();
    expect(screen.getByText("12 accepted · 2 rejected")).toBeInTheDocument();
    expect(screen.getByText("HFR 1.23 · FWHM 2.89")).toBeInTheDocument();
    expect(screen.getByText("dither")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Most recent captured frame" })).toHaveAttribute(
      "src",
      "/api/capture/frames/42/image",
    );
    act(() => { useConnectionStore.getState().setStatus("closed"); });
    expect(screen.queryByText("Tracking")).not.toBeInTheDocument();
    expect(screen.getByText("12 accepted · 2 rejected")).toBeInTheDocument();
    expect(screen.getByText(/Disconnected.*Last reading/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeDisabled();

  });
});
