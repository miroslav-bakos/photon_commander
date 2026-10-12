// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The Planning tab's data layer over `/api/astro/*` (`ssc.astro`, DESIGN.md
 * §3). Every type here is a straight alias into the generated schema — never
 * hand-mirrored (architecture policy: "Generate the TypeScript types from it exactly as
 * the rest of `web/` does").
 *
 * `framing`/`mosaic` are POSTs that are really idempotent computations (pure
 * functions of their body over `ssc/astro`), so they are modelled as
 * `useQuery` keyed on the serialised body rather than `useMutation` — this is
 * what lets a scrub of `session_length_hours` or a profile change refetch
 * automatically and de-duplicate identical requests, the same as a GET would.
 * `enabled: body !== null` is how a caller says "not enough input yet" (no
 * target resolved, no location) without a conditional hook call.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./client";
import type { components } from "./schema";
import { useConnectionStore } from "../store/connectionStore";
import { useSettingsQuery } from "./settings";

// GPS provenance timestamps do not change the numerical observing site.
function useCalculationSiteKey() {
  const latitude = useConnectionStore((state) => state.snapshot?.location?.latitude_deg ?? null);
  const longitude = useConnectionStore((state) => state.snapshot?.location?.longitude_deg ?? null);
  const elevation = useConnectionStore((state) => state.snapshot?.location?.elevation_m ?? null);
  return [latitude, longitude, elevation] as const;
}

// -- Sun, Moon and planets (GET /api/astro/bodies) --------------------------

export type AstroBodyKind = components["schemas"]["BodyKind"];
export type AstroBodyModel = components["schemas"]["BodyPositionModel"];
export type AstroBodiesResponse = components["schemas"]["BodiesResponse"];
export type CometSuggestionModel = components["schemas"]["CometSuggestionModel"];
export type CometSuggestionsResponse = components["schemas"]["CometSuggestionsResponse"];

/** The scrubber's own instant moves these — the only shapes on the map that
 * actually do. `instant` is expected to already be the *settled* instant
 * (`SkyMap.tsx`'s own debounce, the same `VIEW_SETTLE_MS` pattern
 * `useFieldObjectsQuery`'s caller uses for the view), never refetched per
 * animation frame. `placeholderData` keeps the previous instant's positions
 * on screen while the next settled instant's response loads, the same
 * "do not blink the layer off" reasoning `useFieldObjectsQuery` documents.
 *
 * Positions are topocentric, so the endpoint 409s without a configured
 * location (a geocentric Moon is up to 1 deg wrong) — the same guard
 * `/framing` and `/night` already use. `instant` is `null` here whenever the
 * caller has no location either, so this never actually reaches the server
 * without one: an honest "nothing to draw" rather than a request that always
 * 409s. */
export function useAstroBodiesQuery(instant: string | null) {
  return useQuery({
    queryKey: ["astro", "bodies", instant] as const,
    queryFn: async () => {
      if (instant === null) throw new Error("useAstroBodiesQuery: queryFn ran with no instant");
      return unwrap(api.GET("/api/astro/bodies", { params: { query: { instant } } }));
    },
    enabled: instant !== null,
    placeholderData: (previous) => previous,
  });
}

/** Search the bundled maintainer-refreshed JPL SBDB snapshot and calculate
 * topocentric preview coordinates for the selected session instant. This
 * never makes a browser-to-JPL request; source and age travel in the response. */
export function useCometSuggestionsQuery(query: string, instant: string | null) {
  const trimmed = query.trim();
  return useQuery({
    queryKey: ["astro", "comet-suggestions", trimmed, instant] as const,
    queryFn: async () => {
      if (instant === null) throw new Error("useCometSuggestionsQuery: queryFn ran with no instant");
      return unwrap(
        api.GET("/api/astro/comet-suggestions", {
          params: { query: { query: trimmed, instant } },
        }),
      );
    },
    enabled: trimmed.length >= 2 && instant !== null,
    retry: false,
    staleTime: 5 * 60_000,
  });
}

/** Resolve one durable comet identity at the instant a motion action is
 * actually issued. The backend applies the same stale/uncertain-orbit guard
 * as the scheduler; preview coordinates are never reused for motion. */
export async function resolveCometForExecution(
  identity: { spkId: number; designation: string },
): Promise<CometSuggestionModel> {
  const response = await unwrap(
    api.GET("/api/astro/comet-suggestions", {
      params: {
        query: {
          query: identity.designation,
          instant: new Date().toISOString(),
          limit: 20,
        },
      },
    }),
  );
  const comet = response.comets.find(
    (candidate) => candidate.spk_id === identity.spkId && candidate.designation === identity.designation,
  );
  if (comet === undefined) throw new Error("The selected comet is no longer present in the bundled JPL catalogue.");
  if (!comet.execution_safe) {
    throw new Error(comet.execution_rejection ?? "The current comet orbit is not safe for automated pointing.");
  }
  return comet;
}

export type ProfileSummary = components["schemas"]["ProfileSummary"];
export type LensSummary = components["schemas"]["LensSummary"];
export type MountMode = components["schemas"]["MountMode"];
export type FramingRequest = components["schemas"]["FramingRequest"];
export type FramingResponse = components["schemas"]["FramingResponse"];
export type MosaicRequest = components["schemas"]["MosaicRequest"];
export type MosaicResponse = components["schemas"]["MosaicResponse"];
export type MosaicPanelModel = components["schemas"]["MosaicPanelModel"];
export type SessionEnvelopeModel = components["schemas"]["SessionEnvelopeModel"];
export type RotationSampleModel = components["schemas"]["RotationSampleModel"];
export type ShapeModel = components["schemas"]["ShapeModel"];
export type FieldGeometryModel = components["schemas"]["FieldGeometryModel"];
export type TargetResolutionResponse = components["schemas"]["TargetResolutionResponse"];
export type TargetSuggestionsResponse = components["schemas"]["TargetSuggestionsResponse"];
export type DeviceMountModeCheck = components["schemas"]["DeviceMountModeCheck"];
export type BestTargetsRequest = components["schemas"]["BestTargetsRequest"];
export type BestTargetsResponse = components["schemas"]["BestTargetsResponse"];
export type BestTargetModel = components["schemas"]["BestTargetModel"];
export type RotationSeverity = "none" | "amber" | "red";
export type AnnualAltitudeRequest = components["schemas"]["AnnualAltitudeRequest"];
export type AnnualAltitudeResponse = components["schemas"]["AnnualAltitudeResponse"];
export type AnnualAltitudeNightModel = components["schemas"]["AnnualAltitudeNightModel"];
export type NightRequest = components["schemas"]["NightRequest"];
export type NightResponse = components["schemas"]["NightResponse"];
export type EventTimeModel = components["schemas"]["EventTimeModel"];
/** Why an `EventTimeModel.when` is `null` — `occurs` is the only case with a
 * real timestamp. DESIGN.md §4.1: "above roughly 60° latitude astronomical
 * night does not occur at all in summer", reported honestly rather than
 * guessed at. */
export type Outcome = components["schemas"]["Outcome"];
/** Whether the framed target clears the horizon profile (not just a flat
 * minimum altitude) — DESIGN.md §5: "A target behind a roof is not visible,
 * and claiming otherwise is the silent-wrong-answer failure... the worst
 * this app has." Consumed by `overlay.ts`'s horizon markers and
 * `VisibilityPanel`. */
export type FramingVisibilityModel = components["schemas"]["FramingVisibilityModel"];
/** One continuous stretch a target is clear of the horizon mask — always a
 * list, never flattened to a single rise/set pair, because a target can
 * clear a treeline, be blocked by a chimney, and re-emerge. */
export type HorizonIntervalModel = components["schemas"]["HorizonIntervalModel"];
export type MoonAltitudeSampleModel = components["schemas"]["MoonAltitudeSampleModel"];
export type MoonAltitudeSeriesResponse = components["schemas"]["MoonAltitudeSeriesResponse"];
export interface MoonAltitudeRequest {
  start: string;
  end: string;
  step_min?: number;
}

export function useAstroProfilesQuery() {
  return useQuery({
    queryKey: ["astro", "profiles"] as const,
    queryFn: async () => unwrap(api.GET("/api/astro/profiles")),
    staleTime: Infinity, // bundled profile data, not device state
  });
}

export function useAstroTargetQuery(query: string) {
  const trimmed = query.trim();
  return useQuery({
    queryKey: ["astro", "target", trimmed] as const,
    queryFn: async () => unwrap(api.GET("/api/astro/target", { params: { query: { query: trimmed } } })),
    enabled: trimmed.length > 0,
    retry: false,
  });
}

export function useAstroTargetSuggestionsQuery(query: string) {
  const trimmed = query.trim();
  return useQuery({
    queryKey: ["astro", "target-suggestions", trimmed] as const,
    queryFn: async () =>
      unwrap(api.GET("/api/astro/target-suggestions", { params: { query: { query: trimmed } } })),
    enabled: trimmed.length >= 2,
    retry: false,
    staleTime: 5 * 60_000,
  });
}

export function useFramingQuery(body: FramingRequest | null) {
  const location = useCalculationSiteKey();
  return useQuery({
    queryKey: ["astro", "framing", body, location] as const,
    queryFn: async () => {
      // `enabled` below stops react-query from calling this while `body` is
      // null; asserted here rather than trusted, since a stale closure could
      // in principle race a caller flipping the query straight back on.
      if (body === null) throw new Error("useFramingQuery: queryFn ran with no request body");
      return unwrap(api.POST("/api/astro/framing", { body }));
    },
    enabled: body !== null,
  });
}

/** DESIGN.md §3.5's "best targets tonight": what is actually worth imaging
 * on this night from this site, ranked. The scoring lives entirely in
 * `ssc.astro.best_targets` — nothing here re-ranks or filters, so the UI
 * cannot quietly disagree with the model about what "best" means.
 *
 * A whole-catalogue solve, so unlike the per-target queries around it this
 * one is deliberately *not* re-run as the scrubber moves: it is keyed on the
 * night window and the optics, not on the scrub instant. */
export function useBestTargetsQuery(body: BestTargetsRequest | null) {
  const location = useCalculationSiteKey();
  return useQuery({
    queryKey: ["astro", "best-targets", body, location] as const,
    queryFn: async () => {
      if (body === null) throw new Error("useBestTargetsQuery: queryFn ran with no request body");
      return unwrap(api.POST("/api/astro/best-targets", { body }));
    },
    // Fixed-input numerical results remain valid while the request/site is unchanged.
    staleTime: 5 * 60_000,
    enabled: body !== null,
  });
}

/** A year of per-night target altitude reductions, one point per night
 * during astronomical darkness. This is Planning's broad-season counterpart
 * to `/api/astro/night`'s per-session altitude curve; like the other POST
 * computations here, `null` means the caller does not have a resolved target
 * and site yet. */
export function useAnnualAltitudeQuery(body: AnnualAltitudeRequest | null) {
  const location = useCalculationSiteKey();
  return useQuery({
    queryKey: ["astro", "annual-altitude", body, location] as const,
    queryFn: async () => {
      if (body === null) throw new Error("useAnnualAltitudeQuery: queryFn ran with no request body");
      return unwrap(api.POST("/api/astro/annual-altitude", { body }));
    },
    // Fixed-input numerical results remain valid while the request/site is unchanged.
    staleTime: 5 * 60_000,
    enabled: body !== null,
  });
}

/** Moon phase/position/separation (skymap-viz.md: "moon position, phase and
 * separation") plus `horizon_profile_active`/`visibility_intervals`
 * (skymap-viz.md: the visibility-intervals timeline, `VisibilityPanel`).
 * Twilight bands, sun/moon events and the altitude curve are the Scheduling
 * tab's Gantt (DESIGN.md §4.4), not this one — `altitude_samples` is kept
 * minimal in `PlanningTab` accordingly. */
export function useNightQuery(body: NightRequest | null) {
  const location = useCalculationSiteKey();
  const horizon = useSettingsQuery().data?.horizon;
  return useQuery({
    queryKey: ["astro", "night", body, location, horizon] as const,
    queryFn: async () => {
      if (body === null) throw new Error("useNightQuery: queryFn ran with no request body");
      return unwrap(api.POST("/api/astro/night", { body }));
    },
    // Fixed-input numerical results remain valid while the request/site is unchanged.
    staleTime: 5 * 60_000,
    enabled: body !== null,
  });
}

/** The Moon's own altitude across a window (Scheduling tab's grey moon
 * curve, DESIGN.md §4.4) — distinct from `useNightQuery`'s `altitude_curve`,
 * which follows a fixed-RA/Dec target, not the Moon. `null` means no window
 * yet (no plan open), the same "not enough input" convention every query
 * here uses. */
export function useMoonAltitudeQuery(request: MoonAltitudeRequest | null) {
  const location = useCalculationSiteKey();
  return useQuery({
    queryKey: ["astro", "moon-altitude", request, location] as const,
    queryFn: async () => {
      if (request === null) throw new Error("useMoonAltitudeQuery: queryFn ran with no request");
      return unwrap(api.GET("/api/astro/moon-altitude", { params: { query: request } }));
    },
    // Fixed-input numerical results remain valid while the request/site is unchanged.
    staleTime: 5 * 60_000,
    enabled: request !== null,
  });
}

// -- click-through object info (ssc/objectinfo, M11) ------------------------- //

export type ObjectInfoResponse = components["schemas"]["ObjectInfoResponse"];
export type ObjectInfoImageModel = components["schemas"]["ObjectInfoImageModel"];
export type ObjectInfoAttributionModel = components["schemas"]["ObjectInfoAttributionModel"];

/** `designation` is exactly what `GET /api/astro/objects`/`GET /api/astro/target`
 * already gave the caller for a DSO — never a new id scheme. `null` means
 * nothing is clicked yet (no query call, same "not enough input" convention
 * every query in this file uses). */
export function useObjectInfoQuery(designation: string | null) {
  return useQuery({
    queryKey: ["astro", "object-info", designation] as const,
    queryFn: async () => {
      if (designation === null) throw new Error("useObjectInfoQuery: queryFn ran with no designation");
      return unwrap(api.GET("/api/astro/objects/{designation}/info", { params: { path: { designation } } }));
    },
    enabled: designation !== null,
  });
}

/** The cached image's own bytes, proxied through `ssc-core` — never fetched
 * from Commons directly (architecture policy invariant 8). A plain relative path, the
 * same convention `cutoutLayers.ts`'s `displayPngUrl` already uses for an
 * `<img src>` straight from a server response shape rather than a client
 * URL-builder that could drift from the route. */
export function objectInfoImageUrl(designation: string): string {
  return `/api/astro/objects/${encodeURIComponent(designation)}/info/image`;
}

export function useMosaicQuery(body: MosaicRequest | null) {
  const location = useCalculationSiteKey();
  return useQuery({
    queryKey: ["astro", "mosaic", body, location] as const,
    queryFn: async () => {
      if (body === null) throw new Error("useMosaicQuery: queryFn ran with no request body");
      return unwrap(api.POST("/api/astro/mosaic", { body }));
    },
    // Fixed-input numerical results remain valid while the request/site is unchanged.
    staleTime: 5 * 60_000,
    enabled: body !== null,
  });
}


export type AutoNightRequest = components["schemas"]["AutoNightRequest"];
export type AutoNightResponse = components["schemas"]["AutoNightResponse"];
/** Explicit preview calculation; never starts or modifies an observing run. */
export function useAutoNightMutation() {
  return useMutation({
    mutationFn: async (body: AutoNightRequest) => unwrap(api.POST("/api/astro/auto-night", { body })),
  });
}
