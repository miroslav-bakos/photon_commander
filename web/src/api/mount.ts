// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `/api/mount/*` and `StateSnapshot.mount` — the Mount tab's data layer,
 * built 2026-09-10 against the contract pinned in the tab's own brief while
 * the backend serving it is still being written in parallel. `schema.d.ts`
 * has no `mount` routes or `StateSnapshot.mount` field yet, so every shape
 * below is hand-written against that pinned contract rather than a
 * generated alias (frontend-react.md normally forbids this — "types come
 * from the server" — and the exception is deliberate and temporary).
 *
 * TODO once `ssc/api/mount.py` merges and `npm run api:codegen` is rerun:
 * delete `MountState`/`MountCapabilities`/`MountTrackingRate`/`MountAxis`
 * here, replace them with `components["schemas"][...]` aliases the way
 * every other file in this directory does, and drop the bespoke `call()`
 * below in favour of the generated `api` client.
 *
 * `call()` reuses `unwrap`/`unwrapVoid` from `client.ts` so error handling —
 * the verbatim `detail` string on a 409, the "old server" message on a 404
 * — is identical to every other endpoint in this app, including the jog
 * control's silent-refusal case (a 501 for a member this device does not
 * implement, a 422 for a rate above `axis_rate_max_deg_s`).
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { unwrap, unwrapVoid } from "./client";
import { timedApiFetch } from "../performance/diagnostics";
import { useConnectionStore } from "../store/connectionStore";
import type { StateSnapshot } from "./types";
import { acknowledgeCommand, pendingCommandKey, waitForOperation } from "./operations";

export type MountTrackingRate = "sidereal" | "lunar" | "solar";
export type MountAxis = "primary" | "secondary";

export interface MountCapabilities {
  can_slew_async: boolean;
  can_sync: boolean;
  can_park: boolean;
  can_unpark: boolean;
  can_find_home: boolean;
  can_set_tracking: boolean;
  can_move_primary_axis: boolean;
  can_move_secondary_axis: boolean;
  axis_rate_max_deg_s: number | null;
  tracking_rates: MountTrackingRate[];
}

/** Every scalar nullable (invariant 7): `null` means unknown, never 0.
 * `stale`/`polled_at` exist because this device is polled, not pushed —
 * there is no event to tell the UI a reading is current, only a timestamp
 * to judge for itself. `polled_at` is itself nullable: a `mount` can exist
 * (the gateway is attached) with no successful poll yet, in which case it
 * is treated as unconditionally stale — never rendered as an age of "0s". */
export interface MountState {
  polled_at: string | null;
  stale: boolean;
  ra_hours: number | null;
  dec_deg: number | null;
  /** Server-side conversion of the reported mount frame into J2000. */
  ra_j2000_h?: number | null;
  dec_j2000_deg?: number | null;
  site_latitude_deg?: number | null;
  site_longitude_deg?: number | null;
  alt_deg: number | null;
  az_deg: number | null;
  sidereal_time_hours: number | null;
  tracking: boolean | null;
  tracking_rate: MountTrackingRate | null;
  at_park: boolean | null;
  at_home: boolean | null;
  slewing: boolean | null;
  jog_primary_active?: boolean;
  jog_secondary_active?: boolean;
  jog_lease_expires_at?: string | null;
  /** Pinned as `"topocentric"` on this device — coordinates of date, not
   * J2000 (DESIGN.md §4.4b). Left as `string` rather than a literal union:
   * ASCOM's `EquatorialCoordinateType` has other members this device does
   * not report, and the readout must not silently mislabel a value it does
   * not recognise. */
  equatorial_system: string | null;
  capabilities: MountCapabilities | null;
}

export interface TargetCenteringAttempt {
  attempt: number;
  commanded_ra_j2000_h: number;
  commanded_dec_j2000_deg: number;
  solved_ra_j2000_h: number;
  solved_dec_j2000_deg: number;
  error_arcmin: number;
}

export interface TargetCenteringResponse {
  converged: boolean;
  target_ra_j2000_h: number;
  target_dec_j2000_deg: number;
  tolerance_arcmin: number;
  attempts: TargetCenteringAttempt[];
}

export type TargetCenteringStage =
  | "idle"
  | "slewing"
  | "settling"
  | "tracking"
  | "capturing_solving"
  | "evaluating"
  | "correcting"
  | "complete"
  | "failed";

export interface TargetCenteringProgress {
  status: "idle" | "running" | "succeeded" | "failed";
  stage: TargetCenteringStage;
  attempt: number;
  max_attempts: number;
  target_ra_j2000_h: number | null;
  target_dec_j2000_deg: number | null;
  tolerance_arcmin: number | null;
  attempts: TargetCenteringAttempt[];
  message: string;
  error: string | null;
  updated_at: string | null;
}

/**
 * `useConnectionStore`'s `StateSnapshot` does not declare `mount` yet (see
 * this file's header), so it is read defensively at this one call site
 * rather than casting the snapshot type everywhere it is touched.
 *
 * `undefined` — no snapshot at all yet, the WS has not delivered one
 * (`MountTab`'s own convention for "waiting for state"). `null` — a
 * snapshot exists and the device has never been polled, or reports nothing.
 * A `MountState` — the last poll, which may itself be `stale`.
 */
