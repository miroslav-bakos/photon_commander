// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useSyncExternalStore } from "react";
import { Card } from "../components/Card";
import { clearDiagnostics, getDiagnosticsSnapshot, subscribeDiagnostics } from "./diagnostics";

export function PerformanceDiagnostics({ className = "" }: { className?: string }) {
  const entries = useSyncExternalStore(subscribeDiagnostics, getDiagnosticsSnapshot);
  return (
    <Card title="Screen loading diagnostics" collapseKey="system.performance" className={className}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-xs text-fg-muted">
        <p>Times are measured in this browser since this page was opened. Screen time ends after the first painted frame; API time ends when a response arrives.</p>
        <button type="button" onClick={clearDiagnostics} className="rounded border border-border px-2 py-1 text-fg hover:text-accent">Clear measurements</button>
      </div>
      {entries.length === 0 ? <p className="text-sm text-fg-muted">No measurements yet.</p> : (
        <div className="max-h-72 overflow-auto">
          <table className="w-full text-left text-xs">
            <thead><tr className="border-b border-border text-fg-muted"><th className="py-1">Time</th><th>Kind</th><th>Screen / request</th><th>Status</th><th className="text-right">Duration</th></tr></thead>
            <tbody>{entries.map((entry) => <tr key={entry.id} className="border-b border-border/50">
              <td className="whitespace-nowrap py-1 pr-2">{new Date(entry.startedAt).toLocaleTimeString()}</td>
              <td className="pr-2">{entry.kind}</td>
              <td className="break-all pr-2 font-mono">{entry.name}</td>
              <td className="whitespace-nowrap pr-2">{entry.status}</td>
              <td className="whitespace-nowrap text-right">{entry.durationMs === null ? "…" : `${String(entry.durationMs)} ms`}</td>
            </tr>)}</tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
