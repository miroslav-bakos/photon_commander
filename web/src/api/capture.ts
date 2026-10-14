// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `/api/capture/*` and `StateSnapshot.capture` — driving one exposure end to
 * end (DESIGN.md §9.5), built 2026-09-10 against the contract pinned in this
 * tab's own brief while `backend-core` writes the routes themselves in
 * parallel, in this same tree. `schema.d.ts` has no `capture` routes or
 * `StateSnapshot.capture` field yet (checked 2026-09-10 — codegen has not
 * been rerun against the backend change), so every shape below is
 * hand-written rather than a generated alias — the same deliberate,
 * temporary exception `api/mount.ts`'s header documents.
 *
 * **Cross-checked against the concurrent, uncommitted backend change in this
 * same working copy** (`ssc/api/routes/capture.py`, `ssc/api/schemas.py`'s
 * `CaptureExposureRequest`/`CaptureFrameModel`/`CaptureFramesResponse`,
 * `ssc/state/models.py`'s `CaptureState`) rather than left as a guess: every
 * field name below matches those Pydantic models exactly, including the one
 * surprise — the request's `object` (this tab's own pinned contract) is
 * *not* mirrored in the response, which reports `object_name`/`filter_name`
 * (`ssc.store.capture.CaptureFrameRecord`'s own column names) instead.
 * `POST /api/capture/exposure` actually answers 202 with a `CaptureState`
 * body, not an empty one — `useStartExposureMutation` still discards it via
 * `unwrapVoid`, deliberately: `useCaptureState()` reading the WS-pushed
 * snapshot is this app's one source of truth for capture state, never a
 * mutation's own response (frontend-react.md: "server state is never
 * duplicated into component state").
 *
 * TODO once codegen is rerun against the merged backend: delete
 * `CaptureState`/`CaptureExposureRequest`/`CaptureFrame` here, replace them
 * with `components["schemas"][...]` aliases the way every other file in this
 * directory does, and drop the bespoke `call()` below (copied from
 * `mount.ts`, not reused, since a truly shared helper is exactly the kind of
 * abstraction this exception is meant to be temporary rather than build on).
 *
 * `GET /api/capture/sessions` (`CaptureSessionModel`: `object_name`,
 * `started_at_utc`, `ended_at_utc`, `frame_count`, `filters`) is confirmed
 * too but has no caller anywhere in this change — nothing in the brief's
 * verification list needs a sessions view, so it is left for whoever builds
 * the first UI that does rather than wired here with no consumer.
 *
 * **2026-09-10, frame-type brief.** `light: bool` is being replaced by
 * `frame_type: "light"|"dark"|"flat"|"bias"` in `CaptureExposureRequest`, by
 * the same `backend-core` agent, in parallel — *not yet merged* at the time
 * of this change (checked: `ssc/api/schemas.py` still has `light: bool` on
 * disk). Unlike the cross-check above, this part is built to the contract
 * stated in the brief rather than read off a concurrent working copy, and it
 * could not be verified end to end. Two further fields this brief needs —
 * `CaptureState.frame_type` (what is currently exposing, for a reload to
 * recover) and `CaptureFrame.frame_type` (light vs dark in the recent list)
 * — are not named anywhere in either brief, so they are added here as the
 * narrowest addition that satisfies "the frame type belongs in whatever the
 * panel shows about a capture in flight and in the recent-frames list";
 * both are typed optional/undefined-safe so a server that has not grown them
 * yet degrades to "type unknown" rather than a crash or a fabricated guess.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { unwrap, unwrapVoid } from "./client";
import { timedApiFetch } from "../performance/diagnostics";
import { readDownloadBlob } from "./download";
import { useConnectionStore } from "../store/connectionStore";
import type { StateSnapshot } from "./types";
import type { components } from "./schema";
import { acknowledgeCommand, isOperationState, pendingCommandKey, waitForOperation, type OperationState } from "./operations";

/** The four frame types the backend enum defines (`frame-type` brief). Flat
 * carries explicit source/optic provenance from the capture wizard; bias uses
 * the camera's live minimum exposure sentinel. */
export type FrameType = "light" | "dark" | "flat" | "bias";

/** Mirrors `ssc.state.models.CaptureStage` — every value is backed by a real
 * capture-orchestrator/device step. Dithering stays active through the mount
 * settle gate; exposure begins only after it clears. There is no
 * `"platesolving"` because no capture-time plate solve exists. */
export type CaptureStage = "dithering" | "exposing" | "downloading" | "transferring" | "saving";

/** Server-defined manual camera selection and exposure contract. */
export type CaptureExposureRequest = components["schemas"]["CaptureExposureRequest"];

export interface FlatProbeState {
  filter_name: string;
  exposure_s: number;
  median_fraction: number;
  clipped_pixel_fraction: number;
  banding_fraction: number;
  banding_axis: "rows" | "columns" | null;
  decision: "accept" | "probe" | "adjust_source" | "fail";
  reason: string;
}

export interface CalibrationRunState {
  status: "idle" | "running" | "cancelling" | "completed" | "cancelled" | "failed";
  kind: Exclude<FrameType, "light"> | null;
  phase: "preparing" | "probing" | "capturing" | "integrating" | null;
  requested_count: number;
  filter_names: string[];
  current_filter: string | null;
  completed_frames: number;
  total_frames: number;
  exposure_s: number | null;
  gain: number | null;
  probes: FlatProbeState[];
  generate_master: boolean;
  keep_only_master?: boolean;
  master_paths: string[];
  removed_source_frames: number;
  message: string | null;
  error: string | null;
}

export interface CalibrationCaptureStartRequest {
  covered_lens_confirmed?: boolean;
  kind: Exclude<FrameType, "light">;
  count: number;
  duration_s?: number | null;
  gain?: number | null;
  filter_positions?: number[];
  optic_id?: string | null;
  flat_source?: "panel" | "sky" | null;
  generate_master: boolean;
  keep_only_master?: boolean;
}

export interface CalibrationSourceSet {
  optic_id?: string | null;
  frame_ids: number[];
  frame_type: Exclude<FrameType, "light">;
  frame_count: number;
  gain: number | null;
  exposure_s: number | null;
  filter_name: string | null;
  width: number;
  height: number;
  started_at: string;
  ended_at: string;
  can_build: boolean;
  blocker: string | null;
  compatible_bias_frames: number;
}

/** `StateSnapshot.capture` — `null`/`undefined` handled the same way
 * `useMountState` handles a device that is polled rather than pushed:
 * `undefined` (no snapshot at all yet, the WS has not delivered one) and
 * `null` (a snapshot exists but the server has nothing to say about capture
 * yet) are kept distinct from a real `CaptureState`, never collapsed into a
 * default. `in_progress` is what the exposure button and every "a capture is
 * running" message renders from — never local component state, which a page
 * reload would lose (the brief's own verification requirement 1).
 * `frame_type` is this brief's own addition (see this file's header) — the
 * type of whatever capture is in progress or just finished, optional so a
 * server that has not grown the field yet is "unknown", not "light". */
export interface CaptureState {
  in_progress: boolean;
  last_frame_id: number | null;
  last_captured_at: string | null;
  last_error: string | null;
  folder_configured: boolean;
  frame_type?: FrameType | null;
  /** 2026-09-11 progress brief: `stage`/`duration_s`/`started_at`/
   * `object_name`/`gain` describe the *current* exposure only — all set
   * together when one starts and cleared together back to `null`/undefined
   * when it ends (`CaptureService._finish`, both success and failure paths).
   * Optional/undefined-safe like `frame_type` above, for a server that has
   * not grown these fields yet. */
  stage?: CaptureStage | null;
  duration_s?: number | null;
  started_at?: string | null;
  object_name?: string | null;
  gain?: number | null;
  loop_active?: boolean;
  loop_frame_limit?: number | null;
  loop_completed_frames?: number;
  calibration?: CalibrationRunState;
}

/** One catalogued frame — field names match `CaptureFrameModel`
 * (`ssc/api/schemas.py`) exactly, including `object_name`/`filter_name`
 * rather than the request body's `object` (see this file's header). `path`
 * and `created_at` exist on the real model but are left out here: nothing
 * in this change renders them. `frame_type` is this brief's own addition
 * (see this file's header) — optional for the same reason `CaptureState`'s
 * is. */
export interface CaptureFrame {
  optic_id?: "tele" | "wide";
  id: number;
  path?: string;
  object_name: string | null;
  filter_name: string | null;
  date_obs: string;
  exposure_s: number | null;
  gain: number | null;
  width: number;
  height: number;
  status: string;
  frame_type?: FrameType;
}

/** Matches `CaptureFramesResponse` (`ssc/api/schemas.py`) exactly. */
export interface CaptureFramesResponse {
  frames: CaptureFrame[];
}

export interface ArchiveEntry {
  key: string;
  name: string;
  directory: string;
  kind: "fits" | "image";
  size_bytes: number;
  modified_at: string;
  date_obs: string;
  date_source: "capture_catalogue" | "fits_header" | "filesystem_mtime";
  catalogue_id: number | null;
  object_name: string | null;
  filter_name: string | null;
  frame_type: string | null;
  exposure_s: number | null;
  gain: number | null;
  ccd_temp_c: number | null;
  width: number | null;
  height: number | null;
  bayer_pattern: string | null;
  preview_available: boolean;
}

export interface ArchiveEntriesResponse {
  root_name: string;
  storage_total_bytes: number;
  storage_free_bytes: number;
  entries: ArchiveEntry[];
  directories: string[];
  truncated: boolean;
}

export interface ArchiveDeleteResponse {
  files_deleted: number;
  catalogue_rows_deleted: number;
  bytes_deleted: number;
  folder_deleted: string | null;
}

export interface ArchiveTransferResponse {
  files_transferred: number;
  directories_transferred: number;
  bytes_transferred: number;
  catalogue_rows_updated: number;
  destination: string;
  mode: "move" | "copy";
}

export interface CalibrationMaster {
  name: string;
  path: string;
  frame_type: Exclude<FrameType, "light">;
  status: "active" | "archived";
  provenance_available: boolean;
}

export interface CalibrationMastersResponse {
  masters: CalibrationMaster[];
}

/**
 * `useConnectionStore`'s `StateSnapshot` does not declare `capture` yet (see
 * this file's header), so it is read defensively at this one call site
 * rather than casting the snapshot type everywhere it is touched — the same
 * technique `api/mount.ts`'s `useMountState` uses for `mount`.
 */
export function useCaptureState(): CaptureState | null | undefined {
  return useConnectionStore((s) => {
    if (s.snapshot === null) return undefined;
    const withCapture = s.snapshot as StateSnapshot & { capture?: CaptureState | null };
    return withCapture.capture ?? null;
  });
}

/** `fetch` directly rather than the generated `openapi-fetch` client, which
 * only knows the paths `schema.d.ts` declares (see this file's header).
 * Shaped identically to what `openapi-fetch` returns so `unwrap`/
 * `unwrapVoid` — and therefore the verbatim 409/501 `detail` handling —
 * need no special-casing for it. */
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

const postJson = (path: string, body: unknown) =>
  call(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

/**
 * `202` means accepted, not done (the brief's own emphasis) — this mutation
 * resolves as soon as the server has *started* the exposure, and every
 * caller renders "in progress" from `useCaptureState()`'s `in_progress`
 * afterwards, never from this mutation having settled. Every rejection
 * `ssc/api/routes/capture.py` raises — 409 (no folder configured, or one
 * already running), 503 (no camera hosted), 422 (duration outside the
 * camera's own limits), 501 (`StartExposure` not implemented on this
 * driver) — carries its `detail` through unwrapVoid → `ApiError.message`
 * verbatim (`client.ts`'s `describeError` extracts `detail` for any status,
 * not only the 409/501 the brief calls out by name), which is what the
 * brief requires be shown rather than a generic message.
 */
export function useStartExposureMutation() {
  return useMutation({
    mutationFn: async (body: CaptureExposureRequest) => unwrapVoid(postJson("/api/capture/exposure", body)),
  });
}

export function useStopCaptureLoopMutation() {
  return useMutation({
    mutationFn: async () => unwrapVoid(postJson("/api/capture/loop/stop", undefined)),
  });
}

export function useStartCalibrationMutation() {
  return useMutation({
    mutationFn: async (body: CalibrationCaptureStartRequest) =>
      unwrapVoid(postJson("/api/capture/calibration/start", body)),
  });
}

export function useCancelCalibrationMutation() {
  return useMutation({
    mutationFn: async () => unwrapVoid(postJson("/api/capture/calibration/cancel", undefined)),
  });
}

const calibrationSourceSetsKey = ["capture", "calibration", "source-sets"] as const;

export function useCalibrationSourceSetsQuery(enabled: boolean) {
  return useQuery({
    queryKey: calibrationSourceSetsKey,
    enabled,
    queryFn: async () => unwrap(call("/api/capture/calibration/source-sets") as Promise<{
      data?: { sets: CalibrationSourceSet[] };
      error?: unknown;
      response: Response;
    }>),
  });
}

export function useBuildCalibrationMasterMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (frameIds: number[]) =>
      unwrapVoid(postJson("/api/capture/calibration/masters/build", { frame_ids: frameIds })),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: calibrationMastersKey }),
        queryClient.invalidateQueries({ queryKey: ["capture", "archive"] }),
      ]);
    },
  });
}

