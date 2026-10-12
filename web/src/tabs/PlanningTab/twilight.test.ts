// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { EventTimeModel } from "../../api/astro";
import {
  SOLAR_EVENT_ORDER,
  nightWindowForAnchor,
  nightWindowForInstant,
  orderedSolarEvents,
  outcomeMessage,
  resolveEventStart,
} from "./twilight";

/** Bratislava, an ordinary mid-latitude night — all eight events occur, in
 * an order the server does not guarantee (`useNightQuery`'s own JSON is not
 * contractually sorted), deliberately shuffled here. */
function occurringEvents(): EventTimeModel[] {
  const base = [
    ["sunset", "2026-08-28T17:43:16Z"],
    ["civil_dusk", "2026-08-28T18:13:39Z"],
    ["nautical_dusk", "2026-08-28T18:53:26Z"],
    ["astronomical_dusk", "2026-08-28T19:36:43Z"],
    ["astronomical_dawn", "2026-08-29T02:09:14Z"],
    ["nautical_dawn", "2026-08-29T02:52:34Z"],
    ["civil_dawn", "2026-08-29T03:32:23Z"],
    ["sunrise", "2026-08-29T04:02:47Z"],
  ] as const;
  return base
    .map(([event, when]) => ({ event, when, outcome: "occurs" as const, threshold_deg: 0 }))
    .reverse();
}

/** Tromsø, 69.65°N, midsummer — astronomical (and nautical, and civil)
 * twilight do not occur at all; the solver reports `always_above` rather
 * than a guess (DESIGN.md §4.1, architecture policy's own framing of this ticket). */
function tromsoMidsummerEvents(): EventTimeModel[] {
  return [
    { event: "solar_transit", when: "2026-06-22T10:46:11Z", outcome: "occurs", threshold_deg: 0 },
    { event: "sunrise", when: null, outcome: "always_above", threshold_deg: -0.93 },
    { event: "sunset", when: null, outcome: "always_above", threshold_deg: -0.93 },
    { event: "civil_dawn", when: null, outcome: "always_above", threshold_deg: -6 },
    { event: "civil_dusk", when: null, outcome: "always_above", threshold_deg: -6 },
    { event: "nautical_dawn", when: null, outcome: "always_above", threshold_deg: -12 },
    { event: "nautical_dusk", when: null, outcome: "always_above", threshold_deg: -12 },
    { event: "astronomical_dawn", when: null, outcome: "always_above", threshold_deg: -18 },
    { event: "astronomical_dusk", when: null, outcome: "always_above", threshold_deg: -18 },
  ];
}

describe("orderedSolarEvents", () => {
  it("returns the eight solar events in dusk-to-dawn order regardless of server order", () => {
    const ordered = orderedSolarEvents(occurringEvents());
    expect(ordered.map((e) => e.event)).toEqual(SOLAR_EVENT_ORDER);
  });

  it("drops events outside the eight-name vocabulary (e.g. solar_transit) rather than rendering them unlabelled", () => {
    const ordered = orderedSolarEvents(tromsoMidsummerEvents());
    expect(ordered).toHaveLength(8);
    expect(ordered.some((e) => e.event === "solar_transit")).toBe(false);
  });
});

describe("resolveEventStart", () => {
  it("is unselected when no event has been chosen", () => {
    expect(resolveEventStart(occurringEvents(), null, 0)).toEqual({ status: "unselected" });
  });

  it("is pending while the night query has not resolved yet", () => {
    expect(resolveEventStart(undefined, "astronomical_dusk", 0)).toEqual({ status: "pending" });
  });

  it("resolves to the event time plus the offset when the event occurs", () => {
    const resolution = resolveEventStart(occurringEvents(), "astronomical_dusk", 15);
    expect(resolution.status).toBe("resolved");
    if (resolution.status !== "resolved") throw new Error("expected resolved");
    expect(resolution.eventWhen.toISOString()).toBe("2026-08-28T19:36:43.000Z");
    expect(resolution.when.toISOString()).toBe("2026-08-28T19:51:43.000Z");
  });

  it("supports a negative offset (before the event)", () => {
    const resolution = resolveEventStart(occurringEvents(), "nautical_dawn", -10);
    expect(resolution.status).toBe("resolved");
    if (resolution.status !== "resolved") throw new Error("expected resolved");
    expect(resolution.when.toISOString()).toBe("2026-08-29T02:42:34.000Z");
  });

  it("says the event does not occur, at Tromsø in midsummer, rather than falling back to another twilight", () => {
    const resolution = resolveEventStart(tromsoMidsummerEvents(), "astronomical_dusk", 15);
    expect(resolution).toEqual({
      status: "unavailable",
      message: outcomeMessage("always_above", "Astronomical dusk"),
    });
    if (resolution.status !== "unavailable") throw new Error("expected unavailable");
    expect(resolution.message.toLowerCase()).toContain("does not occur");
  });

  it("treats civil and nautical the same as astronomical when they too do not occur", () => {
    for (const name of ["civil_dusk", "nautical_dusk"] as const) {
      const resolution = resolveEventStart(tromsoMidsummerEvents(), name, 0);
      expect(resolution.status).toBe("unavailable");
    }
  });
});

describe("outcomeMessage", () => {
  it("never returns a blank message for a non-occurring outcome", () => {
    for (const outcome of ["always_above", "always_below", "not_in_window"] as const) {
      expect(outcomeMessage(outcome, "Astronomical dusk").length).toBeGreaterThan(0);
    }
  });

  it("is empty for the occurring case (never rendered)", () => {
    expect(outcomeMessage("occurs", "Astronomical dusk")).toBe("");
  });
});

describe("nightWindowForAnchor", () => {
  it("spans local noon through the next day's local noon", () => {
    const anchor = new Date(2026, 7, 28, 15, 0, 0); // 28 Aug, 15:00 local
    const { start, end } = nightWindowForAnchor(anchor);
    expect(start.getHours()).toBe(12);
    expect(start.getDate()).toBe(28);
    expect(end.getHours()).toBe(12);
    expect(end.getDate()).toBe(29);
    expect(end.getTime() - start.getTime()).toBe(24 * 3600_000);
  });

  it("treats a before-noon date anchor as that day's upcoming night", () => {
    const anchor = new Date(2026, 7, 29, 2, 0, 0); // date input's 29th, not a live instant
    const { start, end } = nightWindowForAnchor(anchor);
    expect(start).toEqual(new Date(2026, 7, 29, 12, 0, 0));
    expect(end).toEqual(new Date(2026, 7, 30, 12, 0, 0));
  });

  it("keeps an explicit before-noon instant in the prior observing night", () => {
    const { start, end } = nightWindowForInstant(new Date(2026, 7, 29, 2, 0, 0));
    expect(start).toEqual(new Date(2026, 7, 28, 12, 0, 0));
    expect(end).toEqual(new Date(2026, 7, 29, 12, 0, 0));
  });
});
