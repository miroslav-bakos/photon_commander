// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §4.1: "The UI always shows the resolved absolute time inline —
 * `Astronomical dusk +15 → 22:47` — so the abstraction never hides what will
 * actually happen." The label half of that string is the compiler's own
 * `resolution` echo; the clock half is `start_utc` through the app's local
 * display helper, so the document stays UTC and only this edge is local.
 * `field="end"` shows the same thing for `end_resolution`/`end_utc`.
 *
 * `outcome` matters as much as `when`: an unschedulable item (`start_utc`
 * null) shows the compiler's diagnostic — "astronomical dusk does not occur
 * on this date at this latitude" — never a blank.
 */
import type { CompiledItem } from "../../api/plans";
import { formatClock, resolutionLabel } from "./localTime";

export type ResolvedTimeState =
  | { status: "pending" }
  | { status: "error"; message: string }
  | { status: "unsaved" }
  | { status: "compiled"; item: CompiledItem };

export function ResolvedTimeLabel({ state, field = "start" }: { state: ResolvedTimeState; field?: "start" | "end" }) {
  switch (state.status) {
    case "pending":
      return <p className="text-xs text-fg-muted">Resolving…</p>;
    case "error":
      return (
        <p className="rounded-md border border-danger bg-surface px-2 py-1.5 text-xs text-danger">
          Not compiled: {state.message}
        </p>
      );
    case "unsaved":
      return (
        <p className="text-xs text-fg-muted">
          Save to resolve this {field} against the night.
        </p>
      );
    case "compiled": {
      const { item } = state;
      const utc = field === "start" ? item.start_utc : item.end_utc;
      const resolution = field === "start" ? item.resolution : item.end_resolution;
      if (utc !== null) {
        return (
          <p className="text-sm text-fg" data-testid={field === "start" ? "resolved-time" : "resolved-time-end"}>
            {resolutionLabel(resolution)}
            {" → "}
            <span className="font-medium tabular-nums">{formatClock(utc)}</span>
          </p>
        );
      }
      const reasons = item.diagnostics.filter((d) => d.severity === "error");
      const shown = reasons.length > 0 ? reasons : item.diagnostics;
      return (
        <div className="flex flex-col gap-1" data-testid={field === "start" ? "resolved-time" : "resolved-time-end"}>
          <p className="text-sm text-fg-muted">{resolutionLabel(resolution)} → unschedulable</p>
          {shown.length === 0 ? (
            <p className="rounded-md border border-danger bg-surface px-2 py-1.5 text-xs text-danger">
              The compiler gave no {field} time and no reason — that is a server bug worth reporting.
            </p>
          ) : (
            shown.map((d) => (
              <p key={d.code + d.message} className="rounded-md border border-danger bg-surface px-2 py-1.5 text-xs text-danger">
                {d.message}
              </p>
            ))
          )}
        </div>
      );
    }
  }
}
