// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { AutofocusChart } from "./AutofocusChart";
import { useQueryClient } from "@tanstack/react-query";
import { fireEvent } from "@testing-library/react";
import { setLanguage } from "../../i18n/locale";

function RefreshTelemetry() {
  const client = useQueryClient();
  return <button onClick={() => { void client.refetchQueries(); }}>Refresh telemetry</button>;
}

const availability = vi.hoisted(() => ({ connected: true }));
vi.mock("../../api/deviceAvailability", () => ({ useAlpacaDeviceConnected: () => availability.connected }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  availability.connected = true;
  setLanguage("en");
});

describe("AutofocusChart", () => {
  it.each(["sk", "cs"] as const)("translates compound measurement statuses in %s", async (language) => {
    setLanguage(language);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: "running", stage: "coarse",
      samples: [{ position: 1200, hfr_px: 1.6, star_count: 24, stage: "coarse" }],
      curve: [], candidate_position: null, final_position: null, error: null,
    }), { status: 200, headers: { "content-type": "application/json" } })));
    renderWithQueryClient(<AutofocusChart />);
    expect(await screen.findByText(language === "sk" ? /Meranie hrubého kroku/ : /Měření hrubého kroku/)).toBeInTheDocument();
    expect(screen.queryByText(/Measuring coarse/)).not.toBeInTheDocument();
  });
  it("marks restored interrupted measurements as unverified rather than completed", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: "interrupted", stage: null,
      samples: [{ position: 1200, hfr_px: 1.6, star_count: 24, stage: "fine" }],
      curve: [], candidate_position: 1200, final_position: null, error: null,
    }), { status: 200, headers: { "content-type": "application/json" } })));
    renderWithQueryClient(<AutofocusChart />);
    expect(await screen.findByRole("alert")).toHaveTextContent("current focus position is unverified");
    expect(screen.getByText(/Interrupted · 1 points/)).toBeInTheDocument();
    expect(screen.queryByText(/Applied the validated/)).not.toBeInTheDocument();
  });
  it("plots live samples, the fitted curve and candidate position", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((request: Request) => {
      const pathname = new URL(request.url).pathname;
      expect(pathname).toBe("/api/focuser/autofocus");
      return new Response(JSON.stringify({
        status: "running",
        stage: "coarse",
        samples: [
          { position: 1190, hfr_px: 2.4, star_count: 18, stage: "coarse" },
          { position: 1200, hfr_px: 1.6, star_count: 24, stage: "coarse" },
          { position: 1210, hfr_px: 2.2, star_count: 20, stage: "coarse" },
        ],
        curve: [
          { position: 1190, hfr_px: 2.4 },
          { position: 1200, hfr_px: 1.6 },
          { position: 1210, hfr_px: 2.2 },
        ],
        candidate_position: 1201,
        final_position: null,
        error: null,
      }), { status: 200, headers: { "content-type": "application/json" } });
    }));

    const { container } = renderWithQueryClient(<AutofocusChart />);

    await screen.findByRole("img", { name: "Autofocus curve with 3 measured points" });
    expect(container.querySelectorAll("[data-focus-sample]")).toHaveLength(3);
    expect(container.querySelector("[data-focus-curve]")).toBeInTheDocument();
    expect(container.querySelector("[data-candidate]")).toBeInTheDocument();
    expect(screen.getByText(/Measuring coarse/)).toBeInTheDocument();
  });

  it("shows an honest empty state before autofocus has run", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: "idle",
      stage: null,
      samples: [],
      curve: [],
      candidate_position: null,
      final_position: null,
      error: null,
    }), { status: 200, headers: { "content-type": "application/json" } })));

    renderWithQueryClient(<AutofocusChart />);

    expect(await screen.findByText(/Start autofocus to plot/)).toBeInTheDocument();
  });

  it("reads retained telemetry while the focuser is disconnected", async () => {
    availability.connected = false;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: "completed", stage: null,
      samples: [{ position: 1200, hfr_px: 1.6, star_count: 24, stage: "control" }],
      curve: [], candidate_position: 1200, final_position: 1200, error: null,
    }), { status: 200, headers: { "content-type": "application/json" } })));
    renderWithQueryClient(<AutofocusChart />);
    expect(await screen.findByRole("img", { name: "Autofocus curve with 1 measured points" })).toBeInTheDocument();
  });

  it("shows a failure before the first measurable sample", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: "failed", stage: "baseline", samples: [], curve: [],
      candidate_position: null, final_position: null, error: "No measurable stars",
    }), { status: 200, headers: { "content-type": "application/json" } })));
    renderWithQueryClient(<AutofocusChart />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No measurable stars");
  });

  it("keeps received measurements visible when a refresh fails", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        status: "completed", stage: null,
        samples: [{ position: 1200, hfr_px: 1.6, star_count: 24, stage: "control" }],
        curve: [], candidate_position: 1200, final_position: 1200, error: null,
      }), { status: 200, headers: { "content-type": "application/json" } }))
      .mockRejectedValue(new Error("Network unavailable")));
    renderWithQueryClient(<><AutofocusChart /><RefreshTelemetry /></>);
    await screen.findByRole("img", { name: "Autofocus curve with 1 measured points" });
    fireEvent.click(screen.getByRole("button", { name: "Refresh telemetry" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Showing the last received measurements");
    expect(screen.getByRole("img", { name: "Autofocus curve with 1 measured points" })).toBeInTheDocument();
  });
});
