// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The merged timeline + item list (DESIGN.md §4.4): twilight bands in order
 * and darkness, a dusk that does not occur named at the edge, blocks placed
 * proportionally, diagnostics as badges, a drag that snaps to astronomical
 * dusk and emits an `event` TimeSpec, the gutter's reorder/toggle/expand,
 * the horizontally scrollable axis on a phone, and the render budget for a
 * 20-item plan.
 *
 * `/api/astro/night` is stubbed at `fetch` — the altitude curve and the moon
 * phase are the server's, never derived here.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CompiledItem, CompiledSchedule, PlanItem } from "../../api/plans";
import type { RunState } from "../../api/runs";
import { renderWithQueryClient } from "../../test/queryClient";
import { ADVANCED_ITEM_KINDS, ITEM_KINDS, NORMAL_ITEM_KINDS } from "./itemKinds";
import { NIGHT } from "./testNight";
import { FALLBACK_AXIS_PX, axisRange, timeToPx } from "./timelineGeometry";
import { TimelinePane, type ItemPatch, type TimelinePaneProps } from "./TimelinePane";
import { HEADER_LANE_PX, TimelineStrip } from "./TimelineStrip";
import { snapTargets, twilightBands } from "./timelineGeometry";
import { BELOW_SM_QUERY } from "./useMediaQuery";

const items: PlanItem[] = [
  {
    id: "a",
    kind: "startup",
    start: { kind: "event", event: "sunset", offset_min: 0 },
    end: { kind: "duration", minutes: 10 },
    enabled: true,
  },
  {
    id: "b",
    kind: "goto_and_stack",
    live_stack: true,
    start: { kind: "absolute", local_datetime: "2026-08-28T22:30:00", fold: 0 },
    end: { kind: "duration", minutes: 120 },
    enabled: true,
    target: { kind: "catalog", name: "M31", ra_j2000_h: 0.712, dec_j2000_deg: 41.27 },
    framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
    constraints: {},
    autofocus_before: false,
    exposure_sec: 10,
    gain: 200,
    filter_name: null,
  },
  { id: "c", kind: "shutdown", start: { kind: "asap" }, end: null, enabled: false },
];

const compiled: CompiledItem[] = [
  {
    item_id: "a",
    kind: "startup",
    enabled: true,
    start_utc: "2026-08-28T19:00:00Z",
    end_utc: "2026-08-28T19:10:00Z",
    resolution: "Sunset → 20:00",
    end_resolution: "Duration 10 min → 20:10",
    panels: [],
    diagnostics: [],
  },
  {
    item_id: "b",
    kind: "goto_and_stack",
    enabled: true,
    start_utc: "2026-08-28T21:30:00Z",
    end_utc: "2026-08-28T23:30:00Z",
    resolution: "22:30 → 22:30",
    end_resolution: "Duration 120 min → 00:30",
    panels: [],
    diagnostics: [{ severity: "warning", item_id: "b", code: "moon_too_close", message: "Moon 18° away, limit 30°" }],
  },
  {
    item_id: "c",
    kind: "shutdown",
    enabled: false,
    start_utc: "2026-08-28T23:30:00Z",
    end_utc: "2026-08-28T23:30:00Z",
    resolution: "ASAP → 00:30",
    end_resolution: "ASAP → 00:30",
    panels: [],
    diagnostics: [],
  },
];

const schedule: CompiledSchedule = {
  plan_id: "p1",
  plan_revision: "abc",
  compiled_at: "2026-08-28T12:00:00Z",
  night: NIGHT,
  items: compiled,
};

function json(body: unknown, status: number, url: string): Response {
  const response = new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  Object.defineProperty(response, "url", { value: `http://localhost${url}` });
  return response;
}

/** `moonAltitude: "error"` makes `/api/astro/moon-altitude` fail (a real
 * server error, not an empty success) — the pane's own draws-nothing-not-
 * a-flat-line behaviour on that path. The default (`[]`) is a real, empty
 * 200: harmless for every test that does not care about the Moon curve. */
function stubNightFetch(
  moonAltitude: "error" | { at: string; alt_deg: number }[] = [],
  weatherPoints: {
    t: string;
    cloud_total: number | null;
    cloud_low: number | null;
    cloud_mid: number | null;
    cloud_high: number | null;
    precip_prob: number | null;
    humidity: number | null;
    dew_point_c: number | null;
    temp_c: number | null;
    wind_ms: number | null;
    wind_direction_deg: number | null;
    visibility_m: number | null;
    source: string;
  }[] = [],
) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((request: Request) => {
      const { pathname } = new URL(request.url);
      if (request.method === "POST" && pathname === "/api/astro/night") {
        return Promise.resolve(
          json(
            {
              location: null,
              window_start: NIGHT.window_start_utc,
              window_end: NIGHT.window_end_utc,
              solar_events: [],
              lunar_events: [],
              moon: { illuminated_fraction: 0.63, phase_angle_deg: 70, waxing: true, separation_deg: null },
              target_events: null,
              altitude_curve: [
                { when: "2026-08-28T19:00:00Z", altitude_deg: 10 },
                { when: "2026-08-28T23:00:00Z", altitude_deg: 60 },
                { when: "2026-08-29T05:00:00Z", altitude_deg: 30 },
              ],
              moon_separation_curve: [
                { when: "2026-08-28T19:00:00Z", separation_deg: 30 },
                { when: "2026-08-29T05:00:00Z", separation_deg: 50 },
              ],
              horizon_profile_active: false,
              visibility_intervals: null,
            },
            200,
            pathname,
          ),
        );
      }
      if (request.method === "GET" && pathname === "/api/astro/moon-altitude") {
        if (moonAltitude === "error") return Promise.resolve(json({ detail: "moon altitude unavailable" }, 500, pathname));
        return Promise.resolve(json({ samples: moonAltitude }, 200, pathname));
      }
      if (request.method === "GET" && pathname === "/api/weather/forecast") {
        return Promise.resolve(json({ location: {}, fetched_at: "2026-08-28T18:00:00Z", stale_since: null, points: weatherPoints }, 200, pathname));
      }
      return Promise.resolve(json({ detail: `unexpected ${request.method} ${pathname}` }, 500, pathname));
    }),
  );
}

function renderPane(overrides: Partial<TimelinePaneProps> = {}) {
  const props: TimelinePaneProps = {
    schedule,
    isPending: false,
    isError: false,
    selectedItemId: null,
    onSelectItem: vi.fn(),
    items,
    onEditItem: vi.fn(),
    onReorderItem: vi.fn(),
    onToggleItem: vi.fn(),
    onAddItem: vi.fn(),
    ...overrides,
  };
  renderWithQueryClient(<TimelinePane {...props} />);
  return props;
}