const framesKey = ["capture", "frames"] as const;

/** `limit` defaults to 50 server-side; passed through only when the caller
 * wants fewer. Not invalidated by `useStartExposureMutation` — the new frame
 * shows up in this list once the WS-driven `useCaptureState()` reports a new
 * `last_frame_id`, which is what callers key a refetch off, keeping this
 * file's only source of truth for "a capture just finished" the snapshot,
 * not this mutation's own settling. */
export function useCaptureFramesQuery(limit?: number) {
  return useQuery({
    queryKey: [...framesKey, limit ?? null] as const,
    // `call`'s `data` is `unknown` — this file's own header already flags
    // this shape as unconfirmed against a live response, so the cast
    // happens once here, at this one call site, rather than inside a
    // generic helper `no-unnecessary-type-parameters` would flag as a
    // no-op wrapper around the same single-use cast.
    queryFn: async () =>
      unwrap(
        call(limit === undefined ? "/api/capture/frames" : `/api/capture/frames?limit=${String(limit)}`) as Promise<{
          data?: CaptureFramesResponse;
          error?: unknown;
          response: Response;
        }>,
      ),
  });
}

export function useArchiveEntriesQuery() {
  return useQuery({
    queryKey: ["capture", "archive"] as const,
    queryFn: async () =>
      unwrap(call("/api/capture/archive") as Promise<{
        data?: ArchiveEntriesResponse;
        error?: unknown;
        response: Response;
      }>),
  });
}

