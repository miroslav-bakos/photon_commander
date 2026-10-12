// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TonightResponse } from "../../widgets/tonight/api";
import type { WeatherForecast, ForecastPoint } from "../../api/weather";
import { useConnectionStore } from "../../store/connectionStore";
import { LayoutRouteProvider } from "../../layout/LayoutContext";
import { clearConditionFactors, testConditionFactors, testSnapshot } from "../../test/fixtures";
import { renderWithQueryClient } from "../../test/queryClient";
import { useSchedulingUiStore } from "../SchedulingTab/schedulingUiStore";
import { setLanguage, translate } from "../../i18n/locale";
import { CalendarTab } from "./CalendarTab";

vi.mock("./NightReportPanel", () => ({ NightReportPanel: ({ runId }: { runId: string }) => <p data-testid="selected-report">{runId}</p> }));
let historyMode: "ready" | "loading" | "error" = "ready";
let historyRuns: Array<{ run_id: string; night_date: string; plan_name: string; started_at: string; status: string; plan_revision: number }> = [];

const prediction: TonightResponse = {
  location: {
    latitude_deg: 48.15,
    longitude_deg: 17.1,
    elevation_m: 150,
    timezone: "Europe/Bratislava",
    source: "manual",
    accuracy_m: null,
    acquired_at: "2026-09-23T12:00:00Z",
  },
  generated_at: "2026-09-23T17:00:00Z",
  dark_window_start: "2026-09-24T18:30:00Z",
  dark_window_end: "2026-09-25T03:00:00Z",
  dark_window_note: null,
  moon: {
    illuminated_fraction: 0.9,
    phase_angle_deg: 28,
    waxing: true,
    moonrise: { event: "moonrise", when: "2026-09-24T15:00:00Z", outcome: "occurs", threshold_deg: -0.83 },
    moonset: { event: "moonset", when: "2026-09-25T02:00:00Z", outcome: "occurs", threshold_deg: -0.83 },
  },
  weather: null,
  weather_unavailable_reason: null,
  conditions: {
    category: "fair",
    score: 52,
    reasons: ["moon 80% illuminated"],
    factors: testConditionFactors(),
  },
  conditions_unavailable_reason: null,
};

const today = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Europe/Bratislava",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
}).format(new Date());
const tomorrow = new Date(`${today}T00:00:00Z`);
tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
const tomorrowDate = tomorrow.toISOString().slice(0, 10);
const twoDaysAhead = new Date(tomorrow);
twoDaysAhead.setUTCDate(twoDaysAhead.getUTCDate() + 1);
const twoDaysAheadDate = twoDaysAhead.toISOString().slice(0, 10);
const requestedNightDates: string[] = [];

function forecastPoint(nightDate: string): ForecastPoint {
  const [year = 2000, month = 1, day = 1] = nightDate.split("-").map(Number);
  return {
    t: new Date(Date.UTC(year, month - 1, day, 18)).toISOString(),
    cloud_total: 20,
    cloud_low: 10,
    cloud_mid: 5,
    cloud_high: 5,
    precip_prob: null,
    humidity: 55,
    dew_point_c: 4,
    temp_c: 12,
    wind_ms: 3,
    wind_direction_deg: null,
    visibility_m: null,
    source: "metno",
  };
}

const forecast: WeatherForecast = {
  location: prediction.location,
  fetched_at: "2026-09-23T17:00:00Z",
  stale_since: null,
  points: [forecastPoint(today), forecastPoint(tomorrowDate)],
};

const upcomingPrediction: TonightResponse = {
  ...prediction,
  conditions: { category: "excellent", score: 100, reasons: ["clear sky"], factors: clearConditionFactors() },
};

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  requestedNightDates.length = 0;
  historyRuns = [];
  historyMode = "ready";
  setLanguage("en");
  useConnectionStore.getState().reset();
  useConnectionStore.setState({
    snapshot: testSnapshot({
      location: prediction.location,
      plans: [
        { id: "dated", name: "Galaxy plan", revision: 1, updated_at: "2026-09-23T12:00:00Z", night_date: today },
        { id: "template", name: "Reusable template", revision: 1, updated_at: "2026-09-22T12:00:00Z", night_date: null },
      ],
    }),
  });
  useSchedulingUiStore.setState({ selectedPlanId: null, selectedItemId: null, selectedNightDate: null });
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : input.toString(), "http://localhost");
    if (url.pathname === "/api/runs/history") {
      if (historyMode === "loading") return new Promise<Response>(() => {});
      if (historyMode === "error") return Response.json({ detail: "offline" }, { status: 503 });
      return Response.json({ runs: historyRuns });
    }
    if (url.pathname === "/api/plans") return Response.json({ plans: [] });
    if (url.pathname === "/api/weather/forecast") return Response.json(forecast);
    if (url.pathname === "/api/tonight") {
      const nightDate = url.searchParams.get("night_date") ?? "today";
      requestedNightDates.push(nightDate);
      return Response.json(nightDate === tomorrowDate ? upcomingPrediction : prediction);
    }
    return Response.json({ detail: `unexpected ${url.pathname}${url.search}` }, { status: 500 });
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  setLanguage("en");
});

