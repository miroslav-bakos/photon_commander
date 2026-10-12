// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The item's `end`, beside `TimeSpecPicker`'s `start` (user: "duration
 * should be accompanied with 'until event' and be possible to select e.g.
 * astronomical dawn, or another event"). Three kinds: a plain `Duration` in
 * minutes (the historical `duration_min` behaviour, now `DurationEndSpec`),
 * `Until event` (any solar/lunar event plus an offset), and — only on a
 * target-bearing item — `Until target event` (the item's own target rising
 * above, transiting, or setting below an altitude). `null` (no kind picked)
 * is instantaneous: the item's `end_utc` equals its `start_utc`, exactly as
 * a blank `duration_min` used to mean.
 *
 * The `event`/`target_event` bodies are `EventFields.tsx`, shared verbatim
 * with `TimeSpecPicker`'s own `event`/`target_event` start kinds.
 *
 * Which section is showing is its own state (`selectedKind`), not derived
 * purely from `value`'s kind: clearing the Duration field to blank commits
 * `null` (instantaneous) immediately, and if the section closed the moment
 * that happened, the field the user is mid-edit of would vanish under their
 * cursor. `selectedKind` only changes on an explicit radio click, so the
 * Duration section — and its now-blank input — stays put until they pick
 * something else.
 */
import { SegmentedControl } from "../../components/SegmentedControl";
import { useState } from "react";
import { DraftNumberField } from "../../components/DraftNumberField";
import type { EndSpec, EndSpecKind, LunarEvent, NightWindow, SolarEvent, TargetEvent } from "../../api/plans";
import { EventOffsetFields, TargetEventOffsetFields } from "./EventFields";
import { END_SPEC_KINDS, defaultEndSpec } from "./timeSpecs";

export function EndSpecPicker({
  value,
  onChange,
  hasTarget,
  night,
  durationLabel = "Duration (minutes)",
}: {
  value: EndSpec | null;
  onChange: (end: EndSpec | null) => void;
  /** Whether the item carries its own target — "Until target event" is only
   * offered then (unlike the start picker's `target_event`, which is always
   * offered with a warning: DESIGN.md §4.1's "Until" review asks for this
   * one to be conditional). */
  hasTarget: boolean;
  night?: NightWindow;
  /** A mosaic's duration is per panel — the caller says so in the label
   * rather than this component guessing at `item.kind`. */
  durationLabel?: string;
}) {
  const [selectedKind, setSelectedKind] = useState<EndSpecKind | null>(value?.kind ?? null);
  const kinds = hasTarget ? END_SPEC_KINDS : END_SPEC_KINDS.filter((k) => k.kind !== "target_event");
  const selectKind = (kind: EndSpecKind) => {
    setSelectedKind(kind);
    if (value?.kind !== kind) onChange(defaultEndSpec(kind));
  };
  return (
    <div className="flex flex-col gap-2">
      <span className="text-sm font-medium text-fg-muted">End</span>
      <SegmentedControl<EndSpecKind | "">
        label="End kind"
        value={selectedKind ?? ""}
        options={kinds.map(({ kind, label }) => ({ value: kind, label }))}
        onChange={(kind) => { if (kind !== "") selectKind(kind); }}
      />

      {selectedKind === "duration" && (
        <DraftNumberField
          label={durationLabel}
          hint="An estimate for the timeline, not a completion signal. Blank means instantaneous."
          min={0}
          step={1}
          value={value?.kind === "duration" ? value.minutes : null}
          nullable
          onValueChange={(minutes) => {
            if (minutes === null) {
              onChange(null);
              return;
            }
            if (minutes > 0) onChange({ kind: "duration", minutes });
          }}
        />
      )}

      {value?.kind === "event" && (
        <EventOffsetFields
          id="endspec-event"
          event={value.event}
          offsetMin={value.offset_min}
          night={night}
          onChangeEvent={(event: SolarEvent | LunarEvent) => {
            onChange({ ...value, event });
          }}
          onChangeOffset={(offset_min) => {
            onChange({ ...value, offset_min });
          }}
        />
      )}

      {value?.kind === "target_event" && (
        <TargetEventOffsetFields
          id="endspec-target-event"
          event={value.event}
          altDeg={value.alt_deg}
          offsetMin={value.offset_min}
          hasTarget={hasTarget}
          onChangeEvent={(event: TargetEvent, alt_deg: number | null) => {
            onChange({ ...value, event, alt_deg });
          }}
          onChangeAltDeg={(alt_deg) => {
            onChange({ ...value, alt_deg });
          }}
          onChangeOffset={(offset_min) => {
            onChange({ ...value, offset_min });
          }}
        />
      )}

      {selectedKind === null && <p className="text-xs text-fg-muted">Instantaneous — ends the moment it starts.</p>}
    </div>
  );
}

// Exhaustiveness: every `EndSpecKind` the schema enumerates must be one of
// `END_SPEC_KINDS`, or the switch above silently drops a kind's fields.
const _exhaustive: Exclude<EndSpecKind, (typeof END_SPEC_KINDS)[number]["kind"]>[] = [];
void _exhaustive;
