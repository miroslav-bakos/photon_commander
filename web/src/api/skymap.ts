// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The sky map's data layer: the survey registry (`ssc/skymap/surveys.py`
 * via `GET /api/skymap/surveys`) and the catalogue objects in a field
 * (`GET /api/astro/objects`). Types are aliases into the generated schema,
 * never hand-mirrored. The map *frame* never calls either — the host fetches
 * and forwards over `postMessage` (`SkyMap.tsx`), keeping the iframe API-free.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, unwrap, unwrapVoid } from "./client";
import type { components } from "./schema";

export type SurveyModel = components["schemas"]["SurveyModel"];
export type SurveysResponse = components["schemas"]["SurveysResponse"];
export type FieldObjectModel = components["schemas"]["FieldObjectModel"];
export type FieldObjectsResponse = components["schemas"]["FieldObjectsResponse"];

export type CutoutState = components["schemas"]["CutoutState"];
export type CutoutJobStatus = components["schemas"]["CutoutJobStatus"];
export type CutoutSourceModel = components["schemas"]["CutoutSourceModel"];
export type CutoutManifestResponse = components["schemas"]["CutoutManifestResponse"];
export type CutoutPieceModel = components["schemas"]["CutoutPieceModel"];
export type CutoutWcsModel = components["schemas"]["CutoutWcsModel"];
export type CutoutRequest = components["schemas"]["CutoutRequest"];

export function useClearSkymapCacheMutation() {
  return useMutation({
    mutationFn: async (survey: string) =>
      unwrap(api.DELETE("/api/skymap/cache/{survey}", { params: { path: { survey } } })),
  });
}

export function useSkymapSurveysQuery() {
  return useQuery({
    queryKey: ["skymap", "surveys"] as const,
    queryFn: async () => unwrap(api.GET("/api/skymap/surveys")),
    // Availability (a bundled pack appearing, an upstream going away) is
    // pushed through the snapshot; the registry itself does not change
    // while the page is open.
    staleTime: 5 * 60_000,
  });
}

export interface FieldObjectsRequest {
  ra_deg: number;
  dec_deg: number;
  radius_deg: number;
  limit?: number;
}

export function useFieldObjectsQuery(request: FieldObjectsRequest | null) {
  return useQuery({
    queryKey: ["astro", "objects", request] as const,
    queryFn: async () => {
      if (request === null) throw new Error("useFieldObjectsQuery: queryFn ran with no request");
      return unwrap(api.GET("/api/astro/objects", { params: { query: request } }));
    },
    enabled: request !== null,
    // The bundled catalogue is static for the process lifetime.
    staleTime: Infinity,
    // Keep the previous field's outlines on screen while the next cone loads,
    // so a pan does not blink the layer off and on.
    placeholderData: (previous) => previous,
  });
}

export type BrightStarModel = components["schemas"]["BrightStarModel"];
export type BrightStarsResponse = components["schemas"]["BrightStarsResponse"];

export interface BrightStarsRequest {
  ra_deg: number;
  dec_deg: number;
  radius_deg: number;
  limit?: number;
}

/** The bundled named bright stars in a cone (`GET /api/astro/stars`,
 * `ssc.astro.stars`) — same request/caching shape as
 * `useFieldObjectsQuery`: static bundled data (`staleTime: Infinity`), and
 * `placeholderData` keeps the previous field's stars on screen while the
 * next cone loads rather than blinking the layer off on a pan. */
export function useBrightStarsQuery(request: BrightStarsRequest | null) {
  return useQuery({
    queryKey: ["astro", "stars", request] as const,
    queryFn: async () => {
      if (request === null) throw new Error("useBrightStarsQuery: queryFn ran with no request");
      return unwrap(api.GET("/api/astro/stars", { params: { query: request } }));
    },
    enabled: request !== null,
    staleTime: Infinity,
    placeholderData: (previous) => previous,
  });
}

export type ConstellationBoundariesResponse = components["schemas"]["ConstellationBoundariesResponse"];
export type ConstellationBoundaryRegionModel = components["schemas"]["ConstellationBoundaryRegionModel"];

/** The 88 IAU regions' vertex rings, precessed to J2000 server-side
 * (`ssc.astro.constellations.boundaries_j2000` via `GET
 * /api/astro/constellation-boundaries`) — 1562 vertices, fixed for the
 * process lifetime (no `epoch`/`now`: B1875-to-J2000 precession does not
 * depend on either). `staleTime: Infinity` matches `useFieldObjectsQuery`'s
 * bundled-catalogue treatment above: nothing here changes while the page is
 * open, so there is nothing to refetch on a view change or a scrub tick. */
