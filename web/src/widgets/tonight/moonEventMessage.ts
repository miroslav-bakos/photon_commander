// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { EventTimeModel, Outcome } from "../../api/astro";

/**
 * Moonrise/moonset "may legitimately not occur" (architecture policy): the moon can
 * stay up, or stay down, for the whole night. Mirrors how
 * `PlanningTab/twilight.ts#outcomeMessage` renders a non-occurring solar
 * event — a real sentence, never a blank cell or an epoch date — kept as its
 * own small copy here rather than importing from `PlanningTab` (this widget
 * does not own that tab, and the wording differs: "does the moon rise
 * tonight" reads differently from "does astronomical dusk happen tonight").
 */
export function moonEventMessage(kind: "moonrise" | "moonset", outcome: Outcome): string {
  switch (outcome) {
    case "occurs":
      return "";
    case "always_above":
      return kind === "moonrise" ? "Already up before the window starts" : "Stays up all night";
    case "always_below":
      return kind === "moonrise" ? "Stays down all night" : "Already down before the window starts";
    case "not_in_window":
      return kind === "moonrise" ? "No moonrise in the dark window" : "No moonset in the dark window";
  }
}

export function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Renders as a clock time when the event occurs, or the explicit outcome
 * message otherwise — never a blank string, never `new Date(null)`. */
export function describeMoonEvent(kind: "moonrise" | "moonset", event: EventTimeModel): string {
  if (event.outcome === "occurs" && event.when !== null) return formatClock(event.when);
  return moonEventMessage(kind, event.outcome);
}
