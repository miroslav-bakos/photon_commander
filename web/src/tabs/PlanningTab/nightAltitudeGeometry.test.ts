// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { NightResponse } from "../../api/astro";
import { axisRange, linearAxisMap } from "../SchedulingTab/timelineGeometry";
import { NIGHT } from "../SchedulingTab/testNight";
import {
  altitudeCurvePoints,
  nearestAltitudeSample,
  nightAltitudeHover,
  nightAltitudeMarkers,
  nightWindowFromNightResponse,
  pointerPxToClampedMs,
} from "./nightAltitudeGeometry";

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

describe("nightWindowFromNightResponse", () => {
  it("renames window_start/window_end and each event's when → when_utc", () => {
    const night = nightWindowFromNightResponse(NIGHT_RESPONSE);
    expect(night.window_start_utc).toBe("2026-08-28T19:00:00Z");
    expect(night.window_end_utc).toBe("2026-08-29T05:00:00Z");
    expect(night.solar_events).toEqual([
      { event: "astronomical_dusk", when_utc: "2026-08-28T21:00:00Z", outcome: "occurs", threshold_deg: -18 },
      { event: "astronomical_dawn", when_utc: "2026-08-29T03:30:00Z", outcome: "occurs", threshold_deg: -18 },
    ]);
    expect(night.lunar_events).toEqual([
      { event: "moonrise", when_utc: "2026-08-28T22:00:00Z", outcome: "occurs", threshold_deg: 0 },
      { event: "moonset", when_utc: "2026-08-29T02:00:00Z", outcome: "occurs", threshold_deg: 0 },
    ]);
  });

  it("falls back to the location's timezone, or UTC when it is unset", () => {
    expect(nightWindowFromNightResponse(NIGHT_RESPONSE).tz).toBe("Europe/London");
    const noTz: NightResponse = { ...NIGHT_RESPONSE, location: { ...NIGHT_RESPONSE.location, timezone: null } };
    expect(nightWindowFromNightResponse(noTz).tz).toBe("UTC");
  });
});

describe("nightAltitudeMarkers", () => {
  it("keeps only astronomical dusk/dawn and moonrise/moonset, dropping every other resolved event", () => {
    const markers = nightAltitudeMarkers(NIGHT);
    expect(markers.map((m) => m.event)).toEqual(["astronomical_dusk", "moonrise", "moonset", "astronomical_dawn"]);
  });

  it("drops an event that does not occur rather than inventing a position for it", () => {
    const polarNight = {
      ...NIGHT,
      solar_events: NIGHT.solar_events.map((e) =>
        e.event === "astronomical_dusk" ? { ...e, when_utc: null, outcome: "always_above" } : e,
      ),
    };
    const markers = nightAltitudeMarkers(polarNight);
    expect(markers.map((m) => m.event)).not.toContain("astronomical_dusk");
  });
});

describe("altitudeCurvePoints", () => {
  const range = axisRange(NIGHT, []);
  const axis = linearAxisMap(range, 640);

  it("maps 0deg to the lane bottom and 90deg to the lane top", () => {
    const points = altitudeCurvePoints(
      [
        { when: "2026-08-28T21:00:00Z", altitude_deg: 0 },
        { when: "2026-08-28T22:00:00Z", altitude_deg: 90 },
      ],
      range,
      axis,
      10,
      110,
    );
    const [p0, p1] = points.split(" ");
    expect(p0?.endsWith(",110.0")).toBe(true);
    expect(p1?.endsWith(",10.0")).toBe(true);
  });

  it("clamps an out-of-range altitude rather than drawing off the lane", () => {
    const points = altitudeCurvePoints([{ when: "2026-08-28T21:00:00Z", altitude_deg: 120 }], range, axis, 10, 110);
    expect(points.endsWith(",10.0")).toBe(true);
  });

  it("drops samples outside the axis range", () => {
    const points = altitudeCurvePoints(
      [{ when: "2020-01-01T00:00:00Z", altitude_deg: 45 }],
      range,
      axis,
      10,
      110,
    );
    expect(points).toBe("");
  });
});

