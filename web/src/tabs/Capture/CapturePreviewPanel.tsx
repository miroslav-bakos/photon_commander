// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useConnectionStore } from "../../store/connectionStore";
import { timedApiFetch } from "../../performance/diagnostics";
import { useRef, useState } from "react";
import { ImagePreview } from "../../components/ImagePreview";
import { Card, CardTitle } from "../../components/Card";
import { PlateSolveIcon } from "../../components/ActionIcons";
import {
  useCaptureFrameQualityQuery,
  useCaptureState,
  useSolveCaptureFrameMutation,
} from "../../api/capture";
import { useLatestOperation, type OperationState } from "../../api/operations";
import type { FrameType } from "../../api/capture";

function frameImageUrl(frameId: number, thumbnail: boolean): string {
  return `/api/capture/frames/${String(frameId)}/image${thumbnail ? "?thumbnail=true" : ""}`;
}

function masterImageUrl(kind: Exclude<FrameType, "light">, path: string): string {
  const name = path.split(/[\\/]/).at(-1) ?? path;
  const query = new URLSearchParams({ frame_type: kind, name });
  return `/api/capture/calibration/masters/image?${query.toString()}`;
}

interface ImageFailure {
  kind: "not-ready" | "not-found" | "error";
  message: string;
}

/**
 * `<img onError>` fires with no status code at all, so a plain "image
 * failed" would read the same for a 404 (`CaptureFrameNotFoundError`) and a
 * 409 (`CaptureFrameNotReadyError` — still stacking/debayering,
 * `ssc/api/routes/capture.py:135-155`) even though the second is not an
 * error, just "not yet". A follow-up `fetch` of the same URL is the only way
 * to recover that distinction; it only runs once the `<img>` has already
 * failed, so the happy path stays a plain image request.
 */
async function describeImageFailure(url: string): Promise<ImageFailure> {
  try {
    const response = await timedApiFetch(new Request(new URL(url, window.location.origin), { credentials: "same-origin" }));
    if (response.status === 404) {
      return { kind: "not-found", message: "This frame no longer exists." };
    }
    if (response.status === 409) {
      let detail: string | null = null;
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        detail = null;
      }
      return { kind: "not-ready", message: detail ?? "Not ready yet — still stacking or debayering." };
    }
    return { kind: "error", message: `Could not load this frame's image (HTTP ${String(response.status)}).` };
  } catch {
    return { kind: "error", message: "Could not load this frame's image." };
  }
}

/**
 * Bottom-right quality badge (`ssc-bkv.8`) — the numpy-only HFR/FWHM star
 * metric for the shown frame. Renders through loading/error/data states
 * explicitly rather than only popping in on success, so the badge's absence
 * is never mistaken for "no stars": loading reads "…", a failed fetch reads
 * "unavailable", and only a real response ever shows a number (architecture policy
 * invariant 7 — never fabricate a placeholder size). `star_count === 0` is
 * itself a real, displayable answer ("0★"), distinct from both of those.
 */
function FrameQualityBadge({ frameId }: { frameId: number }) {
  const quality = useCaptureFrameQualityQuery(frameId);

  let label: string;
  if (quality.isPending) {
    label = "Quality: …";
  } else if (quality.isError) {
    label = "Quality: unavailable";
  } else {
    const q = quality.data;
    if (q.star_count === 0 || q.hfr_px === null) {
      label = `${String(q.star_count)}★`;
    } else {
      const hfr =
        q.hfr_arcsec !== null
          ? `HFR ${q.hfr_px.toFixed(2)}px (${q.hfr_arcsec.toFixed(2)}″)`
          : `HFR ${q.hfr_px.toFixed(2)}px`;
      label = `${String(q.star_count)}★  ${hfr}`;
    }
  }

  return (
    <div className="pointer-events-none absolute bottom-2 right-2 rounded-md bg-surface-raised/80 px-2 py-1 text-xs text-fg backdrop-blur-sm">
      {label}
    </div>
  );
}

type PlateSolveMutation = ReturnType<typeof useSolveCaptureFrameMutation>;

function FramePlateSolveButton({ frameId, solve, operation }: { frameId: number; solve: PlateSolveMutation; operation: OperationState | undefined }) {
  const active = operation?.status === "queued" || operation?.status === "running";
  return (
    <button
      type="button"
      aria-label={solve.isPending || active ? "Plate-solving this frame" : "Plate-solve this frame"}
      title={solve.isPending || active ? "Plate-solving…" : "Plate-solve this frame"}
      className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md border border-border px-2 text-fg disabled:opacity-50"
      disabled={solve.isPending || active}
      onClick={() => {
        solve.mutate(frameId);
      }}
    >
      <PlateSolveIcon />
    </button>
  );
}

