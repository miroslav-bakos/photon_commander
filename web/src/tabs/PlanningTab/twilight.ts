// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §4.1: "All three twilight definitions are offered wherever a
 * time is chosen or displayed — not just astronomical." Pure helpers only —
 * the eight `solar_events` names `/api/astro/night` returns, in dusk→dawn
 * order, plus the arithmetic that turns `{event, offset_min}` into the
 * resolved absolute instant the UI must always show inline (§4.1: "the
 * abstraction never hides what will actually happen").
 *
 * `outcome` matters as much as `when` (architecture policy's own framing of this
 * ticket): above ~60° latitude astronomical night does not occur at all in
 * summer, and the solver reports that honestly via `Outcome` rather than
 * guessing — `resolveEventStart` below never falls back to another twilight
 * on its own, it says so.
 */
import type { EventTimeModel, Outcome } from "../../api/astro";

/** The eight solar events `/api/astro/night` returns, in the order they
 * occur through a session that starts before sunset — sunset first through
 * civil/nautical/astronomical dusk, then astronomical/nautical/civil dawn,
 * sunrise last. Fixed order regardless of what order the server returns
 * them in, so a re-render never reshuffles the list. */
export const SOLAR_EVENT_ORDER = [
  "sunset",
  "civil_dusk",
  "nautical_dusk",
  "astronomical_dusk",
  "astronomical_dawn",
  "nautical_dawn",
  "civil_dawn",
  "sunrise",
] as const;

export type SolarEventName = (typeof SOLAR_EVENT_ORDER)[number];

/** Short, human labels — never abbreviated to the point a civil/nautical/
 * astronomical reader has to guess which is which (DESIGN.md §4.1: "Which
 * twilight matters is the user's call"). */
export const SOLAR_EVENT_LABELS: Record<SolarEventName, string> = {
  sunset: "Sunset",
  civil_dusk: "Civil dusk",
  nautical_dusk: "Nautical dusk",
  astronomical_dusk: "Astronomical dusk",
  astronomical_dawn: "Astronomical dawn",
  nautical_dawn: "Nautical dawn",
  civil_dawn: "Civil dawn",
  sunrise: "Sunrise",
};

function isSolarEventName(event: string): event is SolarEventName {
  return (SOLAR_EVENT_ORDER as readonly string[]).includes(event);
}

/** `solar_events` in `SOLAR_EVENT_ORDER`, dropping anything the server sent
 * that this list does not know about (e.g. `solar_transit`, which is not
 * one of the eight dusk/dawn boundaries this UI surfaces) rather than
 * rendering it unlabelled. */
export function orderedSolarEvents(events: EventTimeModel[]): EventTimeModel[] {
  const byName = new Map(events.filter((e) => isSolarEventName(e.event)).map((e) => [e.event, e]));
  return SOLAR_EVENT_ORDER.map((name) => byName.get(name)).filter((e): e is EventTimeModel => e !== undefined);
}

/** Why an event has no time to offset from — architecture policy: "The UI must say
 * 'astronomical night does not occur tonight' rather than rendering a
 * blank, an epoch date, or silently falling back to another twilight." */
export function outcomeMessage(outcome: Outcome, label: string): string {
  switch (outcome) {
    case "always_above":
      return `${label} does not occur — the sun stays above that altitude all night at this location and date.`;
    case "always_below":
      return `${label} does not occur — the sun stays below that altitude all night at this location and date.`;
    case "not_in_window":
      return `${label} does not fall inside the selected night.`;
    case "occurs":
      return "";
  }
}

/** The resolved state of an event-relative `TimeSpec` (DESIGN.md §4.1). Every
 * branch is named so a caller cannot mistake "no event chosen yet" for
 * "chosen but does not occur" for "resolved" — the three are rendered
 * completely differently. */
export type EventStartResolution =
  | { status: "unselected" }
  | { status: "pending" }
  | { status: "unavailable"; message: string }
  | { status: "resolved"; eventWhen: Date; when: Date };

export function resolveEventStart(
  events: EventTimeModel[] | undefined,
  eventName: SolarEventName | null,
  offsetMin: number,
): EventStartResolution {
  if (eventName === null) return { status: "unselected" };
  if (events === undefined) return { status: "pending" };
  const found = events.find((e) => e.event === eventName) ?? null;
  if (found === null) {
    return { status: "unavailable", message: `${SOLAR_EVENT_LABELS[eventName]} was not returned for this night.` };
  }
  if (found.outcome !== "occurs" || found.when === null) {
    return { status: "unavailable", message: outcomeMessage(found.outcome, SOLAR_EVENT_LABELS[eventName]) };
  }
  const eventWhen = new Date(found.when);
  return { status: "resolved", eventWhen, when: new Date(eventWhen.getTime() + offsetMin * 60_000) };
}

/** The upcoming evening named by a calendar date: local noon that day through
 * local noon the next day, wide enough for dusk and dawn across DST. A date
 * selected in "Night of" is not an observation instant, so even a midnight
 * Date from a date input must not silently become the previous night. */
export function nightWindowForAnchor(anchor: Date): { start: Date; end: Date } {
  const start = new Date(anchor);
  start.setHours(12, 0, 0, 0);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  return { start, end };
}

/** Unlike a "Night of" date, an explicit absolute instant before noon belongs
 * to the observing night that began yesterday. */
export function nightWindowForInstant(instant: Date): { start: Date; end: Date } {
  const anchor = new Date(instant);
  if (anchor.getHours() < 12) anchor.setDate(anchor.getDate() - 1);
  return nightWindowForAnchor(anchor);
}
