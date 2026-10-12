// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { autoThemeAnchor, resolveAutoTheme, resolveAutoThemeFromSolarEvents } from "./autoTheme";

const SUNSET = new Date("2026-09-08T19:00:00Z");
const SUNRISE = new Date("2026-09-09T06:30:00Z");

describe("resolveAutoTheme", () => {
  it("is dark before sunset, switching exactly at sunset", () => {
    expect(resolveAutoTheme(new Date("2026-09-08T12:00:00Z"), SUNSET, SUNRISE)).toEqual({
      effective: "dark",
      nextChangeAt: SUNSET,
    });
  });

  it("is red from sunset up to sunrise, switching exactly at sunrise", () => {
    expect(resolveAutoTheme(SUNSET, SUNSET, SUNRISE)).toEqual({ effective: "red", nextChangeAt: SUNRISE });
    expect(resolveAutoTheme(new Date("2026-09-09T02:00:00Z"), SUNSET, SUNRISE)).toEqual({
      effective: "red",
      nextChangeAt: SUNRISE,
    });
  });

  it("is dark again once past sunrise, with no next transition in this window's data", () => {
    expect(resolveAutoTheme(SUNRISE, SUNSET, SUNRISE)).toEqual({ effective: "dark", nextChangeAt: null });
    expect(resolveAutoTheme(new Date("2026-09-09T11:00:00Z"), SUNSET, SUNRISE)).toEqual({
      effective: "dark",
      nextChangeAt: null,
    });
  });

  it("cannot resolve without both events, and says so rather than guessing", () => {
    expect(resolveAutoTheme(new Date(), null, SUNRISE)).toEqual({ effective: "dark", nextChangeAt: null });
    expect(resolveAutoTheme(new Date(), SUNSET, null)).toEqual({ effective: "dark", nextChangeAt: null });
  });
});

describe("autoThemeAnchor", () => {
  it("uses today when now is at or after noon", () => {
    expect(autoThemeAnchor(new Date("2026-09-08T20:00:00")).getDate()).toBe(8);
    expect(autoThemeAnchor(new Date("2026-09-08T12:00:00")).getDate()).toBe(8);
  });

  it("uses yesterday when now is before noon, so the noon-to-noon window still contains it", () => {
    // 03:00 belongs to the night that started the evening before -- the
    // window anchored on *today* would start at today's noon, which is
    // still in the future relative to 03:00.
    expect(autoThemeAnchor(new Date("2026-09-08T03:00:00")).getDate()).toBe(7);
  });
});

describe("resolveAutoThemeFromSolarEvents", () => {
  it("reads the plain sunset/sunrise events, not a twilight tier", () => {
    const events = [
      { event: "civil_dusk", when: "2026-09-08T18:30:00Z", outcome: "occurs" as const, threshold_deg: -6 },
      { event: "sunset", when: SUNSET.toISOString(), outcome: "occurs" as const, threshold_deg: 0 },
      { event: "sunrise", when: SUNRISE.toISOString(), outcome: "occurs" as const, threshold_deg: 0 },
    ];
    expect(resolveAutoThemeFromSolarEvents(new Date("2026-09-09T02:00:00Z"), events)).toEqual({
      effective: "red",
      nextChangeAt: SUNRISE,
    });
  });

  it("treats a non-occurring sunset (polar day/night) as unresolvable, not a guess", () => {
    const events = [
      { event: "sunset", when: null, outcome: "always_above" as const, threshold_deg: 0 },
      { event: "sunrise", when: SUNRISE.toISOString(), outcome: "occurs" as const, threshold_deg: 0 },
    ];
    expect(resolveAutoThemeFromSolarEvents(new Date(), events)).toEqual({ effective: "dark", nextChangeAt: null });
  });
});