beforeEach(() => {
  stubNightFetch();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("TimelinePane — the night", () => {
  it("shows hourly weather icons and a green/amber/red forecast band above the Moon lane", async () => {
    stubNightFetch([], [
      { t: "2026-08-28T20:00:00Z", cloud_total: 10, cloud_low: 0, cloud_mid: 0, cloud_high: 10, precip_prob: 0, humidity: 60, dew_point_c: 8, temp_c: 12, wind_ms: 2, wind_direction_deg: 90, visibility_m: 20_000, source: "metno" },
      { t: "2026-08-28T21:00:00Z", cloud_total: 40, cloud_low: 5, cloud_mid: 20, cloud_high: 30, precip_prob: 0, humidity: 70, dew_point_c: 8, temp_c: 11, wind_ms: 3, wind_direction_deg: 90, visibility_m: 20_000, source: "metno" },
      { t: "2026-08-28T22:00:00Z", cloud_total: 75, cloud_low: 10, cloud_mid: 30, cloud_high: 70, precip_prob: 10, humidity: 80, dew_point_c: 8, temp_c: 10, wind_ms: 4, wind_direction_deg: 90, visibility_m: 20_000, source: "metno" },
    ]);
    renderPane();

    const weather = await screen.findByTestId("weather-band");
    expect(within(weather).getAllByTestId("weather-hour")).toHaveLength(3);
    expect(weather.querySelectorAll('[data-weather-quality="good"]')).toHaveLength(2);
    expect(weather.querySelectorAll('[data-weather-quality="marginal"]')).toHaveLength(2);
    expect(weather.querySelectorAll('[data-weather-quality="poor"]')).toHaveLength(2);
    expect(weather).toHaveTextContent(/75% cloud/);
  });

  it("renders separated mosaic panels as independent named timeline entities", () => {
    const panelItem = (id: string, name: string, ra: number, dec: number): PlanItem => ({
      id,
      kind: "goto_and_stack",
    live_stack: true,
      start: { kind: "asap" },
      end: { kind: "duration", minutes: 60 },
      enabled: true,
      target: { kind: "catalog", name, ra_j2000_h: ra, dec_j2000_deg: dec },
      framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
      constraints: {},
      autofocus_before: false,
      exposure_sec: 10,
      gain: 200,
      filter_name: null,
    });
    const panelItems = [
      panelItem("panel-1", "IC 1805 1", 2.51, 61.4),
      panelItem("panel-2", "IC 1805 2", 2.58, 61.5),
    ];
    const panelSchedule: CompiledSchedule = {
      ...schedule,
      items: panelItems.map((item, index) => ({
        item_id: item.id,
        kind: item.kind,
        enabled: true,
        start_utc: `2026-08-28T2${(index + 1).toString()}:00:00Z`,
        end_utc: `2026-08-28T2${(index + 2).toString()}:00:00Z`,
        resolution: "ASAP",
        end_resolution: "Duration 60 min",
        panels: [],
        diagnostics: [],
      })),
    };
    renderPane({ items: panelItems, schedule: panelSchedule });

    expect(screen.getByTestId("strip-panel-1")).toBeInTheDocument();
    expect(screen.getByTestId("strip-panel-2")).toBeInTheDocument();
    expect(screen.getAllByText("IC 1805 1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("IC 1805 2").length).toBeGreaterThan(0);
    expect(screen.getByTestId("row-panel-1")).toHaveTextContent(/≈ 38 min clean · 228 frames · 63%/);
    expect(screen.getByTestId("row-panel-2")).toHaveTextContent(/≈ 39 min clean · 232 frames · 64%/);
  });

  it("keeps an instantaneous action at its true coordinate with a 44px interaction target", () => {
    renderPane();
    const strip = within(screen.getByTestId("strip-c"));
    expect(strip.getByTestId("short-action-marker")).toBeInTheDocument();
    expect(Number(strip.getByTestId("block-move-handle").getAttribute("width"))).toBeGreaterThanOrEqual(44);
  });

  it("colours the twilight bands in dusk→dawn order — day red, nautical blue, night black, blended not stepped", () => {
    renderPane();
    const axis = screen.getByTestId("strip-axis");
    // The per-band rects still carry data-band/data-level (hover title,
    // and this ordering check); their own `fill` is transparent now — the
    // colour comes from one shared gradient underneath (next assertion).
    const bands = Array.from(axis.querySelectorAll("rect[data-band]"));
    expect(bands.map((b) => b.getAttribute("data-band"))).toEqual([
      "day",
      "civil",
      "nautical",
      "astronomical",
      "night",
      "astronomical",
      "nautical",
      "civil",
      "day",
    ]);
    expect(bands.every((b) => b.getAttribute("fill") === "transparent")).toBe(true);

    // One gradient carries the colour, fading through every band in dusk→
    // dawn order — day, civil, nautical, astronomical, night, and back —
    // via `style="stop-color: ..."` (CSS custom properties on an SVG
    // presentation attribute don't reliably resolve; the inline style does).
    const stops = Array.from(axis.querySelectorAll("linearGradient stop"));
    expect(stops.length).toBeGreaterThanOrEqual(bands.length);
    const stopColors = stops.map((s) => s.getAttribute("style"));
    expect(stopColors[0]).toContain("--ssc-band-day");
    expect(stopColors.some((c) => c?.includes("--ssc-band-night"))).toBe(true);
    const bandFillRect = axis.querySelector('rect[fill^="url(#"]');
    expect(bandFillRect).toBeInTheDocument();

    expect(within(axis).getByTestId("moon-band")).toBeInTheDocument();
  });

  it("clamps the axis to sunset−30min .. sunrise+30min, never wider for an item scheduled outside it", () => {
    const late: CompiledSchedule = {
      ...schedule,
      items: compiled.map((i) => (i.item_id === "c" ? { ...i, end_utc: "2026-08-29T10:00:00Z" } : i)),
    };
    renderPane({ schedule: late });
    const axis = screen.getByTestId("strip-axis");
    // 2026-08-28T18:30Z .. 2026-08-29T06:00Z is 11.5 h; at 1 px/min-equivalent
    // scaling the axis width stays whatever `axisRange` computes — the point
    // here is only that it did not stretch out to 10:00.
    const expectedRange = axisRange(NIGHT, late.items);
    expect(new Date(expectedRange.endMs).toISOString()).toBe("2026-08-29T06:00:00.000Z");
    expect(axis).toBeInTheDocument();
  });

  it("stagers the three dusk labels onto distinct rows on a narrow axis, not stacked on top of each other", () => {
    const range = axisRange(NIGHT, compiled);
    render(
      <TimelineStrip
        orientation="horizontal"
        range={range}
        axisPx={220}
        laneCount={1}
        crossAxisPx={0}
        bands={twilightBands(NIGHT, range)}
        events={snapTargets(NIGHT)}
        timeZone={NIGHT.tz}
        items={[]}
      />,
    );
    const ys = (["civil_dusk", "nautical_dusk", "astronomical_dusk"] as const).map((event) => {
      const text = screen.getByTestId("strip-axis").querySelector(`[data-event="${event}"] text`);
      return text?.getAttribute("y");
    });
    expect(new Set(ys).size).toBe(3);
  });

  it("draws a realtime marker at the current instant when it falls in the night", () => {
    const range = axisRange(NIGHT, compiled);
    const now = Date.parse("2026-08-28T23:00:00Z");
    render(
      <TimelineStrip
        orientation="horizontal"
        range={range}
        axisPx={690}
        laneCount={1}
        crossAxisPx={0}
        bands={twilightBands(NIGHT, range)}
        events={snapTargets(NIGHT)}
        timeZone={NIGHT.tz}
        currentTimeMs={now}
        items={[]}
      />,
    );
    const marker = screen.getByTestId("current-time-marker");
    expect(marker).toHaveTextContent("Now");
    expect(marker.querySelector("line")).toHaveAttribute(
      "x1",
      String(timeToPx(now, range, 690)),
    );
  });

  it("names an astronomical dusk that does not occur instead of leaving a blank", () => {
    const summer: CompiledSchedule = {
      ...schedule,
      night: {
        ...NIGHT,
        dark_start_utc: null,
        dark_end_utc: null,
        solar_events: NIGHT.solar_events.map((e) =>
          e.event === "astronomical_dusk" || e.event === "astronomical_dawn"
            ? { ...e, when_utc: null, outcome: "always_above" }
            : e,
        ),
      },
    };
    renderPane({ schedule: summer });
    expect(screen.getByTestId("missing-astronomical_dusk")).toHaveTextContent("Astronomical dusk does not occur");
    expect(screen.getByTestId("missing-astronomical_dawn")).toHaveTextContent("Astronomical dawn does not occur");
    expect(screen.getByTestId("strip-axis").querySelector('rect[data-band="night"]')).toBeNull();
  });

  it("places each block proportionally between start and end, with the altitude curve behind the target", async () => {
    renderPane();
    const axisPx = Number(screen.getByTestId("strip-axis").getAttribute("width"));
    const range = axisRange(NIGHT, compiled);
    const rect = within(screen.getByTestId("strip-b")).getByTestId("block-rect");
    const expectedX = timeToPx(Date.parse("2026-08-28T21:30:00Z"), range, axisPx);
    const expectedWidth = timeToPx(Date.parse("2026-08-28T23:30:00Z"), range, axisPx) - expectedX;
    expect(Number(rect.getAttribute("x"))).toBeCloseTo(expectedX, 5);
    expect(Number(rect.getAttribute("width"))).toBeCloseTo(expectedWidth, 5);
    expect(await within(screen.getByTestId("strip-b")).findByTestId("altitude-curve")).toBeInTheDocument();
    expect(within(screen.getByTestId("strip-a")).queryByTestId("altitude-curve")).toBeNull();
    await waitFor(() => {
      expect(screen.getByTestId("moon-summary")).toHaveTextContent("Moon 63% lit, waxing");
    });
  });

  it("draws the Moon's altitude fetched from /api/astro/moon-altitude", async () => {
    stubNightFetch([
      { at: "2026-08-28T18:30:00Z", alt_deg: -5 },
      { at: "2026-08-29T00:15:00Z", alt_deg: 40 },
      { at: "2026-08-29T06:00:00Z", alt_deg: -5 },
    ]);
    renderPane();
    await waitFor(() => {
      expect(screen.getByTestId("moon-summary")).toHaveTextContent("Moon 63% lit");
    });
    expect(await within(screen.getByTestId("strip-axis")).findByTestId("moon-curve")).toBeInTheDocument();
    expect(screen.getByTestId("moon-summary")).toHaveTextContent("Moon altitude curve");
  });

  it("keeps the live clock over item blocks instead of hiding it under their fills", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-28T22:00:00Z"));
    renderPane();
    const axis = screen.getByTestId("strip-axis");
    const block = screen.getByTestId("strip-b");
    const marker = screen.getByTestId("current-time-marker");
    expect(block.compareDocumentPosition(marker) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(marker.querySelectorAll("line")).toHaveLength(2);
    vi.useRealTimers();
    expect(axis).toContainElement(marker);
  });

  it("shows target altitude and time-dependent Moon separation on a block hover", async () => {
    renderPane();
    const block = screen.getByTestId("block-b");
    await within(block).findByTestId("altitude-curve");
    fireEvent.pointerEnter(block, { clientX: 400, clientY: 80 });
    expect(within(screen.getByTestId("strip-b")).getByTestId("hover-details")).toHaveTextContent("Target altitude");
    expect(screen.getByTestId("hover-details")).toHaveTextContent(/Moon separation \d+\.\d°/);
    fireEvent.pointerLeave(block);
    expect(screen.queryByTestId("hover-details")).toBeNull();
  });

  it("shows actual run states in the list and translucent block fills, only for the matching plan and night", () => {
    const run: RunState = {
      run_id: "r1", plan_id: "p1", plan_name: "Night", plan_revision: 1,
      night_date: NIGHT.date, status: "running", started_at: "2026-08-28T19:00:00Z",
      ended_at: null, ended_reason: null, current_item_id: "b", gate_held: false,
      gate_reason: null, last_decision: null, last_decision_at: null, journal_length: 3,
      items: [
        { item_id: "a", kind: "startup", enabled: true, status: "done", attempt: 1, panel_index: null, held: false, reason: "", start_utc: null, end_utc: null },
        { item_id: "b", kind: "goto_and_stack", enabled: true, status: "running", attempt: 1, panel_index: null, held: false, reason: "", start_utc: null, end_utc: null },
        { item_id: "c", kind: "shutdown", enabled: true, status: "failed", attempt: 1, panel_index: null, held: false, reason: "", start_utc: null, end_utc: null },
      ],
    };
    renderPane({ run });
    expect(screen.getByTestId("run-status-a")).toHaveTextContent("COMPLETED");
    expect(screen.getByTestId("run-status-b")).toHaveTextContent("IN PROGRESS");
    expect(screen.getByTestId("run-status-c")).toHaveTextContent("FAILED");
    for (const [id, fill] of [["a", "success"], ["b", "accent"], ["c", "danger"]] as const) {
      const block = within(screen.getByTestId(`strip-${id}`)).getByTestId("block-rect");
      expect(block).toHaveAttribute("fill", `var(--ssc-${fill})`);
      expect(block).toHaveAttribute("fill-opacity", "0.35");
    }
    cleanup();
    renderPane({ run: { ...run, night_date: "2026-08-27" } });
    expect(screen.getByTestId("run-status-a")).toHaveTextContent("PENDING");
  });

  it("draws no Moon-altitude line — never a flat one — when /api/astro/moon-altitude fails", async () => {
    stubNightFetch("error");
    renderPane();
    await waitFor(() => {
      expect(screen.getByTestId("moon-summary")).toHaveTextContent("Moon 63% lit");
    });
    // The request failed outright — no line at all, never a flat guess.
    expect(screen.queryByTestId("moon-curve")).toBeNull();
  });

  it("draws the Moon's altitude as one grey line when samples exist, and nothing — not a flat line — when they do not", () => {
    const range = axisRange(NIGHT, compiled);
    const strip = (altitude: { when: string; altitude_deg: number }[] | undefined) => (
      <TimelineStrip
        orientation="horizontal"
        range={range}
        axisPx={800}
        laneCount={1}
        crossAxisPx={0}
        bands={twilightBands(NIGHT, range)}
        events={snapTargets(NIGHT)}
        moon={{ intervals: [], illuminatedFraction: 0.5, waxing: false, altitude }}
        moonAltitude={altitude}
        timeZone={NIGHT.tz}
        items={[]}
      />
    );
    const { rerender } = render(strip(undefined));
    expect(screen.queryByTestId("moon-curve")).toBeNull();
    rerender(
      strip([
        { when: "2026-08-28T22:00:00Z", altitude_deg: 0 },
        { when: "2026-08-29T00:00:00Z", altitude_deg: 45 },
        { when: "2026-08-29T02:00:00Z", altitude_deg: 0 },
      ]),
    );
    const line = screen.getByTestId("moon-curve");
    expect(line).toHaveAttribute("stroke", "var(--ssc-moon)");
    const ys = (line.getAttribute("points") ?? "").split(" ").map((p) => Number(p.split(",")[1]));
    expect(ys).toEqual([HEADER_LANE_PX, HEADER_LANE_PX / 2, HEADER_LANE_PX]);
  });

  it("annotates a diagnostic on its block as a badge with the severity, and hatches a disabled item", () => {
    renderPane();
    const badge = within(screen.getByTestId("strip-b")).getByTestId("block-badge");
    expect(badge).toHaveAttribute("data-severity", "warning");
    expect(badge).toHaveTextContent("warning: moon too close");
    expect(within(screen.getByTestId("strip-b")).getByTestId("block-rect")).toHaveAttribute("data-severity", "warning");
    expect(within(screen.getByTestId("strip-c")).getByTestId("block-disabled")).toBeInTheDocument();
  });

  it("clips each item's altitude curve to its own scheduled span, not the whole night", async () => {
    renderPane();
    const curve = await within(screen.getByTestId("strip-b")).findByTestId("altitude-curve");
    const points = (curve.getAttribute("points") ?? "").split(" ").filter((p) => p !== "");
    // The stub's altitude_curve has samples at 19:00Z, 23:00Z and 05:00Z;
    // only 23:00Z falls inside "b"'s own 21:30–23:30Z slot. The other two
    // are real points on the night's own axis but outside this item's
    // capture, and must not appear on its curve.
    expect(points).toHaveLength(1);
    const axisPx = Number(screen.getByTestId("strip-axis").getAttribute("width"));
    const range = axisRange(NIGHT, compiled);
    const expectedX = timeToPx(Date.parse("2026-08-28T23:00:00Z"), range, axisPx);
    const [x] = (points[0] ?? "").split(",").map(Number);
    expect(x).toBeCloseTo(expectedX, 1);
  });

  it("draws a red altitude-limit line across the item's own clipped span when it sets one, and none when it doesn't", () => {
    const withLimit: PlanItem[] = items.map((item) =>
      item.id === "b" && "constraints" in item ? { ...item, constraints: { ...item.constraints, min_alt_deg: 30 } } : item,
    );
    renderPane({ items: withLimit });
    const line = within(screen.getByTestId("strip-b")).getByTestId("min-alt-line");
    expect(line).toHaveAttribute("stroke", "var(--ssc-danger)");
    expect(within(screen.getByTestId("strip-a")).queryByTestId("min-alt-line")).toBeNull();
    expect(within(screen.getByTestId("strip-c")).queryByTestId("min-alt-line")).toBeNull();
  });
});

describe("TimelinePane — one shared strip, lanes", () => {
  it("keeps sequential, non-overlapping items on the strip's one shared lane", () => {
    renderPane();
    const yOf = (id: string) => Number(within(screen.getByTestId(`strip-${id}`)).getByTestId("block-rect").getAttribute("y"));
    // a: 19:00–19:10Z, b: 21:30–23:30Z, c: 23:30–23:30Z — none overlap, so
    // the single device's schedule draws on one lane, not three.
    expect(yOf("b")).toBe(yOf("a"));
    expect(yOf("c")).toBe(yOf("a"));
  });

  it("keeps genuinely overlapping items on the shared lane", () => {
    const overlapping: CompiledSchedule = {
      ...schedule,
      items: compiled.map((i) => (i.item_id === "b" ? { ...i, start_utc: "2026-08-28T19:05:00Z", end_utc: "2026-08-28T19:15:00Z" } : i)),
    };
    renderPane({ schedule: overlapping });
    const yOf = (id: string) => Number(within(screen.getByTestId(`strip-${id}`)).getByTestId("block-rect").getAttribute("y"));
    // b now overlaps a (19:00–19:10 vs 19:05–19:15): both remain overlaid
    // on the shared lane rather than jumping into a second row.
    expect(yOf("b")).toBe(yOf("a"));
    // c (23:30–23:30, after both) remains on the same lane as well.
    expect(yOf("c")).toBe(yOf("a"));
  });
});

describe("TimelinePane — gestures", () => {
  it("a drag released within the snap radius of astronomical dusk emits an event TimeSpec for it — and is not a click", () => {
    const { onEditItem, onSelectItem } = renderPane();
    const axisPx = Number(screen.getByTestId("strip-axis").getAttribute("width"));
    const range = axisRange(NIGHT, compiled);
    const startPx = timeToPx(Date.parse("2026-08-28T19:00:00Z"), range, axisPx);
    const duskPx = timeToPx(Date.parse("2026-08-28T21:00:00Z"), range, axisPx);
    const handle = within(screen.getByTestId("strip-a")).getByTestId("block-move-handle");
    fireEvent.pointerDown(handle, { clientX: 100, clientY: 10, button: 0, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 100 + (duskPx - startPx) + 5, clientY: 10, pointerId: 1 });
    expect(within(screen.getByTestId("strip-a")).getByTestId("drag-label")).toHaveTextContent("Astronomical dusk");
    expect(onEditItem).not.toHaveBeenCalled();
    fireEvent.pointerUp(handle, { clientX: 100 + (duskPx - startPx) + 5, clientY: 10, pointerId: 1 });
    expect(onEditItem).toHaveBeenCalledWith("a", { start: { kind: "event", event: "astronomical_dusk", offset_min: 0 } });
    expect(onSelectItem).not.toHaveBeenCalled();
  });

  it("a drag released away from any event emits an absolute start in the site's zone; a resize emits a duration", () => {
    const { onEditItem } = renderPane();
    const axisPx = Number(screen.getByTestId("strip-axis").getAttribute("width"));
    const range = axisRange(NIGHT, compiled);
    const hourPx = timeToPx(range.startMs + 3_600_000, range, axisPx);
    const move = within(screen.getByTestId("strip-b")).getByTestId("block-move-handle");
    fireEvent.pointerDown(move, { clientX: 300, clientY: 10, button: 0, pointerId: 1 });
    fireEvent.pointerMove(move, { clientX: 300 + hourPx, clientY: 10, pointerId: 1 });
    fireEvent.pointerUp(move, { clientX: 300 + hourPx, clientY: 10, pointerId: 1 });
    // 21:30Z + 1 h = 22:30Z = 23:30 BST.
    expect(onEditItem).toHaveBeenLastCalledWith("b", { start: { kind: "absolute", local_datetime: "2026-08-28T23:30:00", fold: 0 } });

    const resize = within(screen.getByTestId("strip-b")).getByTestId("block-resize-handle");
    fireEvent.pointerDown(resize, { clientX: 500, clientY: 10, button: 0, pointerId: 1 });
    fireEvent.pointerMove(resize, { clientX: 500 + hourPx, clientY: 10, pointerId: 1 });
    const durationLabel = within(screen.getByTestId("strip-b")).getByTestId("drag-label");
    expect(durationLabel).toHaveTextContent("180 min total");
    expect(durationLabel).toHaveAttribute("text-anchor", "middle");
    fireEvent.pointerUp(resize, { clientX: 500 + hourPx, clientY: 10, pointerId: 1 });
    expect(onEditItem).toHaveBeenLastCalledWith("b", { end: { kind: "duration", minutes: 180 } });
  });

  it("snaps a start resize near the previous end and preserves the fixed end", () => {
    const { onEditItem } = renderPane({ selectedItemId: "b" });
    const axisPx = Number(screen.getByTestId("strip-axis").getAttribute("width"));
    const range = axisRange(NIGHT, compiled);
    const startPx = timeToPx(Date.parse("2026-08-28T21:30:00Z"), range, axisPx);
    const previousEndPx = timeToPx(Date.parse("2026-08-28T19:10:00Z"), range, axisPx);
    const handle = within(screen.getByTestId("strip-b")).getByTestId("block-resize-start-handle");
    const releasedAt = 300 + previousEndPx - startPx + 3;
    fireEvent.pointerDown(handle, { clientX: 300, button: 0, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: releasedAt, pointerId: 1 });
    fireEvent.pointerUp(handle, { clientX: releasedAt, pointerId: 1 });
    expect(onEditItem).toHaveBeenLastCalledWith("b", {
      start: { kind: "relative", offset_min: 0 },
      end: { kind: "duration", minutes: 260 },
    });
  });

  it("does not keep panning after background capture is lost during a zoomed edit", () => {
    const { onEditItem } = renderPane({ selectedItemId: "b" });
    const viewport = screen.getByTestId("timeline-viewport");
    fireEvent.wheel(viewport, { deltaY: -500, clientX: 500 });
    viewport.setPointerCapture = vi.fn();
    fireEvent.pointerDown(screen.getByTestId("strip-axis"), { clientX: 400, clientY: 10, button: 0, pointerId: 1, pointerType: "mouse" });
    fireEvent.lostPointerCapture(viewport, { pointerId: 1 });
    const strip = within(screen.getByTestId("strip-b"));
    const originalStart = strip.getByTestId("block-rect").getAttribute("x");
    const handle = strip.getByTestId("block-resize-handle");
    fireEvent.pointerDown(handle, { clientX: 400, clientY: 10, button: 0, pointerId: 1, pointerType: "mouse" });
    fireEvent.pointerMove(handle, { clientX: 420, clientY: 10, pointerId: 1, pointerType: "mouse" });
    expect(strip.getByTestId("block-rect")).toHaveAttribute("x", originalStart);
    fireEvent.pointerUp(handle, { clientX: 420, clientY: 10, pointerId: 1, pointerType: "mouse" });
    expect(onEditItem).toHaveBeenCalledOnce();
  });

  it.each(["block-move-handle", "block-resize-start-handle", "block-resize-handle"])("cancels interrupted %s capture and accepts the next zoomed gesture", (handleName) => {
    const { onEditItem } = renderPane({ selectedItemId: "b" });
    fireEvent.wheel(screen.getByTestId("timeline-viewport"), { deltaY: -500, clientX: 500 });
    const strip = within(screen.getByTestId("strip-b"));
    const handle = strip.getByTestId(handleName);
    const original = strip.getByTestId("block-rect").getAttribute("x");
    fireEvent.pointerDown(handle, { clientX: 400, clientY: 10, button: 0, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 440, clientY: 10, pointerId: 1 });
    expect(strip.getByTestId("drag-label")).toBeInTheDocument();
    // The browser can lose capture when a handle is replaced during saving,
    // or a touch gesture takes ownership. No pointerup reaches that handle.
    act(() => {
      fireEvent.lostPointerCapture(handle, { pointerId: 1 });
      fireEvent.pointerMove(handle, { clientX: 445, clientY: 10, pointerId: 1 });
    });
    expect(strip.queryByTestId("drag-label")).toBeNull();
    expect(strip.getByTestId("block-rect")).toHaveAttribute("x", original);
    expect(onEditItem).not.toHaveBeenCalled();
    fireEvent.pointerDown(handle, { clientX: 400, clientY: 10, button: 0, pointerId: 2 });
    fireEvent.pointerMove(handle, { clientX: 420, clientY: 10, pointerId: 2 });
    fireEvent.pointerUp(handle, { clientX: 420, clientY: 10, pointerId: 2 });
    expect(onEditItem).toHaveBeenCalledTimes(1);
    expect(strip.queryByTestId("drag-label")).toBeNull();
  });

  it("a move can cross the previous item, while a near-edge drop still snaps to it", () => {
    const { onEditItem } = renderPane();
    const axisPx = Number(screen.getByTestId("strip-axis").getAttribute("width"));
    const range = axisRange(NIGHT, compiled);
    const bStartPx = timeToPx(Date.parse("2026-08-28T21:30:00Z"), range, axisPx);
    const aEndPx = timeToPx(Date.parse("2026-08-28T19:10:00Z"), range, axisPx); // "a"'s own compiled end
    const farPastPx = timeToPx(Date.parse("2026-08-28T17:00:00Z"), range, axisPx); // well before "a" even starts

    const dragTo = (targetPx: number) => {
      const move = within(screen.getByTestId("strip-b")).getByTestId("block-move-handle");
      fireEvent.pointerDown(move, { clientX: 300, clientY: 10, button: 0, pointerId: 1 });
      fireEvent.pointerMove(move, { clientX: 300 + (targetPx - bStartPx), clientY: 10, pointerId: 1 });
      fireEvent.pointerUp(move, { clientX: 300 + (targetPx - bStartPx), clientY: 10, pointerId: 1 });
    };

    dragTo(farPastPx);
    expect(onEditItem).toHaveBeenLastCalledWith("b", { start: { kind: "absolute", local_datetime: "2026-08-28T18:00:00", fold: 0 } });

    dragTo(aEndPx + 3);
    expect(onEditItem).toHaveBeenLastCalledWith("b", { start: { kind: "relative", offset_min: 0 } });
  });

  it("a resize drag past the next enabled item's start clamps there, never overlapping it", () => {
    const itemsWithC: PlanItem[] = items.map((it) => (it.id === "c" ? { ...it, enabled: true } : it));
    const compiledWithC: CompiledItem[] = compiled.map((it) => (it.item_id === "c" ? { ...it, enabled: true } : it));
    const { onEditItem } = renderPane({ items: itemsWithC, schedule: { ...schedule, items: compiledWithC } });
    const axisPx = Number(screen.getByTestId("strip-axis").getAttribute("width"));
    const range = axisRange(NIGHT, compiled);
    const hourPx = timeToPx(range.startMs + 3_600_000, range, axisPx);
    const resize = within(screen.getByTestId("strip-b")).getByTestId("block-resize-handle");
    fireEvent.pointerDown(resize, { clientX: 500, clientY: 10, button: 0, pointerId: 1 });
    fireEvent.pointerMove(resize, { clientX: 500 + hourPx, clientY: 10, pointerId: 1 });
    fireEvent.pointerUp(resize, { clientX: 500 + hourPx, clientY: 10, pointerId: 1 });
    // "c" now starts exactly when "b" ends (23:30Z) — no room to grow into
    // it: the resize clamps the duration back to its original 120 min, not
    // the 180 min the same drag produces when nothing is in the way (the
    // test above this one).
    expect(onEditItem).toHaveBeenLastCalledWith("b", { end: { kind: "duration", minutes: 120 } });
  });

  it("resizes and sticks to the chronologically next block after items have crossed plan order", () => {
    const crossedCompiled = compiled.map((it) =>
      it.item_id === "a"
        ? { ...it, start_utc: "2026-08-28T23:45:00Z", end_utc: "2026-08-28T23:55:00Z" }
        : it,
    );
    const { onEditItem } = renderPane({ schedule: { ...schedule, items: crossedCompiled } });
    const axisPx = Number(screen.getByTestId("strip-axis").getAttribute("width"));
    const range = axisRange(NIGHT, crossedCompiled);
    const hourPx = timeToPx(range.startMs + 3_600_000, range, axisPx);
    const resize = within(screen.getByTestId("strip-b")).getByTestId("block-resize-handle");
    fireEvent.pointerDown(resize, { clientX: 500, clientY: 10, button: 0, pointerId: 1 });
    fireEvent.pointerMove(resize, { clientX: 500 + hourPx, clientY: 10, pointerId: 1 });
    expect(within(screen.getByTestId("strip-b")).getByTestId("drag-label")).toHaveTextContent("touching next item");
    fireEvent.pointerUp(resize, { clientX: 500 + hourPx, clientY: 10, pointerId: 1 });
    expect(onEditItem).toHaveBeenLastCalledWith("b", { end: { kind: "duration", minutes: 135 } });
  });

  it("keeps a second-precision sticky edge after recompilation and allows another resize", () => {
    const boundary = "2026-08-28T23:45:37.250Z";
    const withNext = compiled.map((it) => it.item_id === "a" ? { ...it, start_utc: boundary, end_utc: "2026-08-28T23:55:37.250Z" } : it);
    const props = { schedule: { ...schedule, items: withNext }, selectedItemId: "b", isPending: false, isError: false, onSelectItem: vi.fn() };
    const onEditItem = vi.fn<(id: string, patch: ItemPatch) => void>();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(<QueryClientProvider client={client}><TimelinePane {...props} items={items} onEditItem={onEditItem} /></QueryClientProvider>);
    const gesture = (delta: number) => {
      const resize = within(screen.getByTestId("strip-b")).getByTestId("block-resize-handle");
      fireEvent.pointerDown(resize, { clientX: 500, pointerId: 1 });
      fireEvent.pointerMove(resize, { clientX: 500 + delta, pointerId: 1 });
      fireEvent.pointerUp(resize, { clientX: 500 + delta, pointerId: 1 });
    };
    gesture(150);
    const patch = onEditItem.mock.calls[0]?.[1];
    if (patch === undefined) throw new Error("Resize was not committed");
    expect(patch.end?.minutes).toBeCloseTo(135 + 37.25 / 60, 9);
    const minutes = patch.end?.minutes;
    if (minutes === undefined) throw new Error("Resize did not produce a duration");
    expect(Date.parse("2026-08-28T21:30:00Z") + minutes * 60_000).toBe(Date.parse(boundary));
    // Reflect the real saved duration and compiler's new geometry, unlike
    // the former browser audit that always returned the old compiled spans.
    const nextItems = items.map((it) => it.id === "b" ? { ...it, ...patch } : it);
    const nextSchedule = { ...schedule, items: withNext.map((it) => it.item_id === "b" ? { ...it, end_utc: boundary } : it) };
    view.rerender(<QueryClientProvider client={client}><TimelinePane {...props} items={nextItems} schedule={nextSchedule} onEditItem={onEditItem} /></QueryClientProvider>);
    gesture(-30);
    expect(onEditItem).toHaveBeenCalledTimes(2);
    expect(onEditItem.mock.calls[1]?.[1].end?.minutes).toBeLessThan(minutes);
    view.unmount();
    client.clear();
  });

  it("recovers a sticky boundary from an already slightly overlapping saved block", () => {
    const overlapping = compiled.map((it) => it.item_id === "a"
      ? { ...it, start_utc: "2026-08-28T23:29:37Z", end_utc: "2026-08-28T23:40:00Z" } : it);
    const { onEditItem } = renderPane({ schedule: { ...schedule, items: overlapping }, selectedItemId: "b" });
    const resize = within(screen.getByTestId("strip-b")).getByTestId("block-resize-handle");
    fireEvent.pointerDown(resize, { clientX: 500, pointerId: 1 });
    fireEvent.pointerMove(resize, { clientX: 550, pointerId: 1 });
    fireEvent.pointerUp(resize, { clientX: 550, pointerId: 1 });
    expect(onEditItem).toHaveBeenLastCalledWith("b", { end: { kind: "duration", minutes: 119 + 37 / 60 } });
  });

  it("keeps the short block's two resize targets inside its own edges without overlap", () => {
    renderPane({ selectedItemId: "a" });
    const strip = within(screen.getByTestId("strip-a"));
    const rect = strip.getByTestId("block-rect");
    const left = Number(rect.getAttribute("x")), right = left + Number(rect.getAttribute("width"));
    const start = strip.getByTestId("block-resize-start-handle"), end = strip.getByTestId("block-resize-handle");
    expect(Number(start.getAttribute("x"))).toBeGreaterThanOrEqual(left);
    expect(Number(start.getAttribute("x")) + Number(start.getAttribute("width"))).toBeLessThanOrEqual(Number(end.getAttribute("x")));
    expect(Number(end.getAttribute("x")) + Number(end.getAttribute("width"))).toBeLessThanOrEqual(right);
  });

  it("shows an anchor glyph on a block whose end names an event, with a tooltip explaining a resize converts it to a duration", () => {
    const anchoredItems: PlanItem[] = [
      {
        id: "a",
        kind: "startup",
        start: { kind: "event", event: "sunset", offset_min: 0 },
        end: { kind: "event", event: "astronomical_dusk", offset_min: 0 },
        enabled: true,
      },
    ];
    const anchoredCompiled: CompiledItem[] = [
      {
        item_id: "a",
        kind: "startup",
        enabled: true,
        start_utc: "2026-08-28T19:00:00Z",
        end_utc: "2026-08-28T21:00:00Z",
        resolution: "Sunset → 20:00",
        end_resolution: "Astronomical dusk → 22:00",
        panels: [],
        diagnostics: [],
      },
    ];
    renderPane({ schedule: { ...schedule, items: anchoredCompiled }, items: anchoredItems });
    const strip = screen.getByTestId("strip-a");
    expect(within(strip).getByTestId("end-anchor")).toBeInTheDocument();
    const resizeHandle = within(strip).getByTestId("block-resize-handle");
    expect(resizeHandle.querySelector("title")?.textContent).toMatch(/Converts "until Astronomical dusk" to a duration/);
  });

  it("a resize of an event-anchored block still commits a plain duration on release", () => {
    const anchoredItems: PlanItem[] = [
      {
        id: "b",
        kind: "goto_and_stack",
    live_stack: true,
        start: { kind: "absolute", local_datetime: "2026-08-28T22:30:00", fold: 0 },
        end: { kind: "event", event: "astronomical_dawn", offset_min: 0 },
        enabled: true,
        target: { kind: "catalog", name: "M31", ra_j2000_h: 0.712, dec_j2000_deg: 41.27 },
        framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
        constraints: {},
        autofocus_before: false,
        exposure_sec: 10,
        gain: 200,
        filter_name: null,
      },
    ];
    const { onEditItem } = renderPane({ schedule, items: anchoredItems });
    const axisPx = Number(screen.getByTestId("strip-axis").getAttribute("width"));
    const range = axisRange(NIGHT, compiled);
    const hourPx = timeToPx(range.startMs + 3_600_000, range, axisPx);
    const resize = within(screen.getByTestId("strip-b")).getByTestId("block-resize-handle");
    fireEvent.pointerDown(resize, { clientX: 500, clientY: 10, button: 0, pointerId: 1 });
    fireEvent.pointerMove(resize, { clientX: 500 + hourPx, clientY: 10, pointerId: 1 });
    fireEvent.pointerUp(resize, { clientX: 500 + hourPx, clientY: 10, pointerId: 1 });
    expect(onEditItem).toHaveBeenLastCalledWith("b", { end: { kind: "duration", minutes: 180 } });
  });

  it("a tap on a block selects it without writing", () => {
    const { onEditItem, onSelectItem } = renderPane();
    const handle = within(screen.getByTestId("strip-b")).getByTestId("block-move-handle");
    fireEvent.pointerDown(handle, { clientX: 100, clientY: 10, button: 0, pointerId: 1 });
    fireEvent.pointerUp(handle, { clientX: 101, clientY: 10, pointerId: 1 });
    expect(onSelectItem).toHaveBeenCalledWith("b");
    expect(onEditItem).not.toHaveBeenCalled();
  });

  it("links a row to its block: hovering either highlights both", () => {
    renderPane();
    const row = screen.getByTestId("row-b");
    const block = within(screen.getByTestId("strip-b")).getByTestId(`block-b`);
    const rect = within(screen.getByTestId("strip-b")).getByTestId("block-rect");
    expect(row).not.toHaveAttribute("data-hovered");
    expect(rect).not.toHaveAttribute("data-hovered");

    fireEvent.pointerEnter(row);
    expect(row).toHaveAttribute("data-hovered", "true");
    expect(rect).toHaveAttribute("data-hovered", "true");
    fireEvent.pointerLeave(row);
    expect(row).not.toHaveAttribute("data-hovered");
    expect(rect).not.toHaveAttribute("data-hovered");

    fireEvent.pointerEnter(block);
    expect(rect).toHaveAttribute("data-hovered", "true");
    expect(row).toHaveAttribute("data-hovered", "true");
    fireEvent.pointerLeave(block);
    expect(rect).not.toHaveAttribute("data-hovered");
    expect(row).not.toHaveAttribute("data-hovered");
  });

  it("reorder works on collapsed rows while toggle and edit stay selected-only", async () => {
    const user = userEvent.setup();
    const { onReorderItem, onToggleItem, onSelectItem } = renderPane({ selectedItemId: "a", editor: <div data-testid="editor">editor</div> });

    const collapsedHandle = screen.getByRole("button", { name: "Reorder item 2" });
    expect(collapsedHandle).toBeEnabled();
    fireEvent.keyDown(collapsedHandle, { key: "ArrowUp", altKey: true });
    expect(onReorderItem).toHaveBeenCalledWith("b", 0);
    expect(screen.queryByRole("switch", { name: /item 2/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit item 2" })).toBeNull();

    const handle = screen.getByRole("button", { name: "Reorder item 1" });
    fireEvent.pointerDown(handle, { clientX: 5, clientY: 5, button: 0, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 5, clientY: 400, pointerId: 1 });
    fireEvent.pointerUp(handle, { clientX: 5, clientY: 400, pointerId: 1 });
    expect(onReorderItem).toHaveBeenCalledWith("a", 2);

    const toggle = screen.getByRole("switch", { name: "Disable item 1" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    expect(onToggleItem).toHaveBeenCalledWith("a", false);

    const [first, second] = within(screen.getByRole("list", { name: "Plan items" })).getAllByRole("listitem");
    if (first === undefined || second === undefined) throw new Error("expected rows");
    expect(within(first).getByTestId("editor")).toBeInTheDocument();
    expect(within(second).queryByTestId("editor")).toBeNull();
    expect(screen.getByRole("button", { name: "Edit item 1" })).toHaveAttribute("aria-expanded", "true");

    // Selecting item "b" by its name (the one thing every row keeps,
    // selected or not) reveals its own gutter — including a keyboard
    // reorder, which needed selecting it first.
    await user.click(screen.getByRole("button", { name: /^2\. / }));
    expect(onSelectItem).toHaveBeenCalledWith("b");
  });

  it("a keyboard reorder (Alt+↑/↓) works once its row is selected", async () => {
    const user = userEvent.setup();
    const { onReorderItem } = renderPane({ selectedItemId: "b" });
    await user.click(screen.getByRole("button", { name: "Reorder item 2" }));
    await user.keyboard("{Alt>}{ArrowUp}{/Alt}");
    expect(onReorderItem).toHaveBeenCalledWith("b", 0);
  });

  it("a click expands the item — revealing its gutter — a second click on the block or row collapses it, Escape collapses too", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [selected, setSelected] = useState<string | null>(null);
      return (
        <TimelinePane
          schedule={schedule}
          isPending={false}
          isError={false}
          selectedItemId={selected}
          onSelectItem={setSelected}
          items={items}
          editor={<div data-testid="editor">editor</div>}
        />
      );
    }
    renderWithQueryClient(<Harness />);
    const block = () => within(screen.getByTestId("strip-b")).getByTestId("block-move-handle");
    const tap = (target: Element) => {
      fireEvent.pointerDown(target, { clientX: 50, clientY: 10, button: 0, pointerId: 1 });
      fireEvent.pointerUp(target, { clientX: 51, clientY: 10, pointerId: 1 });
    };
    expect(screen.queryByTestId("editor")).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit item 2" })).toBeNull();
    tap(block());
    expect(screen.getByRole("button", { name: "Edit item 2" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("editor")).toBeInTheDocument();
    tap(block());
    // Collapsed again: the chevron (and the rest of item 2's gutter) is
    // gone entirely, not merely `aria-expanded="false"` — the collapsed
    // view is the timeline plus item 2's name, nothing else.
    expect(screen.queryByRole("button", { name: "Edit item 2" })).toBeNull();
    expect(screen.queryByTestId("editor")).toBeNull();

    await user.click(screen.getByRole("button", { name: /^2\. / }));
    expect(screen.getByRole("button", { name: "Edit item 2" })).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getByRole("button", { name: /^1\. / }));
    expect(screen.getByRole("button", { name: "Edit item 1" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByRole("button", { name: "Edit item 2" })).toBeNull();
    await user.click(screen.getByRole("button", { name: /^1\. / }));
    expect(screen.queryByRole("button", { name: "Edit item 1" })).toBeNull();

    await user.click(screen.getByRole("button", { name: /^1\. / }));
    expect(screen.getByTestId("editor")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("editor")).toBeNull();
  });

  it("offers all item kinds in the add menu and adds the chosen one", async () => {
    const user = userEvent.setup();
    const { onAddItem } = renderPane();
    const options = within(screen.getByLabelText("Add item")).getAllByRole("option");
    expect(options.map((o) => (o as HTMLOptionElement).value)).toEqual([...NORMAL_ITEM_KINDS, ...ADVANCED_ITEM_KINDS]);
    expect(options).toHaveLength(ITEM_KINDS.length);
    await user.selectOptions(screen.getByLabelText("Add item"), "capture_calibration");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(onAddItem).toHaveBeenCalledWith("capture_calibration");
  });

  it("marks the unsaved item and lists an unschedulable one without a block", () => {
    const unschedulable: CompiledSchedule = {
      ...schedule,
      items: schedule.items.map((i) => (i.item_id === "b" ? { ...i, start_utc: null, end_utc: null, resolution: "never rises" } : i)),
    };
    renderPane({ schedule: unschedulable, dirtyItemId: "a" });
    expect(screen.getByText("unsaved")).toBeInTheDocument();
    expect(screen.getByText(/unschedulable — never rises/)).toBeInTheDocument();
    expect(within(screen.getByTestId("strip-b")).queryByTestId("block-rect")).toBeNull();
  });

  it("says why when the compile failed, and still lists the items", () => {
    renderPane({ schedule: undefined, isError: true, errorMessage: "no observing location is set" });
    expect(screen.getByRole("alert")).toHaveTextContent("no observing location is set");
    expect(within(screen.getByRole("list", { name: "Plan items" })).getAllByRole("listitem")).toHaveLength(3);
  });
});

describe("TimelinePane — orientation and budget", () => {
  it("consumes wheel zoom so the scheduling page does not scroll vertically", () => {
    renderPane();
    const viewport = screen.getByTestId("timeline-viewport");
    const wheel = new WheelEvent("wheel", { bubbles: true, cancelable: true, deltaY: 120, clientX: 200 });

    const propagated = viewport.dispatchEvent(wheel);

    expect(propagated).toBe(false);
    expect(wheel.defaultPrevented).toBe(true);
  });

  it("keeps a readable horizontal axis on a phone", () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: query === BELOW_SM_QUERY,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    renderPane();
    const axis = screen.getByTestId("strip-axis");
    expect(axis).toHaveAttribute("data-orientation", "horizontal");
    // The strip's cross-axis (its SVG `width`, time running down its
    // height) fills the measured container width for the ordinary
    // single-lane case — `FALLBACK_AXIS_PX`, since jsdom has no
    // `ResizeObserver` — not the bare 44 px finger-target lane thickness
    // (real user report, 2026-09-08: "the gantt chart on mobile is
    // completely broken and unusable" — every overlaid label was clipped
    // against a 44 px canvas).
    expect(Number(axis.getAttribute("width"))).toBeGreaterThanOrEqual(FALLBACK_AXIS_PX);
    const rect = within(screen.getByTestId("strip-b")).getByTestId("block-rect");
    expect(Number(rect.getAttribute("width"))).toBeGreaterThan(0);
  });

  it("opens a current-night mobile timeline with the Now marker in view", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-28T22:00:00Z"));
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: query === BELOW_SM_QUERY,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    const clientWidth = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(360);

    renderPane();

    const viewport = screen.getByTestId("timeline-viewport");
    const marker = screen.getByTestId("current-time-marker");
    const markerX = Number(marker.querySelector("line")?.getAttribute("x1"));
    expect(viewport.scrollLeft).toBeGreaterThan(0);
    expect(markerX).toBeGreaterThanOrEqual(viewport.scrollLeft);
    expect(markerX).toBeLessThanOrEqual(viewport.scrollLeft + 360);
    clientWidth.mockRestore();
  });

  it("leaves one-finger touch panning to the mobile timeline scroller", () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: query === BELOW_SM_QUERY,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    renderPane();

    const viewport = screen.getByTestId("timeline-viewport");
    expect(viewport).toHaveClass("overflow-x-auto");
    expect(viewport).toHaveStyle({ touchAction: "auto" });

    // Timeline blocks retain exclusive touch gestures for deliberate move and
    // resize interactions instead of turning those gestures into scrolling.
    const moveHandle = within(screen.getByTestId("strip-b")).getByTestId("block-move-handle");
    expect(moveHandle).toHaveStyle({ touchAction: "none" });
  });

  it("keeps a multi-hour idle gap on the shared horizontal axis", () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: query === BELOW_SM_QUERY,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    renderPane();
    // Item "a" (10 min) is followed by a long idle gap before item "b".
    // The horizontal axis keeps that gap visible while allowing the user to
    // pan it naturally inside the mobile scroller.
    const rectA = within(screen.getByTestId("strip-a")).getByTestId("block-rect");
    const rectB = within(screen.getByTestId("strip-b")).getByTestId("block-rect");
    const gapPx = Number(rectB.getAttribute("x")) - (Number(rectA.getAttribute("x")) + Number(rectA.getAttribute("width")));
    expect(gapPx).toBeGreaterThan(0);
  });

  it("renders a 20-item plan inside the frame budget", () => {
    const many: PlanItem[] = Array.from({ length: 20 }, (_, i) => ({
      id: `t${String(i)}`,
      kind: "goto_and_stack",
    live_stack: true,
      start: { kind: "relative", offset_min: 0 },
      end: { kind: "duration", minutes: 20 },
      enabled: true,
      target: { kind: "catalog", name: `Target ${String(i)}`, ra_j2000_h: i, dec_j2000_deg: 10 + i },
      framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
      constraints: {},
      autofocus_before: false,
      exposure_sec: 10,
      gain: 200,
      filter_name: null,
    }));
    const manyCompiled: CompiledItem[] = many.map((item, i) => ({
      item_id: item.id,
      kind: item.kind,
      enabled: true,
      start_utc: new Date(Date.parse("2026-08-28T20:00:00Z") + i * 25 * 60_000).toISOString(),
      end_utc: new Date(Date.parse("2026-08-28T20:20:00Z") + i * 25 * 60_000).toISOString(),
      resolution: "after previous",
      end_resolution: "Duration 20 min → after previous",
      panels: [],
      diagnostics: i % 3 === 0 ? [{ severity: "warning", item_id: item.id, code: "overlaps_previous", message: "overlaps" }] : [],
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    // One schedule object across renders, as TanStack hands the tab one.
    const manySchedule: CompiledSchedule = { ...schedule, items: manyCompiled };
    const ui = (selectedItemId: string | null) => (
      <QueryClientProvider client={client}>
        <TimelinePane
          schedule={manySchedule}
          isPending={false}
          isError={false}
          selectedItemId={selectedItemId}
          onSelectItem={vi.fn()}
          items={many}
          onEditItem={vi.fn()}
        />
      </QueryClientProvider>
    );
    const t0 = performance.now();
    const { rerender, unmount } = render(ui(null));
    const mountMs = performance.now() - t0;
    expect(screen.getAllByTestId("block-rect")).toHaveLength(20);
    // The per-frame case: the tree already mounted, one prop changes.
    const t1 = performance.now();
    rerender(ui("t5"));
    const rerenderMs = performance.now() - t1;
    // A scrub frame: one pointer move on a block re-renders that strip only.
    const handle = within(screen.getByTestId("strip-t5")).getByTestId("block-move-handle");
    fireEvent.pointerDown(handle, { clientX: 100, clientY: 10, button: 0, pointerId: 1 });
    const t2 = performance.now();
    fireEvent.pointerMove(handle, { clientX: 140, clientY: 10, pointerId: 1 });
    const dragStepMs = performance.now() - t2;
    fireEvent.pointerCancel(handle, { pointerId: 1 });
    unmount();
    client.clear();
    console.info(
      `[TimelinePane] 20-item plan: mount ${mountMs.toFixed(1)} ms, selection re-render ${rerenderMs.toFixed(1)} ms, drag step ${dragStepMs.toFixed(1)} ms (jsdom)`,
    );
    // The drag step is the per-frame path and is held to the 16 ms budget
    // even in jsdom; a whole-pane re-render (one strip plus twenty gutters
    // of HTML) measures ~13 ms here and is bounded loosely so a slow CI
    // box does not turn a measurement into a flake.
    expect(dragStepMs).toBeLessThan(16);
    expect(rerenderMs).toBeLessThan(40);
  });
});
