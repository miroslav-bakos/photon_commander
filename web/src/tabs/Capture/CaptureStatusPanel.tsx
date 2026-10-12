// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Card, CardTitle } from "../../components/Card";
import { useCaptureState, type CaptureStage, type CaptureState, type FrameType } from "../../api/capture";
import { useNow } from "../MountTab/useNow";
import { useConnectionStore } from "../../store/connectionStore";
import { schedulerOperationLabel } from "../../app/footerActivity";

/** Duplicated rather than imported from `CapturePanel.tsx`'s private
 * `frameTypeLabel` — the same small-constant duplication `AlpacaDevicesPanel`
 * and `ConnectionsFooter` already use for `KIND_LETTER`, since these are
 * four-entry maps local to what each file renders, not worth a shared module. */
const FRAME_TYPE_LABEL: Record<FrameType, string> = {
  light: "Light",
  dark: "Dark",
  flat: "Flat",
  bias: "Bias",
};

/** Mirrors `ssc.state.models.CaptureStage` — see `api/capture.ts`'s own
 * `CaptureStage` type doc for why there is no `platesolving` entry. */
const STAGE_LABEL: Record<CaptureStage, string> = {
  dithering: "Dithering mount",
  exposing: "Exposing",
  downloading: "Downloading",
  transferring: "Transferring picture",
  saving: "Saving",
};

/**
 * Top-most section of the Capture screen's right (`detail`) panel (owner,
 * 2026-09-11: "right panel top most section with current capture details +
 * progress"), above `AlpacaDevicesPanel`. Reads the same `useCaptureState()`
 * the rest of the Capture screen already uses — no new backend call, just
 * the `stage`/`duration_s`/`started_at`/`object_name`/`frame_type`/`gain`
 * fields the 2026-09-11 progress brief added to `CaptureState`.
 *
 * Three honest states, the same convention every other panel on this screen
 * follows: `undefined` (no snapshot yet), `null` (a snapshot exists but the
 * server has nothing to say about capture), and a `CaptureState` that may or
 * may not have an exposure `in_progress`. `stage`/`duration_s`/`started_at`/
 * `object_name`/`frame_type`/`gain` are only ever read while `in_progress` is
 * true — the backend clears all six together in `CaptureService._finish`, so
 * a finished exposure's stale numbers are never rendered as if still live
 * (`ssc/state/models.py`'s `CaptureState` docstring, cross-checked here).
 */
export function CaptureStatusPanel() {
  const capture = useCaptureState();
  const run = useConnectionStore((state) => state.snapshot?.run ?? null);
  const scheduled = run?.status === "running" ? run : null;

  return (
    <Card>
      <CardTitle>Capture status</CardTitle>
      {capture === undefined && <p className="text-sm text-fg-muted">Waiting for state…</p>}
      {capture === null && <p className="text-sm text-fg-muted">No capture state yet.</p>}
      {capture !== undefined && capture !== null && !capture.in_progress && scheduled === null && (
        <p className="text-sm text-fg-muted">Idle — no exposure in progress.</p>
      )}
      {capture !== undefined && capture !== null && !capture.in_progress && scheduled !== null && (
        <div className="space-y-1 text-sm" role="status" aria-label="Scheduled capture activity">
          <p className="text-fg">{schedulerOperationLabel(scheduled)}</p>
          <p className="text-xs text-fg-muted">{scheduled.plan_name}</p>
          {scheduled.last_decision !== null && (
            <p className="text-xs text-fg-muted">{scheduled.last_decision}</p>
          )}
        </div>
      )}
      {capture?.in_progress &&
        capture.calibration?.status === "running" && capture.calibration.phase === "integrating" ? (
          <MasterIntegration calibration={capture.calibration} />
        ) : capture?.in_progress ? (
          <RunningExposure capture={capture} />
        ) : null}
    </Card>
  );
}

function MasterIntegration({ calibration }: { calibration: NonNullable<CaptureState["calibration"]> }) {
  const kind = calibration.kind == null ? "Calibration" : FRAME_TYPE_LABEL[calibration.kind];

  return (
    <div className="space-y-1 text-sm" role="status" aria-label="Calibration master activity">
      <p className="text-fg">{calibration.message ?? `Building ${kind.toLowerCase()} master`}</p>
      <p className="text-xs text-fg-muted tabular-nums">
        {kind} frames: {calibration.completed_frames}/{calibration.total_frames} saved
      </p>
      <p className="text-xs text-fg-muted">Integration can take several minutes for a large set.</p>
    </div>
  );
}

function RunningExposure({ capture }: { capture: CaptureState }) {
  // Ticks its own clock rather than waiting on a WS push — nothing pushes a
  // new frame just because a second passed (`useNow`'s own doc), and without
  // this the progress bar would freeze at whatever elapsed time happened to
  // be true when the last snapshot arrived.
  const now = useNow(200);

  const stageLabel = capture.stage != null ? STAGE_LABEL[capture.stage] : null;
  const frameTypeLabel = capture.frame_type != null ? FRAME_TYPE_LABEL[capture.frame_type] : null;

  let elapsedS: number | null = null;
  if (capture.started_at != null && capture.duration_s != null && capture.duration_s > 0) {
    const startedMs = Date.parse(capture.started_at);
    if (!Number.isNaN(startedMs)) {
      elapsedS = Math.max(0, Math.min(capture.duration_s, (now - startedMs) / 1000));
    }
  }
  const totalS = capture.duration_s ?? null;
  const pct = elapsedS !== null && totalS !== null && totalS > 0 ? Math.min(100, (elapsedS / totalS) * 100) : null;

  return (
    <div className="space-y-2 text-sm">
      <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-1">
        <dt className="text-fg-muted">Object</dt>
        <dd className="truncate text-fg">{capture.object_name ?? "—"}</dd>
        <dt className="text-fg-muted">Frame</dt>
        <dd className="text-fg">{frameTypeLabel ?? "—"}</dd>
        <dt className="text-fg-muted">Gain</dt>
        <dd className="text-fg">{capture.gain ?? "—"}</dd>
        <dt className="text-fg-muted">Stage</dt>
        <dd className="text-fg">{stageLabel ?? "—"}</dd>
      </dl>
      {pct !== null && elapsedS !== null && totalS !== null && (
        <div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-border-strong" role="presentation">
            <div className="h-full bg-dot-ok transition-[width]" style={{ width: `${String(pct)}%` }} />
          </div>
          <p className="mt-1 text-xs text-fg-muted tabular-nums">
            {elapsedS.toFixed(0)}s / {totalS.toFixed(0)}s
          </p>
        </div>
      )}
    </div>
  );
}
