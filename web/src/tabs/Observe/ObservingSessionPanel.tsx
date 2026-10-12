// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useTelemetryAvailability } from "./useTelemetryAvailability";
import { ImagePreview } from "../../components/ImagePreview";
import { Pause, Play, Square } from "lucide-react";
import { Link } from "react-router-dom";
import { useCaptureFrameQualityQuery, useCaptureState, useLiveStackQuery } from "../../api/capture";
import { usePauseRun, useResumeRun, useStopRun } from "../../api/runs";
import { useMountState } from "../../api/mount";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { useConnectionStore } from "../../store/connectionStore";

function value(value: number | null | undefined, digits = 2): string {
  return value == null ? "—" : value.toFixed(digits);
}

export function ObservingSessionPanel() {
  const capture = useCaptureState();
  const liveStack = useLiveStackQuery(true);
  const mount = useMountState();
  const run = useConnectionStore((state) => state.snapshot?.run ?? null);
  const quality = useCaptureFrameQualityQuery(capture?.last_frame_id);
  const pause = usePauseRun();
  const resume = useResumeRun();
  const stop = useStopRun();
  const actions = new Set(run?.available_actions ?? []);
  const currentIndex = run?.items?.findIndex((item) => item.item_id === run.current_item_id) ?? -1;
  const next = currentIndex < 0 ? undefined : run?.items?.slice(currentIndex + 1).find((item) => item.enabled && item.status !== "done");
  const { unavailable, mountUnavailable, now } = useTelemetryAvailability(mount);
  const stackUnavailable = unavailable ?? (liveStack.isError || liveStack.data == null ? "Unknown"
    : now - liveStack.dataUpdatedAt > 15000 ? "Stale" : null);
  const headline = unavailable ?? (capture == null ? "Unknown" : capture.in_progress
    ? capture.stage ?? "Capturing" : stackUnavailable ?? (liveStack.data?.active === true ? "Stacking" : "Idle"));
  const mountLabel = mountUnavailable ?? (mount?.slewing === true ? "Slewing" : mount?.tracking === true ? "Tracking"
    : mount?.at_park === true ? "Parked" : mount?.slewing === false && mount.tracking === false && mount.at_park === false ? "Idle" : "Unknown");
  const busy = unavailable !== null || pause.isPending || resume.isPending || stop.isPending;

  return (
    <Card layoutId="observing-now" className="mobile-preview-card space-y-3">
      <div className="flex items-center justify-between gap-2">
        <CardTitle>Observing now</CardTitle>
        <span className={`text-xs font-medium ${capture?.in_progress === true || liveStack.data?.active === true ? "text-accent" : "text-fg-muted"}`}>
          {headline}
        </span>
      </div>

      {mount?.polled_at != null && mountUnavailable !== null && <p className="text-xs text-warning">Last mount reading: {new Date(mount.polled_at).toLocaleTimeString()}</p>}
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-fg-muted">Target</dt><dd className="truncate text-right text-fg">{capture?.object_name ?? liveStack.data?.object_name ?? "—"}</dd>
        <dt className="text-fg-muted">Plan</dt><dd className="truncate text-right text-fg">{run?.plan_name ?? (unavailable ?? "Manual")}</dd>
        <dt className="text-fg-muted">Mount</dt><dd className="text-right text-fg">{mountLabel}</dd>
        <dt className="text-fg-muted">Live stack</dt><dd className="text-right text-fg">{(liveStack.data?.active === true ? `${String(liveStack.data.frame_count)} accepted · ${liveStack.data.rejected_count == null ? "Unknown" : String(liveStack.data.rejected_count)} rejected` : liveStack.data?.active === false ? "Off" : "Unknown")}{stackUnavailable !== null && liveStack.data != null && <span className="ml-2 text-warning">{stackUnavailable} · Last reading</span>}</dd>
        <dt className="text-fg-muted">Last frame</dt><dd className="text-right text-fg">HFR {value(quality.data?.hfr_px)} · FWHM {value(quality.data?.fwhm_px)}</dd>
        <dt className="text-fg-muted">Next</dt><dd className="truncate text-right text-fg">{next?.kind.replaceAll("_", " ") ?? (run?.status === "running" ? "Finish plan" : "—")}</dd>
      </dl>

      {capture?.last_frame_id != null && (
        <>
          <ImagePreview src={`/api/capture/frames/${String(capture.last_frame_id)}/image`} alt="Most recent captured frame" label="Observing image viewer" />
          <Link to="/live" className="block text-xs text-accent underline">Open Capture</Link>
        </>
      )}

      {run !== null && (actions.has("pause") || actions.has("resume") || actions.has("stop")) && (
        <div className="flex flex-wrap gap-2 border-t border-border pt-3">
          {actions.has("pause") && <Button disabled={busy} onClick={() => { pause.mutate(run.run_id); }}><Pause className="size-4" />Pause</Button>}
          {actions.has("resume") && <Button variant="primary" disabled={busy} onClick={() => { resume.mutate(run.run_id); }}><Play className="size-4" />Resume</Button>}
          {actions.has("stop") && <Button variant="danger" disabled={busy} onClick={() => { if (window.confirm(`Stop ${run.plan_name}? The active item will be aborted and the mount will be parked.`)) stop.mutate(run.run_id); }}><Square className="size-4" />Stop</Button>}
        </div>
      )}
    </Card>
  );
}
