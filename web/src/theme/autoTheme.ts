// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * "Auto" is a stored *choice* (`AppearanceSettings.theme === "auto"`), never
 * a display value — `useThemeSync.ts` is the one place that resolves it to
 * an actual `dark`/`red` `data-theme`, and this is the pure half of that:
 * given the current instant and tonight's sunset/sunrise, which of the two
 * applies, and when does that stop being true. Kept separate from
 * `useThemeSync.ts` so it is testable without mounting anything, the same
 * reason `timelineGeometry.ts`'s pure functions are split from
 * `TimelineStrip.tsx`.
 */
import type { EventTimeModel } from "../api/astro";

export interface AutoThemeResolution {
  effective: "dark" | "red";
  /** The next instant this resolution stops applying — schedule a
   * re-resolution for exactly this instant, never a short poll. `null` when
   * unknown: either sunset/sunrise did not occur for the queried night (an
   * honest "cannot resolve", not a guess — architecture policy invariant 7), or `now`
   * has already passed both events in this window, so the next transition
   * (tomorrow's sunset) is not data this window has — the caller should
   * requery for a window anchored on `now` again rather than trust a stale
   * instant here. */
  nextChangeAt: Date | null;
}

/** `sunset`/`sunrise` here are one night's pair from a
 * `nightWindowForAnchor`-shaped query — sunset in the evening, sunrise the
 * following morning, `sunset < sunrise` always. `now` need not be inside
 * that window; if it has already passed `sunrise`, this is genuinely "no
 * next transition from this data", not a wraparound to compute. */
export function resolveAutoTheme(now: Date, sunset: Date | null, sunrise: Date | null): AutoThemeResolution {
  if (sunset === null || sunrise === null) return { effective: "dark", nextChangeAt: null };
  if (now < sunset) return { effective: "dark", nextChangeAt: sunset };
  if (now < sunrise) return { effective: "red", nextChangeAt: sunrise };
  return { effective: "dark", nextChangeAt: null };
}

/** The `nightWindowForAnchor`-style anchor date whose noon-to-noon window
 * actually contains `now` — `nightWindowForAnchor(now)` alone is wrong for
 * this: its window starts at `now`'s own local noon, which is *later* than
 * `now` for anything before noon (an early-morning check would ask for a
 * window that has not started yet, missing the sunset/sunrise that already
 * happened overnight). Before noon, the relevant night began the evening
 * before, so the anchor is `now` minus one day. */
export function autoThemeAnchor(now: Date): Date {
  const anchor = new Date(now);
  if (anchor.getHours() < 12) anchor.setDate(anchor.getDate() - 1);
  return anchor;
}

function findEventTime(events: readonly EventTimeModel[], name: string): Date | null {
  const found = events.find((e) => e.event === name);
  if (found?.outcome !== "occurs" || found.when === null) return null;
  return new Date(found.when);
}

/** `solar_events` straight off a `NightRequest` response for the window
 * `autoThemeAnchor(now)` produced — pulls out the plain sunset/sunrise pair
 * (not a twilight tier: the user asked for "sunrise / sunset times"
 * specifically) and resolves through `resolveAutoTheme`. */
export function resolveAutoThemeFromSolarEvents(
  now: Date,
  solarEvents: readonly EventTimeModel[],
): AutoThemeResolution {
  return resolveAutoTheme(now, findEventTime(solarEvents, "sunset"), findEventTime(solarEvents, "sunrise"));
}
