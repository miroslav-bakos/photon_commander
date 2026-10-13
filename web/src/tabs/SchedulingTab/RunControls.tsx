// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { translate } from "../../i18n/locale";
import {
  usePauseRun,
  useResumeInterruptedRun,
  useResumeRun,
  useStartRun,
  useStopRun,
  type RunState,
} from "../../api/runs";
import type { ComponentProps } from "react";
import { StartPlanIcon, StopIcon } from "../../components/ActionIcons";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { useConnectionStore } from "../../store/connectionStore";
import { isAlpacaDeviceConnected } from "../../api/deviceAvailability";
import { useArchiveEntriesQuery, useCaptureState } from "../../api/capture";
import { CheckCircle2, CircleAlert, CircleX } from "lucide-react";
import { ActionableError } from "../../components/ActionableError";
import { useTonightQuery } from "../../widgets/tonight/api";

const ACTIVE = new Set<RunState["status"]>(["running", "paused"]);
type BadgeTone = NonNullable<ComponentProps<typeof Badge>["tone"]>;

const STATUS_TONE: Record<RunState["status"], BadgeTone> = {
  running: "accent",
  paused: "warning",
  stopped: "neutral",
  completed: "success",
  completed_with_warnings: "warning",
  interrupted: "warning",
  failed: "danger",
};

function message(error: unknown): string | null {
  return error instanceof Error ? error.message : null;
}

function clock(iso: string, timeZone: string | undefined): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone,
  });
}

function itemTiming(run: RunState, timeZone: string | undefined): string | null {
  const item = run.items?.find((candidate) => candidate.item_id === run.current_item_id);
  if (item === undefined) return null;
  const span =
    item.start_utc === null
      ? null
      : item.end_utc === null
        ? clock(item.start_utc, timeZone)
        : `${clock(item.start_utc, timeZone)}–${clock(item.end_utc, timeZone)}`;
  return `${item.status}${span === null ? "" : ` · ${span}`}`;
}

interface RunControlsProps {
  planId: string | null;
  nightDate: string | null;
  timeZone: string | undefined;
  startBlockedReason: string | null;
  itemLabel: (itemId: string) => string;
  onExecutionStart?: (planId: string) => void;
}

