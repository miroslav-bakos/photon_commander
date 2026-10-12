// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `/api/mount/polar-alignment/*` and `StateSnapshot.polar_alignment` — the
 * Polar align tab's data layer. Unlike `api/mount.ts`/`api/capture.ts`,
 * every shape here is a real generated alias (`schema.d.ts` was regenerated
 * against the merged backend for this change, `npm run api:codegen`), so
 * there is no hand-mirrored type and no bespoke `fetch` wrapper to delete
 * later — `types come from the server` (frontend-react.md), from day one.
 */
import { useMutation } from "@tanstack/react-query";
import { api, unwrap, unwrapVoid } from "./client";
import { acknowledgeCommand, pendingCommandKey, waitForOperation } from "./operations";
import { useConnectionStore } from "../store/connectionStore";
import type { components } from "./schema";

export type PolarAlignmentState = components["schemas"]["PolarAlignmentState"];
export type PolarSimulationRequest = components["schemas"]["PolarSimulationRequest"];
export type PolarSimulationResponse = components["schemas"]["PolarSimulationResponse"];
export type PolarAlignmentPointState = components["schemas"]["PolarAlignmentPointState"];
export type PolarAlignmentPointStatus = components["schemas"]["PolarAlignmentPointStatus"];
export type PolarAlignmentStatus = components["schemas"]["PolarAlignmentStatus"];
export type PolarAlignmentResult = components["schemas"]["PolarAlignmentResult"];
export type PolarAlignmentAssessment = components["schemas"]["PolarAlignmentAssessment"];
export type PolarAlignmentRunKind = components["schemas"]["PolarAlignmentRunKind"];
export type PolarRefinementResult = components["schemas"]["PolarRefinementResult"];
export type PolarAlignmentStartRequest = Omit<components["schemas"]["PolarAlignmentStartRequest"], "unpark_if_needed"> & { unpark_if_needed?: boolean };
export type PolarAlignmentRecheckRequest = Omit<components["schemas"]["PolarAlignmentRecheckRequest"], "unpark_if_needed"> & { unpark_if_needed?: boolean };
export type PolarRefinementStartRequest = components["schemas"]["PolarRefinementStartRequest"];
export type PolarAlignmentStartMode = components["schemas"]["PolarAlignmentStartMode"];
export type PolarAlignmentStartInfo = components["schemas"]["PolarAlignmentStartInfo"];
export type PlateSolveHereRequest = components["schemas"]["PlateSolveHereRequest"];
export interface PlateSolveHereResponse {
  ra_j2000_hours: number;
  dec_j2000_deg: number;
  date_obs: string;
  pixel_scale_arcsec: number;
  rotation_deg: number;
  solver: string;
  solver_version?: string | null;
  mount_ra_j2000_hours?: number | null;
  mount_dec_j2000_deg?: number | null;
  mount_altitude_deg?: number | null;
  mount_azimuth_deg?: number | null;
}

/** `undefined` — no snapshot at all yet (the WS has not delivered one).
 * `null` — a snapshot exists but this process has never run a polar
 * alignment (`status is None`'s own meaning, per the backend model's
 * docstring). A `PolarAlignmentState` — the live or last-finished run,
 * updated by every `polar_alignment.changed` event via `store/reduce.ts`,
 * never duplicated into this tab's own component state. */
export function usePolarAlignmentState(): PolarAlignmentState | null | undefined {
  return useConnectionStore((s) => {
    if (s.snapshot === null) return undefined;
    return s.snapshot.polar_alignment ?? null;
  });
}

/** Artificial sky only: the endpoint has no mount or camera dependency. */
export function useSimulatePolarAlignmentMutation() {
  return useMutation({
    mutationFn: async (body: PolarSimulationRequest) =>
      unwrap(api.POST("/api/mount/polar-alignment/simulate", { body })),
  });
}

/**
 * `POST .../start` answers 202 with a `PolarAlignmentState` mirroring the
 * snapshot section (the route's own docstring), discarded here via
 * `unwrapVoid` on purpose: `usePolarAlignmentState()` reading the WS-pushed
 * snapshot is this tab's one source of truth for run state, never a
 * mutation's own response (frontend-react.md: "server state is never
 * duplicated into component state" — the same choice `useStartExposureMutation`
 * makes in `api/capture.ts`).
 *
 * Every refusal (`ssc/api/routes/polar_alignment.py`) — 409 already
 * running / not in polar mode / site, ephemeris or position unknown / no
 * solver available, or 503 no camera/telescope hosted — carries its
 * `detail` through to `ApiError.message` verbatim via `client.ts`'s
 * `describeError`, which is what a caller must show rather than a generic
 * message (architecture policy invariant 7).
 */
export function useStartPolarAlignmentMutation() {
  return useMutation({
    mutationFn: async (body: PolarAlignmentStartRequest) =>
      unwrapVoid(api.POST("/api/mount/polar-alignment/start", { body: body as components["schemas"]["PolarAlignmentStartRequest"] })),
  });
}

/** Independent same-direction sweep using the prior measurement's point count. */
export function useRecheckPolarAlignmentMutation() {
  return useMutation({
    mutationFn: async (body: PolarAlignmentRecheckRequest) =>
      unwrapVoid(api.POST("/api/mount/polar-alignment/recheck", { body: body as components["schemas"]["PolarAlignmentRecheckRequest"] })),
  });
}

/** Deliberately not rate-limited server-side (the route's own docstring,
 * mirroring `POST /api/mount/stop`) — must work whatever else is in
 * flight. 409 ("no run is in progress") is shown the same verbatim way. */
export function useCancelPolarAlignmentMutation() {
  return useMutation({
    mutationFn: async () => unwrapVoid(api.POST("/api/mount/polar-alignment/cancel")),
  });
}

/** Stop the continuous post-sweep refinement loop. */
export function useStopPolarRefinementMutation() {
  return useMutation({
    mutationFn: async () => unwrapVoid(api.POST("/api/mount/polar-alignment/refine/stop")),
  });
}

/** Start (or restart) live updates from the completed sweep without slewing
 * through the measurement points again. */
export function useStartPolarRefinementMutation() {
  return useMutation({
    mutationFn: async (body: PolarRefinementStartRequest) =>
      unwrapVoid(api.POST("/api/mount/polar-alignment/refine/start", { body })),
  });
}

/** `POST .../solve-here` — one exposure where the scope already points,
 * solved, returned next to what the mount reported for the same instant
 * (the route's own docstring). Moves nothing, so it is a plain mutation
 * result read from `solveHere.data`, never folded into the WS-driven
 * `PolarAlignmentState`: it is not part of a run and has no snapshot
 * section of its own. 409 while a sweep or refinement loop is running,
 * since both assume they alone are driving the camera — shown verbatim,
 * same as every other refusal on this tab. */
export function useSolveHereMutation() {
  return useMutation({
    mutationFn: async (body: PlateSolveHereRequest) =>
      waitForOperation<PlateSolveHereResponse>(await unwrap(api.POST(
        "/api/mount/polar-alignment/solve-here",
        { body, headers: { "Idempotency-Key": pendingCommandKey("solve_here") } },
      ))),
    onSuccess: () => { acknowledgeCommand("solve_here"); },
  });
}