function useArchiveDeleteMutation<TBody>(path: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: TBody) =>
      waitForOperation<ArchiveDeleteResponse>(await unwrap(call(path, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": pendingCommandKey("archive_delete"),
        },
        body: JSON.stringify(body),
      }) as Promise<{
        data?: OperationState;
        error?: unknown;
        response: Response;
      }>)),
    onSuccess: async () => {
      acknowledgeCommand("archive_delete");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["capture", "archive"] }),
        queryClient.invalidateQueries({ queryKey: framesKey }),
        queryClient.invalidateQueries({ queryKey: calibrationMastersKey }),
        queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "masters"] }),
      ]);
    },
    // A terminal operation failure was accepted and executed by the server.
    // Reusing its idempotency key would only retrieve that same failed operation
    // forever, even after the user stops the activity that made deletion busy.
    onError: () => { acknowledgeCommand("archive_delete"); },
  });
}

export function useDeleteArchiveFilesMutation() {
  return useArchiveDeleteMutation<{ keys: string[] }>("/api/capture/archive/delete-files");
}

export function useDeleteArchiveFolderMutation() {
  return useArchiveDeleteMutation<{ directory: string }>("/api/capture/archive/delete-folder");
}

export function useTransferArchiveFilesMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { keys: string[]; directories: string[]; destination: string; mode: "move" | "copy" }) =>
      waitForOperation<ArchiveTransferResponse>(await unwrap(call("/api/capture/archive/transfer", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": pendingCommandKey("archive_transfer"),
        },
        body: JSON.stringify(body),
      }) as Promise<{ data?: OperationState; error?: unknown; response: Response }>)),
    onSuccess: async () => {
      acknowledgeCommand("archive_transfer");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["capture", "archive"] }),
        queryClient.invalidateQueries({ queryKey: framesKey }),
      ]);
    },
  });
}

