// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { act, cleanup, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { testSnapshot } from "../../test/fixtures";
import { useConnectionStore } from "../../store/connectionStore";
import { DashboardTab } from "./DashboardTab";
import { LayoutRouteProvider } from "../../layout/LayoutContext";
import { useLayoutStore } from "../../layout/layoutStore";
import { useMountState } from "../../api/mount";
import { useAutofocusStateQuery, useFocuserPositionQuery } from "../../api/focuser";
import { useFilterWheelQuery } from "../../api/filterwheel";
import { useHeaterQuery } from "../../api/heater";
import { useForecastQuery } from "../../api/weather";
import { useCaptureDitherSettingsQuery } from "../../api/settings";
import { useCaptureFrameQualityQuery, useCaptureFramesQuery, useCaptureState, useLiveStackQuery } from "../../api/capture";

vi.mock("@tanstack/react-query", async (original) => {
  const actual = await original<typeof import("@tanstack/react-query")>();
  return { ...actual, useQueries: () => [{ data: { hfr_px: 1.5, fwhm_px: 2.4 } }] };
});
vi.mock("../../api/mount", () => ({ useMountState: vi.fn() }));
vi.mock("../../api/focuser", () => ({ useFocuserPositionQuery: vi.fn(), useAutofocusStateQuery: vi.fn() }));
vi.mock("../../api/filterwheel", () => ({ useFilterWheelQuery: vi.fn() }));
vi.mock("../../api/heater", () => ({ useHeaterQuery: vi.fn() }));
vi.mock("../../api/weather", () => ({ useForecastQuery: vi.fn(), nearestForecastPoint: (points: unknown[]) => points[0] ?? null }));
vi.mock("../../api/settings", () => ({ useCaptureDitherSettingsQuery: vi.fn() }));
vi.mock("../../api/capture", () => ({
  useCaptureState: vi.fn(),
  useCaptureFramesQuery: vi.fn(),
  useCaptureFrameQualityQuery: vi.fn(),
  useLiveStackQuery: vi.fn(),
}));
vi.mock("../Observe/ObservingModeSwitch", () => ({ ObservingModeSwitch: () => <div>Capture / Live stack</div> }));
vi.mock("../Observe/ObservingSessionPanel", async () => {
  const { Card, CardTitle } = await import("../../components/Card");
  return { ObservingSessionPanel: () => <Card layoutId="observing-now"><CardTitle>Observing now</CardTitle></Card> };
});
vi.mock("./NightReportPanel", () => ({ NightReportPanel: () => <div>Night report</div> }));

afterEach(() => {
  cleanup();
  useConnectionStore.getState().reset();
  useLayoutStore.setState({ unlocked: false, saved: { version: 1, tabs: {} } });
  Reflect.deleteProperty(document, "elementFromPoint");
});

