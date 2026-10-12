// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { AlertTriangle, Radio } from "lucide-react";
import { useCaptureState } from "../api/capture";
import { useConnectionStore } from "../store/connectionStore";

function titleCase(value: string): string {
  return value.length === 0 ? value : `${value[0]?.toUpperCase() ?? ""}${value.slice(1).replaceAll("_", " ")}`;
}

export function GlobalOperationBanner() {
  const status = useConnectionStore((state) => state.status);
  const snapshot = useConnectionStore((state) => state.snapshot);
  const capture = useCaptureState();
  const devices = snapshot?.devices;
  const link = snapshot?.link;
  const rigOffline = link?.control_connected !== true;
  const telemetryStale = devices?.stale === true;
  // Native Alpaca uses polling; link health decides whether this is a problem.
  const eventsUnavailable = link?.status === "degraded";

  if (status !== "open" || rigOffline || telemetryStale || eventsUnavailable) {
    const detail = status !== "open"
      ? status === "connecting" ? "Connecting to Photon Commander…" : "Live connection lost. Controls will return after reconnection."
      : rigOffline ? "Rig offline. Connect the telescope to resume observing."
      : telemetryStale ? "Rig telemetry is stale. Waiting for a fresh device check."
      : link?.reason || "Rig control is connected, but the event connection is unavailable. Live device updates may be delayed.";
    const summary = status !== "open"
      ? status === "connecting" ? "Connecting to Photon Commander…" : "Live connection lost"
      : rigOffline ? "Rig offline"
        : telemetryStale ? "Rig telemetry is stale" : "Live device updates delayed";
    return (
      <details role="alert" className="border-b border-warning bg-warning/10 text-xs text-warning">
        <summary className="flex min-h-11 cursor-pointer items-center gap-2 px-3 sm:min-h-9 sm:px-4">
          <AlertTriangle aria-hidden="true" className="size-4 shrink-0" />
          <span className="min-w-0 flex-1 font-medium">{summary}</span>
          <span className="shrink-0 underline">Details</span>
        </summary>
        <p className="break-words px-3 pb-2 sm:px-4">{detail}</p>
      </details>
    );
  }

  const run = snapshot?.run;
  const activeRun = run?.status === "running" || run?.status === "paused";
  if (capture?.in_progress !== true && !activeRun) return null;
  const operation = capture?.in_progress === true
    ? titleCase(capture.stage ?? "capturing")
    : run?.status === "paused" ? "Plan paused" : titleCase(run?.last_decision ?? "running plan");
  const context = capture?.object_name ?? run?.plan_name ?? null;

  return (
    <div role="status" className="flex min-h-9 items-center gap-2 border-b border-accent bg-accent-muted px-3 py-1.5 text-xs text-fg sm:px-4">
      <Radio className="size-4 shrink-0 text-accent" />
      <span className="font-semibold text-accent">{operation}</span>
      {context !== null && <span className="truncate text-fg-muted">{context}</span>}
    </div>
  );
}
