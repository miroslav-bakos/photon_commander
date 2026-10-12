// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { CaptureState, LiveStackResponse } from "../api/capture";
import type { AutofocusState } from "../api/focuser";
import type { FocuserPositionResponse } from "../api/focuser";
import type { FilterWheelState } from "../api/filterwheel";
import type { MountState, TargetCenteringProgress } from "../api/mount";
import type { PolarAlignmentState } from "../api/polarAlignment";
import type { StateSnapshot } from "../api/types";

export interface FooterActivity {
  id: string;
  label: string;
  detail: string | null;
  current: number | null;
  total: number | null;
}

interface FooterActivityInput {
  snapshot: StateSnapshot | null;
  capture: CaptureState | null | undefined;
  polarAlignment: PolarAlignmentState | null | undefined;
  autofocus: AutofocusState | undefined;
  centering: TargetCenteringProgress | undefined;
  liveStack: LiveStackResponse | undefined;
  mount: MountState | null | undefined;
  filterWheel: FilterWheelState | undefined;
  focuser: FocuserPositionResponse | undefined;
  now: number;
}

const finishedRunItemStatuses = new Set(["done", "skipped", "failed", "aborted"]);

const SCHEDULE_EFFECT_LABELS: Readonly<Record<string, string>> = {
  autofocus: "Autofocusing",
  capture_calibration: "Capturing calibration frames",
  center_target: "Plate solving and centering",
  dither: "Dithering and settling",
  expose_frame: "Exposing",
  find_home: "Finding home",
  park: "Parking",
  reset_live_stack: "Preparing live stack",
  set_filter: "Changing filter",
  set_gain: "Setting gain",
  set_tracking: "Setting tracking",
  set_tracking_rate: "Setting tracking rate",
  slew_to: "Slewing",
  stop_live_stack: "Finalizing live stack",
  sync_to: "Synchronizing mount",
  unpark: "Unparking",
};

/** Convert the durable executor's current journal reason into operator prose.
 * Unknown decisions remain visible verbatim instead of falling back to a
 * generic `goto 1/5`, so newly added phases are still diagnosable. */
export function schedulerOperationLabel(run: NonNullable<StateSnapshot["run"]>): string {
  if (run.gate_held) return "Held";
  const decision = run.last_decision?.trim() ?? "";
  const effect = /(?:issuing\s+)?([a-z_]+)(?:\s+(?:completed|failed))?(?:\s+\(attempt\s+\d+\))?/i.exec(decision)?.[1];
  if (effect !== undefined) {
    const label = SCHEDULE_EFFECT_LABELS[effect.toLowerCase()];
    if (label !== undefined) return label;
  }
  const current = run.items?.find((item) => item.item_id === run.current_item_id);
  if (decision !== "") return titleCase(decision);
  return current === undefined ? "Running plan" : titleCase(current.kind);
}

function counted(
  id: string,
  label: string,
  current: number,
  total: number,
  detail: string | null = null,
): FooterActivity {
  return {
    id,
    label,
    detail,
    current: Math.max(0, current),
    total: total > 0 ? total : null,
  };
}

function indefinite(id: string, label: string, detail: string | null = null): FooterActivity {
  return { id, label, detail, current: null, total: null };
}

function titleCase(value: string): string {
  return value.length === 0 ? value : `${value.charAt(0).toUpperCase()}${value.slice(1).replaceAll("_", " ")}`;
}

