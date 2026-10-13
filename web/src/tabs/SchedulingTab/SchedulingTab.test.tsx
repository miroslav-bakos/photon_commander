// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The one behaviour worth pinning at the tab level: a `PUT` refused with
 * `409` (someone else saved first) shows the conflict notice and refetches
 * the stored plan — and never retries with the new revision, which would
 * be the silent overwrite the revision exists to prevent.
 *
 * Exercised through the real `api/plans.ts` hooks against a stubbed
 * `fetch`, so the status handling, `unwrap` and the conflict predicate are
 * all under test — not a mocked hook.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { LayoutRouteProvider } from "../../layout/LayoutContext";
import { useLayoutStore } from "../../layout/layoutStore";
import { setLanguage, translate } from "../../i18n/locale";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PlanCompileResponse, PlanRecord } from "../../api/plans";
import type { RunState } from "../../api/runs";
import { useConnectionStore } from "../../store/connectionStore";
import { testSnapshot } from "../../test/fixtures";
import { renderWithQueryClient } from "../../test/queryClient";
import { SchedulingTab } from "./SchedulingTab";
import { useSchedulingUiStore } from "./schedulingUiStore";

afterEach(cleanup);
afterEach(() => { act(() => { setLanguage("en"); }); });
afterEach(() => { useLayoutStore.getState().resetAll(); });

const record: PlanRecord = {
  document: {
    schema_version: 7,
    id: "p1",
    name: "Night of 2026-08-28",
    items: [
      {
        id: "w1",
        kind: "wait",
        start: { kind: "event", event: "sunset", offset_min: 0 },
        end: { kind: "duration", minutes: 5 },
        enabled: true,
        requires_confirmation: false,
      },
    ],
    created_at: "2026-08-28T10:00:00Z",
    updated_at: "2026-08-28T10:00:00Z",
  },
  revision: 1,
  created_at: "2026-08-28T10:00:00Z",
  updated_at: "2026-08-28T10:00:00Z",
  night_date: "2026-08-28",
};

const waitItem = record.document.items[0];
if (waitItem === undefined) throw new Error("Missing wait fixture");

const compiled: PlanCompileResponse = {
  schedule: {
    plan_id: "p1",
    plan_revision: "abc",
    compiled_at: "2026-08-28T12:00:00Z",
    night: {
      date: "2026-08-28",
      tz: "Europe/London",
      window_start_utc: "2026-08-28T11:00:00Z",
      window_end_utc: "2026-08-29T11:00:00Z",
      dark_start_utc: "2026-08-28T21:00:00Z",
      dark_end_utc: "2026-08-29T03:30:00Z",
      solar_events: [],
      lunar_events: [],
    },
    items: [
      {
        item_id: "w1",
        kind: "wait",
        enabled: true,
        start_utc: "2026-08-28T19:00:00Z",
        end_utc: "2026-08-28T19:05:00Z",
        resolution: "Sunset → 20:00",
        end_resolution: "Duration 5 min → 20:05",
        panels: [],
        diagnostics: [],
      },
    ],
  },
  diagnostics: [{ severity: "warning", item_id: null, code: "missing_shutdown", message: "the plan does not end with a shutdown item" }],
};

const observingLocation = {
  latitude_deg: 51.5,
  longitude_deg: -0.1,
  elevation_m: 20,
  timezone: "Europe/London",
  source: "manual" as const,
  accuracy_m: null,
  acquired_at: "2026-08-28T00:00:00Z",
};

function json(body: unknown, status: number, url: string): Response {
  const response = new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  Object.defineProperty(response, "url", { value: `http://localhost${url}` });
  return response;
}

/** Route the stubbed fetch by method + path; count the plan GETs. */
function stubFetch(
  onPut: () => Response,
  extra?: (request: Request) => Response | null | Promise<Response | null>,
) {
  const gets = { plan: 0 };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      const { pathname } = new URL(request.url);
      const method = request.method;
      const extraResponse = await extra?.(request);
      if (extraResponse != null) return extraResponse;
      if (method === "GET" && pathname === "/api/plans") return json({ plans: [] }, 200, pathname);
      if (method === "GET" && pathname === "/api/plans/p1") {
        gets.plan += 1;
        return json(record, 200, pathname);
      }
      if (method === "GET" && pathname === "/api/astro/profiles") return json({ profiles: [] }, 200, pathname);
      if (method === "POST" && pathname === "/api/plans/validate") {
        return json({ diagnostics: compiled.diagnostics }, 200, pathname);
      }
      if (method === "POST" && pathname === "/api/plans/p1/compile") return json(compiled, 200, pathname);
      if (method === "PUT" && pathname === "/api/plans/p1") return onPut();
      return json({ detail: `unexpected ${method} ${pathname}` }, 500, pathname);
    }),
  );
  return gets;
}

