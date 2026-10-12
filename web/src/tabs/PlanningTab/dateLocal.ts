// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/** `<input type="datetime-local">`'s own string format, both directions.
 * Deliberately *not* UTC (architecture policy invariant 5: UTC in storage and every
 * interface, local only at the display edge) — this is exactly that display
 * edge, and the input reflects the browser's local time zone, matching what
 * the user actually types. `FramingRequest.start_time` is sent as the
 * `Date`'s own ISO string (`toISOString()`, UTC), never this string. */
export function toDatetimeLocalValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function fromDatetimeLocalValue(value: string): Date | null {
  if (value === "") return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `<input type="date">`'s own string format — the "night of ___" anchor for
 * the event-relative session-start picker (`twilight.ts`'s
 * `nightWindowForAnchor`), local for the same reason as
 * `toDatetimeLocalValue` above. */
export function toDateLocalValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function fromDateLocalValue(value: string): Date | null {
  if (value === "") return null;
  const [y, m, d] = value.split("-").map(Number);
  if (y === undefined || m === undefined || d === undefined) return null;
  const date = new Date(y, m - 1, d);
  return Number.isNaN(date.getTime()) ? null : date;
}
