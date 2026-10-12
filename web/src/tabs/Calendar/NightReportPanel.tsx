// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Camera, CircleAlert, Focus, Move3d, Play, Square } from "lucide-react";
import { useRunDetailQuery } from "../../api/runs";
import { Card, CardTitle } from "../../components/Card";

function eventIcon(text: string) {
  const value = text.toLowerCase();
  if (value.includes("focus")) return Focus;
  if (value.includes("dither") || value.includes("slew") || value.includes("center")) return Move3d;
  if (value.includes("expos") || value.includes("frame")) return Camera;
  if (value.includes("fail") || value.includes("error") || value.includes("held")) return CircleAlert;
  if (value.includes("stop") || value.includes("abort")) return Square;
  return Play;
}

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function NightReportPanel({ runId }: { runId: string }) {
  const detail = useRunDetailQuery(runId);
  if (detail.isError) return <Card><p role="alert">Night report unavailable. Retry when the connection returns.</p></Card>;
  if (detail.data === undefined) return <Card>Loading the night report…</Card>;
  const run = detail.data.run;

  const journal = detail.data.journal;
  const journalEvents = journal.map((entry) => ({
    key: `journal-${String(entry.seq)}`,
    at: entry.at,
    label: entry.effect?.name.replaceAll("_", " ") ?? entry.control?.replaceAll("_", " ") ?? entry.kind.replaceAll("_", " "),
    detail: entry.reason,
  }));
  const events = journalEvents
    .sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime())
;
  const summary = detail.data.capture_summary;
  const finalMount = detail.data.final_mount;
  const held = new Set<string>();
  let recoverySeconds = 0;
  let previous = 0;
  for (const entry of journal) {
    const at = Date.parse(entry.at);
    if (held.size > 0 && previous > 0) recoverySeconds += Math.max(0, at - previous) / 1000;
    if (entry.item_id !== null && entry.held !== null) {
      if (entry.held) held.add(entry.item_id); else held.delete(entry.item_id);
    }
    if (entry.item_id !== null && (entry.control === "skip" || entry.control === "retry")) held.delete(entry.item_id);
    if (entry.kind === "run_ended") held.clear();
    previous = at;
  }
  const failed = journal.filter((entry) => entry.effect_outcome === "failed" || entry.status === "failed").length;

  return (
    <Card className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div><CardTitle>Night report</CardTitle><p className="text-xs text-fg-muted">{run.plan_name} · {run.night_date} · Revision {run.plan_revision}</p></div>
        <p className="text-xs text-fg-muted">{`${String(summary.saved_frames)} saved frames`} · {String(failed)} recorded failures · {run.status}</p>
      </div>
      <dl className="grid grid-cols-2 gap-2 text-sm">
        <dt>Saved light integration</dt><dd>{(summary.captured_integration_s / 60).toFixed(1)} min{summary.unknown_exposure_frames > 0 ? " + unknown exposures" : ""}</dd>
        <dt>Accepted integration</dt><dd>{summary.accepted_integration_s == null ? "Unknown" : `${(summary.accepted_integration_s / 60).toFixed(1)} min recorded`}</dd>
        <dt>Stack outcomes</dt><dd>{summary.accepted_frames} accepted · {summary.rejected_frames} rejected · {summary.stack_error_frames} errors</dd>
        <dt>Unmeasured light frames</dt><dd>{summary.stack_unmeasured_frames}</dd>
        <dt>Incomplete saves</dt><dd>{summary.pending_frames}</dd>
        <dt>Recorded hold / recovery time</dt><dd>{(recoverySeconds / 60).toFixed(1)} min{held.size > 0 ? " · ongoing" : ""}</dd>
        <dt>Mount at run end</dt><dd>{finalMount?.at_park === true ? "Parked" : finalMount?.slewing === true ? "Moving" : finalMount?.tracking === true ? "Tracking" : finalMount?.at_park === false ? "Not parked" : "Unknown — no fresh reading recorded"}{finalMount?.polled_at != null && ` · ${clock(finalMount.polled_at)}`}</dd>
      </dl>
      <p className="text-xs text-fg-muted">Saved totals cover this run’s retained catalogue frames. Stack outcomes are recorded separately; unmeasured frames are not assumed accepted. Mount status is the reading at run end, not its current state.</p>
      {events.length === 0 ? <p className="text-sm text-fg-muted">No run events have been recorded yet.</p> : (
        <ol className="max-h-72 space-y-1 overflow-y-auto border-l border-border pl-3">
          {events.map((event) => {
            const Icon = eventIcon(`${event.label} ${event.detail}`);
            return <li key={event.key} className="grid grid-cols-[auto_auto_1fr] items-start gap-2 py-1 text-xs"><Icon className="mt-0.5 size-3.5 text-fg-muted" /><time className="tabular-nums text-fg-muted">{clock(event.at)}</time><span><span className="font-medium capitalize text-fg">{event.label}</span>{event.detail !== "" && <span className="ml-1 text-fg-muted">· {event.detail}</span>}</span></li>;
          })}
        </ol>
      )}
      {run.ended_reason !== null && <p className="rounded-md border border-border bg-bg px-3 py-2 text-xs text-fg-muted">Outcome: {run.ended_reason}</p>}
    </Card>
  );
}