export function useMountState(): MountState | null | undefined {
  return useConnectionStore((s) => {
    if (s.snapshot === null) return undefined;
    const withMount = s.snapshot as StateSnapshot & { mount?: MountState | null };
    return withMount.mount ?? null;
  });
}

/** `fetch` directly rather than the generated `openapi-fetch` client, which
 * only knows the paths `schema.d.ts` declares (see this file's header).
 * Shaped identically to what `openapi-fetch` returns so `unwrap`/
 * `unwrapVoid` need no special-casing for it. */
async function call(
  path: string,
  init?: RequestInit,
): Promise<{ data?: unknown; error?: unknown; response: Response }> {
  const response = await timedApiFetch(new Request(new URL(path, window.location.origin), { credentials: "same-origin", ...init }));
  const text = await response.text();
  let body: unknown;
  if (text.length > 0) {
    try {
      body = JSON.parse(text);
    } catch {
      body = undefined;
    }
  }
  return response.ok ? { data: body, response } : { error: body, response };
}

const post = (path: string) => call(path, { method: "POST" });
const postJson = (path: string, body: unknown) =>
  call(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const putJson = (path: string, body: unknown) =>
  call(path, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export function useMountSlewMutation() {
  return useMutation({
    mutationFn: async (body: { ra_hours: number; dec_deg: number }) =>
      unwrapVoid(postJson("/api/mount/slew", body)),
  });
}

/** Slew, capture/solve, and apply bounded correction slews until centered. */
export function useMountCenterMutation() {
  return useMutation({
    mutationFn: async (body: {
      ra_hours: number;
      dec_deg: number;
      tolerance_arcmin: number;
      exposure_s: number;
      gain: number | null;
      max_attempts: number;
      unpark_if_needed?: boolean;
    }) => waitForOperation<TargetCenteringResponse>(await unwrap(call("/api/mount/center", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": pendingCommandKey("target_centering"),
      },
      body: JSON.stringify(body),
    })) as import("./operations").OperationState),
    onSuccess: () => { acknowledgeCommand("target_centering"); },
  });
}

export function useMountCenterProgressQuery(enabled: boolean) {
  return useQuery<TargetCenteringProgress>({
    queryKey: ["mount", "center", "status"],
    queryFn: () => unwrap(call("/api/mount/center/status")) as Promise<TargetCenteringProgress>,
    enabled,
    // The footer keeps this query mounted on every screen. Preserve the
    // responsive in-run display without polling an idle centering service
    // four times per second forever.
    refetchInterval: enabled
      ? (query) => query.state.data?.status === "running" ? 250 : 2000
      : false,
  });
}

export function useMountZenithMutation() {
  return useMutation({
    mutationFn: async (body: { unpark_if_needed?: boolean } = {}) =>
      unwrapVoid(postJson("/api/mount/slew-zenith", body)),
  });
}

export function useMountSyncMutation() {
  return useMutation({
    mutationFn: async (body: { ra_hours: number; dec_deg: number; unpark_if_needed?: boolean }) =>
      unwrapVoid(postJson("/api/mount/sync", body)),
  });
}

/** The jog control (DESIGN.md §4.4b's centrepiece). `rate_deg_s: 0` is a
 * release, not a special case — callers send it exactly the way they send
 * any other rate. */
export function useMountMoveAxisMutation() {
  return useMutation({
    mutationFn: async (body: { axis: MountAxis; rate_deg_s: number }) =>
      unwrapVoid(postJson("/api/mount/move-axis", body)),
  });
}

/** The panic button. No body, no confirmation — callers decide when it is
 * safe to call this, not this hook. */
export function useMountStopMutation() {
  return useMutation({
    mutationFn: async () => unwrapVoid(post("/api/mount/stop")),
  });
}

export function useMountTrackingMutation() {
  return useMutation({
    mutationFn: async (body: { enabled: boolean; rate?: MountTrackingRate }) =>
      unwrapVoid(putJson("/api/mount/tracking", body)),
  });
}

/** Refuses below the horizon on this device (409, `device_code: 1279`) —
 * the caller must show `error.message` verbatim rather than assuming
 * success (DESIGN.md §4.4b / the tab's brief). */
export function useMountParkMutation() {
  return useMutation({
    mutationFn: async () => unwrapVoid(post("/api/mount/park")),
  });
}

/** Permits motion; does not itself raise the arm (correct ASCOM semantics
 * — the arm rises when motion follows). Never labelled "open arm" in the UI
 * for exactly that reason. */
export function useMountUnparkMutation() {
  return useMutation({
    mutationFn: async () => unwrapVoid(post("/api/mount/unpark")),
  });
}

export function useMountFindHomeMutation() {
  return useMutation({
    mutationFn: async () => unwrapVoid(post("/api/mount/find-home")),
  });
}
