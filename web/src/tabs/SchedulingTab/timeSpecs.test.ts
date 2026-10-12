// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `eventOptionLabel`: every event dropdown reads "Astronomical dusk · 21:13"
 * once a night has compiled, "does not occur" for a diagnosed absence, and
 * the bare name before anything has compiled at all.
 */
import { describe, expect, it } from "vitest";
import { NIGHT } from "./testNight";
import { eventOptionLabel } from "./timeSpecs";

describe("eventOptionLabel", () => {
  it("is the bare name before anything has compiled", () => {
    expect(eventOptionLabel("astronomical_dusk", undefined)).toBe("Astronomical dusk");
  });

  it("appends the resolved local time once a night is known", () => {
    // 2026-08-28T21:00:00Z is 22:00 in Europe/London (BST).
    expect(eventOptionLabel("astronomical_dusk", NIGHT)).toMatch(/^Astronomical dusk · \d{2}:\d{2}$/);
    const midpoint = { ...NIGHT, solar_events: [
      ...NIGHT.solar_events,
      { event: "astronomical_midnight", when_utc: "2026-08-29T00:15:00Z", outcome: "occurs", threshold_deg: -18 },
    ] };
    expect(eventOptionLabel("astronomical_midnight", midpoint)).toMatch(/^Astronomical midnight · \d{2}:15$/);
  });

  it("says the event does not occur, and stays selectable, when the compiler diagnosed that", () => {
    const summer = {
      ...NIGHT,
      solar_events: NIGHT.solar_events.map((e) =>
        e.event === "astronomical_dusk" ? { ...e, when_utc: null, outcome: "always_above" } : e,
      ),
    };
    expect(eventOptionLabel("astronomical_dusk", summer)).toBe("Astronomical dusk · does not occur");
  });

  it("falls back to the bare name for an event the night payload omits", () => {
    const bare = { ...NIGHT, lunar_events: [] };
    expect(eventOptionLabel("moonrise", bare)).toBe("Moonrise");
  });
});
