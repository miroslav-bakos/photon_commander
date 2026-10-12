// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Small, local formatting helpers for the Mount tab's readout. No
 * precedent to reuse: the sexagesimal RA/Dec formatting under
 * `web/src/skymap/**` belongs to `skymap-viz`, not this tab, so this is a
 * short, deliberately independent implementation rather than a reach across
 * that boundary.
 *
 * Every formatter returns the fixed string `"unknown"` for `null` — never a
 * numeric-looking `0` or `0.0` (invariant 7: `null` means unknown, not
 * zero, and rendering it as zero is a lie the user could act on).
 */
export const UNKNOWN = "unknown";

export function formatRaHours(hours: number | null): string {
  if (hours === null) return UNKNOWN;
  const h = ((hours % 24) + 24) % 24;
  const totalSeconds = Math.round(h * 3600);
  const hh = Math.floor(totalSeconds / 3600);
  const mm = Math.floor((totalSeconds % 3600) / 60);
  const ss = totalSeconds % 60;
  return `${pad(hh)}h ${pad(mm)}m ${pad(ss)}s`;
}

export function formatDecDeg(deg: number | null): string {
  if (deg === null) return UNKNOWN;
  const sign = deg < 0 ? "-" : "+";
  const abs = Math.abs(deg);
  const totalArcsec = Math.round(abs * 3600);
  const dd = Math.floor(totalArcsec / 3600);
  const mm = Math.floor((totalArcsec % 3600) / 60);
  const ss = totalArcsec % 60;
  return `${sign}${pad(dd)}° ${pad(mm)}′ ${pad(ss)}″`;
}

export function formatDeg1(deg: number | null): string {
  if (deg === null) return UNKNOWN;
  return `${deg.toFixed(1)}°`;
}

export function formatBool(value: boolean | null, whenTrue: string, whenFalse: string): string {
  if (value === null) return UNKNOWN;
  return value ? whenTrue : whenFalse;
}

function pad(n: number): string {
  return n.toString().padStart(2, "0");
}

/** Seconds since `isoTimestamp`, or `null` if there has never been a
 * reading (backend: `mount` can exist with no successful poll yet) or the
 * timestamp cannot be parsed — "cannot tell" is not the same as "just now". */
export function ageSeconds(isoTimestamp: string | null, nowMs: number): number | null {
  if (isoTimestamp === null) return null;
  const then = Date.parse(isoTimestamp);
  if (Number.isNaN(then)) return null;
  return Math.max(0, (nowMs - then) / 1000);
}

export function formatAge(seconds: number | null): string {
  if (seconds === null) return "unknown age";
  if (seconds < 1.5) return "just now";
  if (seconds < 60) return `${Math.round(seconds).toString()}s ago`;
  const minutes = Math.round(seconds / 60);
  return `${minutes.toString()}m ago`;
}
