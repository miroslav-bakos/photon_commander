// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect, useMemo, useState } from "react";
import { useNightQuery, type NightRequest } from "../api/astro";
import { nightWindowForAnchor } from "../tabs/PlanningTab/twilight";
import { useConnectionStore } from "../store/connectionStore";
import { autoThemeAnchor, resolveAutoThemeFromSolarEvents, type AutoThemeResolution } from "./autoTheme";

/**
 * The live-updating half of `"auto"` theme resolution — shared by
 * `useThemeSync.ts` (which writes the result to `data-theme`) and
 * `AppearancePanel.tsx` (which only *displays* it, next to the toggle that
 * chose `"auto"` in the first place, so the switch is a visible, honest
 * state rather than a background behaviour the user can't see is working).
 * Both calls share one `useNightQuery` cache entry (identical request body,
 * same query key) — this is not two independent fetches.
 *
 * `null` when `enabled` is `false`, when no location is configured (auto
 * cannot resolve — architecture policy invariant 7: unknown is not zero, never a
 * guessed theme), or while the first resolution is still loading.
 */
export function useAutoThemeResolution(enabled: boolean): AutoThemeResolution | null {
  const location = useConnectionStore((s) => s.snapshot?.location ?? null);

  // Forces the request below to recompute "now" and requery — bumped at the
  // next known sunset/sunrise, or hourly when there is none to schedule
  // against (`autoTheme.ts`'s own doc comment on `nextChangeAt: null`).
  // Never a short poll: a value that only changes twice a day does not need one.
  const [anchorTick, setAnchorTick] = useState(0);

  const nightRequest: NightRequest | null = useMemo(() => {
    if (!enabled || location === null) return null;
    const window = nightWindowForAnchor(autoThemeAnchor(new Date()));
    return {
      start_time: window.start.toISOString(),
      end_time: window.end.toISOString(),
      min_altitude_deg: 0,
      altitude_samples: 2,
    };
    // `anchorTick` is a deliberate cache-buster, not a value the request
    // body reads — its only job is making this memo re-run "now" at the
    // scheduled instant.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, location, anchorTick]);
  const night = useNightQuery(nightRequest);

  const resolution = useMemo(() => {
    if (!enabled || night.data === undefined) return null;
    return resolveAutoThemeFromSolarEvents(new Date(), night.data.solar_events);
  }, [enabled, night.data]);

  useEffect(() => {
    if (!enabled) return;
    const HOURLY_SAFETY_NET_MS = 60 * 60 * 1000;
    const delayMs =
      resolution?.nextChangeAt != null
        ? Math.max(1000, resolution.nextChangeAt.getTime() - Date.now())
        : HOURLY_SAFETY_NET_MS;
    const id = window.setTimeout(() => {
      setAnchorTick((n) => n + 1);
    }, delayMs);
    return () => {
      window.clearTimeout(id);
    };
  }, [enabled, resolution]);

  return resolution;
}
