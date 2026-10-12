// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Session start as a `TimeSpec` (DESIGN.md §4.1), not a bare timestamp:
 * either an absolute `datetime-local` (unchanged from before) or an
 * event-relative pick — "astronomical dusk + 15 min" — against one of the
 * eight solar events `/api/astro/night` returns. Civil, nautical and
 * astronomical are offered side by side, never narrowed to one (§4.1: "the
 * interface must not narrow that to one opinion").
 *
 * The resolved absolute instant is always shown inline —
 * `Astronomical dusk +15 → 22:47` — because §4.1 is explicit the
 * abstraction must never hide what will actually happen. When the chosen
 * event does not occur at all (`Outcome !== "occurs"`, e.g. astronomical
 * dusk above ~60° latitude in summer), that is said plainly rather than
 * silently falling back to another twilight or a blank field.
 *
 * Resolution itself (`resolveEventStart`) lives in `twilight.ts`, shared
 * with `PlanningTab` so the *displayed* resolved time and the *actual*
 * session start used for framing/visibility are computed the same way —
 * never two copies that could disagree.
 */
import { SegmentedControl } from "../../components/SegmentedControl";
import { DraftNumberField } from "../../components/DraftNumberField";
import { Field } from "../../components/Field";
import type { EventTimeModel } from "../../api/astro";
import {
  SOLAR_EVENT_LABELS,
  SOLAR_EVENT_ORDER,
  type EventStartResolution,
  type SolarEventName,
} from "./twilight";

const SELECT_CLASSES =
  "min-h-11 rounded-md border border-border-strong bg-bg px-3 text-base text-fg";

function formatClock(date: Date): string {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function optionLabel(name: SolarEventName, events: EventTimeModel[] | undefined): string {
  const label = SOLAR_EVENT_LABELS[name];
  if (events === undefined) return label;
  const found = events.find((e) => e.event === name) ?? null;
  if (found === null) return `${label} — does not occur tonight`;
  if (found.outcome !== "occurs" || found.when === null) return `${label} — does not occur tonight`;
  return `${label} — ${formatClock(new Date(found.when))}`;
}

function formatOffset(offsetMin: number): string {
  if (offsetMin === 0) return "";
  const sign = offsetMin > 0 ? "+" : "−";
  return ` ${sign}${String(Math.abs(offsetMin))}`;
}

export type StartMode = "absolute" | "event";

export function SessionStartField({
  mode,
  onModeChange,
  absoluteLocalValue,
  onAbsoluteLocalChange,
  anchorDateLocalValue,
  onAnchorDateLocalChange,
  eventName,
  onEventChange,
  offsetMin,
  onOffsetChange,
  solarEvents,
  isError,
  resolution,
}: {
  mode: StartMode;
  onModeChange: (mode: StartMode) => void;
  /** `<input type="datetime-local">`'s own string format. */
  absoluteLocalValue: string;
  onAbsoluteLocalChange: (value: string) => void;
  /** `<input type="date">`'s own string format — which calendar night's
   * dusk/dawn boundaries to offer. */
  anchorDateLocalValue: string;
  onAnchorDateLocalChange: (value: string) => void;
  eventName: SolarEventName | null;
  onEventChange: (name: SolarEventName | null) => void;
  offsetMin: number;
  onOffsetChange: (minutes: number) => void;
  /** `NightResponse.solar_events` for the anchor night — `undefined` while
   * loading, so option labels can say "does not occur tonight" only once an
   * actual answer is in hand rather than guessing from a stale one. */
  solarEvents: EventTimeModel[] | undefined;
  isError: boolean;
  resolution: EventStartResolution;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-sm font-medium text-fg-muted">Session start</span>
      <SegmentedControl<StartMode>
        label="Session start mode"
        value={mode}
        options={[{ value: "absolute", label: "Absolute" }, { value: "event", label: "Event-relative" }]}
        onChange={onModeChange}
      />

      {mode === "absolute" && (
        <Field
          label="Absolute time"
          type="datetime-local"
          value={absoluteLocalValue}
          onChange={(e) => {
            onAbsoluteLocalChange(e.target.value);
          }}
        />
      )}

      {mode === "event" && (
        <div className="flex flex-col gap-2 rounded-md border border-border bg-surface-raised p-3">
          <Field
            label="Night of"
            type="date"
            value={anchorDateLocalValue}
            onChange={(e) => {
              onAnchorDateLocalChange(e.target.value);
            }}
          />

          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-medium text-fg-muted" htmlFor="start-event-select">
              Solar event
            </label>
            <select
              id="start-event-select"
              className={SELECT_CLASSES}
              value={eventName ?? ""}
              onChange={(e) => {
                const value = e.target.value;
                onEventChange(value === "" ? null : (value as SolarEventName));
              }}
            >
              <option value="" disabled>
                Choose civil, nautical or astronomical…
              </option>
              {SOLAR_EVENT_ORDER.map((name) => (
                <option key={name} value={name}>
                  {optionLabel(name, solarEvents)}
                </option>
              ))}
            </select>
          </div>

          <DraftNumberField
            label="Offset (minutes)"
            hint="Positive is after the event, negative is before it."
            step={1}
            value={offsetMin}
            onValueChange={(value) => { if (value !== null) onOffsetChange(Math.round(value)); }}
          />

          {isError && (
            <p className="rounded-md border border-danger bg-surface px-2 py-1.5 text-xs text-danger">
              Could not load tonight's solar events.
            </p>
          )}
          {!isError && resolution.status === "unselected" && (
            <p className="text-xs text-fg-muted">Choose a solar event above.</p>
          )}
          {!isError && resolution.status === "pending" && (
            <p className="text-xs text-fg-muted">Resolving…</p>
          )}
          {!isError && resolution.status === "unavailable" && (
            <p className="rounded-md border border-danger bg-surface px-2 py-1.5 text-xs text-danger">
              {resolution.message}
            </p>
          )}
          {!isError && resolution.status === "resolved" && eventName !== null && (
            <p className="text-sm text-fg">
              {SOLAR_EVENT_LABELS[eventName]}
              {formatOffset(offsetMin)}
              {" → "}
              <span className="font-medium tabular-nums">{formatClock(resolution.when)}</span>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