describe("nearestAltitudeSample", () => {
  const samples = [
    { when: "2026-08-28T20:00:00Z", altitude_deg: 10 },
    { when: "2026-08-28T21:00:00Z", altitude_deg: 20 },
    { when: "2026-08-28T22:00:00Z", altitude_deg: 30 },
  ];

  it("returns null for an empty series", () => {
    expect(nearestAltitudeSample([], Date.parse("2026-08-28T21:00:00Z"))).toBeNull();
  });

  it("clamps to the first sample when the pointer is before it", () => {
    const nearest = nearestAltitudeSample(samples, Date.parse("2026-08-28T00:00:00Z"));
    expect(nearest?.when).toBe("2026-08-28T20:00:00Z");
  });

  it("clamps to the last sample when the pointer is after it", () => {
    const nearest = nearestAltitudeSample(samples, Date.parse("2026-08-29T12:00:00Z"));
    expect(nearest?.when).toBe("2026-08-28T22:00:00Z");
  });

  it("picks the closer sample either side of the pointer", () => {
    expect(nearestAltitudeSample(samples, Date.parse("2026-08-28T20:50:00Z"))?.when).toBe("2026-08-28T21:00:00Z");
    expect(nearestAltitudeSample(samples, Date.parse("2026-08-28T20:10:00Z"))?.when).toBe("2026-08-28T20:00:00Z");
  });

  it("resolves an exact tie to the earlier sample, deterministically", () => {
    // Exactly 30 minutes from both 20:00 and 21:00.
    const nearest = nearestAltitudeSample(samples, Date.parse("2026-08-28T20:30:00Z"));
    expect(nearest?.when).toBe("2026-08-28T20:00:00Z");
  });
});

describe("pointerPxToClampedMs", () => {
  const range = { startMs: Date.parse("2026-08-28T20:00:00Z"), endMs: Date.parse("2026-08-29T00:00:00Z") };
  const axis = linearAxisMap(range, 400);

  it("clamps a negative px to the range start", () => {
    expect(pointerPxToClampedMs(-50, range, axis)).toBe(range.startMs);
  });

  it("clamps a px past the axis length to the range end", () => {
    expect(pointerPxToClampedMs(1000, range, axis)).toBe(range.endMs);
  });

  it("maps mid-axis px to the proportional instant", () => {
    expect(pointerPxToClampedMs(200, range, axis)).toBe(range.startMs + (range.endMs - range.startMs) / 2);
  });
});

describe("nightAltitudeHover", () => {
  const targetSamples = [
    { when: "2026-08-28T20:00:00Z", altitude_deg: 10 },
    { when: "2026-08-28T22:00:00Z", altitude_deg: 40 },
  ];
  const moonSamples = [
    { when: "2026-08-28T20:05:00Z", altitude_deg: 5 },
    { when: "2026-08-28T21:55:00Z", altitude_deg: 25 },
  ];

  it("returns null when both series are empty", () => {
    expect(nightAltitudeHover(Date.parse("2026-08-28T21:00:00Z"), [], [])).toBeNull();
  });

  it("snaps target and moon independently to their own nearest sample", () => {
    const hover = nightAltitudeHover(Date.parse("2026-08-28T21:59:00Z"), targetSamples, moonSamples);
    expect(hover?.target).toEqual({ atMs: Date.parse("2026-08-28T22:00:00Z"), altitudeDeg: 40 });
    expect(hover?.moon).toEqual({ atMs: Date.parse("2026-08-28T21:55:00Z"), altitudeDeg: 25 });
    // The anchor (cursor/local-time) instant is the target's own sample time.
    expect(hover?.atMs).toBe(Date.parse("2026-08-28T22:00:00Z"));
  });

  it("falls back to the Moon's own instant when the target series is empty", () => {
    const hover = nightAltitudeHover(Date.parse("2026-08-28T21:50:00Z"), [], moonSamples);
    expect(hover?.target).toBeNull();
    expect(hover?.atMs).toBe(Date.parse("2026-08-28T21:55:00Z"));
  });
});