beforeEach(() => {
  const device = (id: string, kind: "telescope" | "camera" | "focuser" | "filterwheel" | "switch", label: string) => ({
    id, kind, number: 0, label, connected: true, note: null,
    temperature_c: null, temperature_note: null,
  });
  useConnectionStore.setState({
    status: "open",
    snapshot: testSnapshot({
      link: { endpoint: null, reason: null, events_dropped: 0, connect_attempts: 0, changed_at: null, events_connected: true, status: "ready", control_connected: true },
      devices: {
        polled_at: "2026-09-16T05:00:00Z",
        stale: false,
        items: [
          device("telescope:0", "telescope", "Mount"),
          device("camera:0", "camera", "Camera"),
          device("focuser:0", "focuser", "Focuser"),
          device("filterwheel:0", "filterwheel", "Filter"),
          device("switch:0", "switch", "Heater"),
        ],
      },
      run: {
        run_id: "run-1", plan_id: "plan-1", plan_name: "IC 1805", plan_revision: 1,
        night_date: "2026-09-15", status: "running", started_at: "2026-09-15T22:00:00Z",
        ended_at: null, ended_reason: null,
        current_item_id: "panel-2", items: [{ item_id: "panel-2", kind: "goto_and_stack", enabled: true, status: "running", attempt: 1, panel_index: 2, held: false, reason: "exposing", start_utc: null, end_utc: null }],
        gate_held: false, gate_reason: null, last_decision: null, last_decision_at: null,
        journal_length: 1, available_actions: [],
      },
      polar_alignment: {
        status: "completed", run_kind: "measure", started_at: "2026-09-16T00:00:00Z", ended_at: "2026-09-16T00:04:00Z",
        exposure_started_at: null, exposure_duration_s: null, current_point_index: null,
        preflight_reason: null, points: [], history: [], start: null, error: null,
        result: {
          axis_altitude_deg: 48, axis_azimuth_deg: 0, altitude_error_deg: 0.01, azimuth_error_deg: -0.015,
          azimuth_error_sky_deg: -0.01, total_error_deg: 0.02, altitude_correction_deg: -0.01,
          azimuth_correction_deg: 0.015, circle_radius_deg: 42, arc_span_deg: 60, rms_residual_arcsec: 1,
          max_residual_arcsec: 2, residual_dof: 2, noise_amplification: 1, axis_uncertainty_arcsec: 20,
          point_count: 5, epoch: "2026-09-16T00:04:00Z", refraction_applied: true, has_residual_check: true,
        },
        assessment: {
          verdict: "recheck", message: "Recheck", current_total_error_arcmin: 1.2, current_axis_uncertainty_arcsec: 20,
          previous_total_error_arcmin: null, previous_axis_uncertainty_arcsec: null, axis_separation_arcmin: null,
          max_error_arcmin: 2, max_axis_separation_arcmin: 2, max_axis_uncertainty_arcsec: 120,
        },
        refinement: { status: null, started_at: null, ended_at: null, stage: null, exposure_started_at: null, exposure_duration_s: null, iteration: 0, degenerate_count: 0, last_degenerate_reason: null, error: null, result: null },
      },
    }),
  });
  vi.mocked(useMountState).mockReturnValue({ ra_hours: 2.7, dec_deg: 61.1, alt_deg: 48, az_deg: 120, tracking: true, tracking_rate: "sidereal", at_park: false, at_home: false, slewing: false, stale: false, polled_at: new Date().toISOString(), sidereal_time_hours: 3, equatorial_system: "topocentric", capabilities: null });
  vi.mocked(useFocuserPositionQuery).mockReturnValue({ data: { position: 1204, is_moving: false } } as ReturnType<typeof useFocuserPositionQuery>);
  vi.mocked(useAutofocusStateQuery).mockReturnValue({ data: { status: "completed", stage: null, samples: [], curve: [], final_position: 1204, completed_at: "2026-09-16T00:30:00Z" } } as unknown as ReturnType<typeof useAutofocusStateQuery>);
  vi.mocked(useFilterWheelQuery).mockReturnValue({ data: { names: ["Dark", "IR", "LP"], position: 2 } } as ReturnType<typeof useFilterWheelQuery>);
  vi.mocked(useHeaterQuery).mockReturnValue({ data: { enabled: true } } as ReturnType<typeof useHeaterQuery>);
  vi.mocked(useForecastQuery).mockReturnValue({ data: { points: [{ t: "2026-09-16T05:00:00Z", cloud_total: 10, cloud_low: 2, cloud_mid: 3, cloud_high: 5, precip_prob: 0, humidity: 60, temp_c: 12.3, dew_point_c: 8.1, wind_ms: 1, wind_direction_deg: 90, visibility_m: 20000, source: "metno" }] } } as unknown as ReturnType<typeof useForecastQuery>);
  vi.mocked(useCaptureDitherSettingsQuery).mockReturnValue({ data: { enabled: true, every_n_frames: 5, min_pixels: 50, max_pixels: 200 } } as ReturnType<typeof useCaptureDitherSettingsQuery>);
  vi.mocked(useCaptureState).mockReturnValue({ in_progress: true, last_frame_id: 7, last_captured_at: "2026-09-16T05:00:00Z", last_error: null, folder_configured: true, stage: "exposing", duration_s: 30, object_name: "IC 1805 2", gain: 200, loop_active: true, loop_completed_frames: 17 });
  vi.mocked(useCaptureFramesQuery).mockReturnValue({ data: { frames: [{ id: 7 }] }, refetch: vi.fn() } as unknown as ReturnType<typeof useCaptureFramesQuery>);
  vi.mocked(useCaptureFrameQualityQuery).mockReturnValue({ data: { star_count: 40, hfr_px: 1.4, fwhm_px: 2.3 } } as ReturnType<typeof useCaptureFrameQualityQuery>);
  vi.mocked(useLiveStackQuery).mockReturnValue({ data: { active: true, frame_count: 15, rejected_count: 2, total_integration_s: 450, object_name: "IC 1805 2", sequence: 1, output_path: "stack.fits", parameters_path: "stack.json", dark_master: null, bias_master: null, flat_master: null, error: null } } as ReturnType<typeof useLiveStackQuery>);
});

