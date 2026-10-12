// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
/** Site-local civil dates; never derive a night's duration from a fixed 24 hours. */
function nextDate(date: string, days: number): string {
  const at = new Date(`${date}T12:00:00Z`);
  at.setUTCDate(at.getUTCDate() + days);
  return at.toISOString().slice(0, 10);
}
function parts(at: Date, timeZone: string) {
  return new Map(new Intl.DateTimeFormat("en-GB", { timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).formatToParts(at).map((part) => [part.type, part.value]));
}
function siteNoon(date: string, timeZone: string): Date {
  const expected = `${date}T12:00`;
  const base = Date.parse(`${date}T12:00:00Z`);
  for (let offset = -15 * 60; offset <= 15 * 60; offset += 15) {
    const candidate = new Date(base - offset * 60_000);
    const value = parts(candidate, timeZone);
    if (`${value.get("year") ?? ""}-${value.get("month") ?? ""}-${value.get("day") ?? ""}T${value.get("hour") ?? ""}:${value.get("minute") ?? ""}` === expected) return candidate;
  }
  throw new Error("The observing site's local noon could not be resolved.");
}
export function siteNightWindow(nightDate: string, timeZone: string): { start: Date; end: Date } {
  return { start: siteNoon(nightDate, timeZone), end: siteNoon(nextDate(nightDate, 1), timeZone) };
}
export function siteNightDate(at: Date, timeZone: string): string {
  const value = parts(at, timeZone);
  const date = `${value.get("year") ?? ""}-${value.get("month") ?? ""}-${value.get("day") ?? ""}`;
  return Number(value.get("hour")) < 12 ? nextDate(date, -1) : date;
}
