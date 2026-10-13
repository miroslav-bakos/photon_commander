// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Radio } from "lucide-react";
import { useCaptureState } from "../api/capture";
import { useConnectionStore } from "../store/connectionStore";

// Keep a recovery warning steady across short fresh/stale gaps. This is only
// presentation: authoritative stale flags and command guards remain unchanged.
function useTelemetryRecovery(initiallyStale: boolean): boolean {
  const [recovering, setRecovering] = useState(initiallyStale);
  const lastFreshSample = useRef<number | null>(null);
  useEffect(() => useConnectionStore.subscribe((state) => {
    const devices = state.snapshot?.devices;
    if (devices?.stale === true) {
      lastFreshSample.current = null;
      setRecovering(true);
      return;
    }
    if (devices?.stale !== false || devices.polled_at == null) return;
    const sample = Date.parse(devices.polled_at);
    if (!Number.isFinite(sample) || sample === lastFreshSample.current) return;
    const previous = lastFreshSample.current;
    if (previous !== null && sample > previous && sample - previous <= 5000) {
      setRecovering(false);
    }
    lastFreshSample.current = sample;
  }), []);
  return recovering;
}

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
  const telemetryRecovering = useTelemetryRecovery(telemetryStale);
  const waitingForTelemetry = telemetryStale || telemetryRecovering;
  // Native Alpaca uses polling; link health decides whether this is a problem.
  const eventsUnavailable = link?.status === "degraded";

  if (status !== "open" || rigOffline || waitingForTelemetry || eventsUnavailable) {
    const detail = status !== "open"
      ? status === "connecting" ? "Connecting to Photon Commander…" : "Live connection lost. Controls will return after reconnection."
      : rigOffline ? "Rig offline. Connect the telescope to resume observing."
      : waitingForTelemetry ? "Device readings are unavailable or intermittent. Waiting for consistently fresh updates."
      : link.reason ?? "Rig control is connected, but the event connection is unavailable. Live device updates may be delayed.";
    const summary = status !== "open"
      ? status === "connecting" ? "Connecting to Photon Commander…" : "Live connection lost"
      : rigOffline ? "Rig offline"
        : waitingForTelemetry ? "Waiting for current device readings" : "Live device updates delayed";
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