function captureActivity(capture: CaptureState | null | undefined, now: number): FooterActivity | null {
  const calibration = capture?.calibration;
  if (calibration?.status === "running" || calibration?.status === "cancelling") {
    const kind = calibration.kind ?? "calibration";
    if (calibration.phase === "integrating") {
      return counted(
        "calibration-master",
        calibration.message ?? `Building ${kind} master`,
        calibration.completed_frames,
        calibration.total_frames,
      );
    }
    if (calibration.phase === "capturing") {
      return counted(
        "calibration-capture",
        `Capturing ${kind} frames`,
        calibration.completed_frames,
        calibration.total_frames,
        calibration.current_filter === null ? null : `Filter ${calibration.current_filter}`,
      );
    }
    const phase = calibration.phase === "probing" ? `Finding ${kind} exposure` : `Preparing ${kind} sequence`;
    return indefinite("calibration-prepare", calibration.message ?? phase);
  }

  if (capture?.in_progress !== true) return null;
  const stage = capture.stage ?? null;
  if (stage === "exposing" && capture.started_at != null && capture.duration_s != null && capture.duration_s > 0) {
    const startedMs = Date.parse(capture.started_at);
    if (!Number.isNaN(startedMs)) {
      const elapsed = Math.min(capture.duration_s, Math.max(0, (now - startedMs) / 1000));
      return {
        id: "exposure",
        label: `Exposing ${capture.frame_type ?? "frame"}`,
        detail: capture.object_name ?? null,
        current: elapsed,
        total: capture.duration_s,
      };
    }
  }
  const labels: Record<string, string> = {
    dithering: "Dithering and settling",
    exposing: `Exposing ${capture.frame_type ?? "frame"}`,
    downloading: "Downloading exposure",
    transferring: "Transferring exposure",
    saving: "Saving exposure",
  };
  return indefinite("capture", labels[stage ?? ""] ?? "Capturing frame", capture.object_name ?? null);
}

function polarActivity(state: PolarAlignmentState | null | undefined): FooterActivity | null {
  const refinement = state?.refinement;
  if (refinement?.status === "running") {
    const label = refinement.stage === "capturing"
      ? "Capturing polar refinement"
      : refinement.stage === "solving"
        ? "Solving polar refinement"
        : "Refining polar alignment";
    return indefinite("polar-refinement", label, `Iteration ${String(refinement.iteration + 1)}`);
  }
  if (state?.status !== "running") return null;
  if (state.preflight_reason != null) {
    return indefinite("polar-alignment", "Polar alignment: Preflight", state.preflight_reason);
  }
  const points = state.points ?? [];
  const solved = points.filter((point) => point.status === "solved").length;
  const current = points.find((point) => point.index === state.current_point_index);
  const stage = current?.status === undefined ? "Working" : titleCase(current.status);
  return counted("polar-alignment", `Polar alignment: ${stage}`, solved, points.length);
}

function autofocusActivity(state: AutofocusState | undefined): FooterActivity | null {
  if (state?.status !== "running") return null;
  const stage = state.stage === null || state.stage === undefined ? "Starting" : titleCase(state.stage);
  return indefinite("autofocus", `Autofocus: ${stage}`, `${String(state.samples.length)} samples`);
}

function centeringActivity(state: TargetCenteringProgress | undefined): FooterActivity | null {
  if (state?.status !== "running") return null;
  return counted(
    "centering",
    state.message.trim() === "" ? `Centering: ${titleCase(state.stage)}` : state.message,
    Math.max(0, state.attempt),
    state.max_attempts,
  );
}

function scheduleActivity(snapshot: StateSnapshot | null): FooterActivity | null {
  const run = snapshot?.run;
  if (run?.status !== "running") return null;
  const items = (run.items ?? []).filter((item) => item.enabled);
  const completed = items.filter((item) => finishedRunItemStatuses.has(item.status)).length;
  const current = items.find((item) => item.item_id === run.current_item_id);
  const detail = run.gate_held ? run.gate_reason : current?.reason;
  const operation = schedulerOperationLabel(run);
  const reason = detail == null || detail.trim() === "" ? null : detail;
  return indefinite(
    "schedule",
    run.gate_held ? `Schedule held: ${run.plan_name}` : operation,
    [`Plan ${run.plan_name}`, `${String(completed)}/${String(items.length)} items`, reason]
      .filter((part): part is string => part !== null)
      .join(" · "),
  );
}

function liveStackActivity(state: LiveStackResponse | undefined): FooterActivity | null {
  if (state?.active !== true) return null;
  const pending = state.pending_files ?? 0;
  if (state.processing_file != null) {
    const filename = state.processing_file.split("/").at(-1) ?? state.processing_file;
    return indefinite("live-stack", "Calibrating, registering and stacking", `${filename} · queue ${String(pending)}`);
  }
  if (pending > 0) return indefinite("live-stack-queue", "Waiting for stack input", `${String(pending)} files`);
  return null;
}

