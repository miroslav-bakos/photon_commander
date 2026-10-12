import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { NightResponse } from "../../api/astro";
import { renderWithQueryClient } from "../../test/queryClient";
import { NightAltitudeChart } from "./NightAltitudeChart";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const NIGHT_RESPONSE: NightResponse = {
  location: { latitude_deg: 51.5, longitude_deg: -0.1, elevation_m: null, timezone: "Europe/London", source: "manual", accuracy_m: null, acquired_at: "2026-08-28T00:00:00Z" },
  window_start: "2026-08-28T19:00:00Z",
  window_end: "2026-08-29T05:00:00Z",
  solar_events: [
    { event: "astronomical_dusk", when: "2026-08-28T21:00:00Z", outcome: "occurs", threshold_deg: -18 },
    { event: "astronomical_dawn", when: "2026-08-29T03:30:00Z", outcome: "occurs", threshold_deg: -18 },
  ],
  lunar_events: [
    { event: "moonrise", when: "2026-08-28T22:00:00Z", outcome: "occurs", threshold_deg: 0 },
    { event: "moonset", when: "2026-08-29T02:00:00Z", outcome: "occurs", threshold_deg: 0 },
  ],
  moon: { illuminated_fraction: 0.4, phase_angle_deg: 100, waxing: true, separation_deg: 45 },
  target_events: null,
  altitude_curve: [
    { when: "2026-08-28T19:00:00Z", altitude_deg: 10 },
    { when: "2026-08-29T00:00:00Z", altitude_deg: 60 },
    { when: "2026-08-29T05:00:00Z", altitude_deg: 5 },
  ],
  moon_separation_curve: null,
  horizon_profile_active: false,
  visibility_intervals: null,
};

it("keeps the ruler at actual sidebar pixels and restores it after Yearly view", () => {
  vi.spyOn(SVGElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, left: 0, top: 0, right: 350, bottom: 220, width: 350, height: 220, toJSON: () => ({}) });
  vi.stubGlobal("ResizeObserver", class { observe = vi.fn(); disconnect = vi.fn(); });
  renderWithQueryClient(<NightAltitudeChart night={NIGHT_RESPONSE} moonAltitude={NIGHT_RESPONSE.altitude_curve ?? []} annualAltitudeRequest={null} />);
  const svg = screen.getByRole("img");
  expect(svg.getAttribute("viewBox")?.split(" ")[2]).toBe("350");
  expect(svg.querySelector("text")?.style.fontSize).toBe("10px");
  expect(screen.getByTestId("planning-twilight-gradient")).toHaveAttribute("fill", expect.stringContaining("url(#"));
  expect(screen.getByTestId("planning-moon-band").querySelector("path")).not.toBeNull();
  expect(svg.querySelector('polyline[stroke="var(--ssc-moon)"]')).not.toBeNull();
  fireEvent.click(screen.getByRole("radio", { name: "Yearly" }));
  fireEvent.click(screen.getByRole("radio", { name: "Daily" }));
  expect(screen.getByRole("img").getAttribute("viewBox")?.split(" ")[2]).toBe("350");
});