const calibrationMastersKey = ["capture", "calibration", "masters"] as const;

export function useCalibrationMastersQuery() {
  return useQuery({
    queryKey: calibrationMastersKey,
    queryFn: async () => unwrap(call("/api/capture/calibration/masters") as Promise<{
      data?: CalibrationMastersResponse;
      error?: unknown;
      response: Response;
    }>),
  });
}

export function useMoveCalibrationMasterMutation(destination: "active" | "archived") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: Pick<CalibrationMaster, "frame_type" | "name">) =>
      unwrap(postJson(`/api/capture/calibration/masters/${destination === "active" ? "restore" : "archive"}`, body) as Promise<{
        data?: CalibrationMaster;
        error?: unknown;
        response: Response;
      }>),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: calibrationMastersKey }),
        queryClient.invalidateQueries({ queryKey: ["capture", "archive"] }),
        queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "masters"] }),
      ]);
    },
  });
}

export function archiveImageUrl(entry: ArchiveEntry, thumbnail = false): string {
  const query = new URLSearchParams({ key: entry.key, v: entry.modified_at });
  if (thumbnail) query.set("thumbnail", "true");
  return `/api/capture/archive/image?${query.toString()}`;
}

export async function downloadArchiveSelectionZip(body: { keys: string[]; directories: string[] }): Promise<void> {
  const response = await timedApiFetch(new Request(new URL("/api/capture/archive/selection-zip", window.location.origin), {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `Could not create archive ZIP (${String(response.status)})`);
  }
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filename = /filename="?([^";]+)"?/i.exec(disposition)?.[1] ?? "archive-selection.zip";
  const url = globalThis.URL.createObjectURL(await readDownloadBlob(response));
  try {
    const anchor = globalThis.document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    globalThis.document.body.append(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    globalThis.URL.revokeObjectURL(url);
  }
}