function hardwareActivities(input: FooterActivityInput): FooterActivity[] {
  const activities: FooterActivity[] = [];
  if (input.mount?.jog_primary_active === true || input.mount?.jog_secondary_active === true) {
    const axes = [
      input.mount.jog_primary_active === true ? "primary" : null,
      input.mount.jog_secondary_active === true ? "secondary" : null,
    ].filter((axis): axis is string => axis !== null);
    activities.push(indefinite("mount-jog", "Jogging scope", `${axes.join(" + ")} axis · dead-man protected`));
  }
  if (input.mount?.slewing === true) activities.push(indefinite("mount-slew", "Slewing scope"));
  if (input.filterWheel?.position === -1) activities.push(indefinite("filter-wheel", "Moving filter wheel"));
  if (input.focuser?.is_moving === true) {
    activities.push(indefinite(
      "focuser-move",
      "Moving focuser",
      input.focuser.position == null ? null : `Position ${String(input.focuser.position)}`,
    ));
  }
  return activities;
}

function backgroundActivities(snapshot: StateSnapshot | null): FooterActivity[] {
  const activities: FooterActivity[] = [];
  for (const operation of snapshot?.operations ?? []) {
    if (operation.status !== "queued" && operation.status !== "running") continue;
    activities.push({
      id: `operation-${operation.id}`,
      label: operation.message.trim() === "" ? titleCase(operation.kind) : operation.message,
      detail: null,
      current: operation.progress_current,
      total: operation.progress_total,
    });
  }
  const discovery = snapshot?.discovery;
  if (discovery?.running === true) {
    activities.push(counted("discovery", "Discovering devices", discovery.completed, discovery.total));
  }
  for (const pack of snapshot?.skymap?.jobs ?? []) {
    if (pack.status === "queued" || pack.status === "running") {
      activities.push(counted("skymap-pack", `Downloading ${pack.survey} sky map`, pack.done, pack.total));
    }
  }
  for (const bulk of snapshot?.skymap?.bulk_cutout_jobs ?? []) {
    if (bulk.status === "queued" || bulk.status === "running") {
      activities.push(counted(
        "skymap-bulk",
        "Downloading target imagery",
        bulk.done + bulk.skipped + bulk.failed,
        bulk.total,
      ));
    }
  }
  for (const cutout of snapshot?.skymap?.cutouts ?? []) {
    if (cutout.status === "queued" || cutout.status === "running") {
      activities.push(indefinite("skymap-cutout", `Downloading ${cutout.source} target imagery`));
    }
  }
  return activities;
}

/**
 * Select every active, server-observed long-running workflow. Array order is
 * also the display priority: concrete camera work wins over orchestration,
 * which wins over background downloads. The footer shows the first item and
 * reports the remainder as concurrent work instead of pretending it vanished.
 */
export function selectFooterActivities(input: FooterActivityInput): FooterActivity[] {
  return [
    captureActivity(input.capture, input.now),
    polarActivity(input.polarAlignment),
    autofocusActivity(input.autofocus),
    centeringActivity(input.centering),
    ...hardwareActivities(input),
    scheduleActivity(input.snapshot),
    liveStackActivity(input.liveStack),
    ...backgroundActivities(input.snapshot),
  ].filter((activity): activity is FooterActivity => activity !== null);
}

export function activityProgress(activity: FooterActivity): number | null {
  if (activity.current === null || activity.total === null || activity.total <= 0) return null;
  return Math.min(100, Math.max(0, (activity.current / activity.total) * 100));
}

export function formatActivityCount(activity: FooterActivity): string | null {
  if (activity.current === null || activity.total === null) return activity.detail;
  const decimals = activity.id === "exposure" ? 1 : 0;
  const current = activity.current.toFixed(decimals);
  const total = activity.total.toFixed(decimals);
  return `${current}/${total}${activity.id === "exposure" ? "s" : ""}`;
}
