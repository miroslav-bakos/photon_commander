// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ForecastPoint, WeatherForecast } from "../../api/weather";
import type { TonightResponse } from "../tonight/api";
import { useConnectionStore } from "../../store/connectionStore";
import { testConditionFactors, testSnapshot } from "../../test/fixtures";
import { renderWithQueryClient } from "../../test/queryClient";
import { observingDate } from "./nightConditions";
import { WeatherWidget } from "./WeatherWidget";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const timezone = "Europe/Bratislava";
const today = observingDate(new Date(), timezone);

function addDays(date: string, days: number): string {
  const shifted = new Date(`${date}T00:00:00Z`);
  shifted.setUTCDate(shifted.getUTCDate() + days);
  return shifted.toISOString().slice(0, 10);
}

function point(date: string, hour: number, values: Partial<ForecastPoint> = {}): ForecastPoint {
  const [year = 2026, month = 1, day = 1] = date.split("-").map(Number);
  return {
    t: new Date(Date.UTC(year, month - 1, day, hour)).toISOString(),
    cloud_total: 24,
    cloud_low: 10,
    cloud_mid: 8,
    cloud_high: 6,
    precip_prob: 0,
    humidity: 55,
    dew_point_c: 4,
    temp_c: 12,
    wind_ms: 2,
    wind_direction_deg: 180,
    visibility_m: 10000,
    source: "metno",
    ...values,
  };
}

function nightlyPrediction(date: string): TonightResponse {
  return {
    location: {
      latitude_deg: 48.15,
      longitude_deg: 17.1,
      elevation_m: 150,
      timezone,
      source: "manual",
      accuracy_m: null,
      acquired_at: "2026-09-28T00:00:00Z",
    },
    generated_at: `${date}T17:00:00Z`,
    dark_window_start: `${date}T18:30:00Z`,
    dark_window_end: `${addDays(date, 1)}T03:00:00Z`,
    dark_window_note: null,
    moon: {
      illuminated_fraction: 0.3,
      phase_angle_deg: 60,
      waxing: true,
      moonrise: { event: "moonrise", when: null, outcome: "not_in_window", threshold_deg: -0.83 },
      moonset: { event: "moonset", when: null, outcome: "not_in_window", threshold_deg: -0.83 },
    },
    weather: { fetched_at: `${date}T17:00:00Z`, stale_since: null, rows: [] },
    weather_unavailable_reason: null,
    conditions: {
      category: "fair",
      score: 52,
      reasons: date === today ? ["Moon factor affects this score"] : ["Heavy cloud expected"],
      factors: testConditionFactors(),
    },
    conditions_unavailable_reason: null,
  };
}

beforeEach(() => {
  useConnectionStore.setState({
    snapshot: testSnapshot({
      location: {
        latitude_deg: 48.15,
        longitude_deg: 17.1,
        elevation_m: 150,
        timezone,
        source: "manual",
        accuracy_m: null,
        acquired_at: "2026-09-28T00:00:00Z",
      },
    }),
  });
});

describe("WeatherWidget", () => {
  it("opens the nightly mini calendar and keeps current weather details in the popup", async () => {
    const tomorrow = addDays(today, 1);
    const forecast: WeatherForecast = {
      location: {
        latitude_deg: 48.15,
        longitude_deg: 17.1,
        elevation_m: 150,
        timezone,
        source: "manual",
        accuracy_m: null,
        acquired_at: "2026-09-28T00:00:00Z",
      },
      fetched_at: new Date().toISOString(),
      stale_since: null,
      points: [point(today, 18), point(tomorrow, 18)],
    };
    vi.stubGlobal("fetch", vi.fn((request: Request) => {
      const url = new URL(request.url);
      let body: unknown;
      if (url.pathname === "/api/weather/forecast") body = forecast;
      else if (url.pathname === "/api/tonight") {
        const date = url.searchParams.get("night_date") ?? today;
        body = nightlyPrediction(date);
      } else throw new Error(`Unexpected request: ${url.pathname}`);
      const response = new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
      Object.defineProperty(response, "url", { value: request.url });
      return Promise.resolve(response);
    }));

    const { container } = renderWithQueryClient(<MemoryRouter><WeatherWidget /></MemoryRouter>);
    const weatherTrigger = await screen.findByRole("img", { name: /weather 12°c/i });
    expect(screen.queryByTestId("night-forecast-mini-calendar")).not.toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.some(([request]) =>
      request instanceof Request && new URL(request.url).searchParams.has("night_date"),
    )).toBe(false);
    await userEvent.click(weatherTrigger);

    expect(container.querySelector("details")).toHaveProperty("open", true);
    expect(container.querySelectorAll("details")).toHaveLength(1);
    const miniCalendar = screen.getByTestId("night-forecast-mini-calendar");
    expect(within(miniCalendar).getByText("Night forecast")).toBeInTheDocument();
    expect(miniCalendar.querySelector(".grid-cols-7")).toBeInTheDocument();
    expect(within(miniCalendar).getByTestId(`mini-night-${today}`)).toHaveAttribute(
      "aria-label",
      expect.stringContaining("Fair 52"),
    );
    expect(within(miniCalendar).getByTestId(`mini-night-${tomorrow}`)).toHaveAttribute("aria-label", expect.stringContaining("Fair 52"));
    expect(within(miniCalendar).getByTestId(`mini-night-${tomorrow}`)).toHaveAttribute(
      "title",
      expect.stringContaining("Heavy cloud expected"),
    );
    expect(within(miniCalendar).queryByTestId(`mini-night-${addDays(today, 2)}`)).not.toBeInTheDocument();
    expect(within(miniCalendar).getByRole("link", { name: "Open calendar" })).toHaveAttribute("href", "/calendar");
    expect(screen.getByText("Current weather")).toBeInTheDocument();
    expect(screen.getByText("Cloud total")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Waxing crescent, 30% illuminated" })).toBeInTheDocument();
    expect(screen.getByText("Tonight and moon")).toBeInTheDocument();
    expect(screen.getByText("Moonrise")).toBeInTheDocument();
    expect(screen.getByText("Dark window")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Score breakdown" })).toHaveTextContent("−20.0 points");
    expect(screen.getByRole("region", { name: "Score breakdown" })).toHaveTextContent("−5.0 points");
  });

  it("maps early-morning forecast rows to the previous observing night", async () => {
    const forecast: WeatherForecast = {
      location: {
        latitude_deg: 48.15,
        longitude_deg: 17.1,
        elevation_m: 150,
        timezone,
        source: "manual",
        accuracy_m: null,
        acquired_at: "2026-09-28T00:00:00Z",
      },
      fetched_at: new Date().toISOString(),
      stale_since: null,
      points: [point(addDays(today, 1), 1)],
    };
    vi.stubGlobal("fetch", vi.fn((request: Request) => {
      const url = new URL(request.url);
      const body = url.pathname === "/api/weather/forecast"
        ? forecast
        : nightlyPrediction(url.searchParams.get("night_date") ?? today);
      const response = new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
      Object.defineProperty(response, "url", { value: request.url });
      return Promise.resolve(response);
    }));

    renderWithQueryClient(<MemoryRouter><WeatherWidget /></MemoryRouter>);
    await userEvent.click(await screen.findByRole("img", { name: /weather 12°c/i }));
    const miniCalendar = screen.getByTestId("night-forecast-mini-calendar");
    expect(within(miniCalendar).getByTestId(`mini-night-${today}`)).toBeInTheDocument();
    expect(within(miniCalendar).queryByTestId(`mini-night-${addDays(today, 1)}`)).not.toBeInTheDocument();
  });
});