function FramePlateSolve({ solve, operation }: { solve: PlateSolveMutation; operation: OperationState | undefined }) {
  const recovered = operation?.status === "succeeded"
    ? operation.result as unknown as NonNullable<PlateSolveMutation["data"]>
    : undefined;
  const result = solve.data ?? recovered;
  return (
    <div className="mt-3 rounded-md border border-border bg-surface-raised p-3 text-sm">
      {solve.isError && <p role="alert" className="mt-2 text-danger">{solve.error.message}</p>}
      {operation?.status === "failed" && <p role="alert" className="mt-2 text-danger">{operation.error}</p>}
      {result !== undefined && (
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-fg-muted">
          <dt>J2000 RA</dt><dd className="text-fg">{result.ra_j2000_hours.toFixed(5)} h</dd>
          <dt>J2000 Dec</dt><dd className="text-fg">{result.dec_j2000_deg.toFixed(4)}°</dd>
          <dt>Pixel scale</dt><dd className="text-fg">{result.pixel_scale_arcsec.toFixed(3)}″/px</dd>
          <dt>Rotation</dt><dd className="text-fg">{result.rotation_deg.toFixed(2)}°</dd>
          <dt>Solver</dt>
          <dd className="text-fg">{result.solver}{result.solver_version === null ? "" : ` ${result.solver_version}`}</dd>
        </dl>
      )}
    </div>
  );
}

/**
 * The big preview for one frame. Keyed by `frameId` from the caller so a
 * failure recorded for the previous frame never lingers onto the next one —
 * a remount is a plain, effect-free way to reset `failure` rather than an
 * effect watching `frameId`.
 */
function FramePreviewImage({ frameId, solve, operation }: { frameId: number; solve: PlateSolveMutation; operation: OperationState | undefined }) {
  const [failure, setFailure] = useState<ImageFailure | null>(null);
  const failureRequest = useRef(0);
  const url = frameImageUrl(frameId, false);


  return (
    <div className="preview-body flex min-h-0 flex-1 flex-col">
      {failure !== null && <p role={failure.kind === "not-ready" ? "status" : "alert"} className="p-4 text-center text-sm text-fg-muted">{failure.message}</p>}
      <div hidden={failure !== null} className={failure !== null ? "hidden" : "contents"}>
      <ImagePreview src={url} alt={`Frame #${String(frameId)}`} label="Capture image viewer" onLoad={() => { failureRequest.current += 1; setFailure(null); }} onError={() => {
        const generation = useConnectionStore.getState().generation;
        const request = ++failureRequest.current;
        void describeImageFailure(url).then((result) => {
          if (request === failureRequest.current && generation === useConnectionStore.getState().generation) setFailure(result);
        });
      }}>
        <FrameQualityBadge frameId={frameId} />
      </ImagePreview>
      </div>
      <FramePlateSolve solve={solve} operation={operation} />
    </div>
  );
}

/**
 * `ssc-bkv.5`: the big preview + collapsible timeline that sits above
 * `CapturePanel` (unchanged — its own "Recent frames" text list is a
 * separate concern). Selection is local component state, `null` meaning
 * "follow the latest frame": while `null` the big preview tracks
 * `useCaptureState().last_frame_id` live, so a capture finishing updates it
 * without a click; clicking a thumbnail locks the preview to that frame
 * until another thumbnail (or the current-latest one) is clicked.
 */
export function CapturePreviewPanel() {
  const captureState = useCaptureState();
  const solve = useSolveCaptureFrameMutation();
  const calibration = captureState?.calibration;
  const calibrationOwnsPreview = calibration !== undefined && calibration.status !== "idle";
  const latestMasterPath = calibration?.status === "completed"
    ? calibration.master_paths.at(-1) ?? null
    : null;
  const latestFrameId = calibrationOwnsPreview ? null : captureState?.last_frame_id ?? null;
  // Only a frame produced in this running app session belongs here. Durable
  // captures live in Archive; falling back to the catalogue made yesterday's
  // image look like a fresh preview after every restart.
  const shownFrameId = latestFrameId;
  const solveOperation = useLatestOperation(`capture_frame_solve:${String(shownFrameId ?? "none")}`);

  return (
    <div className="space-y-3">
      <Card layoutId="capture.preview" className="mobile-preview-card">
        <div className="mb-3 flex items-center justify-between">
          <CardTitle>Preview</CardTitle>
          {shownFrameId !== null && <FramePlateSolveButton frameId={shownFrameId} solve={solve} operation={solveOperation} />}
        </div>
        {latestMasterPath !== null && calibration?.kind != null ? (
          <div className="preview-body flex min-h-0 flex-1 flex-col">
            <ImagePreview
              label="Calibration image viewer"
              src={masterImageUrl(calibration.kind, latestMasterPath)}
              alt={`Generated ${calibration.kind} master`}
            />
            <p className="mt-2 text-xs text-fg-muted">Final generated master · auto-stretched preview</p>
          </div>
        ) : calibrationOwnsPreview ? (
          <p className="text-sm text-fg-muted">
            {calibration.status === "running" || calibration.status === "cancelling"
              ? "Calibration frames are being captured without per-frame previews. The generated master will appear here when complete."
              : "No calibration master was generated. Raw calibration frames remain available in Archive."}
          </p>
        ) : shownFrameId === null ? (
          <p className="text-sm text-fg-muted">No image captured in this app session.</p>
        ) : (
          <FramePreviewImage key={shownFrameId} frameId={shownFrameId} solve={solve} operation={solveOperation} />
        )}
      </Card>
    </div>
  );
}
