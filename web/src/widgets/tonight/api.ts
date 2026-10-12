// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `GET /api/tonight` (`ssc/api/routes/tonight.py`) — the header widget's data
 * layer. Every type is an alias into the generated schema, never hand-mirrored:
 * this file previously carried hand-written interfaces because the route landed
 * mid-change and `api:codegen` also widens `MoonInfoModel`; that follow-up has
 * since landed, so the aliases are real.
 *
 * The comments below say what the generated types cannot: Pydantic field
 * descriptions do not survive into `schema.d.ts`, and every nullable pair here
 * ("value, or the reason there is no value") is exactly the shape architecture policy
 * invariant 7 demands and a bare `| null` fails to explain.
 */
import { queryOptions, useQuery } from "@tanstack/react-query";
import { ApiError, api, unwrap } from "../../api/client";
import type { EventTimeModel, Outcome } from "../../api/astro";
import type { components } from "../../api/schema";

/** `ssc.api.conditions.ConditionsCategory` — score bands chosen to read
 * naturally on a header widget rather than forcing a user to interpret a
 * bare number out of 100. */
export type ConditionsCategory = components["schemas"]["ConditionsCategory"];
export type TonightCloudModel = components["schemas"]["TonightCloudModel"];
export type TonightWeatherRowModel = components["schemas"]["TonightWeatherRowModel"];

/** `stale_since` is non-null once the cached forecast has aged past the
 * provider's own freshness window — render it, never hide it (invariant 7: a
 * dark site with no signal must show its staleness, not present cached numbers
 * as current). `rows` are the forecast rows falling inside tonight's dark
 * window — evidence for `conditions`, chronological, never a substitute for
 * `conditions.reasons`. */
export type TonightWeatherModel = components["schemas"]["TonightWeatherModel"];

/** `reasons` is always non-empty when `conditions` is non-null — why the score
 * is what it is, in the order each factor was evaluated. */
export type CaptureConditionsModel = components["schemas"]["CaptureConditionsModel"];

/** `waxing`/`phase_angle_deg` are required, not inferred — see `moonPhase.ts`
 * for why an illuminated fraction alone cannot draw the limb on the right
 * side. */
export type MoonTonightModel = components["schemas"]["MoonTonightModel"];

/**
 * `dark_window_start`/`_end` are both null when the solved solar events show no
 * astronomical darkness in the searched span at all — `dark_window_note` says
 * why, the same `Outcome`-flavoured honesty as a non-occurring twilight event.
 *
 * `weather` is null iff `weather_unavailable_reason` is set (no cached
 * forecast, fresh or stale, for this location). `conditions` is null iff
 * `conditions_unavailable_reason` is set — weather itself unavailable, or no
 * forecast row falls inside tonight's window. `moon` is unaffected by either:
 * it never depends on a weather provider.
 */
export type TonightResponse = components["schemas"]["TonightResponse"];

/** True once `backend-core` has actually shipped the route on *this* server —
 * distinguishes "no location set" (409, a real answer) from "this server
 * predates the endpoint" (404, not a verdict about tonight at all) so the
 * widget never shows `ApiError`'s stale-server wording, which reads as a bug
 * report, for what is here just an unreleased feature. */
export function isTonightRouteMissing(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

export function isLocationRequired(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

/** Re-exported so callers of this module don't also need `../../api/astro`
 * just to type-annotate a moonrise/moonset outcome. */
export type { Outcome, EventTimeModel };

export function useTonightQuery(enabled: boolean) {
  return useQuery({
    queryKey: ["tonight"] as const,
    queryFn: () => unwrap(api.GET("/api/tonight")),
    enabled,
    // Cloud/dew-point/wind move on the provider's own schedule, not the
    // device's — a slow poll is honest without hammering the proxy.
    refetchInterval: 5 * 60_000,
    staleTime: 60_000,
    retry: false,
  });
}

export function nightPredictionQueryOptions(nightDate: string, enabled = true) {
  return queryOptions({
    queryKey: ["night-prediction", nightDate] as const,
    queryFn: () => unwrap(api.GET("/api/tonight", {
      params: { query: { night_date: nightDate } },
    })),
    enabled,
    staleTime: 5 * 60_000,
    retry: false,
  });
}
