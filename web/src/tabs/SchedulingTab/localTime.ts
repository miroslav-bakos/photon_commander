// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The Scheduling tab's display edge (architecture policy invariant 5: UTC in storage
 * and every interface, local only here). Pure helpers, no React.
 */

/** `HH:MM` in the browser's zone, the same shape every Planning panel uses. */
export function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

/** The spec label the compiler prefixed a `resolution`/`end_resolution`
 * string with, without the clock it appended — that half is re-rendered
 * locally (`ResolvedTimeLabel`, and `TimelineStrip`'s "until event" resize
 * tooltip). */
export function resolutionLabel(resolution: string): string {
  const arrow = resolution.lastIndexOf(" → ");
  return arrow === -1 ? resolution : resolution.slice(0, arrow);
}

/** Minutes between two UTC instants, for a compiled item's duration. */
export function minutesBetween(startIso: string, endIso: string): number {
  return Math.round((new Date(endIso).getTime() - new Date(startIso).getTime()) / 60_000);
}

export function formatMinutes(minutes: number): string {
  if (minutes < 60) return `${String(minutes)} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${String(h)} h` : `${String(h)} h ${String(m).padStart(2, "0")}`;
}

/** `<input type="datetime-local">` accepts `YYYY-MM-DDTHH:MM[:SS]`; the
 * server echoes `AbsoluteTimeSpec.local_datetime` as `YYYY-MM-DDTHH:MM:SS`.
 * Trimmed to the minute for the control, and *only* rewritten into the
 * document when the user actually edits it. */
export function toDatetimeLocalInput(naiveIso: string): string {
  return naiveIso.slice(0, 16);
}

/** Now, as a naive local wall-clock reading — the seed for a fresh
 * `absolute` TimeSpec. Never sent with a zone: the zone is Settings', not
 * the plan's (`ssc.plan.models.AbsoluteTimeSpec`). */
export function nowAsNaiveLocal(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${String(d.getFullYear())}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * How many instants a naive wall-clock reading names in `timeZone`:
 * `2` on the autumn DST night (01:30 happens twice — DESIGN.md §4.1: "an
 * autumn night can contain 01:30 twice"), `0` on the spring night (the hour
 * that is skipped), `1` otherwise. Only the `2` case needs the user to say
 * which occurrence they mean (`fold`), so the picker shows that choice only
 * then.
 *
 * `timeZone` is the *site's* zone from Settings when known — the document
 * compiles there, not in the browser — and the browser's own otherwise.
 * Returns `null` when the reading cannot be parsed or the zone is unknown to
 * this runtime, so a caller shows the choice rather than hiding it on a
 * guess.
 */
export function wallClockOccurrences(naiveLocal: string, timeZone: string | undefined): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(naiveLocal);
  if (match === null) return null;
  const [y, mo, d, h, mi] = match.slice(1).map(Number) as [number, number, number, number, number];
  let formatter: Intl.DateTimeFormat;
  try {
    formatter = new Intl.DateTimeFormat("en-GB", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return null;
  }
  const wanted = `${String(y).padStart(4, "0")}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}T${String(h).padStart(2, "0")}:${String(mi).padStart(2, "0")}`;
  const asIfUtc = Date.UTC(y, mo - 1, d, h, mi);
  // Every zone offset on Earth is within ±14 h of UTC and a multiple of
  // 15 min (Kathmandu is +5:45), and a transition shifts by at most 2 h.
  // Scanning the surrounding day at 15-min steps therefore finds every
  // instant that reads as `wanted` — one normally, two across the autumn
  // fold, none across the spring gap.
  let count = 0;
  for (let offsetMin = -15 * 60; offsetMin <= 15 * 60; offsetMin += 15) {
    const candidate = new Date(asIfUtc - offsetMin * 60_000);
    if (formatWallClock(formatter, candidate) === wanted) count += 1;
  }
  return count;
}

function formatWallClock(formatter: Intl.DateTimeFormat, at: Date): string {
  const parts = new Map(formatter.formatToParts(at).map((p) => [p.type, p.value]));
  return `${parts.get("year") ?? ""}-${parts.get("month") ?? ""}-${parts.get("day") ?? ""}T${parts.get("hour") ?? ""}:${parts.get("minute") ?? ""}`;
}
