// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Bottom-right ambient signal that a target-field cutout is being fetched,
 * or just failed.
 *
 * There is no fetch-progress *fraction* to show and therefore no
 * determinate bar: `CutoutState` (backend) carries a running `bytes` count
 * but no total size, and each upstream fetch completes as one unit
 * server-side — `bytes` jumps straight from 0 to
 * final, never through a percentage in between. A bar that claimed a
 * fraction here would animate against a denominator that does not exist,
 * i.e. it would lie. This is the honest version instead: an indeterminate
 * activity indicator (architecture policy invariant 7 — no silent degradation, and no
 * fabricated precision either), built entirely from `CutoutSourceRow.state`
 * — the exact per-source state machine `LayersPanel`'s "Target field" rows
 * already render (`cutoutLayers.ts`). No second copy of cutout-job status
 * is kept here; a real determinate bar needs the backend to stream with
 * `Content-Length`, which is a separate, backend-owned follow-up.
 *
 * Stacks into the same bottom-right column `SkyMap.tsx` already keeps for
 * the imagery-loading/digital-zoom/mount-pointing indicators
 * (`bottom-10/16/22 right-2`, one line each) at the next slot,
 * `bottom-28`, and sits well clear of both the full-width `ViewReadout` bar
 * pinned to the map's own `bottom-0` and `SkyMapCreditsFooter` (outside
 * this card entirely, in `Shell.tsx`'s footer) — this renders inside the
 * map `Card`, not the app-shell chrome, so neither is touched.
 *
 * One line at a time, not one per source — `summarizeCutoutFetchStatus`
 * (`cutoutLayers.ts`) picks a single row to speak for the whole group; see
 * its own doc comment for the priority (an active fetch beats a stale
 * failure) and why a footprint-miss "unavailable" never appears here. The
 * "Target field" group in `LayersPanel` still shows every row in full —
 * this is a summary and an activity/failure signal, not a second control
 * surface, and offers no action of its own (no retry button; that stays
 * with the row that already has one).
 */
import { summarizeCutoutFetchStatus, type CutoutSourceRow } from "./cutoutLayers";

export function CutoutFetchIndicator({ rows }: { rows: readonly CutoutSourceRow[] | null }) {
  const summary = rows === null ? null : summarizeCutoutFetchStatus(rows);
  if (summary === null) return null;

  if (summary.kind === "fetching") {
    return (
      <div
        aria-live="polite"
        className="pointer-events-none absolute bottom-28 right-2 flex items-center gap-1.5 text-[10px] text-fg-muted"
      >
        <span aria-hidden="true" className="h-2 w-2 flex-shrink-0 rounded-full bg-dot-unknown" />
        <span>
          {summary.status === "queued" ? "Queued: " : "Fetching "}
          {summary.label}
          {summary.extraCount > 0 ? ` +${String(summary.extraCount)} more` : ""}…
        </span>
        {/* Static activity marker: no completion fraction is available. */}
        <span aria-hidden="true" className="h-1 w-8 flex-shrink-0 rounded-full bg-dot-unknown" />
      </div>
    );
  }

  // summary.kind === "failed": stays on screen for as long as the failure
  // is current — resolved only by a retry (from `LayersPanel`'s own row)
  // landing, or the target changing — never auto-dismissed, so a failure is
  // exactly as visible as a success (the owner's own requirement).
  return (
    <div
      aria-live="polite"
      className="pointer-events-none absolute bottom-28 right-2 flex max-w-[70%] flex-col items-end gap-0.5 text-right text-[10px] text-fg-muted"
    >
      <span className="flex items-center gap-1.5">
        <span aria-hidden="true" className="h-2 w-2 flex-shrink-0 rounded-full bg-dot-bad" />
        {summary.label} fetch failed
        {summary.extraCount > 0 ? ` (+${String(summary.extraCount)} more)` : ""}
      </span>
      {/* `title`: the raw upstream text, tooltip material only — `reason` is
       * already the human sentence (`CutoutRowState`'s own "unavailable"
       * doc comment, `cutoutLayers.ts`). */}
      <span title={summary.detail ?? undefined}>{summary.reason}</span>
    </div>
  );
}