export function useArchiveHeaderQuery(key: string | null) {
  return useQuery({
    queryKey: ["capture", "archive", "header", key],
    enabled: key !== null,
    queryFn: async () => unwrap(call(`/api/capture/archive/header?${new URLSearchParams({ key: key ?? "" }).toString()}`) as Promise<{
      data?: Record<string, string | number | boolean>;
      error?: unknown;
      response: Response;
    }>),
  });
}

export async function archiveQuality(key: string): Promise<CaptureFrameQuality> {
  const query = new URLSearchParams({ key });
  return unwrap(call(`/api/capture/archive/quality?${query.toString()}`) as Promise<{
    data?: CaptureFrameQuality;
    error?: unknown;
    response: Response;
  }>);
}

export function useArchiveQualityQuery(key: string | null) {
  return useQuery({
    queryKey: ["capture", "archive", "quality", key],
    enabled: key !== null,
    queryFn: () => archiveQuality(key ?? ""),
  });
}

/** Refetches the frame list once a new frame lands — called from wherever
 * renders `useCaptureState()`'s `last_frame_id`, not from the mutation
 * above, keeping "a capture finished" driven by the one state that survives
 * a reload. */
export function useInvalidateCaptureFrames() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: framesKey });
}

/** Matches `CaptureFrameQualityModel` (`ssc/api/schemas.py`) exactly —
 * the numpy-only HFR/FWHM star metric (`ssc.capture.quality`, `ssc-bkv.8`)
 * for one catalogued frame. `star_count === 0` means every size field is
 * `null` — an empty or starless frame, never a fabricated size (architecture policy
 * invariant 7); the `*_arcsec` fields are independently `null` whenever the
 * frame's FITS header carried no usable pixel scale, even when the `*_px`
 * fields are known — the overlay must render both cases, not assume one
 * implies the other. */