describe("CalendarTab", () => {
  it("shows quality for today and forecast-covered unscheduled nights, explains it, and opens plans in Schedule", async () => {
    renderWithQueryClient(
      <MemoryRouter initialEntries={["/calendar"]}>
        <LayoutRouteProvider>
          <Routes>
            <Route path="/calendar" element={<CalendarTab />} />
            <Route path="/scheduling" element={<p>Schedule opened</p>} />
          </Routes>
        </LayoutRouteProvider>
      </MemoryRouter>,
    );

    expect((await screen.findAllByText("Fair 52")).length).toBeGreaterThan(0);
    const todayCell = screen.getByTestId(`calendar-day-${today}`);
    const qualityTrigger = todayCell.querySelector("summary");
    if (qualityTrigger === null) throw new Error("Today's quality trigger was not rendered");
    await userEvent.click(qualityTrigger);
    expect(todayCell.querySelector("details")).toHaveAttribute("open");
    const scoreDetails = within(todayCell).getByRole("tooltip");
    expect(scoreDetails).toHaveTextContent("Clouds");
    expect(scoreDetails).toHaveTextContent("−20.0 points");
    expect(scoreDetails).toHaveTextContent("Precipitation");
    expect(scoreDetails).toHaveTextContent("−5.0 points");
    const tomorrowCell = screen.getByTestId(`calendar-day-${tomorrowDate}`);
    expect(within(tomorrowCell).getAllByText("Excellent 100").length).toBeGreaterThan(0);
    const outsideForecastCell = screen.queryByTestId(`calendar-day-${twoDaysAheadDate}`);
    expect(outsideForecastCell?.querySelector("[data-night-quality]")).toBeFalsy();
    expect(requestedNightDates).toEqual(expect.arrayContaining([today, tomorrowDate]));
    expect(requestedNightDates).not.toContain(twoDaysAheadDate);
    expect(screen.getByRole("heading", { name: /unscheduled plans/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reusable template" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: `Open Galaxy plan scheduled for ${today}` }));
    expect(await screen.findByText("Schedule opened")).toBeInTheDocument();
    expect(useSchedulingUiStore.getState()).toMatchObject({
      selectedPlanId: "dated",
      selectedItemId: null,
      selectedNightDate: today,
    });
  });
});

function renderCalendar() {
  return renderWithQueryClient(<MemoryRouter><CalendarTab /></MemoryRouter>);
}

it("selects an executed night and switches between its persisted runs", async () => {
  historyRuns = ["newer", "older"].map((run_id) => ({ run_id, night_date: today, plan_name: "Original executed name", started_at: `${today}T01:00:00Z`, status: "completed", plan_revision: 3 }));
  renderCalendar();
  await userEvent.click(screen.getByRole("button", { name: `View night report for ${today}` }));
  expect(await screen.findByTestId("selected-report")).toHaveTextContent("newer");
  expect(screen.getByText(/Multiple executions are recorded/)).toBeInTheDocument();
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Execution" }), "older");
  expect(screen.getByTestId("selected-report")).toHaveTextContent("older");
  expect(useSchedulingUiStore.getState().selectedPlanId).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Previous month" }));
  expect(screen.queryByTestId("selected-report")).not.toBeInTheDocument();
});

it("shows an empty executed night without navigating to plan editing", async () => {
  renderCalendar();
  const day = screen.getByRole("button", { name: `View night report for ${today}` });
  day.focus();
  await userEvent.keyboard("{Enter}");
  expect(await screen.findByText("No recorded execution for this night.")).toBeInTheDocument();
  expect(day).toHaveAttribute("aria-pressed", "true");
  expect(day).toHaveClass("min-h-[44px]", "min-w-[44px]");
});

it.each(["loading", "error"] as const)("surfaces history %s without claiming an empty night", async (mode) => {
  historyMode = mode;
  renderCalendar();
  await userEvent.click(screen.getByRole("button", { name: `View night report for ${today}` }));
  expect(await screen.findByText(mode === "loading" ? "Loading night history…" : "Night history unavailable. Retry when the connection returns.")).toBeInTheDocument();
  expect(screen.queryByText("No recorded execution for this night.")).not.toBeInTheDocument();
});

it.each(["sk", "cs"] as const)("localizes night selection and Execute in %s", async (language) => {
  setLanguage(language);
  renderCalendar();
  const label = translate(`View night report for ${today}`, language);
  expect(label).not.toBe(`View night report for ${today}`);
  await userEvent.click(screen.getByRole("button", { name: label }));
  expect(await screen.findByText(translate("No recorded execution for this night.", language))).toBeInTheDocument();
  expect(translate("Execute", language)).toBe(language === "sk" ? "Vykonať" : "Spustit");
});

it("opens historical execution when a past saved plan is selected", async () => {
  const previous = new Date(`${today}T00:00:00Z`);
  previous.setUTCDate(previous.getUTCDate() - 1);
  const past = previous.toISOString().slice(0, 10);
  const snapshot = useConnectionStore.getState().snapshot;
  if (snapshot === null) throw new Error("Missing snapshot fixture");
  useConnectionStore.setState({ snapshot: { ...snapshot, plans: [{ id: "past-plan", name: "Past saved plan", revision: 9, updated_at: `${today}T00:00:00Z`, night_date: past }] } });
  historyRuns = [{ run_id: "past-run", night_date: past, plan_name: "Executed revision", started_at: `${today}T01:00:00Z`, status: "completed", plan_revision: 2 }];
  renderCalendar();
  await userEvent.click(screen.getByRole("button", { name: `Open Past saved plan scheduled for ${past}` }));
  expect(await screen.findByTestId("selected-report")).toHaveTextContent("past-run");
  expect(useSchedulingUiStore.getState().selectedPlanId).toBeNull();
});
