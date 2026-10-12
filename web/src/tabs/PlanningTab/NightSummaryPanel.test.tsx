// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { NightResponse } from "../../api/astro";
import { NightSummaryPanel } from "./NightSummaryPanel";

// See SessionStartField.test.tsx's own comment: `globals: false` means
// `@testing-library/react`'s auto-cleanup never registers, and this file
// renders more than once.
afterEach(cleanup);

const location: NightResponse["location"] = {
  latitude_deg: 48.1486,
  longitude_deg: 17.1077,
  elevation_m: 150,
  timezone: "Europe/Bratislava",
  source: "manual",
  accuracy_m: null,
  acquired_at: "2026-08-28T15:00:00Z",
};

// Half-lit is the ambiguous case by design: 0.5 is First Quarter *and*
// Last Quarter, and they are mirror images. `phase_angle_deg`/`waxing`
// are what disambiguate them (ssc/astro/events.py `MoonPhase`), so the
// fixture commits to one — First Quarter — rather than leaving it open.
const moon: NightResponse["moon"] = {
  illuminated_fraction: 0.5,
  phase_angle_deg: 90,
  waxing: true,
  separation_deg: null,
};

function nightResponse(overrides: Partial<NightResponse> = {}): NightResponse {
  return {
    location,
    window_start: "2026-08-28T15:00:00Z",
    window_end: "2026-08-29T06:00:00Z",
    solar_events: [
      { event: "sunset", when: "2026-08-28T17:43:16Z", outcome: "occurs", threshold_deg: -1.2 },
      { event: "civil_dusk", when: "2026-08-28T18:13:39Z", outcome: "occurs", threshold_deg: -6 },
      { event: "nautical_dusk", when: "2026-08-28T18:53:26Z", outcome: "occurs", threshold_deg: -12 },
      { event: "astronomical_dusk", when: "2026-08-28T19:36:43Z", outcome: "occurs", threshold_deg: -18 },
      { event: "astronomical_dawn", when: "2026-08-29T02:09:14Z", outcome: "occurs", threshold_deg: -18 },
      { event: "nautical_dawn", when: "2026-08-29T02:52:34Z", outcome: "occurs", threshold_deg: -12 },
      { event: "civil_dawn", when: "2026-08-29T03:32:23Z", outcome: "occurs", threshold_deg: -6 },
      { event: "sunrise", when: "2026-08-29T04:02:47Z", outcome: "occurs", threshold_deg: -1.2 },
    ],
    lunar_events: [],
    moon,
    target_events: null,
    altitude_curve: null,
    moon_separation_curve: null,
    horizon_profile_active: false,
    visibility_intervals: null,
    ...overrides,
  };
}

describe("NightSummaryPanel", () => {
  it("lists all eight solar events, civil/nautical/astronomical as equal rows", () => {
    render(<NightSummaryPanel night={nightResponse()} isPending={false} isError={false} />);
    expect(screen.getByText("Sunset")).toBeInTheDocument();
    expect(screen.getByText("Civil dusk")).toBeInTheDocument();
    expect(screen.getByText("Nautical dusk")).toBeInTheDocument();
    expect(screen.getByText("Astronomical dusk")).toBeInTheDocument();
    expect(screen.getByText("Astronomical dawn")).toBeInTheDocument();
    expect(screen.getByText("Nautical dawn")).toBeInTheDocument();
    expect(screen.getByText("Civil dawn")).toBeInTheDocument();
    expect(screen.getByText("Sunrise")).toBeInTheDocument();
  });

  it("says a non-occurring event does not occur, not a blank row (Tromsø midsummer)", () => {
    const night = nightResponse({
      solar_events: nightResponse().solar_events.map((e) =>
        e.event === "astronomical_dusk" ? { ...e, when: null, outcome: "always_above" as const } : e,
      ),
    });
    render(<NightSummaryPanel night={night} isPending={false} isError={false} />);
    expect(screen.getByText(/does not occur/i)).toBeInTheDocument();
  });

  it("shows a loading state rather than an empty list while pending", () => {
    render(<NightSummaryPanel night={undefined} isPending={true} isError={false} />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it("surfaces a fetch error rather than a silently empty summary", () => {
    render(<NightSummaryPanel night={undefined} isPending={false} isError={true} />);
    expect(screen.getByText(/could not load/i)).toBeInTheDocument();
  });
});
