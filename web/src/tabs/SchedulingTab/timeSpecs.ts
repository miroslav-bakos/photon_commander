// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §4.1's `TimeSpec` vocabulary as the picker offers it: the five
 * kinds, every solar and lunar event the schema enumerates (all three
 * twilights, both directions — never narrowed to astronomical), the three
 * target events, and a valid seed for each kind. Pure data and functions;
 * `TimeSpecPicker.tsx` is the control over them.
 */
import type { EndSpec, EndSpecKind, LunarEvent, NightWindow, SolarEvent, TargetEvent, TimeSpec, TimeSpecKind } from "../../api/plans";
import { SOLAR_EVENT_LABELS } from "../PlanningTab/twilight";
import { formatClock, nowAsNaiveLocal } from "./localTime";

export const TIME_SPEC_KINDS: readonly { kind: TimeSpecKind; label: string }[] = [
  { kind: "absolute", label: "Absolute" },
  { kind: "event", label: "Sun / Moon event" },
  { kind: "target_event", label: "Target event" },
  { kind: "relative", label: "After previous" },
  { kind: "asap", label: "ASAP" },
];

/** Dusk → dawn order, then the two transits, then the Moon. A fixed order
 * so the list never reshuffles between renders. The `satisfies` clause
 * makes an event the schema adds and this list omits a type error. */
export const SOLAR_EVENTS = [
  "sunset",
  "civil_dusk",
  "nautical_dusk",
  "astronomical_dusk",
  "astronomical_midnight",
  "astronomical_dawn",
  "nautical_dawn",
  "civil_dawn",
  "sunrise",
  "solar_transit",
] as const satisfies readonly SolarEvent[];
export const LUNAR_EVENTS = ["moonrise", "moon_transit", "moonset"] as const satisfies readonly LunarEvent[];
// If either line fails to compile, the list above is missing an event the
// schema enumerates — the picker would silently narrow the choice (§4.1).
const _missingSolar: Exclude<SolarEvent, (typeof SOLAR_EVENTS)[number]>[] = [];
const _missingLunar: Exclude<LunarEvent, (typeof LUNAR_EVENTS)[number]>[] = [];
void _missingSolar;
void _missingLunar;

export const EVENT_LABELS: Record<SolarEvent | LunarEvent, string> = {
  ...SOLAR_EVENT_LABELS,
  astronomical_midnight: "Astronomical midnight",
  solar_transit: "Solar transit",
  moonrise: "Moonrise",
  moon_transit: "Moon transit",
  moonset: "Moonset",
};

export const TARGET_EVENT_LABELS: Record<TargetEvent, string> = {
  rises_above: "Rises above",
  transits: "Transits",
  sets_below: "Sets below",
};

/** Short forms for the axis's own event labels, used only when the full
 * label (`EVENT_LABELS`) does not fit the gap to the previous label on the
 * same row (`timelineGeometry.labelLayout`). Not every event needs one —
 * `EVENT_LABELS` is the fallback for anything omitted here. */
export const ABBREVIATED_EVENT_LABELS: Partial<Record<SolarEvent | LunarEvent, string>> = {
  civil_dusk: "Civil",
  nautical_dusk: "Naut.",
  astronomical_dusk: "Astro.",
  astronomical_midnight: "Astro. midnight",
  astronomical_dawn: "Astro.",
  nautical_dawn: "Naut.",
  civil_dawn: "Civil",
  solar_transit: "Transit",
  moon_transit: "Transit",
};

/** A fresh spec of `kind`, seeded so it is valid on the wire immediately.
 * `event` seeds with sunset — the one solar anchor that is not a twilight
 * opinion — and the user picks the twilight they mean from the full list. */
export function defaultTimeSpec(kind: TimeSpecKind): TimeSpec {
  switch (kind) {
    case "absolute":
      return { kind, local_datetime: nowAsNaiveLocal(), fold: 0 };
    case "event":
      return { kind, event: "sunset", offset_min: 0 };
    case "target_event":
      return { kind, event: "rises_above", alt_deg: 30, offset_min: 0 };
    case "relative":
      return { kind, offset_min: 0 };
    case "asap":
      return { kind };
  }
}

export const END_SPEC_KINDS: readonly { kind: EndSpecKind; label: string }[] = [
  { kind: "duration", label: "Duration" },
  { kind: "event", label: "Until event" },
  { kind: "target_event", label: "Until target event" },
];

/** A fresh `EndSpec` of `kind`, seeded so it is valid on the wire
 * immediately — the same convention as `defaultTimeSpec`. `duration`
 * seeds at 30 minutes, an estimate the user is expected to change, never
 * zero (architecture policy: `None` means unknown, never zero — and here there is
 * no "unknown" to fall back to, the field is being set to a real kind). */
export function defaultEndSpec(kind: EndSpecKind): EndSpec {
  switch (kind) {
    case "duration":
      return { kind, minutes: 30 };
    case "event":
      return { kind, event: "sunset", offset_min: 0 };
    case "target_event":
      return { kind, event: "sets_below", alt_deg: 30, offset_min: 0 };
  }
}

/**
 * `Astronomical dusk · 21:13` for the axis event dropdowns (DESIGN.md
 * §4.4's picker, not the axis itself): the event's own label, plus the
 * time it resolves to for the *selected night* — `night` is the compiled
 * schedule's own `NightWindow`, never recomputed here. `does not occur`
 * when the compiler says so, so the option stays selectable and honest
 * rather than silently vanishing. Plain names before anything has
 * compiled — `night` is `undefined` then, not guessed at.
 */
export function eventOptionLabel(event: SolarEvent | LunarEvent, night: NightWindow | undefined): string {
  const label = EVENT_LABELS[event];
  if (night === undefined) return label;
  const resolved = [...night.solar_events, ...night.lunar_events].find((e) => e.event === event);
  if (resolved === undefined) return label;
  if (resolved.outcome !== "occurs" || resolved.when_utc === null) return `${label} · does not occur`;
  return `${label} · ${formatClock(resolved.when_utc)}`;
}