const runningRun: RunState = {
  run_id: "run-1",
  plan_id: "p1",
  plan_name: "Night of 2026-08-28",
  plan_revision: 1,
  night_date: "2026-08-28",
  status: "running",
  started_at: "2026-08-28T18:00:00Z",
  ended_at: null,
  ended_reason: null,
  current_item_id: "w1",
  items: [
    {
      item_id: "w1",
      kind: "wait",
      enabled: true,
      status: "waiting",
      attempt: 0,
      panel_index: null,
      held: false,
      reason: "",
      start_utc: "2026-08-28T19:00:00Z",
      end_utc: "2026-08-28T19:05:00Z",
    },
  ],
  gate_held: false,
  gate_reason: null,
  last_decision: null,
  last_decision_at: null,
  journal_length: 1,
  available_actions: ["pause", "stop"],
};

describe("SchedulingTab", () => {
  beforeEach(() => {
    useSchedulingUiStore.setState({ editLocks: {} });
    useConnectionStore.getState().reset();
    useConnectionStore.setState({
      snapshot: testSnapshot({
        location: observingLocation,
        plans: [{ id: "p1", name: "Night of 2026-08-28", revision: 1, updated_at: "2026-08-28T10:00:00Z", night_date: "2026-08-28" }],
      }),
    });
    useSchedulingUiStore.getState().select("p1", "w1");
  });

  it("persists chronological order when a start is changed in the item editor", async () => {
    const first = { ...waitItem, start: { kind: "absolute" as const, local_datetime: "2026-08-28T20:00:00", fold: 0 as const } };
    const second = { ...first, id: "w2", start: { kind: "absolute" as const, local_datetime: "2026-08-28T21:00:00", fold: 0 as const } };
    let saved: PlanRecord = { ...record, document: { ...record.document, items: [first, second] } };
    let written = false;
    stubFetch(() => { throw new Error("unexpected PUT fallback"); }, async (request) => {
      const path = new URL(request.url).pathname;
      if (path === "/api/plans/p1" && request.method === "GET") return json(saved, 200, path);
      if (path === "/api/plans/p1" && request.method === "PUT") {
        const body = await request.json() as { document: PlanRecord["document"] };
        saved = { ...saved, document: body.document, revision: 2 };
        written = true;
        return json(saved, 200, path);
      }
      if (path === "/api/plans/p1/compile") return json({ ...compiled, schedule: { ...compiled.schedule,
        items: [...compiled.schedule.items, { ...compiled.schedule.items[0], item_id: "w2", start_utc: "2026-08-28T20:00:00Z", end_utc: "2026-08-28T20:05:00Z" }],
      } }, 200, path);
      return null;
    });
    renderWithQueryClient(<SchedulingTab />);
    const input = await screen.findByLabelText("Local date and time");
    await screen.findByTestId("resolved-time");
    fireEvent.change(input, { target: { value: "2026-08-28T22:00" } });
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => { expect(written).toBe(true); });
    expect(saved.document.items.map((item) => item.id)).toEqual(["w2", "w1"]);
  });

  it.each(["running", "paused"] as const)("automatically locks a %s plan and enables edits only after Unlock", async (status) => {
    useConnectionStore.setState({ snapshot: testSnapshot({ location: observingLocation,
      plans: [{ id: "p1", name: record.document.name, revision: 1, updated_at: record.updated_at, night_date: record.night_date }], run: { ...runningRun, status } }) });
    const put = vi.fn(() => json(record, 200, "/api/plans/p1"));
    stubFetch(put);
    const user = userEvent.setup();
    renderWithQueryClient(<SchedulingTab />);
    await screen.findByTestId("resolved-time");
    expect(screen.getByRole("button", { name: "Unlock plan" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Duration (minutes)")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reorder item 1" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Disable item 1" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Rename" })).toBeDisabled();
    expect(screen.getByLabelText("Night of")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Duration (minutes)"), { target: { value: "10" } });
    expect(put).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Unlock plan" }));
    expect(screen.getByLabelText("Duration (minutes)")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reorder item 1" })).toBeEnabled();
    expect(screen.getByLabelText("Night of")).toBeEnabled();
    expect(screen.getByTestId("block-move-handle")).toBeInTheDocument();
    act(() => {
      useConnectionStore.setState({ snapshot: testSnapshot({ location: observingLocation,
        plans: [{ id: "p1", name: record.document.name, revision: 1, updated_at: record.updated_at, night_date: record.night_date }],
        run: { ...runningRun, run_id: "run-2" } }) });
    });
    expect(screen.getByLabelText("Duration (minutes)")).toBeDisabled();
    expect(screen.getByTestId("block-move-handle")).toHaveStyle({ cursor: "pointer" });
    await user.click(screen.getByRole("button", { name: "Unlock plan" }));
    await user.click(screen.getByRole("button", { name: "Lock plan" }));
    expect(screen.getByLabelText("Duration (minutes)")).toBeDisabled();
    expect(put).not.toHaveBeenCalled();
  });

  it("blocks pointer moves and keyboard reorder while locked and saves reorder after Unlock", async () => {
    const twoItems = { ...record, document: { ...record.document,
      items: [waitItem, { ...waitItem, id: "w2" }] } };
    let writtenIds: string[] | null = null;
    stubFetch(() => { throw new Error("unexpected PUT fallback"); }, async (request) => {
      const path = new URL(request.url).pathname;
      if (path === "/api/plans/p1" && request.method === "GET") return json(twoItems, 200, path);
      if (path === "/api/plans/p1" && request.method === "PUT") {
        const body = await request.json() as { document: { items: { id: string }[] } };
        writtenIds = body.document.items.map((item) => item.id);
        return json(twoItems, 200, path);
      }
      return null;
    });
    const user = userEvent.setup();
    renderWithQueryClient(<SchedulingTab />);
    await screen.findByTestId("resolved-time");
    await user.click(screen.getByRole("button", { name: "Lock plan" }));
    const handle = screen.getByTestId("block-move-handle");
    fireEvent.pointerDown(handle, { clientX: 100, clientY: 10, button: 0, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 200, clientY: 10, pointerId: 1 });
    fireEvent.pointerUp(handle, { clientX: 200, clientY: 10, pointerId: 1 });
    fireEvent.keyDown(screen.getByRole("button", { name: "Reorder item 1" }), { altKey: true, key: "ArrowDown" });
    expect(writtenIds).toBeNull();
    await user.click(screen.getByRole("button", { name: "Unlock plan" }));
    fireEvent.keyDown(screen.getByRole("button", { name: "Reorder item 1" }), { altKey: true, key: "ArrowDown" });
    await waitFor(() => { expect(writtenIds).toEqual(["w2", "w1"]); });
  });

  it("locks an idle plan manually, protects a draft and relocks when execution begins", async () => {
    stubFetch(() => json(record, 200, "/api/plans/p1"));
    const user = userEvent.setup();
    renderWithQueryClient(<SchedulingTab />);
    await screen.findByTestId("resolved-time");
    const duration = screen.getByLabelText("Duration (minutes)");
    await user.clear(duration);
    await user.type(duration, "10");
    await user.tab();
    await user.click(screen.getByRole("button", { name: "Lock plan" }));
    expect(duration).toHaveValue(10);
    expect(duration).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Unlock plan" }));
    expect(duration).toHaveValue(10);
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    act(() => {
      useConnectionStore.setState({ snapshot: testSnapshot({ location: observingLocation,
        plans: [{ id: "p1", name: record.document.name, revision: 1, updated_at: record.updated_at, night_date: record.night_date }], run: runningRun }) });
    });
    expect(duration).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("keeps a locked editor disabled after its card is moved to another panel", async () => {
    useConnectionStore.setState({ snapshot: testSnapshot({ location: observingLocation,
      plans: [{ id: "p1", name: record.document.name, revision: 1, updated_at: record.updated_at, night_date: record.night_date }], run: runningRun }) });
    useLayoutStore.getState().placeCards("/scheduling", "primary", ["wait-operator-checkpoint"]);
    stubFetch(() => json(record, 200, "/api/plans/p1"));
    renderWithQueryClient(<MemoryRouter initialEntries={["/scheduling"]}>
      <LayoutRouteProvider><SchedulingTab /></LayoutRouteProvider>
    </MemoryRouter>);
    await screen.findByTestId("resolved-time");
    const duration = screen.getByLabelText("Duration (minutes)");
    expect(duration.closest("[data-layout-card-id]")).toHaveAttribute("data-layout-panel", "primary");
    expect(duration).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete item" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Unlock plan" }));
    expect(duration).toBeEnabled();
  });

  it("shows the missing-shutdown warning and the resolved start inline", async () => {
    stubFetch(() => json(record, 200, "/api/plans/p1"));
    renderWithQueryClient(<SchedulingTab />);
    expect(await screen.findByText(/does not end with a shutdown item/, {}, { timeout: 3000 })).toBeInTheDocument();
    expect(await screen.findByTestId("resolved-time")).toHaveTextContent(/^Sunset → \d{2}:\d{2}$/);
    expect(screen.queryByRole("button", { name: /Create M31 first-light night/ })).toBeNull();
  });

  it("keeps the unsaved marker and start guard when its editor is collapsed", async () => {
    const user = userEvent.setup();
    stubFetch(() => json(record, 200, "/api/plans/p1"));
    renderWithQueryClient(<SchedulingTab />);
    const duration = await screen.findByLabelText("Duration (minutes)");
    await user.clear(duration);
    await user.type(duration, "12");
    await user.click(screen.getByRole("button", { name: "Edit item 1" }));
    expect(screen.queryByLabelText("Duration (minutes)")).toBeNull();
    expect(screen.getByTestId("row-w1")).toHaveTextContent("unsaved");
    expect(screen.getByRole("button", { name: "Start selected plan" })).toBeDisabled();
  });

  it("does not refetch a saved revision because its websocket summary is still older", async () => {
    const user = userEvent.setup();
    const gets = stubFetch(() => json({ ...record, revision: 2 }, 200, "/api/plans/p1"));
    renderWithQueryClient(<SchedulingTab />);
    const duration = await screen.findByLabelText("Duration (minutes)");
    await user.clear(duration);
    await user.type(duration, "12");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => { expect(screen.queryByText("Unsaved changes")).toBeNull(); });
    expect(gets.plan).toBe(1);
    // A genuinely newer snapshot still refreshes the stored document.
    useConnectionStore.setState({ snapshot: testSnapshot({
      location: observingLocation,
      plans: [{ id: "p1", name: record.document.name, revision: 3, updated_at: record.document.updated_at, night_date: "2026-08-28" }],
    }) });
    await waitFor(() => { expect(gets.plan).toBeGreaterThan(1); });
  });

  it("reuses the saved plan atomically when Night of changes", async () => {
    let scheduledBody: unknown = null;
    stubFetch(
      () => json(record, 200, "/api/plans/p1"),
      async (request) => {
        if (request.method !== "POST" || new URL(request.url).pathname !== "/api/plans/p1/apply-night") return null;
        scheduledBody = await request.json();
        return json({ ...record, night_date: "2026-09-24" }, 200, "/api/plans/p1/apply-night");
      },
    );
    renderWithQueryClient(<SchedulingTab />);

    const dateField = await screen.findByLabelText("Night of");
    await waitFor(() => { expect(dateField).toBeEnabled(); });
    fireEvent.change(dateField, { target: { value: "2026-09-24" } });

    await waitFor(() => { expect(scheduledBody).toEqual({ night_date: "2026-09-24", expected_revision: record.revision }); });
    expect(useSchedulingUiStore.getState().selectedNightDate).toBe("2026-09-24");
  });

  it("keeps the displayed night unchanged until reuse succeeds", async () => {
    let releaseSave: (() => void) | undefined;
    const saveGate = new Promise<void>((resolve) => { releaseSave = resolve; });
    let requestStarted = false;
    stubFetch(() => json(record, 200, "/api/plans/p1"), async (request) => {
      if (request.method !== "POST" || new URL(request.url).pathname !== "/api/plans/p1/apply-night") return null;
      requestStarted = true;
      await saveGate;
      return json({ ...record, night_date: "2026-09-24" }, 200, "/api/plans/p1/apply-night");
    });
    renderWithQueryClient(<SchedulingTab />);
    const field = await screen.findByLabelText("Night of");
    await waitFor(() => { expect(field).toBeEnabled(); });
    fireEvent.change(field, { target: { value: "2026-09-24" } });
    await waitFor(() => { expect(requestStarted).toBe(true); });
    expect(field).toHaveValue("2026-08-28");
    expect(field).toBeDisabled();
    expect(screen.getByRole("status", { name: "Applying plan changes" })).toHaveTextContent("Saving changes…");
    releaseSave?.();
    await waitFor(() => { expect(field).toHaveValue("2026-09-24"); });
  });

  it("retains the previous night when reuse fails", async () => {
    stubFetch(() => json(record, 200, "/api/plans/p1"), (request) => {
      if (request.method !== "POST" || new URL(request.url).pathname !== "/api/plans/p1/apply-night") return null;
      return json({ detail: "Someone else changed this plan" }, 409, "/api/plans/p1/apply-night");
    });
    renderWithQueryClient(<SchedulingTab />);
    const field = await screen.findByLabelText("Night of");
    await waitFor(() => { expect(field).toBeEnabled(); });
    fireEvent.change(field, { target: { value: "2026-09-24" } });
    await screen.findByText(/Someone else changed this plan/);
    expect(field).toHaveValue("2026-08-28");
    expect(useSchedulingUiStore.getState().selectedNightDate).not.toBe("2026-09-24");
  });

  it("prevents changing the night while its plan is running", async () => {
    useConnectionStore.setState({ snapshot: testSnapshot({
      location: observingLocation,
      plans: [{ id: "p1", name: record.document.name, revision: 1, updated_at: record.document.updated_at, night_date: "2026-08-28" }],
      run: runningRun,
    }) });
    stubFetch(() => json(record, 200, "/api/plans/p1"));
    renderWithQueryClient(<SchedulingTab />);
    expect(await screen.findByLabelText("Night of")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Use tonight" })).toBeDisabled();
  });

  it("applies the plan to the selected night using the fetched revision", async () => {
    let appliedBody: unknown = null;
    stubFetch(
      () => json(record, 200, "/api/plans/p1"),
      async (request) => {
        if (request.method !== "POST" || new URL(request.url).pathname !== "/api/plans/p1/apply-night") return null;
        appliedBody = await request.json();
        return json({ ...record, revision: record.revision + 1 }, 200, "/api/plans/p1/apply-night");
      },
    );
    renderWithQueryClient(<SchedulingTab />);
    const button = await screen.findByRole("button", { name: "Use plan for selected night" });
    await waitFor(() => { expect(button).toBeEnabled(); });
    fireEvent.click(button);
    await waitFor(() => { expect(appliedBody).toEqual({ night_date: "2026-08-28", expected_revision: record.revision }); });
  });

  it("shows a red warning when astronomical darkness never occurs", async () => {
    stubFetch(
      () => json(record, 200, "/api/plans/p1"),
      (request) => {
        if (request.method !== "POST" || new URL(request.url).pathname !== "/api/plans/p1/compile") return null;
        return json({
          ...compiled,
          schedule: {
            ...compiled.schedule,
            night: {
              ...compiled.schedule.night,
              dark_start_utc: null,
              dark_end_utc: null,
              solar_events: [{ event: "astronomical_midnight", when_utc: null,
                outcome: "always_above", threshold_deg: -18 }],
            },
          },
        }, 200, "/api/plans/p1/compile");
      },
    );
    renderWithQueryClient(<SchedulingTab />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("No astronomical night on this date"));
    expect(screen.getByRole("alert")).toHaveClass("text-danger");
  });

  it("marks primary scheduling controls with phone-safe touch targets", async () => {
    stubFetch(() => json(record, 200, "/api/plans/p1"));
    renderWithQueryClient(<SchedulingTab />);

    const controls = [
      await screen.findByLabelText("Open plan"),
      screen.getByLabelText("Night of"),
      screen.getByRole("button", { name: "New plan" }),
      screen.getByRole("button", { name: "Start selected plan" }),
      await screen.findByLabelText("Add item"),
      screen.getByRole("button", { name: "Add" }),
    ];
    for (const control of controls) {
      expect(control).toHaveClass("min-h-[44px]");
    }
  });

  it("starts the selected plan for the exact night shown by the compiled schedule", async () => {
    const user = userEvent.setup();
    let startBody: unknown = null;
    stubFetch(
      () => json(record, 200, "/api/plans/p1"),
      async (request) => {
        const { pathname } = new URL(request.url);
        if (request.method !== "POST" || pathname !== "/api/runs") return null;
        startBody = await request.json();
        return json({ run: runningRun }, 201, pathname);
      },
    );

    renderWithQueryClient(<SchedulingTab />);
    const start = await screen.findByRole("button", { name: "Start selected plan" });
    await waitFor(() => {
      expect(start).toBeEnabled();
    });
    await user.click(start);

    await waitFor(() => {
      expect(startBody).toEqual({ plan_id: "p1", night_date: "2026-08-28" });
    });
    expect(screen.getByLabelText("Duration (minutes)")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Unlock plan" })).toBeEnabled();
  });

  it.each(["en", "sk", "cs"] as const)("offers a confirmed fresh run in %s when the selected plan has been interrupted", async (locale) => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    let startBody: unknown = null;
    useConnectionStore.setState({
      snapshot: testSnapshot({
        location: observingLocation,
        plans: [{ id: "p1", name: "Night of 2026-08-28", revision: 2, updated_at: "2026-08-28T10:00:00Z", night_date: "2026-08-28" }],
        run: {
          ...runningRun,
          status: "interrupted",
          available_actions: ["resume_interrupted"],
        },
      }),
    });
    stubFetch(
      () => json({ ...record, revision: 2 }, 200, "/api/plans/p1"),
      async (request) => {
        const { pathname } = new URL(request.url);
        if (request.method !== "POST" || pathname !== "/api/runs") return null;
        startBody = await request.json();
        return json({ run: runningRun }, 201, pathname);
      },
    );

    renderWithQueryClient(<SchedulingTab />);
    const restart = await screen.findByRole("button", { name: "Restart plan" });
    await waitFor(() => expect(restart).toBeEnabled());
    act(() => { setLanguage(locale); });
    await user.click(restart);

    expect(confirm).toHaveBeenCalledWith(
      translate("Start a new run of Night of 2026-08-28 for 2026-08-28? The previous run will remain in its history.", locale),
    );
    await waitFor(() => {
      expect(startBody).toEqual({ plan_id: "p1", night_date: "2026-08-28" });
    });
  });

  it.each([
    ["Pause", "/api/runs/run-1/pause", runningRun],
    ["Stop", "/api/runs/run-1/stop", runningRun],
    [
      "Resume",
      "/api/runs/run-1/resume",
      { ...runningRun, status: "paused", available_actions: ["resume", "stop"] } satisfies RunState,
    ],
    [
      "Resume interrupted run",
      "/api/runs/run-1/resume-interrupted",
      { ...runningRun, status: "interrupted", available_actions: ["resume_interrupted"] } satisfies RunState,
    ],
  ])("shows authoritative run timing and sends the %s control", async (label, expectedPath, run) => {
    const user = userEvent.setup();
    let controlPath: string | null = null;
    useConnectionStore.setState({
      snapshot: testSnapshot({
        location: observingLocation,
        plans: [{ id: "p1", name: "Night of 2026-08-28", revision: 1, updated_at: "2026-08-28T10:00:00Z", night_date: "2026-08-28" }],
        run,
      }),
    });
    stubFetch(
      () => json(record, 200, "/api/plans/p1"),
      (request) => {
        const { pathname } = new URL(request.url);
        if (request.method !== "POST" || pathname !== expectedPath) return null;
        controlPath = pathname;
        return json({ run }, 200, pathname);
      },
    );

    renderWithQueryClient(<SchedulingTab />);
    expect(await screen.findByText(/1\. Wait \/ operator checkpoint · waiting/)).toBeInTheDocument();
    if (label === "Resume") await user.click(screen.getByRole("button", { name: "Unlock plan" }));
    await user.click(screen.getByRole("button", { name: label }));
    await waitFor(() => {
      expect(controlPath).toBe(expectedPath);
    });
    if (label.startsWith("Resume")) {
      expect(screen.getByLabelText("Duration (minutes)")).toBeDisabled();
    }
  });

  it("shows the backend's actionable reason when a plan cannot start", async () => {
    const user = userEvent.setup();
    stubFetch(
      () => json(record, 200, "/api/plans/p1"),
      (request) => {
        const { pathname } = new URL(request.url);
        if (request.method !== "POST" || pathname !== "/api/runs") return null;
        return json({ detail: "the telescope is not connected" }, 503, pathname);
      },
    );

    renderWithQueryClient(<SchedulingTab />);
    const start = await screen.findByRole("button", { name: "Start selected plan" });
    await waitFor(() => {
      expect(start).toBeEnabled();
    });
    await user.click(start);

    expect(await screen.findByRole("alert")).toHaveTextContent("the telescope is not connected");
  });

  it("reconciles an applied save whose response was lost without sending another PUT", async () => {
    const user = userEvent.setup();
    let saved = record;
    let writes = 0;
    stubFetch(() => { throw new Error("unexpected fallback"); }, async (request) => {
      if (request.method === "GET" && new URL(request.url).pathname === "/api/plans/p1") return json(saved, 200, "/api/plans/p1");
      if (request.method === "PUT") {
        writes += 1;
        const body = await request.json() as { document: PlanRecord["document"] };
        saved = { ...record, document: body.document, revision: 2 };
        throw new TypeError("Connection lost after server commit");
      }
      return null;
    });
    renderWithQueryClient(<SchedulingTab />);
    const duration = await screen.findByLabelText("Duration (minutes)");
    await user.clear(duration);
    await user.type(duration, "12");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("button", { name: "Check saved plan" })).toBeEnabled();
    expect(duration).toHaveValue(12);
    expect(screen.getByRole("button", { name: "Retry pending change" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Discard pending change" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Check saved plan" }));
    await waitFor(() => { expect(screen.queryByText("Unsaved changes")).toBeNull(); });
    expect(writes).toBe(1);
    expect(duration).toHaveValue(12);
  });

  it("selects a newly saved item when the displayed plan was initially a fallback", async () => {
    const user = userEvent.setup();
    useSchedulingUiStore.setState({ selectedPlanId: null, selectedItemId: null });
    stubFetch(() => { throw new Error("unexpected fallback"); }, async (request) => {
      if (request.method === "PUT") {
        const body = await request.json() as { document: PlanRecord["document"] };
        return json({ ...record, document: body.document, revision: 2 }, 200, "/api/plans/p1");
      }
      return null;
    });
    renderWithQueryClient(<SchedulingTab />);
    await user.selectOptions(await screen.findByLabelText("Add item"), "wait");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => { expect(useSchedulingUiStore.getState().selectedItemId).not.toBeNull(); });
    expect(useSchedulingUiStore.getState().selectedItemId).not.toBe("w1");
    expect(useSchedulingUiStore.getState().selectedPlanId).toBe("p1");
  });

  it("keeps an ambiguous structural delete and reconciles it from the saved document", async () => {
    const user = userEvent.setup();
    let saved = record;
    let writes = 0;
    stubFetch(() => { throw new Error("unexpected fallback"); }, async (request) => {
      if (request.method === "GET" && new URL(request.url).pathname === "/api/plans/p1") return json(saved, 200, "/api/plans/p1");
      if (request.method === "PUT") {
        writes += 1;
        const body = await request.json() as { document: PlanRecord["document"] };
        saved = { ...record, document: body.document, revision: 2 };
        throw new TypeError("Response lost");
      }
      return null;
    });
    renderWithQueryClient(<SchedulingTab />);
    await user.click(await screen.findByRole("button", { name: "Delete item" }));
    await screen.findByRole("button", { name: "Check saved plan" });
    expect(screen.getByRole("button", { name: "Start selected plan" })).toBeDisabled();
    expect(screen.getAllByText("Check the pending plan save before starting.").length).toBeGreaterThan(0);
    await user.click(await screen.findByRole("button", { name: "Check saved plan" }));
    await waitFor(() => { expect(screen.queryByRole("button", { name: "Check saved plan" })).toBeNull(); });
    expect(saved.document.items).toHaveLength(0);
    expect(writes).toBe(1);
  });

  it("checks the pending plan identity even after another plan is opened", async () => {
    const user = userEvent.setup();
    let saved = record;
    const other = { ...record, revision: 7, document: { ...record.document, id: "p2", name: "Other plan" } };
    const snapshot = useConnectionStore.getState().snapshot;
    if (snapshot === null) throw new Error("Missing test snapshot");
    useConnectionStore.setState({ snapshot: { ...snapshot, plans: [...(snapshot.plans ?? []),
      { id: "p2", name: "Other plan", revision: 7, updated_at: other.document.updated_at, night_date: "2026-08-28" }] } });
    stubFetch(() => { throw new Error("unexpected fallback"); }, async (request) => {
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/api/plans/p1") return json(saved, 200, path);
      if (request.method === "GET" && path === "/api/plans/p2") return json(other, 200, path);
      if (request.method === "POST" && path === "/api/plans/p2/compile") return json(compiled, 200, path);
      if (request.method === "PUT") {
        const body = await request.json() as { document: PlanRecord["document"] };
        saved = { ...record, document: body.document, revision: 2 };
        throw new TypeError("Response lost");
      }
      return null;
    });
    renderWithQueryClient(<SchedulingTab />);
    const duration = await screen.findByLabelText("Duration (minutes)");
    await user.clear(duration);
    await user.type(duration, "12");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByRole("button", { name: "Check saved plan" });
    await user.selectOptions(screen.getByLabelText("Open plan"), "p2");
    await user.click(screen.getByRole("button", { name: "Check saved plan" }));
    await waitFor(() => { expect(screen.queryByRole("button", { name: "Check saved plan" })).toBeNull(); });
    expect(screen.getByLabelText("Open plan")).toHaveValue("p2");
  });

  it("refuses to retry an uncertain save against someone else's newer revision", async () => {
    const user = userEvent.setup();
    let saved = record;
    let writes = 0;
    stubFetch(() => { throw new Error("unexpected fallback"); }, (request) => {
      if (request.method === "GET" && new URL(request.url).pathname === "/api/plans/p1") return json(saved, 200, "/api/plans/p1");
      if (request.method === "PUT") {
        writes += 1;
        saved = { ...record, revision: 3 };
        throw new TypeError("Response lost");
      }
      return null;
    });
    renderWithQueryClient(<SchedulingTab />);
    const duration = await screen.findByLabelText("Duration (minutes)");
    await user.clear(duration);
    await user.type(duration, "12");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(await screen.findByRole("button", { name: "Check saved plan" }));
    expect(await screen.findByText(/cannot overwrite the newer revision/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry pending change" })).toBeDisabled();
    expect(duration).toHaveValue(12);
    expect(writes).toBe(1);
  });

  it("retries only on request after a read confirms the original revision", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    stubFetch(() => { throw new Error("unexpected fallback"); }, async (request) => {
      if (request.method === "PUT") {
        const body = await request.json() as { document: PlanRecord["document"]; expected_revision: number };
        bodies.push(body);
        if (bodies.length === 1) throw new TypeError("Request lost before saving");
        return json({ ...record, document: body.document, revision: 2 }, 200, "/api/plans/p1");
      }
      return null;
    });
    renderWithQueryClient(<SchedulingTab />);
    const duration = await screen.findByLabelText("Duration (minutes)");
    await user.clear(duration);
    await user.type(duration, "12");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(await screen.findByRole("button", { name: "Check saved plan" }));
    const retry = screen.getByRole("button", { name: "Retry pending change" });
    await waitFor(() => { expect(retry).toBeEnabled(); });
    expect(bodies).toHaveLength(1);
    await user.click(retry);
    await waitFor(() => { expect(screen.queryByRole("button", { name: "Check saved plan" })).toBeNull(); });
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(bodies[1]).toMatchObject({ expected_revision: 1 });
    expect(duration).toHaveValue(12);
  });

  it("does not change another plan's selection when a recovery read completes late", async () => {
    const user = userEvent.setup();
    let saved = record;
    let wrote = false;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const other = { ...record, document: { ...record.document, id: "p2", name: "Other plan" } };
    stubFetch(() => { throw new Error("unexpected fallback"); }, async (request) => {
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/api/plans/p1") {
        if (wrote) await gate;
        return json(saved, 200, path);
      }
      if (request.method === "GET" && path === "/api/plans/p2") return json(other, 200, path);
      if (request.method === "POST" && path === "/api/plans/p2/compile") return json(compiled, 200, path);
      if (request.method === "PUT") {
        const body = await request.json() as { document: PlanRecord["document"] };
        saved = { ...record, document: body.document, revision: 2 };
        wrote = true;
        throw new TypeError("Response lost");
      }
      return null;
    });
    renderWithQueryClient(<SchedulingTab />);
    await user.click(await screen.findByRole("button", { name: "Delete item" }));
    await user.click(await screen.findByRole("button", { name: "Check saved plan" }));
    await screen.findByRole("button", { name: "Checking saved plan…" });
    act(() => { useSchedulingUiStore.getState().select("p2", "w1"); });
    release();
    await waitFor(() => { expect(screen.queryByRole("button", { name: "Checking saved plan…" })).toBeNull(); });
    expect(useSchedulingUiStore.getState().selectedPlanId).toBe("p2");
    expect(useSchedulingUiStore.getState().selectedItemId).toBe("w1");
  });

  it("on a 409 shows the conflict notice, refetches the plan, and keeps the edit unsaved", async () => {
    const user = userEvent.setup();
    const gets = stubFetch(() => json({ detail: "plan p1 is at revision 2, expected 1" }, 409, "/api/plans/p1"));
    renderWithQueryClient(<SchedulingTab />);

    const duration = await screen.findByLabelText("Duration (minutes)");
    await waitFor(() => {
      expect(gets.plan).toBe(1);
    });
    await user.clear(duration);
    await user.type(duration, "12");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/changed elsewhere/);
    await waitFor(() => {
      expect(gets.plan).toBeGreaterThanOrEqual(2);
    });
    // The edit is still here, still marked, and exactly one PUT was sent —
    // no retry against the new revision.
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(screen.getByLabelText("Duration (minutes)")).toHaveValue(12);
    const fetchMock = vi.mocked(globalThis.fetch);
    const puts = fetchMock.mock.calls.filter(([req]) => (req as Request).method === "PUT");
    expect(puts).toHaveLength(1);
  });

  it("an exposure block set to 'until the block ends' with no end shows the server's ERROR before it can be saved", async () => {
    // `/api/plans/validate` here actually implements the one rule under
    // test (`ssc.plan.validate._exposure_blocks`'s `open_ended_exposure_
    // needs_end`), reading the posted document rather than returning a
    // canned response — so this pins the live wiring (draft → validate →
    // per-item banner), not just that some diagnostic renders.
    const withBlock: PlanRecord = {
      ...record,
      document: {
        ...record.document,
        items: [
          ...record.document.items,
          {
            id: "e1",
            kind: "take_exposures",
            start: { kind: "asap" },
            end: null,
            enabled: true,
            count: { kind: "until_block_end" },
            exposure_sec: 10,
            gain: 80,
            filter_name: "LP",
            frame_type: "light",
            dither: null,
            target: null,
            framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
            constraints: { min_alt_deg: null, min_moon_sep_deg: null },
            live_stack: false,
          },
        ],
      },
    };
    useSchedulingUiStore.getState().select("p1", "e1");

    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (request: Request) => {
        const { pathname } = new URL(request.url);
        const method = request.method;
        if (method === "GET" && pathname === "/api/plans") return json({ plans: [] }, 200, pathname);
        if (method === "GET" && pathname === "/api/plans/p1") return json(withBlock, 200, pathname);
        if (method === "GET" && pathname === "/api/astro/profiles") return json({ profiles: [] }, 200, pathname);
        if (method === "POST" && pathname === "/api/plans/validate") {
          const body = (await request.json()) as {
            document: { items: { id: string; kind: string; end: unknown; count?: { kind: string } }[] };
          };
          const diagnostics = body.document.items
            .filter((it) => it.kind === "take_exposures" && it.count?.kind === "until_block_end" && it.end === null)
            .map((it) => ({
              severity: "error" as const,
              item_id: it.id,
              code: "open_ended_exposure_needs_end",
              message: "this block exposes until its window closes but has no end: give it a duration, or an end at an event",
            }));
          return json({ diagnostics }, 200, pathname);
        }
        if (method === "POST" && pathname === "/api/plans/p1/compile") return json(compiled, 200, pathname);
        return json({ detail: `unexpected ${method} ${pathname}` }, 500, pathname);
      }),
    );

    renderWithQueryClient(<SchedulingTab />);

    // Shown twice by design: once in the plan-wide "Plan check" list, and
    // once scoped to this item inside the editor itself (ItemEditor.tsx) —
    // the point being that it is visible right where the block is being
    // configured, not only in a list the user has to scroll to.
    const matches = await screen.findAllByText(/exposes until its window closes but has no end/);
    expect(matches.length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("error").length).toBeGreaterThanOrEqual(2);
  });
});


