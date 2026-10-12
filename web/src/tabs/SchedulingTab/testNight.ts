// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * One fixture night for the timeline tests (Europe/London, 2026-08-28):
 * every twilight occurs, the Moon rises at 22:00Z and sets at 02:00Z. Not a
 * solved ephemeris — round numbers, so expected pixels are arithmetic. Lives
 * beside the tests it serves; nothing in the app imports it.
 */
import type { CompiledSchedule } from "../../api/plans";

type NightWindow = CompiledSchedule["night"];

export function resolvedEvent(name: string, when: string | null, outcome = "occurs"): NightWindow["solar_events"][number] {
  return { event: name, when_utc: when, outcome, threshold_deg: 0 };
}

export const NIGHT: NightWindow = {
  date: "2026-08-28",
  tz: "Europe/London",
  window_start_utc: "2026-08-28T11:00:00Z",
  window_end_utc: "2026-08-29T11:00:00Z",
  dark_start_utc: "2026-08-28T21:00:00Z",
  dark_end_utc: "2026-08-29T03:30:00Z",
  solar_events: [
    resolvedEvent("sunset", "2026-08-28T19:00:00Z"),
    resolvedEvent("civil_dusk", "2026-08-28T19:40:00Z"),
    resolvedEvent("nautical_dusk", "2026-08-28T20:20:00Z"),
    resolvedEvent("astronomical_dusk", "2026-08-28T21:00:00Z"),
    resolvedEvent("astronomical_dawn", "2026-08-29T03:30:00Z"),
    resolvedEvent("nautical_dawn", "2026-08-29T04:10:00Z"),
    resolvedEvent("civil_dawn", "2026-08-29T04:50:00Z"),
    resolvedEvent("sunrise", "2026-08-29T05:30:00Z"),
  ],
  lunar_events: [resolvedEvent("moonrise", "2026-08-28T22:00:00Z"), resolvedEvent("moonset", "2026-08-29T02:00:00Z")],
};
