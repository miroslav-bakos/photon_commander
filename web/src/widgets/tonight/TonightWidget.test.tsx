// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Degrade-honestly coverage (architecture policy invariant 7 + this ticket): no
 * location -> "set a location", not an error; a stale forecast says so
 * rather than presenting cached numbers as current; a non-occurring
 * moonrise/moonset renders the outcome, never a blank or an epoch date; the
 * widget never renders literal white — enforced separately by
 * `no-literal-color`, so not re-checked here — and it never crashes the
 * shell when `/api/tonight` 404s (this server predating the route).
 */
import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StateSnapshot } from "../../api/types";
import { testSnapshot } from "../../test/fixtures";
import type { TonightResponse } from "./api";
import { TonightWidget } from "./TonightWidget";
import { renderWithQueryClient } from "../../test/queryClient";

afterEach(cleanup);

const mockUseConnectionStore = vi.fn<(selector: (s: { snapshot: StateSnapshot | null }) => unknown) => unknown>();
vi.mock("../../store/connectionStore", () => ({
  useConnectionStore: (selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
    mockUseConnectionStore(selector),
}));

function withLocation(latitude_deg = 48.15): StateSnapshot {
  return testSnapshot({
    location: {
      latitude_deg,
      longitude_deg: 17.1,
      elevation_m: 150,
      timezone: "Europe/Bratislava",
      source: "manual",
      accuracy_m: null,
      acquired_at: "2026-08-28T00:00:00Z",
    },
  });
}

function useSnapshot(snapshot: StateSnapshot) {
  mockUseConnectionStore.mockImplementation((selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
    selector({ snapshot }),
  );
}

/** A REAL `Response`, not a duck-typed literal. `useTonightQuery` goes through
 * `openapi-fetch`, which reads `headers`, `clone()` and `text()`, and `unwrap`
 * reads `response.url` to name the path in an `ApiError` — a hand-rolled stub
 * with only `ok`/`status`/`json` satisfies none of that and fails as a parse
 * error rather than as the status under test. `url` is defined explicitly
 * because the constructor always leaves it empty. */
function mockFetchOnce(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() => {
      const response = new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
      Object.defineProperty(response, "url", { value: "http://localhost/api/tonight" });
      return Promise.resolve(response);
    }),
  );
}

const renderWithClient = renderWithQueryClient;

function baseTonight(overrides: Partial<TonightResponse> = {}): TonightResponse {
  return {
    location: {
      latitude_deg: 48.1486,
      longitude_deg: 17.1077,
      elevation_m: 150,
      timezone: "Europe/Bratislava",
      source: "manual",
      accuracy_m: null,
      acquired_at: "2026-08-28T00:00:00Z",
    },
    generated_at: "2026-08-28T18:00:00Z",
    dark_window_start: "2026-08-28T19:36:00Z",
    dark_window_end: "2026-08-29T02:09:00Z",
    dark_window_note: null,
    moon: {
      illuminated_fraction: 0.92,
      phase_angle_deg: 30,
      waxing: true,
      moonrise: { event: "moonrise", when: "2026-08-28T16:00:00Z", outcome: "occurs", threshold_deg: -0.83 },
      moonset: { event: "moonset", when: null, outcome: "always_above", threshold_deg: -0.83 },
    },
    weather: {
      fetched_at: "2026-08-28T17:52:00Z",
      stale_since: null,
      rows: [
        {
          t: "2026-08-28T20:00:00Z",
          cloud: { total: 80, low: 5, mid: 10, high: 65 },
          precip_prob: 10,
          humidity: 70,
          dew_point_c: 2.1,
          temp_c: 8,
          wind_ms: 3.3,
          source: "open-meteo",
        },
      ],
    },
    weather_unavailable_reason: null,
    conditions: {
      category: "poor",
      score: 22,
      reasons: ["High cloud 80%", "Moon 92% illuminated, above the horizon all night"],
      factors: [
        { name: "clouds", measurement: "avg total 100.0%, low 100.0%, high 100.0%", points_deducted: 65 },
        { name: "moon", measurement: "29% illuminated, above horizon 100% of dark window", points_deducted: 13 },
        { name: "dew_point", measurement: "avg temperature/dew-point margin 8.0°C", points_deducted: 0 },
        { name: "wind", measurement: "avg wind 2.0 m/s", points_deducted: 0 },
        { name: "precipitation", measurement: "max precipitation chance 0%", points_deducted: 0 },
        { name: "humidity", measurement: "avg humidity 55%", points_deducted: 0 },
      ],
    },
    conditions_unavailable_reason: null,
    ...overrides,
  };
}