export interface CaptureFrameQuality {
  star_count: number;
  detected_source_count?: number;
  contrast_snr?: number | null;
  hfr_px: number | null;
  fwhm_px: number | null;
  hfr_arcsec: number | null;
  fwhm_arcsec: number | null;
  background: number;
  noise: number;
}

export interface CaptureFramePlateSolve {
  ra_j2000_hours: number;
  dec_j2000_deg: number;
  date_obs: string;
  pixel_scale_arcsec: number;
  rotation_deg: number;
  solver: string;
  solver_version: string | null;
}

export interface LiveStackResetRequest {
  dark_master_id?: number | null;
  bias_master_id?: number | null;
  flat_master_id?: number | null;
  dark_master_path?: string | null;
  bias_master_path?: string | null;
  flat_master_path?: string | null;
  auto_select_masters?: boolean;
  watch_folder?: string | null;
  watch_recursive?: boolean;
  optic_id?: string | null;
  binning?: 1 | 2;
}

export interface LiveStackMaster {
  name: string;
  path: string;
  frame_type: FrameType;
}

export interface LiveStackMastersResponse {
  masters: LiveStackMaster[];
}

export interface LiveStackDirectoryResponse {
  path: string;
  parent: string | null;
  directories: string[];
}

export interface LiveStackResponse {
  sequence: number;
  output_path: string;
  parameters_path: string;
  frame_count: number;
  dark_master: string | null;
  bias_master: string | null;
  flat_master: string | null;
  active: boolean;
  error: string | null;
  auto_select_masters?: boolean;
  master_selection_message?: string | null;
  watch_folder?: string | null;
  watch_recursive?: boolean;
  processing_file?: string | null;
  pending_files?: number;
  binning?: 1 | 2;
  last_shift_y_px?: number | null;
  last_shift_x_px?: number | null;
  registration_peak?: number | null;
  registration_peak_ratio?: number | null;
  object_name?: string | null;
  filter_name?: string | null;
  exposure_s?: number | null;
  gain?: number | null;
  ccd_temp_c?: number | null;
  hfr_px?: number | null;
  fwhm_px?: number | null;
  star_count?: number | null;
  detected_source_count?: number | null;
  frame_contrast_snr?: number | null;
  snr_estimate?: number | null;
  rejected_count?: number;
  last_rejection_reason?: string | null;
  rejected_folder?: string | null;
  total_integration_s?: number | null;
  target_header?: string | null;
}

export interface LiveStackRtspStartRequest {
  host: string;
  port: number;
  path: string;
  width: number;
  height: number;
  fps: number;
  bitrate_kbps: number;
  update_interval_s: 5 | 10 | 30;
  auto_crop: boolean;
  wide_camera_until_stacked: boolean;
}

export interface LiveStackRtspResponse extends Partial<LiveStackRtspStartRequest> {
  active: boolean;
  url: string | null;
  clients: number;
  encoded_version: string | null;
  last_error: string | null;
  encoder: string | null;
  source?: "wide_camera" | "live_stack" | null;
}

export interface YoutubeStreamResponse {
  publishing: boolean;
  active: boolean;
  configured: boolean;
  last_error: string | null;
}

