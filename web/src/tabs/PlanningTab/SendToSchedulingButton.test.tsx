// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { EventTimeModel, MosaicPanelModel } from "../../api/astro";
import type { TargetIntent } from "../../api/plans";
import { useConnectionStore } from "../../store/connectionStore";
import { testSnapshot } from "../../test/fixtures";
import { renderWithQueryClient } from "../../test/queryClient";
import { useSchedulingUiStore } from "../SchedulingTab/schedulingUiStore";
import { SendToSchedulingButton } from "./SendToSchedulingButton";

const intent: TargetIntent = {
  name: "IC 1396",
  ra_j2000_h: 21.6493,
  dec_j2000_deg: 57.4891,
  framing: { mode: "eq", pa_offset_deg: 0, lens: "tele" },
  mosaic: { ra_num: 2, dec_num: 1, overlap_pct: 20 },
  constraints: { min_alt_deg: 25, min_moon_sep_deg: null },
  panels: [],
};

const panels = [
  { center: { row: 0, col: 0, ra_j2000_h: 21.61, dec_j2000_deg: 57.48 } },
  { center: { row: 0, col: 1, ra_j2000_h: 21.69, dec_j2000_deg: 57.49 } },
] as MosaicPanelModel[];
const solarEvents = [
  { event: "astronomical_dusk", when: "2026-09-15T21:00:00Z", outcome: "occurs", threshold_deg: -18 },
  { event: "astronomical_dawn", when: "2026-09-16T03:30:00Z", outcome: "occurs", threshold_deg: -18 },
] as EventTimeModel[];

describe("SendToSchedulingButton", () => {
  afterEach(() => {
    useConnectionStore.getState().reset();
    useSchedulingUiStore.setState({ selectedPlanId: null, selectedItemId: null, selectedNightDate: null });
  });

  it("always sends exact mosaic panel centres as separate items", async () => {
    useConnectionStore.setState({
      snapshot: testSnapshot({
        plans: [{ id: "p1", name: "Tonight", revision: 1, updated_at: "2026-09-15T12:00:00Z", night_date: "2026-09-15" }],
      }),
    });
    useSchedulingUiStore.getState().selectPlan("p1");
    let posted: unknown = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      const url = new URL(request.url, "http://localhost");
      if (request.method === "GET" && url.pathname === "/api/plans") {
        return Response.json({ plans: [] });
      }
      if (request.method === "POST" && url.pathname === "/api/plans/actions/schedule-intent") {
        posted = await request.json();
        return Response.json({
          document: {
            schema_version: 7,
            id: "p1",
            name: "Tonight",
            items: [],
            created_at: "2026-09-15T12:00:00Z",
            updated_at: "2026-09-15T12:00:00Z",
          },
          revision: 2,
          item_id: "panel-1",
        });
      }
      return Response.json({ detail: "unexpected request" }, { status: 500 });
    }));

    renderWithQueryClient(
      <MemoryRouter>
        <SendToSchedulingButton intent={intent} nightLabel="2026-09-15" mosaicPanels={panels} solarEvents={solarEvents} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Schedule" }));

    await waitFor(() => {
      expect(posted).toMatchObject({
        plan_id: null,
        night_date: "2026-09-15",
        new_plan: { name: "Plan for 2026-09-15", items: [] },
        duration_min: 195,
        intent: {
          panels: panels.map(({ center }) => center),
        },
      });
    });
    expect(useSchedulingUiStore.getState().selectedNightDate).toBe("2026-09-15");
  });

  it("does not invent equal panel durations when astronomical darkness is absent", () => {
    renderWithQueryClient(<MemoryRouter>
      <SendToSchedulingButton intent={intent} nightLabel="2026-09-15" mosaicPanels={panels} solarEvents={solarEvents.map((event) => ({
        ...event,
        when: null,
        outcome: "always_above",
      }))} />
    </MemoryRouter>);
    expect(screen.getByRole("button", { name: "Schedule" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Astronomical dusk and dawn must both occur");
  });

  it("waits for resolved panel centres instead of sending a combined mosaic", () => {
    renderWithQueryClient(<MemoryRouter>
      <SendToSchedulingButton intent={intent} nightLabel="2026-09-15" solarEvents={solarEvents} />
    </MemoryRouter>);
    expect(screen.getByRole("button", { name: "Schedule" })).toBeDisabled();
    expect(screen.getByText("Mosaic panels are still resolving.")).toBeInTheDocument();
  });

  it("keeps a single-field capture on its astronomical dusk-to-dawn event window", async () => {
    let posted: unknown = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      const pathname = new URL(request.url, "http://localhost").pathname;
      if (request.method === "POST" && pathname === "/api/plans/actions/schedule-intent") {
        posted = await request.json();
        return Response.json({
          document: { schema_version: 7, id: "night", name: "Plan for 2026-09-15", items: [],
            created_at: "2026-09-15T12:00:00Z", updated_at: "2026-09-15T12:00:00Z" },
          revision: 1, item_id: "single",
        });
      }
      return Response.json({ plans: [] });
    }));
    renderWithQueryClient(<MemoryRouter>
      <SendToSchedulingButton intent={{ ...intent, mosaic: { ...intent.mosaic, ra_num: 1 } }} nightLabel="2026-09-15" />
    </MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Schedule" }));
    await waitFor(() => {
      expect(posted).not.toBeNull();
    });
    expect(posted).not.toHaveProperty("duration_min");
  });

  it("reuses only the plan matching the planning night, even when another plan is selected", async () => {
    useConnectionStore.setState({
      snapshot: testSnapshot({
        plans: [
          { id: "other", name: "Plan for 2026-09-19", revision: 2, updated_at: "2026-09-19T12:00:00Z", night_date: "2026-09-19" },
          { id: "night", name: "Plan for 2026-09-18", revision: 1, updated_at: "2026-09-18T12:00:00Z", night_date: "2026-09-18" },
        ],
      }),
    });
    useSchedulingUiStore.getState().selectPlan("other");
    let posted: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      if (request.method === "POST" && new URL(request.url, "http://localhost").pathname === "/api/plans/actions/schedule-intent") {
        posted = await request.json() as Record<string, unknown>;
        return Response.json({
          document: { schema_version: 7, id: "night", name: "Plan for 2026-09-18", items: [],
            created_at: "2026-09-18T12:00:00Z", updated_at: "2026-09-18T12:00:00Z" },
          revision: 2, item_id: "capture",
        });
      }
      return Response.json({ plans: [] });
    }));
    renderWithQueryClient(<MemoryRouter>
      <SendToSchedulingButton intent={intent} nightLabel="2026-09-18" mosaicPanels={panels} solarEvents={solarEvents} />
    </MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Schedule" }));
    await waitFor(() => {
      expect(posted).toMatchObject({ plan_id: "night", night_date: "2026-09-18" });
    });
    expect(posted).not.toHaveProperty("new_plan");
    expect(useSchedulingUiStore.getState().selectedNightDate).toBe("2026-09-18");
  });
});
