// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The event+offset controls shared by every picker that names a solar/lunar
 * event or a target event: `TimeSpecPicker`'s `event`/`target_event` starts
 * and `EndSpecPicker`'s `event`/`target_event` ends. Factored out so the
 * four call sites share one set of `<select>`s and one offset field rather
 * than four copies of the same markup (DESIGN.md §4.1/§4.4 "Until" review).
 */
import { DraftNumberField } from "../../components/DraftNumberField";
import type { LunarEvent, NightWindow, SolarEvent, TargetEvent } from "../../api/plans";
import { LUNAR_EVENTS, SOLAR_EVENTS, TARGET_EVENT_LABELS, eventOptionLabel } from "./timeSpecs";

const SELECT_CLASSES = "min-h-[44px] rounded-md border border-border-strong bg-bg px-3 text-base text-fg lg:min-h-11";

export function OffsetField({
  value,
  onChange,
  label = "Offset (minutes)",
}: {
  value: number;
  onChange: (minutes: number) => void;
  label?: string;
}) {
  return (
    <DraftNumberField
      label={label}
      hint="Positive is after, negative is before."
      step={1}
      value={value}
      onValueChange={(minutes) => { if (minutes !== null) onChange(minutes); }}
    />
  );
}

/** `event` + `offset_min` — the body of a solar/lunar `event`-kind spec,
 * start or end. Every `<option>` reads "Astronomical dusk · 21:13" once
 * `night` is known (DESIGN.md §4.4's dropdown-time review), the bare name
 * before that. */
export function EventOffsetFields({
  id,
  event,
  offsetMin,
  night,
  onChangeEvent,
  onChangeOffset,
}: {
  /** Unique per picker instance, so two on one page (start and end) do not
   * collide on `<label htmlFor>`. */
  id: string;
  event: SolarEvent | LunarEvent;
  offsetMin: number;
  night: NightWindow | undefined;
  onChangeEvent: (event: SolarEvent | LunarEvent) => void;
  onChangeOffset: (offsetMin: number) => void;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-md border border-border bg-surface-raised p-3">
      <div className="flex flex-col gap-1.5">
        <label className="text-sm font-medium text-fg-muted" htmlFor={id}>
          Event
        </label>
        <select
          id={id}
          className={SELECT_CLASSES}
          value={event}
          onChange={(e) => {
            onChangeEvent(e.target.value as SolarEvent | LunarEvent);
          }}
        >
          <optgroup label="Sun">
            {SOLAR_EVENTS.map((name) => (
              <option key={name} value={name}>
                {eventOptionLabel(name, night)}
              </option>
            ))}
          </optgroup>
          <optgroup label="Moon">
            {LUNAR_EVENTS.map((name) => (
              <option key={name} value={name}>
                {eventOptionLabel(name, night)}
              </option>
            ))}
          </optgroup>
        </select>
      </div>
      <OffsetField value={offsetMin} onChange={onChangeOffset} />
    </div>
  );
}

/** `event` + optional `alt_deg` + `offset_min` — the body of a `target_event`
 * spec, start or end. Target events have no per-night resolved time to show
 * (they depend on the item's own target, not the night alone), so — unlike
 * `EventOffsetFields` — the options are always the bare labels. */
export function TargetEventOffsetFields({
  id,
  event,
  altDeg,
  offsetMin,
  hasTarget,
  onChangeEvent,
  onChangeAltDeg,
  onChangeOffset,
}: {
  id: string;
  event: TargetEvent;
  altDeg: number | null | undefined;
  offsetMin: number;
  /** Whether the item carries its own target — a `target_event` is
   * otherwise unschedulable, said up front rather than left to the compile
   * diagnostic alone. */
  hasTarget: boolean;
  onChangeEvent: (event: TargetEvent, altDeg: number | null) => void;
  onChangeAltDeg: (altDeg: number) => void;
  onChangeOffset: (offsetMin: number) => void;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-md border border-border bg-surface-raised p-3">
      {!hasTarget && (
        <p className="rounded-md border border-warning bg-surface px-2 py-1.5 text-xs text-warning">
          This item has no target of its own — a target event cannot be resolved for it.
        </p>
      )}
      <div className="flex flex-col gap-1.5">
        <label className="text-sm font-medium text-fg-muted" htmlFor={id}>
          Target event
        </label>
        <select
          id={id}
          className={SELECT_CLASSES}
          value={event}
          onChange={(e) => {
            const next = e.target.value as TargetEvent;
            // `transits` has no altitude of its own — `alt_deg` must be
            // omitted there and present for the other two.
            onChangeEvent(next, next === "transits" ? null : (altDeg ?? 30));
          }}
        >
          {(Object.keys(TARGET_EVENT_LABELS) as TargetEvent[]).map((name) => (
            <option key={name} value={name}>
              {TARGET_EVENT_LABELS[name]}
            </option>
          ))}
        </select>
      </div>
      {event !== "transits" && (
        <DraftNumberField
          label="Altitude (degrees)"
          step={1}
          min={-90}
          max={90}
          value={altDeg ?? null}
          onValueChange={(next) => { if (next !== null) onChangeAltDeg(next); }}
        />
      )}
      <OffsetField value={offsetMin} onChange={onChangeOffset} />
    </div>
  );
}