export function useLiveStackMastersQuery() {
  return useQuery({
    queryKey: ["capture", "live-stack", "masters"] as const,
    queryFn: async () =>
      unwrap(
        call("/api/capture/live-stack/masters") as Promise<{
          data?: LiveStackMastersResponse;
          error?: unknown;
          response: Response;
        }>,
      ),
  });
}

export async function browseLiveStackDirectories(path?: string): Promise<LiveStackDirectoryResponse> {
  const query = path === undefined || path.trim() === "" ? "" : `?path=${encodeURIComponent(path)}`;
  return unwrap(
    call(`/api/capture/live-stack/directories${query}`) as Promise<{
      data?: LiveStackDirectoryResponse;
      error?: unknown;
      response: Response;
    }>,
  );
}

export function useLiveStackQuery(enabled: boolean) {
  return useQuery({
    queryKey: ["capture", "live-stack", "status"] as const,
    queryFn: async () => {
      const result = await call("/api/capture/live-stack");
      // This documented absence is an empty state, not a failed reading.
      if (result.response.status === 404 && typeof result.error === "object" && result.error !== null && "detail" in result.error && result.error.detail === "no live stack has been started") return null;
      return unwrap(
        Promise.resolve(result) as Promise<{
          data?: LiveStackResponse;
          error?: unknown;
          response: Response;
        }>,
      );
    },
    enabled,
    retry: false,
    refetchInterval: 5000,
  });
}

export function useLiveStackRtspQuery(enabled = true) {
  return useQuery({
    queryKey: ["capture", "live-stack", "rtsp"] as const,
    queryFn: async () =>
      unwrap(
        call("/api/capture/live-stack/rtsp") as Promise<{
          data?: LiveStackRtspResponse;
          error?: unknown;
          response: Response;
        }>,
      ),
    enabled,
    refetchInterval: 2_000,
  });
}

export function useStartLiveStackRtspMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: LiveStackRtspStartRequest) =>
      unwrap(
        postJson("/api/capture/live-stack/rtsp/start", body) as Promise<{
          data?: LiveStackRtspResponse;
          error?: unknown;
          response: Response;
        }>,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "rtsp"] }),
  });
}

export function useStopLiveStackRtspMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        postJson("/api/capture/live-stack/rtsp/stop", undefined) as Promise<{
          data?: LiveStackRtspResponse;
          error?: unknown;
          response: Response;
        }>,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "rtsp"] }),
  });
}

export function useRestartLiveStackRtspMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        postJson("/api/capture/live-stack/rtsp/restart", undefined) as Promise<{
          data?: LiveStackRtspResponse;
          error?: unknown;
          response: Response;
        }>,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "rtsp"] }),
  });
}

const youtubeStreamKey = ["capture", "live-stack", "youtube"] as const;

export function useYoutubeStreamQuery() {
  return useQuery({
    queryKey: youtubeStreamKey,
    queryFn: async () =>
      unwrap(call("/api/capture/live-stack/youtube") as Promise<{
        data?: YoutubeStreamResponse;
        error?: unknown;
        response: Response;
      }>),
    refetchInterval: 2_000,
  });
}

export function useStartYoutubeStreamMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(postJson("/api/capture/live-stack/youtube/start", undefined) as Promise<{
        data?: YoutubeStreamResponse;
        error?: unknown;
        response: Response;
      }>),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: youtubeStreamKey }),
  });
}

export function useStopYoutubeStreamMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(postJson("/api/capture/live-stack/youtube/stop", undefined) as Promise<{
        data?: YoutubeStreamResponse;
        error?: unknown;
        response: Response;
      }>),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: youtubeStreamKey }),
  });
}

export function useUploadLiveStackMasterMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ frameType, file }: { frameType: Exclude<FrameType, "light">; file: File }) => {
      const form = new FormData();
      form.append("frame_type", frameType);
      form.append("file", file);
      const operation = await unwrap(
        call("/api/capture/live-stack/masters", {
          method: "POST",
          headers: { "Idempotency-Key": pendingCommandKey("live_stack_master_upload") },
          body: form,
        }) as Promise<{
          data?: OperationState;
          error?: unknown;
          response: Response;
        }>,
      );
      return waitForOperation<LiveStackMaster>(operation);
    },
    onSuccess: () => {
      acknowledgeCommand("live_stack_master_upload");
      return queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "masters"] });
    },
  });
}

export function useResetLiveStackMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: LiveStackResetRequest) =>
      unwrap(
        postJson("/api/capture/live-stack/reset", body) as Promise<{
          data?: LiveStackResponse;
          error?: unknown;
          response: Response;
        }>,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "status"] }),
  });
}

export function useStopLiveStackMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        postJson("/api/capture/live-stack/stop", undefined) as Promise<{
          data?: LiveStackResponse;
          error?: unknown;
          response: Response;
        }>,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "status"] }),
  });
}

export function useSaveLiveStackMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const accepted = await unwrap(
        call("/api/capture/live-stack/save", {
          method: "POST",
          headers: { "Idempotency-Key": pendingCommandKey("live_stack_save") },
        }) as Promise<{
          data?: OperationState;
          error?: unknown;
          response: Response;
        }>,
      );
      if (!isOperationState(accepted)) {
        // Legacy mocked/older servers returned the saved stack directly.
        return accepted as unknown as LiveStackResponse; // eslint-disable-line @typescript-eslint/no-unnecessary-type-assertion
      }
      await waitForOperation<Record<string, unknown>>(accepted);
      return unwrap(call("/api/capture/live-stack") as Promise<{
        data?: LiveStackResponse;
        error?: unknown;
        response: Response;
      }>);
    },
    onSuccess: () => {
      acknowledgeCommand("live_stack_save");
      return queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "status"] });
    },
  });
}

export function useAddLiveStackFrameMutation() {
  return useMutation({
    mutationFn: async (frameId: number) =>
      unwrap(
        postJson(`/api/capture/live-stack/frame/${String(frameId)}`, undefined) as Promise<{
          data?: LiveStackResponse;
          error?: unknown;
          response: Response;
        }>,
      ),
  });
}

export function usePollLiveStackMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        postJson("/api/capture/live-stack/poll", undefined) as Promise<{
          data?: LiveStackResponse;
          error?: unknown;
          response: Response;
        }>,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "status"] }),
  });
}

/** Solve the FITS already stored for this frame. The server does not take
 * another exposure and does not move the mount. */
export function useSolveCaptureFrameMutation() {
  return useMutation({
    mutationFn: async (frameId: number) =>
      waitForOperation<CaptureFramePlateSolve>(await unwrap(
        call(`/api/capture/frames/${String(frameId)}/solve`, {
          method: "POST",
          headers: { "Idempotency-Key": pendingCommandKey(`capture_frame_solve.${String(frameId)}`) },
        }) as Promise<{
          data?: OperationState;
          error?: unknown;
          response: Response;
        }>,
      )),
    onSuccess: (_result, frameId) => { acknowledgeCommand(`capture_frame_solve.${String(frameId)}`); },
  });
}

/** `GET /api/capture/frames/{id}/quality` — the bottom-right quality overlay
 * on the big preview reads this. `frameId` of `null`/`undefined` disables
 * the query (`enabled: false`) rather than firing it at `.../null/quality`,
 * the same guard the image-fetching call sites in `CapturePreviewPanel.tsx`
 * already use for an absent frame id. */
export function useCaptureFrameQualityQuery(frameId: number | null | undefined) {
  return useQuery({
    queryKey: ["capture", "frame-quality", frameId ?? null] as const,
    queryFn: async ({ signal }) =>
      unwrap(
        call(`/api/capture/frames/${String(frameId)}/quality`, { signal }) as Promise<{
          data?: CaptureFrameQuality;
          error?: unknown;
          response: Response;
        }>,
      ),
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
    enabled: frameId !== null && frameId !== undefined,
  });
}