export function useConstellationBoundariesQuery(enabled: boolean) {
  return useQuery({
    queryKey: ["astro", "constellation-boundaries"] as const,
    queryFn: async () => unwrap(api.GET("/api/astro/constellation-boundaries")),
    // Gated on the layer toggle (default off, `layerPrefs.ts`): a viewer who
    // never turns this on should not pay for a 1562-vertex fetch. Once
    // fetched, `staleTime: Infinity` keeps it — toggling off and back on
    // within the same session does not refetch.
    enabled,
    staleTime: Infinity,
  });
}

/** Deletes one cached target-field cutout. Like the pack mutations above,
 * this does not invalidate a query — the snapshot's `skymap.cutouts` drops
 * it once the WS catches up. */
export function useDeleteSkymapCutoutMutation() {
  return useMutation({
    mutationFn: async (cutoutId: string) =>
      unwrapVoid(api.DELETE("/api/skymap/cutouts/{cutout_id}", { params: { path: { cutout_id: cutoutId } } })),
  });
}

/** Every cached target-field image, in one request — the list has no bound
 * otherwise, and grows one row per field a target was ever framed on. */
export function useDeleteAllCutoutsMutation() {
  return useMutation({
    mutationFn: async () => unwrap(api.DELETE("/api/skymap/cutouts", {})),
  });
}

// -- target-field cutouts (Planning tab's "Target field" layer group) ------- //
//
// A cutout job's live progress (byte count while running) lives in the
// snapshot's `skymap.cutouts` (kept current by the WS, like the offline-pack
// jobs above) — but that list is capped at the last 50 jobs server-side, so
// it is not a complete history and must not be trusted alone for a job's
// *status*: a genuinely complete job can age out of it. These hooks are the
// uncapped complement: the point lookup on a target change (so an offline
// user sees what is already cached without fetching, and so a job that has
// aged out of the snapshot is still found) and the pieces/WCS themselves.
// `SkyMap.tsx` merges this query's result with the snapshot's own list
// before resolving each source's row state — see its own doc comment.

export interface CutoutPointRequest {
  ra_deg: number;
  dec_deg: number;
  radius_deg: number;
}

/** Cutouts already covering `request`'s point, newest first — checked on a
 * target change *before* offering "Fetch for this field", so an offline
 * user with a cached cutout sees it without needing the network. */
export function useCachedCutoutsQuery(request: CutoutPointRequest | null) {
  return useQuery({
    queryKey: ["skymap", "cutouts", "covering", request] as const,
    queryFn: async () => {
      if (request === null) throw new Error("useCachedCutoutsQuery: queryFn ran with no request");
      return unwrap(api.GET("/api/skymap/cutouts", { params: { query: request } }));
    },
    enabled: request !== null,
    placeholderData: (previous) => previous,
  });
}

/** Every completed field already cached for one source. This never starts a
 * network fetch. */
export function useCachedCutoutsBySourceQuery(source: string) {
  return useQuery({
    queryKey: ["skymap", "cutouts", "source", source] as const,
    queryFn: async () =>
      unwrap(api.GET("/api/skymap/cutouts", { params: { query: { source } } })),
    staleTime: 30_000,
  });
}

/** Starts (or, on a cache hit, simply reports) one cutout job. Like the
 * offline-pack mutation above, the 202 body's `cutout` is not stored here —
 * the caller only needs the id to track, and the snapshot's
 * `skymap.cutouts` is what the panel renders progress from. */
export function useRequestCutoutMutation() {
  return useMutation({
    mutationFn: async (body: CutoutRequest) => unwrap(api.POST("/api/skymap/cutouts", { body })),
  });
}

/** The pieces (display PNG + WCS) for every *complete, currently visible*
 * cutout, batched into one query keyed by the sorted id set — up to three
 * ids (one per registry source), so one request per render is simpler than
 * juggling a variable number of `useQuery` calls (React's rules of hooks
 * forbid a hook count that depends on data). Never polled while a job is
 * still running: nothing to draw until it finishes, and a completed
 * cutout's pieces never change under the same id. */
export function useCutoutManifestsQuery(cutoutIds: readonly string[]) {
  const sortedIds = [...cutoutIds].sort();
  return useQuery({
    queryKey: ["skymap", "cutouts", "manifests", sortedIds] as const,
    queryFn: async () => {
      const manifests = await Promise.all(
        sortedIds.map((id) => unwrap(api.GET("/api/skymap/cutouts/{cutout_id}", { params: { path: { cutout_id: id } } }))),
      );
      return new Map(manifests.map((manifest) => [manifest.id, manifest]));
    },
    enabled: sortedIds.length > 0,
    staleTime: Infinity,
  });
}