export function RunControls({ planId, nightDate, timeZone, startBlockedReason, itemLabel, onExecutionStart }: RunControlsProps) {
  const snapshot = useConnectionStore((state) => state.snapshot);
  const capture = useCaptureState();
  const archive = useArchiveEntriesQuery();
  const tonight = useTonightQuery(snapshot?.location != null);
  const run = snapshot?.run ?? null;
  const start = useStartRun();
  const pause = usePauseRun();
  const resume = useResumeRun();
  const stop = useStopRun();
  const resumeInterrupted = useResumeInterruptedRun();
  const busy = start.isPending || pause.isPending || resume.isPending || stop.isPending || resumeInterrupted.isPending;
  const actions = new Set(run?.available_actions ?? []);
  const active = run !== null && ACTIVE.has(run.status);
  const restarting = run !== null && !active && run.plan_id === planId;
  const timing = run === null ? null : itemTiming(run, timeZone);
  const error =
    message(start.error) ??
    message(pause.error) ??
    message(resume.error) ??
    message(stop.error) ??
    message(resumeInterrupted.error);
  const storageLow = archive.data !== undefined && archive.data.storage_free_bytes < 1024 ** 3;
  const conditionsPoor = tonight.data?.conditions?.category === "poor" || tonight.data?.conditions?.category === "very_poor";

  const preflight = [
    { label: "Live server state", ok: snapshot !== null, blocker: true, action: "Wait for reconnection." },
    { label: "Observing location", ok: snapshot?.location != null, blocker: true, action: "Set the site in Settings → Location." },
    { label: "Mount connected", ok: snapshot?.devices == null || isAlpacaDeviceConnected(snapshot.devices, "telescope"), blocker: true, action: "Connect the mount in Settings → Devices." },
    { label: "Camera connected", ok: snapshot?.devices == null || isAlpacaDeviceConnected(snapshot.devices, "camera"), blocker: true, action: "Connect the camera in Settings → Devices." },
    { label: "Capture folder", ok: capture == null || capture.folder_configured, blocker: true, action: "Choose a capture folder in Settings." },
    {
      label: "Capture storage",
      ok: archive.data !== undefined && !storageLow,
      blocker: storageLow,
      action: storageLow
        ? "Less than 1 GB remains. Free capture storage before starting."
        : archive.isError ? "Storage could not be checked; verify it in Library." : "Checking available capture storage.",
    },
    {
      label: "Weather and Moon",
      ok: tonight.data?.conditions != null && !conditionsPoor,
      blocker: false,
      action: conditionsPoor
        ? `${tonight.data?.conditions?.category.replaceAll("_", " ") ?? "Poor"} forecast: verify cloud, wind, dew, and Moon conditions before starting.`
        : tonight.isError ? "Conditions could not be checked; verify the sky before starting." : "Checking tonight's conditions.",
    },
    { label: "Plan compiled for this night", ok: planId !== null && nightDate !== null && startBlockedReason === null, blocker: true, action: startBlockedReason ?? "Select a plan and observing night." },
    {
      label: "Dark temperature coverage",
      ok: false,
      blocker: false,
      action: "Dark suitability cannot be confirmed without a validated sensor-temperature prediction and tolerance. Ambient forecasts are not sensor temperatures. Review matching camera, optic, exposure, gain and readout settings in the dark library; add a dark calibration item if coverage cannot be confirmed. Existing darks may be reused when suitable.",
    },
    { label: "Focuser available", ok: snapshot?.devices == null || isAlpacaDeviceConnected(snapshot.devices, "focuser"), blocker: false, action: "Autofocus steps will fail unless a focuser is connected." },
  ];
  const firstPreflightBlocker = preflight.find((check) => check.blocker && !check.ok);

  const startReason =
    snapshot === null
      ? "Waiting for live server state."
      : planId === null
        ? "Select or create a plan first."
        : nightDate === null
          ? "Waiting for the plan to compile for a night."
          : active
            ? `Stop ${run.plan_name} before starting another run.`
            : firstPreflightBlocker?.action ?? startBlockedReason;

  return (
    <Card className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <CardTitle>Plan run</CardTitle>
        {run !== null && <Badge tone={STATUS_TONE[run.status]}>{run.status}</Badge>}
      </div>

      {run === null ? (
        <p className="text-sm text-fg-muted">Not started. Timed items execute only after this plan is started.</p>
      ) : (
        <div className="space-y-1 text-sm">
          <p>
            <span className="text-fg-muted">Plan:</span> {run.plan_name} · night {run.night_date}
          </p>
          {run.current_item_id !== null && (
            <p>
              <span className="text-fg-muted">Current:</span>{" "}
              {run.plan_id === planId ? itemLabel(run.current_item_id) : run.current_item_id}
              {timing === null ? "" : ` · ${timing}`}
            </p>
          )}
          {run.gate_held && run.gate_reason !== null && <p className="text-warning">Held: {run.gate_reason}</p>}
          {run.ended_reason !== null && <p className="text-fg-muted">{run.ended_reason}</p>}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {!active && (
          <Button
            variant="primary"
            disabled={busy || startReason !== null}
            onClick={() => {
              if (planId === null || nightDate === null) return;
              const parked = snapshot?.mount?.at_park === true || snapshot?.mount?.at_home === true;
              if (parked && !window.confirm(translate("The telescope is parked or at home. Allow this plan to unpark and move it?"))) return;
              if (
                restarting && !parked &&
                !window.confirm(
                  translate(`Start a new run of ${run.plan_name} for ${nightDate}? The previous run will remain in its history.`),
                )
              ) {
                return;
              }
              onExecutionStart?.(planId);
              start.mutate({ planId, nightDate });
            }}
          >
            <StartPlanIcon />
            {start.isPending ? (restarting ? "Restarting…" : "Starting…") : restarting ? "Restart plan" : "Start selected plan"}
          </Button>
        )}
        {run !== null && actions.has("pause") && (
          <Button
            disabled={busy}
            onClick={() => {
              pause.mutate(run.run_id);
            }}
          >
            {pause.isPending ? "Pausing…" : "Pause"}
          </Button>
        )}
        {run !== null && actions.has("resume") && (
          <Button
            variant="primary"
            disabled={busy}
            onClick={() => {
              onExecutionStart?.(run.plan_id);
              resume.mutate(run.run_id);
            }}
          >
            {resume.isPending ? "Resuming…" : "Resume"}
          </Button>
        )}
        {run !== null && actions.has("resume_interrupted") && (
          <Button
            variant="primary"
            disabled={busy}
            onClick={() => {
              onExecutionStart?.(run.plan_id);
              resumeInterrupted.mutate(run.run_id);
            }}
          >
            {resumeInterrupted.isPending ? "Resuming…" : "Resume interrupted run"}
          </Button>
        )}
        {run !== null && actions.has("stop") && (
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => {
              stop.mutate(run.run_id);
            }}
          >
            <StopIcon />
            {stop.isPending ? "Stopping…" : "Stop"}
          </Button>
        )}
      </div>

      <details className="rounded-md border border-border bg-bg px-3 py-2" open={firstPreflightBlocker !== undefined}>
        <summary className="cursor-pointer text-sm font-medium text-fg">Preflight · {String(preflight.filter((check) => check.ok).length)}/{String(preflight.length)} ready</summary>
        <ul className="mt-2 space-y-2">
          {preflight.map((check) => {
            const Icon = check.ok ? CheckCircle2 : check.blocker ? CircleX : CircleAlert;
            return <li key={check.label} className="flex items-start gap-2 text-xs"><Icon className={`mt-0.5 size-4 shrink-0 ${check.ok ? "text-success" : check.blocker ? "text-danger" : "text-warning"}`} /><span><span className="font-medium text-fg">{check.label}</span>{!check.ok && <span className="block text-fg-muted">{check.action}</span>}</span></li>;
          })}
        </ul>
      </details>

      {startReason !== null && !active && <p className="text-xs text-fg-muted">{startReason}</p>}
      {error !== null && (
        <ActionableError message={error} impact="The plan did not change state." action="Resolve the failed preflight item, then try the command again." />
      )}
    </Card>
  );
}