describe("TonightWidget", () => {
  it("says to set a location rather than erroring, when none is set", () => {
    useSnapshot(testSnapshot({ location: null }));
    renderWithClient(<TonightWidget />);
    expect(screen.getByText("Tonight")).toBeInTheDocument();
  });

  it("shows the location message on tap when no location is set", async () => {
    useSnapshot(testSnapshot({ location: null }));
    renderWithClient(<TonightWidget />);
    await userEvent.click(screen.getByText("Tonight"));
    expect(screen.getByText(/no observing location is set/i)).toBeInTheDocument();
  });

  it("treats a 409 from the endpoint itself the same way — set a location, not an error", async () => {
    useSnapshot(withLocation());
    mockFetchOnce(409, { detail: "no location set" });
    renderWithClient(<TonightWidget />);
    await screen.findByText("Tonight");
    await userEvent.click(screen.getByText("Tonight"));
    expect(screen.getByText(/no observing location is set/i)).toBeInTheDocument();
  });

  it("renders nothing (not a scary error) when this server predates /api/tonight", async () => {
    useSnapshot(withLocation());
    mockFetchOnce(404, { detail: "not found" });
    const { container } = renderWithClient(<TonightWidget />);
    await vi.waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it("shows the verdict and moon phase at a glance once conditions load", async () => {
    useSnapshot(withLocation());
    mockFetchOnce(200, baseTonight());
    renderWithClient(<TonightWidget />);
    expect(await screen.findByText("Poor")).toBeInTheDocument();
  });

  it("puts the full reasoning behind a native <details> disclosure, collapsed by default", async () => {
    // jsdom does not apply the UA stylesheet that hides a closed <details>'s
    // content (`details:not([open]) > *:not(summary){display:none}`) — a
    // real browser does, which is what actually keeps the header compact.
    // Asserting on the `open` property itself is the meaningful, engine-
    // independent check that the reasoning starts collapsed.
    useSnapshot(withLocation());
    mockFetchOnce(200, baseTonight());
    const { container } = renderWithClient(<TonightWidget />);
    await screen.findByText("Poor");
    const details = container.querySelector("details");
    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    await userEvent.click(screen.getByText("Poor"));
    expect(details?.open).toBe(true);
    expect(screen.getByText(/high cloud 80%/i)).toBeInTheDocument();
  });

  it("renders a non-occurring moonset as its outcome, never a blank or an epoch date", async () => {
    useSnapshot(withLocation());
    mockFetchOnce(200, baseTonight());
    renderWithClient(<TonightWidget />);
    await userEvent.click(await screen.findByText("Poor"));
    expect(screen.getByText(/stays up all night/i)).toBeInTheDocument();
    expect(screen.queryByText(/1970|invalid date/i)).not.toBeInTheDocument();
  });

  it("shows staleness explicitly rather than presenting a stale forecast as current", async () => {
    useSnapshot(withLocation());
    const tonight = baseTonight();
    mockFetchOnce(200, {
      ...tonight,
      weather: { ...tonight.weather, stale_since: "2026-08-28T14:00:00Z" },
    });
    renderWithClient(<TonightWidget />);
    await userEvent.click(await screen.findByText("Poor"));
    expect(screen.getByText(/^Forecast is stale/)).toBeInTheDocument();
  });

  it("shows the moon even when conditions are unavailable — moon never depends on the weather provider", async () => {
    useSnapshot(withLocation());
    mockFetchOnce(200, {
      ...baseTonight(),
      weather: null,
      weather_unavailable_reason: "no forecast provider configured",
      conditions: null,
      conditions_unavailable_reason: "no forecast provider configured",
    });
    renderWithClient(<TonightWidget />);
    await userEvent.click(await screen.findByText("Tonight"));
    expect(screen.getByText(/capture conditions unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/waxing gibbous/i)).toBeInTheDocument();
  });

  it("renders a non-occurring dark window as its note, not a blank pair of times", async () => {
    useSnapshot(withLocation());
    mockFetchOnce(200, {
      ...baseTonight(),
      dark_window_start: null,
      dark_window_end: null,
      dark_window_note: "No astronomical darkness at this latitude and date.",
    });
    renderWithClient(<TonightWidget />);
    await userEvent.click(await screen.findByText("Poor"));
    expect(screen.getByText(/no astronomical darkness/i)).toBeInTheDocument();
  });

  it("mirrors the moon glyph's accessible label for first vs last quarter — the 50% trap", async () => {
    useSnapshot(withLocation());
    mockFetchOnce(
      200,
      baseTonight({ moon: { ...baseTonight().moon, illuminated_fraction: 0.5, waxing: true } }),
    );
    renderWithClient(<TonightWidget />);
    await screen.findByText("Poor");
    expect(screen.getByRole("img", { name: /first quarter/i })).toBeInTheDocument();
  });
});
