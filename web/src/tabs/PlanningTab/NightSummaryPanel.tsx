// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §4.1: "the night summary wherever twilight times are listed" —
 * all eight solar events (sunset through civil/nautical/astronomical dusk,
 * then astronomical/nautical/civil dawn through sunrise), so a user can see
 * the whole shape of the night before picking a session start, rather than
 * picking blind. Civil, nautical and astronomical are listed as equal rows
 * — none highlighted over the others, matching architecture policy's "do not default
 * the UI to astronomical and bury the others".
 *
 * `outcome` renders as its own message whenever it is not `"occurs"` — never
 * a blank row or an omitted one, since above ~60° latitude a whole tier of
 * twilight can fail to happen for the night in view.
 */
import { Card } from "../../components/Card";
import type { NightResponse } from "../../api/astro";
import { SOLAR_EVENT_LABELS, orderedSolarEvents, outcomeMessage, type SolarEventName } from "./twilight";

function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function NightSummaryPanel({
  night,
  isPending,
  isError,
}: {
  night: NightResponse | undefined;
  isPending: boolean;
  isError: boolean;
}) {
  return (
    <Card title="Night summary" collapseKey="planning.nightsummary" className="space-y-3">
      {isError && <p className="text-sm text-danger">Could not load tonight's solar events.</p>}
      {!isError && isPending && <p className="text-sm text-fg-muted">Loading…</p>}

      {!isError && night !== undefined && (
        <ul className="space-y-1 text-sm">
          {orderedSolarEvents(night.solar_events).map((event) => (
            <li key={event.event} className="flex items-center justify-between gap-2">
              <span className="text-fg-muted">{SOLAR_EVENT_LABELS[event.event as SolarEventName]}</span>
              {event.outcome === "occurs" && event.when !== null ? (
                <span className="font-medium tabular-nums text-fg">{formatClock(event.when)}</span>
              ) : (
                <span className="text-xs text-warning">
                  {outcomeMessage(event.outcome, SOLAR_EVENT_LABELS[event.event as SolarEventName])}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
