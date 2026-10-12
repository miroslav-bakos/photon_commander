// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { testSnapshot } from "../../test/fixtures";
import { useConnectionStore } from "../../store/connectionStore";
import { NightReportPanel } from "./NightReportPanel";

const overrides = vi.hoisted(() => ({ latest: null as unknown, journal: null as unknown, pending: false, error: false, requested: "" }));

vi.mock("../../api/runs", () => ({
  useRunDetailQuery: (runId: string) => { overrides.requested = runId; return ({
    isPending: overrides.pending,
    isError: overrides.error,
    data: overrides.pending ? undefined : {
      run: overrides.latest ?? { run_id: "run-1", plan_name: "M31 mosaic", night_date: "2026-09-22", status: "running", plan_revision: 1, ended_reason: null },
      capture_summary: { saved_frames: 120, pending_frames: 1, captured_integration_s: 3600, unknown_exposure_frames: 0, accepted_frames: 110, rejected_frames: 3, stack_error_frames: 1, stack_unmeasured_frames: 6, accepted_integration_s: 3300 },
      final_mount: null,
      journal: overrides.journal ?? [{
        seq: 1,
        at: "2026-09-22T21:11:00Z",
        kind: "effect_completed",
        reason: "cloud gate held the next panel",
        item_id: "panel-2",
        panel_index: 1,
        attempt: 1,
        step: 3,
        status: "failed",
        held: true,
        effect: { name: "center_target", args: {}, completion: "event", event_kind: null, deadline_s: null },
        effect_outcome: "failed",
        control: null,
        run_status: "running",
      }],
    },
  }); },
}));

afterEach(() => {
  useConnectionStore.getState().reset();
  overrides.latest = null;
  overrides.journal = null;
  overrides.pending = false;
  overrides.error = false;
});

describe("NightReportPanel", () => {
  it("replays durable run events together with captured frames", () => {
    useConnectionStore.setState({
      snapshot: testSnapshot({
        run: {
          run_id: "run-1",
          plan_id: "plan-1",
          plan_name: "M31 mosaic",
          plan_revision: 1,
          night_date: "2026-09-22",
          status: "running",
          started_at: "2026-09-22T21:00:00Z",
          ended_at: null,
          ended_reason: null,
          current_item_id: "panel-2",
          items: [],
          gate_held: true,
          gate_reason: "cloud",
          last_decision: null,
          last_decision_at: null,
          journal_length: 1,
          available_actions: ["pause", "stop"],
        },
      }),
    });

    render(<NightReportPanel runId="run-1" />);
    expect(screen.getByText(/120 saved frames · 1 recorded failures · running/i)).toBeInTheDocument();
    expect(screen.getByText(/center target/i)).toBeInTheDocument();
    expect(screen.getByText("55.0 min recorded")).toBeInTheDocument();
    expect(screen.getByText(/Unknown — no fresh reading recorded/)).toBeInTheDocument();
    expect(screen.getByText(/cloud gate held the next panel/i)).toBeInTheDocument();
  });
});

const pastRun = { run_id: "persisted-night", plan_name: "Persisted night", night_date: "2026-09-22", status: "completed" };

it("shows a completed persisted night when restart leaves no live run", () => {
  overrides.latest = pastRun;
  render(<NightReportPanel runId="run-1" />);
  expect(screen.getByText(/Persisted night/)).toBeInTheDocument();
  expect(screen.getByText(/120 saved frames/)).toBeInTheDocument();
});

it.each(["skip", "retry"])("ends recorded recovery time at %s", (control) => {
  overrides.latest = pastRun;
  overrides.journal = [
    { seq: 1, at: "2026-09-22T21:00:00Z", kind: "item_transition", item_id: "held", held: true },
    { seq: 2, at: "2026-09-22T21:01:00Z", kind: "control", item_id: "held", held: null, control },
    { seq: 3, at: "2026-09-22T21:10:00Z", kind: "run_ended", item_id: null, held: null },
  ];
  render(<NightReportPanel runId="run-1" />);
  expect(screen.getByText("1.0 min")).toBeInTheDocument();
});

it("requests the explicitly selected historical run without a live snapshot", () => {
  render(<NightReportPanel runId="selected-historical-run" />);
  expect(overrides.requested).toBe("selected-historical-run");
});

it("shows report loading before any totals", () => {
  overrides.pending = true;
  render(<NightReportPanel runId="history" />);
  expect(screen.getByText("Loading the night report…")).toBeInTheDocument();
  expect(screen.queryByText(/saved frames/)).not.toBeInTheDocument();
});

it("hides cached totals when the historical report request fails", () => {
  overrides.error = true;
  render(<NightReportPanel runId="history" />);
  expect(screen.getByRole("alert")).toHaveTextContent("Night report unavailable");
  expect(screen.queryByText(/120 saved frames/)).not.toBeInTheDocument();
});