describe("DashboardTab", () => {
  function editableDashboard() {
    return <MemoryRouter initialEntries={["/dashboard"]}><LayoutRouteProvider><DashboardTab /></LayoutRouteProvider></MemoryRouter>;
  }

  function dashboard() {
    return <MemoryRouter initialEntries={["/dashboard"]}><DashboardTab /></MemoryRouter>;
  }

  it("keeps last readings visible through stale telemetry and recovery", () => {
    renderWithQueryClient(dashboard());
    const focuser = screen.getByRole("link", { name: "Open Focuser controls" });
    const camera = screen.getByRole("link", { name: "Open Camera controls" });
    const stack = screen.getByRole("link", { name: "Open Live stacking controls" });
    const original = useConnectionStore.getState().snapshot!;
    act(() => { useConnectionStore.setState({ snapshot: { ...original, devices: { ...original.devices!, stale: true } } }); });
    expect(within(focuser).getByText("1204", { selector: "p" })).toBeInTheDocument();
    expect(within(camera).getByText("30.00 s")).toBeInTheDocument();
    expect(within(stack).getByText("15")).toBeInTheDocument();
    expect(within(focuser).getByRole("status")).toHaveTextContent("Stale");
    expect(within(camera).getByRole("status")).toHaveTextContent("Last reading");
    act(() => { useConnectionStore.setState({ snapshot: original }); });
    expect(within(focuser).queryByRole("status")).not.toBeInTheDocument();
    expect(within(focuser).getByText("1204", { selector: "p" })).toBeInTheDocument();
  });

  it("labels disconnected cached readings without claiming live activity", () => {
    useConnectionStore.setState({ status: "closed" });
    renderWithQueryClient(dashboard());
    const focuser = screen.getByRole("link", { name: "Open Focuser controls" });
    const camera = screen.getByRole("link", { name: "Open Camera controls" });
    expect(within(focuser).getByText("1204", { selector: "p" })).toBeInTheDocument();
    expect(within(focuser).getByRole("status")).toHaveTextContent("Disconnected");
    expect(within(camera).getByText("30.00 s")).toBeInTheDocument();
    expect(camera).not.toHaveClass("border-accent");
  });

  it("retains query readings on read errors with a warning", () => {
    vi.mocked(useFocuserPositionQuery).mockReturnValue({ data: { position: 1204, is_moving: false }, isError: true, dataUpdatedAt: Date.now() } as ReturnType<typeof useFocuserPositionQuery>);
    renderWithQueryClient(dashboard());
    const focuser = screen.getByRole("link", { name: "Open Focuser controls" });
    expect(within(focuser).getByText("1204", { selector: "p" })).toBeInTheDocument();
    expect(within(focuser).getByRole("status")).toHaveTextContent("Read failed");
  });

  it("keeps stack counts on refresh errors without calling the cached stack active", () => {
    vi.mocked(useLiveStackQuery).mockReturnValue({ data: { active: true, frame_count: 15, rejected_count: 2, total_integration_s: 450 }, isError: true, dataUpdatedAt: Date.now() } as ReturnType<typeof useLiveStackQuery>);
    renderWithQueryClient(dashboard());
    const stack = screen.getByRole("link", { name: "Open Live stacking controls" });
    expect(within(stack).getByText("15")).toBeInTheDocument();
    expect(within(stack).getByText("2")).toBeInTheDocument();
    expect(within(stack).getByRole("status")).toHaveTextContent("Read failed · Last reading");
    expect(within(stack).queryByText("Active")).not.toBeInTheDocument();
    expect(stack).not.toHaveClass("border-accent");
  });

  it("retains mount coordinates and aged query readings with card-specific warnings", () => {
    vi.mocked(useMountState).mockReturnValue({ ra_hours: 2.7, dec_deg: 61.1, alt_deg: 48, az_deg: 120, tracking: true, tracking_rate: "sidereal", at_park: false, at_home: false, slewing: false, stale: true, polled_at: new Date(Date.now() - 20000).toISOString(), sidereal_time_hours: 3, equatorial_system: "topocentric", capabilities: null });
    vi.mocked(useFilterWheelQuery).mockReturnValue({ data: { names: ["Dark", "IR", "LP"], position: 2 }, dataUpdatedAt: Date.now() - 20000 } as ReturnType<typeof useFilterWheelQuery>);
    renderWithQueryClient(dashboard());
    const mountCard = screen.getByRole("link", { name: "Open Mount controls" });
    const filter = screen.getByRole("link", { name: "Open Filter wheel controls" });
    expect(within(mountCard).getByText("48.0° / 120.0°")).toBeInTheDocument();
    expect(within(mountCard).getByRole("status")).toHaveTextContent("Stale · Last reading");
    expect(mountCard).not.toHaveClass("border-accent");
    expect(within(filter).getByText("LP")).toBeInTheDocument();
    expect(within(filter).getByRole("status")).toHaveTextContent("Stale · Last reading");
    expect(within(screen.getByRole("link", { name: "Open Focuser controls" })).queryByRole("status")).not.toBeInTheDocument();
  });

  it.each([
    ["read error", true, 0, "Read failed · Last reading"],
    ["aged reading", false, 20000, "Stale · Last reading"],
  ] as const)("retains dithering settings after %s without claiming live activity", (_label, isError, age, warning) => {
    vi.mocked(useCaptureDitherSettingsQuery).mockReturnValue({
      data: { enabled: true, every_n_frames: 5, min_pixels: 50, max_pixels: 200 },
      isError, dataUpdatedAt: Date.now() - age,
    } as ReturnType<typeof useCaptureDitherSettingsQuery>);
    renderWithQueryClient(dashboard());
    const dither = screen.getByRole("link", { name: "Open Dithering controls" });
    expect(within(dither).getByText("Enabled")).toBeInTheDocument();
    expect(within(dither).getByText("50–200 px")).toBeInTheDocument();
    expect(within(dither).getByText("Every 5 frames")).toBeInTheDocument();
    expect(within(dither).getByRole("status")).toHaveTextContent(warning);
    expect(dither).not.toHaveClass("border-accent");
  });

  it("does not fabricate previous readings or idle state before any snapshot", () => {
    useConnectionStore.setState({ snapshot: null, status: "connecting" });
    vi.mocked(useMountState).mockReturnValue(null);
    vi.mocked(useCaptureState).mockReturnValue(null);
    vi.mocked(useFocuserPositionQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useFocuserPositionQuery>);
    vi.mocked(useFilterWheelQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useFilterWheelQuery>);
    vi.mocked(useHeaterQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useHeaterQuery>);
    vi.mocked(useLiveStackQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useLiveStackQuery>);
    vi.mocked(useCaptureDitherSettingsQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useCaptureDitherSettingsQuery>);
    renderWithQueryClient(dashboard());
    expect(screen.getByText("Disconnected. Waiting for readings.")).toBeInTheDocument();
    expect(screen.queryByText(/Last reading/)).not.toBeInTheDocument();
    expect(screen.queryByText("No active plan")).not.toBeInTheDocument();
    expect(within(screen.getByRole("link", { name: "Open Camera controls" })).queryByText("Idle")).not.toBeInTheDocument();
  });

  it("does not claim a disconnected live stack stopped", () => {
    useConnectionStore.setState({ status: "closed" });
    renderWithQueryClient(dashboard());
    expect(screen.queryByText("Stopped")).not.toBeInTheDocument();
    expect(screen.getAllByText("Disconnected").length).toBeGreaterThan(0);
  });

  it("shows live overview cards with device connection dots and active state", () => {
    renderWithQueryClient(dashboard());

    const region = screen.getByRole("region", { name: "Observatory status" });
    expect(region.children).toHaveLength(10);
    expect(region).toHaveClass("grid-cols-2", "lg:grid-cols-4");
    expect(region).not.toHaveClass("grid-cols-1");
    expect(within(region).getByText("Observing now")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Equipment and imaging details" })).toContainElement(region);
    expect(region.closest("details")).toBeNull();
    expect(screen.getByRole("heading", { name: "Observe" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /4 · Review/ })).toHaveAttribute("href", "/calendar");
    expect(within(region).queryByText("Night report")).not.toBeInTheDocument();
    expect(screen.queryByText("Night report")).not.toBeInTheDocument();
    expect(screen.getByText("IC 1805")).toBeInTheDocument();
    expect(screen.getByText("15")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByLabelText("Mount connected")).toHaveClass("bg-dot-ok");
    expect(screen.getByLabelText("Camera connected")).toHaveClass("bg-dot-ok");
    const mountCard = screen.getByLabelText("Mount connected").closest<HTMLElement>(".rounded-lg");
    const filterCard = screen.getByText("LP").closest<HTMLElement>(".rounded-lg");
    expect(mountCard?.style.backgroundColor).toContain("var(--ssc-accent)");
    expect(filterCard).toHaveClass("border-danger");
    expect(filterCard?.style.backgroundColor).toContain("var(--ssc-danger)");
    const polarCard = screen.getByText("0.02° total").closest<HTMLElement>(".rounded-lg");
    expect(polarCard?.style.backgroundColor).toContain("var(--ssc-accent)");
    expect(screen.getByText("+0.01°")).toBeInTheDocument();
    expect(screen.getByText("-0.01°")).toBeInTheDocument();
    expect(screen.getByText("≤ 0.03°")).toBeInTheDocument();
  });

  it("does not highlight polar alignment above the desired error", () => {
    const snapshot = useConnectionStore.getState().snapshot;
    const polarAlignment = snapshot?.polar_alignment;
    if (snapshot === null || polarAlignment?.result == null) throw new Error("polar alignment fixture is missing");
    useConnectionStore.setState({ snapshot: {
      ...snapshot,
      polar_alignment: {
        ...polarAlignment,
        result: { ...polarAlignment.result, total_error_deg: 0.1 },
      },
    } });
    renderWithQueryClient(dashboard());
    const polarCard = screen.getByText("0.10° total").closest<HTMLElement>(".rounded-lg");
    expect(polarCard?.style.backgroundColor).not.toContain("var(--ssc-accent)");
    expect(screen.getByText("Adjustment recommended")).toBeInTheDocument();
  });

  it("reorders dashboard cards with the edit controls and restores saved order", () => {
    useLayoutStore.getState().setUnlocked(true);
    const view = renderWithQueryClient(editableDashboard());
    const region = screen.getByRole("region", { name: "Observatory status" });
    expect(region).toHaveAttribute("data-layout-panel-target", "secondary");
    expect(region.children).toHaveLength(10);
    expect(region.querySelector('[data-layout-card-id="observing-now"]')).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Move Mount down" }));
    expect(useLayoutStore.getState().saved.tabs["/dashboard"]?.cards.mount?.order).toBe(1);
    expect(useLayoutStore.getState().saved.tabs["/dashboard"]?.cards.focuser?.order).toBe(0);
    expect(screen.getByLabelText("Mount connected").closest<HTMLElement>("[data-layout-card-id]")?.style.order).toBe("1");

    view.unmount();
    renderWithQueryClient(editableDashboard());
    expect(screen.getByLabelText("Mount connected").closest<HTMLElement>("[data-layout-card-id]")?.style.order).toBe("1");
  });

  it("moves Observing now within the equipment grid using its existing layout ID", () => {
    useLayoutStore.getState().setUnlocked(true);
    renderWithQueryClient(editableDashboard());
    fireEvent.click(screen.getByRole("button", { name: "Move Observing now up" }));
    expect(useLayoutStore.getState().saved.tabs["/dashboard"]?.cards["observing-now"]?.order).toBe(8);
    const grid = screen.getByRole("region", { name: "Observatory status" });
    expect(grid.querySelector<HTMLElement>('[data-layout-card-id="observing-now"]')?.style.order).toBe("8");
  });

  it("pointer-drags a dashboard card to a new grid position", () => {
    useLayoutStore.getState().setUnlocked(true);
    renderWithQueryClient(editableDashboard());
    const mountCard = screen.getByLabelText("Mount connected").closest<HTMLElement>("[data-layout-card-id]");
    const filterCard = screen.getByLabelText("Filter wheel connected").closest<HTMLElement>("[data-layout-card-id]");
    expect(mountCard).not.toBeNull();
    expect(filterCard).not.toBeNull();
    if (mountCard === null) throw new Error("Mount card missing");
    Object.defineProperty(document, "elementFromPoint", { configurable: true, value: vi.fn(() => filterCard) });
    const handle = within(mountCard).getByTitle("Drag this card");
    fireEvent.pointerDown(handle, { pointerId: 7, button: 0, clientX: 200, clientY: 300 });
    fireEvent.pointerUp(handle, { pointerId: 7, button: 0, clientX: 200, clientY: 200 });
    expect(useLayoutStore.getState().saved.tabs["/dashboard"]?.cards.mount?.order).toBe(1);
    expect(useLayoutStore.getState().saved.tabs["/dashboard"]?.cards.focuser?.order).toBe(0);
  });

  it("opens the exact Telescope control card from a dashboard status card", () => {
    function LocationProbe() {
      const location = useLocation();
      return <output aria-label="Current route">{`${location.pathname}${location.search}`}</output>;
    }

    renderWithQueryClient(
      <MemoryRouter initialEntries={["/dashboard"]}>
        <DashboardTab />
        <LocationProbe />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link", { name: "Open Focuser controls" }));
    expect(screen.getByLabelText("Current route")).toHaveTextContent("/mount?panel=focuser");
  });
});