it("keeps the compiled preflight valid during a background refresh of the same revision", async () => {
  useConnectionStore.setState({ snapshot: testSnapshot({ location: observingLocation,
    plans: [{ id: "p1", name: record.document.name, revision: 1, updated_at: record.updated_at, night_date: record.night_date }],
    run: runningRun,
  }) });
  useSchedulingUiStore.getState().select("p1", "w1");
  stubFetch(() => json(record, 200, "/api/plans/p1"));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><SchedulingTab /></QueryClientProvider>);
  try {
    await screen.findByTestId("resolved-time");
    expect(screen.getByText("Dark temperature coverage")).toBeInTheDocument();
    expect(screen.getByText(/Ambient forecasts are not sensor temperatures/)).toBeInTheDocument();
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => { /* Remain pending during background refetch. */ })));
    void client.refetchQueries({ predicate: (query) => query.queryKey.includes("compile") });
    await waitFor(() => { expect(client.isFetching()).toBeGreaterThan(0); });
    expect(screen.queryByText("Waiting for the schedule to compile.")).toBeNull();
  } finally { view.unmount(); client.clear(); }
});


it("permits active-plan night editing after explicit Unlock with a next-start notice", async () => {
  useConnectionStore.setState({ snapshot: testSnapshot({ location: observingLocation,
    plans: [{ id: "p1", name: record.document.name, revision: 1, updated_at: record.updated_at, night_date: record.night_date }], run: runningRun }) });
  useSchedulingUiStore.getState().select("p1", "w1");
  stubFetch(() => json(record, 200, "/api/plans/p1"));
  renderWithQueryClient(<SchedulingTab />);
  await screen.findByTestId("resolved-time");
  await userEvent.click(screen.getByRole("button", { name: "Unlock plan" }));
  expect(screen.getByLabelText("Night of")).toBeEnabled();
  expect(screen.getByText(/Edits and night changes apply to the next start/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Start selected plan" })).toBeNull();
});
