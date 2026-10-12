// SPDX-License-Identifier: GPL-3.0-only
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { HistoricalNightPanel } from "./HistoricalNightPanel";

const summary = {
  saved_frames: 380, pending_frames: 0, captured_integration_s: 22800,
  accepted_integration_s: null, rejected_integration_s: null,
  stack_unmeasured_frames: 380, unknown_exposure_frames: 0,
};
const report = {
  night_date: "2026-10-06", lights: summary,
  calibration: { ...summary, saved_frames: 0, captured_integration_s: 0, stack_unmeasured_frames: 0 },
  objects: [{ object_name: "M31", lights: summary }],
  executions: [{
    run: { run_id: "original-run", plan_revision: 64, status: "completed", started_at: "2026-10-06T20:00:00Z" },
    plan: { name: "Original night", items: [{ id: "m31", kind: "goto_and_stack", filter_name: "UVIRCUT", exposure_sec: 60, gain: 200 }] },
    schedule: { night: { tz: "Europe/Bratislava" }, items: [{
      item_id: "m31", kind: "goto_and_stack", enabled: true, target: { name: "M31" }, panels: [],
      start_utc: "2026-10-06T20:00:00Z", end_utc: "2026-10-07T02:20:00Z",
    }] },
    items: [{ item_id: "m31", lights: summary, panel_attribution_available: false }],
  }],
};

afterEach(() => { vi.unstubAllGlobals(); });

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

describe("HistoricalNightPanel", () => {
  it("uses the night aggregate and immutable snapshot without fetching the edited current plan", async () => {
    const fetch = vi.fn((request: Request) => {
      expect(new URL(request.url).pathname).toBe("/api/runs/nights/2026-10-06");
      return Promise.resolve(response(report));
    });
    vi.stubGlobal("fetch", fetch);
    renderWithQueryClient(<HistoricalNightPanel nightDate="2026-10-06" />);
    expect(await screen.findByText("Complete observing night")).toBeInTheDocument();
    expect(screen.getByText("Original night · Revision 64 · completed")).toBeInTheDocument();
    expect(screen.getAllByText("380.0 min").length).toBeGreaterThan(0);
    expect(screen.getByText(/Current conditions and forecasts are not substituted/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Original night · Revision 64 · completed"));
    expect(screen.getByText("Filter: UVIRCUT")).toBeInTheDocument();
    expect(screen.getByText("Captured panel attribution was not recorded.")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("recovers from a failed night read through the visible retry button", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new Error("Network lost")).mockResolvedValue(response(report)));
    renderWithQueryClient(<HistoricalNightPanel nightDate="2026-10-06" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the complete night report.");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Complete observing night")).toBeInTheDocument();
  });

  it("shows an empty night without implying captured data", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ ...report, executions: [] })));
    renderWithQueryClient(<HistoricalNightPanel nightDate="2026-10-06" />);
    expect(await screen.findByText("No recorded execution for this night.")).toBeInTheDocument();
    expect(screen.queryByText("Captured targets")).not.toBeInTheDocument();
  });
});
