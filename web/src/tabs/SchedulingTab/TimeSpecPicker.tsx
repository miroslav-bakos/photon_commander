// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §4.1's `TimeSpec`, as one picker used by every item kind:
 * `absolute | event | target_event | relative | asap`. The `event` list is
 * **every** solar and lunar event the schema enumerates — civil, nautical and
 * astronomical dusk and dawn side by side, never narrowed to astronomical
 * (§4.1: "the interface must not narrow that to one opinion").
 *
 * Purely a control over the document's `start`: the resolved absolute time
 * the compiler produced is `ResolvedTimeLabel`'s, rendered beside this by
 * the item editor, so the picker never has to guess at a resolution the
 * server has not made.
 *
 * `absolute` is a naive local wall-clock reading, stored without a zone (the
 * zone is Settings', `ssc.plan.models.AbsoluteTimeSpec`). On the autumn DST
 * night that reading is ambiguous — 01:30 happens twice — and only then is
 * the "first / second occurrence" choice (`fold`) shown.
 *
 * The `event`/`target_event` bodies (the select(s) + offset) are
 * `EventFields.tsx` — shared verbatim with `EndSpecPicker`'s own `event`/
 * `target_event` kinds, so the two pickers never drift.
 */
import { SegmentedControl } from "../../components/SegmentedControl";
import { Field } from "../../components/Field";
import type { LunarEvent, NightWindow, SolarEvent, TargetEvent, TimeSpec } from "../../api/plans";
import { EventOffsetFields, OffsetField, TargetEventOffsetFields } from "./EventFields";
import { toDatetimeLocalInput, wallClockOccurrences } from "./localTime";
import { TIME_SPEC_KINDS, defaultTimeSpec } from "./timeSpecs";

export function TimeSpecPicker({
  value,
  onChange,
  hasTarget,
  siteTimeZone,
  night,
}: {
  value: TimeSpec;
  onChange: (spec: TimeSpec) => void;
  /** Whether the item carries its own target — `target_event` is otherwise
   * unschedulable, which the picker says up front rather than leaving it to
   * the compile diagnostic alone. */
  hasTarget: boolean;
  /** The site's zone from Settings when known; the fold check runs against
   * the browser's own zone otherwise. */
  siteTimeZone: string | undefined;
  /** The compiled night, for the event dropdown's own resolved time
   * (`Astronomical dusk · 21:13`). `undefined` before anything has
   * compiled — the dropdown then shows the bare names. */
  night?: NightWindow;
}) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-sm font-medium text-fg-muted">Start</span>
      <SegmentedControl
        label="Start kind"
        value={value.kind}
        options={TIME_SPEC_KINDS.map(({ kind, label }) => ({ value: kind, label }))}
        onChange={(kind) => { if (value.kind !== kind) onChange(defaultTimeSpec(kind)); }}
      />

      {value.kind === "absolute" && <AbsoluteFields value={value} onChange={onChange} siteTimeZone={siteTimeZone} />}

      {value.kind === "event" && (
        <EventOffsetFields
          id="timespec-event"
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

      {value.kind === "target_event" && (
        <TargetEventOffsetFields
          id="timespec-target-event"
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

      {value.kind === "relative" && (
        <div className="rounded-md border border-border bg-surface-raised p-3">
          <OffsetField
            label="Minutes after the previous item completes"
            value={value.offset_min}
            onChange={(offset_min) => {
              onChange({ ...value, offset_min });
            }}
          />
        </div>
      )}

      {value.kind === "asap" && (
        <p className="text-xs text-fg-muted">As soon as the previous enabled item completes.</p>
      )}
    </div>
  );
}

function AbsoluteFields({
  value,
  onChange,
  siteTimeZone,
}: {
  value: Extract<TimeSpec, { kind: "absolute" }>;
  onChange: (spec: TimeSpec) => void;
  siteTimeZone: string | undefined;
}) {
  const occurrences = wallClockOccurrences(value.local_datetime, siteTimeZone);
  const zoneNote = siteTimeZone ?? "this browser's zone";
  return (
    <div className="flex flex-col gap-2">
      <Field
        label="Local date and time"
        hint={`Site wall-clock time (${zoneNote}); the zone comes from Settings, not from the plan.`}
        type="datetime-local"
        value={toDatetimeLocalInput(value.local_datetime)}
        onChange={(e) => {
          if (e.target.value !== "") onChange({ ...value, local_datetime: e.target.value });
        }}
      />
      {occurrences === 0 && (
        <p className="rounded-md border border-warning bg-surface-raised px-2 py-1.5 text-xs text-warning">
          This wall-clock time does not exist at the site — it falls in the hour skipped when clocks go forward.
        </p>
      )}
      {occurrences !== 1 && occurrences !== 0 && (
        <div className="flex flex-col gap-1.5">
          <span className="text-sm font-medium text-fg-muted">Which occurrence</span>
          <p className="text-xs text-fg-muted">
            {occurrences === 2
              ? "Clocks go back that night, so this time happens twice."
              : "Could not check this time against the site's zone; say which occurrence you mean in case the clocks go back that night."}
          </p>
          <SegmentedControl
            label="Which occurrence"
            value={value.fold === 1 ? "second" : "first"}
            options={[
              { value: "first", label: "First (before clocks go back)" },
              { value: "second", label: "Second (after clocks go back)" },
            ]}
            onChange={(fold) => { onChange({ ...value, fold: fold === "second" ? 1 : 0 }); }}
          />
        </div>
      )}
    </div>
  );
}
