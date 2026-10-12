// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §4.3's item vocabulary as the editor sees it: the sixteen kinds
 * the generated `Plan["items"]` union enumerates, a label for each, a
 * factory for a fresh item of each, and — the part that keeps this from
 * becoming sixteen forms — a per-kind list of *field descriptors*. Every
 * descriptor reads and writes one field of the generated item type by
 * narrowing on `in`/`kind`, so a field name that is not in the schema fails
 * to typecheck rather than being invented here.
 *
 * Field names are the schema's, verbatim (`pa_offset_deg`, `overlap_pct`,
 * `frame_type`…). Nothing in this file knows what the device does with them.
 *
 * schema_version 2 (`ssc.plan.models`, 2026-09-10): a target-bearing item's
 * `target` is a discriminated `TargetSpec` — `{kind: "catalog", name,
 * ra_j2000_h, dec_j2000_deg}`, `{kind: "body", body}`, or a provenance-rich
 * `{kind: "comet", ...}` identity — never flat
 * `target_name`/`ra_j2000_h`/`dec_j2000_deg` on the item itself (those
 * survive server-side only as read-only properties, `null` for a body, and
 * are not part of the wire shape — DESIGN.md §3.6). The field list below is
 * therefore computed per item (`fieldsFor`), not a static per-kind table:
 * which fields apply depends on whether the item's own target is presently
 * a catalogue object or a body, not just on its `kind`.
 */
import type {
  BodyId,
  BodyTargetSpec,
  CatalogTargetSpec,
  ExposureFrameType,
  Plan,
  PlanItem,
  PlanItemKind,
  TargetSpec,
  TimeSpec,
} from "../../api/plans";

/** Every kind, in the order §4.3 lists them. The `satisfies` check makes a
 * kind added to the schema and forgotten here a type error. */
export const ITEM_KINDS = [
  "startup",
  "wait",
  "autofocus",
  "goto_and_stack",
  "take_exposures",
  "mosaic",
  "set_exposure",
  "set_gain",
  "filter",
  "heater",
  "dither",
  "solve",
  "sync",
  "slew",
  "find_home",
  "polar_alignment",
  "park",
  "unpark",
  "capture_calibration",
  "shutdown",
] as const satisfies readonly PlanItemKind[];

/** The reusable, outcome-oriented blocks shown first to ordinary operators.
 * Everything else remains available for imported/legacy plans and expert
 * hardware workflows, but no longer competes with these in one flat menu. */
export const NORMAL_ITEM_KINDS = [
  "polar_alignment",
  "take_exposures",
  "mosaic",
  "capture_calibration",
  "autofocus",
  "wait",
  "shutdown",
] as const satisfies readonly PlanItemKind[];

export const ADVANCED_ITEM_KINDS = ITEM_KINDS.filter(
  (kind): kind is Exclude<PlanItemKind, (typeof NORMAL_ITEM_KINDS)[number]> =>
    !(NORMAL_ITEM_KINDS as readonly PlanItemKind[]).includes(kind),
);

type Exhaustive = Exclude<PlanItemKind, (typeof ITEM_KINDS)[number]>;
// If this line fails to compile, `ITEM_KINDS` is missing a kind from the schema.
const _exhaustive: Exhaustive[] = [];
void _exhaustive;

export const ITEM_KIND_LABELS: Record<PlanItemKind, string> = {
  startup: "Start-up (level, dark, calibrate)",
  wait: "Wait / operator checkpoint",
  autofocus: "Autofocus",
  goto_and_stack: "Telescope GoTo + live stack",
  take_exposures: "Image target",
  mosaic: "Mosaic",
  set_exposure: "Set exposure",
  set_gain: "Set gain",
  filter: "Change filter",
  heater: "Dew heater",
  dither: "Dither",
  solve: "Plate solve",
  sync: "Sync",
  slew: "Slew",
  find_home: "Slew home",
  polar_alignment: "Polar alignment (automatic)",
  park: "Park",
  unpark: "Unpark",
  capture_calibration: "Calibration (matched set)",
  shutdown: "Safe shutdown",
};

// --- TargetSpec (DESIGN.md §3.6) --------------------------------------------

/** Every body selectable as a target — every `BodyId` except the Sun. The
 * Sun is refused server-side, at both the resolver and the plan-document
 * boundary (`BodyTargetSpec._refuse_the_sun`); it never belongs in this
 * list, so the editor cannot offer a control that would only ever 422
 * (DESIGN.md §3.6: "a hard exclusion..., not a confirmation dialog"). The
 * `satisfies` check makes a body added to `BodyId` and forgotten here a
 * type error, the same pattern as `ITEM_KINDS` above. */
export const TARGET_BODY_IDS = [
  "moon",
  "mercury",
  "venus",
  "mars",
  "jupiter",
  "saturn",
  "uranus",
  "neptune",
] as const satisfies readonly Exclude<BodyId, "sun">[];

type BodyExhaustive = Exclude<Exclude<BodyId, "sun">, (typeof TARGET_BODY_IDS)[number]>;
// If this line fails to compile, `TARGET_BODY_IDS` is missing a body from the schema.
const _bodyExhaustive: BodyExhaustive[] = [];
void _bodyExhaustive;

function bodyLabel(body: BodyId): string {
  return body.charAt(0).toUpperCase() + body.slice(1);
}

function newCatalogTarget(): CatalogTargetSpec {
  return { kind: "catalog", name: null, ra_j2000_h: 0, dec_j2000_deg: 0 };
}
function newBodyTarget(): BodyTargetSpec {
  return { kind: "body", body: "moon" };
}

/** The item's own target, whichever kind of item carries one — `undefined`
 * for a kind with no target at all, `null` for a `sync` with none set. */
export function getTarget(item: PlanItem): TargetSpec | null | undefined {
  switch (item.kind) {
    case "goto_and_stack":
    case "take_exposures":
    case "mosaic":
    case "slew":
    case "startup":
      return item.target;
    case "sync":
      return item.target;
    default:
      return undefined;
  }
}

/** Replace a target-bearing item's `target`, narrowing per kind so the
 * result is a real `PlanItem` member rather than a cast. A no-op for a kind
 * with no target field. */
export function setTarget(item: PlanItem, target: TargetSpec): PlanItem {
  switch (item.kind) {
    case "goto_and_stack":
    case "take_exposures":
    case "mosaic":
    case "slew":
    case "startup":
      return { ...item, target };
    case "sync":
      return { ...item, target };
    default:
      return item;
  }
}

/** A one-line description of a target, for the item summary and the
 * timeline row label — always visibly a body when it is one (DESIGN.md
 * §3.6: "a body result... showing it beside a fixed catalogue entry without
 * that distinction invites exactly the 'store the coordinates' mistake"). */
function targetSummary(target: TargetSpec | null | undefined): string {
  if (target === null || target === undefined) return "no target";
  if (target.kind === "body") return `${bodyLabel(target.body)} (body)`;
  if (target.kind === "comet") return `${target.full_name} (comet)`;
  if (target.name !== null && target.name !== undefined && target.name !== "") return target.name;
  return `${target.ra_j2000_h.toFixed(3)} h, ${target.dec_j2000_deg.toFixed(2)}°`;
}

/** Kinds that carry their own target, and so can use a `target_event`
 * start (`ssc.plan.models.TargetEventTimeSpec`'s own docstring). `sync`
 * only counts with a `target` actually set — checked on the item, not
 * here. */
export function itemHasTarget(item: PlanItem): boolean {
  switch (item.kind) {
    case "goto_and_stack":
    case "mosaic":
    case "take_exposures":
      return true;
    case "slew":
      return (item.destination ?? "target") === "target" && item.target != null;
    case "sync":
      return item.target !== null && item.target !== undefined;
    default:
      return false;
  }
}

/** A fresh item of `kind`. Numeric fields the schema requires get a real
 * value the user is expected to change (RA/Dec 0/0 is a place on the sky,
 * not a guess about the user's target — it is visibly wrong until edited).
 * Optional ones stay `null`: `None` means unknown, never zero (architecture policy). */
export function newItem(kind: PlanItemKind, id: string, start: TimeSpec): PlanItem {
  const base = { id, start, end: null, enabled: true };
  switch (kind) {
    case "startup":
      return { ...base, kind, target: null, polar_point_count: 3, polar_step_deg: 30, polar_exposure_s: 2, polar_gain: 200, polar_start_point: "best", target_ra_hours: null, target_dec_deg: null };
    case "wait":
      return { ...base, kind, requires_confirmation: true, instructions: "Review the physical adjustment, then Resume to accept and continue." };
    case "autofocus":
      return { ...base, kind, temperature_trigger_delta_c: null };
    case "goto_and_stack":
      return {
        ...base,
        kind,
        target: newCatalogTarget(),
        framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
        constraints: { min_alt_deg: null, min_moon_sep_deg: null },
        autofocus_before: false,
        live_stack: true,
        exposure_sec: 60,
        gain: 200,
        filter_name: null,
        dither: { pixels: null, min_pixels: 15, max_pixels: 200, every_n_frames: 6 },
      };
    case "take_exposures":
      // A fixed count of 60 frames at 60 s / gain 200 is a real value the
      // user is expected to change (not "0 frames", which would silently
      // sit in a plan doing nothing) — the same rule the RA/Dec 0/0 comment
      // above states for a target. No filter and Light are the server's own
      // defaults (`TakeExposuresItem.filter_name`/`frame_type`), surfaced
      // here rather than hidden: `filter_name: null` immediately earns the
      // server's own "sets no filter" warning instead of masking it.
      return {
        ...base,
        kind,
        target: newCatalogTarget(),
        count: { kind: "fixed", frames: 60 },
        exposure_sec: 60,
        gain: 200,
        filter_name: null,
        dither: { pixels: null, min_pixels: 15, max_pixels: 200, every_n_frames: 6 },
        frame_type: "light",
        center_after_drift: null,
        framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
        constraints: { min_alt_deg: null, min_moon_sep_deg: null },
        live_stack: true,
      };
    case "mosaic":
      return {
        ...base,
        kind,
        live_stack: true,
        target: newCatalogTarget(),
        framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
        mosaic: { ra_num: 2, dec_num: 2, overlap_pct: 20 },
        constraints: { min_alt_deg: null, min_moon_sep_deg: null },
        exposure_sec: 60,
        gain: 200,
        filter_name: null,
        dither: { pixels: null, min_pixels: 15, max_pixels: 200, every_n_frames: 6 },
      };
    case "set_exposure":
      return { ...base, kind, exposure_sec: 60 };
    case "set_gain":
      return { ...base, kind, gain: 200 };
    case "filter":
      return { ...base, kind, position: "" };
    case "heater":
      return { ...base, kind, heater_on: true };
    case "dither":
      return { ...base, kind, dither_pixels: null };
    case "solve":
      return { ...base, kind };
    case "sync":
      return { ...base, kind, target: null };
    case "slew":
      return { ...base, kind, target: newCatalogTarget(), destination: "target", unpark_before: false };
    case "find_home":
      return { ...base, kind, park_after: false };
    case "polar_alignment":
      return { ...base, kind, point_count: 3, step_deg: 30, exposure_s: 2, gain: 200, start_point: "best", start_ra_hours: null, start_dec_deg: null };
    case "park":
      return { ...base, kind };
    case "unpark":
      return { ...base, kind };
    case "capture_calibration":
      return { ...base, kind, frame_type: "dark", count: 50, exposure_sec: 60, gain: 200, filter_name: "Dark", optic_id: "tele", flat_source: null, generate_master: true, safe_state: true };
    case "shutdown":
      return { ...base, kind };
  }
}

/** A one-line summary for the item list: the target, or the one number that
 * distinguishes this item from another of the same kind. */
export function itemSummary(item: PlanItem): string {
  switch (item.kind) {
    case "goto_and_stack":
    case "mosaic":
      return targetSummary(item.target);
    case "slew":
      if ((item.destination ?? "target") === "polar_best") return "Polar-alignment best field";
      return targetSummary(item.target);
    case "sync":
      return targetSummary(item.target);
    case "take_exposures": {
      const count = item.count.kind === "fixed" ? `${String(item.count.frames)} ×` : "until block end,";
      const filter = item.filter_name === null || item.filter_name === undefined || item.filter_name === "" ? "" : ` through ${item.filter_name}`;
      return `${targetSummary(item.target)} · ${count} ${String(item.exposure_sec)} s ${item.frame_type}${filter}, gain ${String(item.gain)}${item.live_stack ? " · live stack" : ""}`;
    }
    case "set_exposure":
      return `${String(item.exposure_sec)} s`;
    case "set_gain":
      return `gain ${String(item.gain)}`;
    case "filter":
      return item.position === "" ? "no position" : item.position;
    case "heater":
      return item.heater_on ? "on" : "off";
    case "dither":
      return item.dither_pixels === null || item.dither_pixels === undefined ? "" : `${String(item.dither_pixels)} px`;
    case "capture_calibration":
      return item.count === null ? item.frame_type : `${String(item.count)} × ${item.frame_type}`;
    case "autofocus":
      return item.temperature_trigger_delta_c === null || item.temperature_trigger_delta_c === undefined
        ? ""
        : `Δ${String(item.temperature_trigger_delta_c)} °C`;
    default:
      return "";
  }
}

// --- the per-kind field map ------------------------------------------------ //

export type FieldValue = string | number | boolean | null;

interface FieldBase {
  /** Stable key for React and for `<label htmlFor>`. */
  key: string;
  label: string;
  hint?: string;
  read: (item: PlanItem) => FieldValue;
  write: (item: PlanItem, value: FieldValue) => PlanItem;
}
export interface TextFieldDef extends FieldBase {
  type: "text";
  /** Empty string is stored as `null` rather than `""`. */
  nullable?: boolean;
}
export interface NumberFieldDef extends FieldBase {
  type: "number";
  step?: number;
  min?: number;
  max?: number;
  unit?: string;
  /** Cleared input is stored as `null` ("no limit requested"), never 0. */
  nullable?: boolean;
}
export interface BooleanFieldDef extends FieldBase {
  type: "boolean";
  onLabel: string;
  offLabel: string;
}
export interface SelectFieldDef extends FieldBase {
  type: "select";
  options?: readonly { value: string; label: string }[];
  /** Options come from connected-device/profile state at render time. */
  dynamic?: "lens" | "filter";
  /** Offer an explicit no-change/unset choice for optional device state. */
  allowUnset?: boolean;
}
export type FieldDef = TextFieldDef | NumberFieldDef | BooleanFieldDef | SelectFieldDef;

const num = (v: FieldValue): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: FieldValue): string => (typeof v === "string" ? v : "");
const supportsDither = (
  item: PlanItem,
): item is Extract<PlanItem, { kind: "goto_and_stack" | "mosaic" | "take_exposures" }> =>
  item.kind === "goto_and_stack" || item.kind === "mosaic" || item.kind === "take_exposures";

/** Which target reference kind an item presently carries — for `sync`,
 * whether it has one at all. Drives `fieldsFor` below: the RA/Dec/name
 * fields only make sense for a catalogue target, the body picker only for
 * a body one, and neither for a `sync` with no target set. */
const targetKindRequired: SelectFieldDef = {
  key: "target.kind",
  type: "select",
  label: "Target type",
  options: [
    { value: "catalog", label: "Catalogue object" },
    { value: "body", label: "Solar-system body" },
    { value: "comet", label: "Comet (from Planning)" },
  ],
  read: (item) => getTarget(item)?.kind ?? null,
  write: (item, v) => {
    if (v === "comet") return item;
    return setTarget(item, v === "body" ? newBodyTarget() : newCatalogTarget());
  },
};

/** As `targetKindRequired`, but for `sync`'s optional target — a third
 * option, "current pointing", writes `target: null` (sync to the last
 * plate solve rather than a named target). */
const targetKindOptional: SelectFieldDef = {
  key: "target.kind",
  type: "select",
  label: "Target type",
  options: [
    { value: "none", label: "Current pointing (no target)" },
    { value: "catalog", label: "Catalogue object" },
    { value: "body", label: "Solar-system body" },
    { value: "comet", label: "Comet (from Planning)" },
  ],
  hint: "Leave as current pointing to sync at wherever the scope is pointing.",
  read: (item) => (item.kind === "sync" ? (item.target?.kind ?? "none") : null),
  write: (item, v) => {
    if (item.kind !== "sync") return item;
    if (v === "body") return { ...item, target: newBodyTarget() };
    if (v === "catalog") return { ...item, target: newCatalogTarget() };
    if (v === "comet") return item;
    return { ...item, target: null };
  },
};

const targetName: TextFieldDef = {
  key: "target.name",
  type: "text",
  label: "Target name",
  nullable: true,
  read: (item) => {
    const target = getTarget(item);
    return target?.kind === "catalog" ? (target.name ?? null) : null;
  },
  write: (item, v) => {
    const target = getTarget(item);
    if (target?.kind !== "catalog") return item;
    return setTarget(item, { ...target, name: str(v) === "" ? null : str(v) });
  },
};

/** RA/Dec for a catalogue target — required by `CatalogTargetSpec` itself. */
const raRequired: NumberFieldDef = {
  key: "target.ra_j2000_h",
  type: "number",
  label: "RA (J2000)",
  unit: "h",
  step: 0.0001,
  min: 0,
  max: 23.9999,
  read: (item) => {
    const target = getTarget(item);
    return target?.kind === "catalog" ? target.ra_j2000_h : null;
  },
  write: (item, v) => {
    const target = getTarget(item);
    if (target?.kind !== "catalog") return item;
    return setTarget(item, { ...target, ra_j2000_h: num(v) ?? 0 });
  },
};
const decRequired: NumberFieldDef = {
  key: "target.dec_j2000_deg",
  type: "number",
  label: "Dec (J2000)",
  unit: "°",
  step: 0.0001,
  min: -90,
  max: 90,
  read: (item) => {
    const target = getTarget(item);
    return target?.kind === "catalog" ? target.dec_j2000_deg : null;
  },
  write: (item, v) => {
    const target = getTarget(item);
    if (target?.kind !== "catalog") return item;
    return setTarget(item, { ...target, dec_j2000_deg: num(v) ?? 0 });
  },
};

/** The body picker — a closed `<select>`, never free text, so the Sun is
 * excluded by not being an option rather than by a rejected keystroke
 * (DESIGN.md §3.6). */
const targetBody: SelectFieldDef = {
  key: "target.body",
  type: "select",
  label: "Body",
  options: TARGET_BODY_IDS.map((body) => ({ value: body, label: bodyLabel(body) })),
  hint: "Its position is computed fresh when this item runs, never stored — it moves between now and then.",
  read: (item) => {
    const target = getTarget(item);
    return target?.kind === "body" ? target.body : null;
  },
  write: (item, v) => {
    const target = getTarget(item);
    if (target?.kind !== "body") return item;
    const body = (TARGET_BODY_IDS as readonly string[]).includes(v as string) ? (v as BodyId) : "moon";
    return setTarget(item, { kind: "body", body });
  },
};

const framingMode: SelectFieldDef = {
  key: "framing.mode",
  type: "select",
  label: "Framing mode",
  options: [
    { value: "altaz", label: "Alt-az" },
    { value: "eq", label: "Equatorial" },
  ],
  read: (it) => ("framing" in it ? it.framing.mode : null),
  write: (it, v) => ("framing" in it ? { ...it, framing: { ...it.framing, mode: v === "eq" ? "eq" : "altaz" } } : it),
};
const framingPa: NumberFieldDef = {
  key: "framing.pa_offset_deg",
  type: "number",
  label: "PA offset",
  unit: "°",
  step: 0.1,
  read: (it) => ("framing" in it ? it.framing.pa_offset_deg : null),
  write: (it, v) => ("framing" in it ? { ...it, framing: { ...it.framing, pa_offset_deg: num(v) ?? 0 } } : it),
};
const framingLens: SelectFieldDef = {
  key: "framing.lens",
  type: "select",
  label: "Lens",
  dynamic: "lens",
  read: (it) => ("framing" in it ? it.framing.lens : null),
  write: (it, v) => ("framing" in it ? { ...it, framing: { ...it.framing, lens: str(v) } } : it),
};
const mosaicRa: NumberFieldDef = {
  key: "mosaic.ra_num",
  type: "number",
  label: "RA panels",
  step: 1,
  min: 1,
  max: 12,
  read: (it) => (it.kind === "mosaic" ? it.mosaic.ra_num : null),
  write: (it, v) => (it.kind === "mosaic" ? { ...it, mosaic: { ...it.mosaic, ra_num: num(v) ?? 1 } } : it),
};
const mosaicDec: NumberFieldDef = {
  key: "mosaic.dec_num",
  type: "number",
  label: "Dec panels",
  step: 1,
  min: 1,
  max: 12,
  read: (it) => (it.kind === "mosaic" ? it.mosaic.dec_num : null),
  write: (it, v) => (it.kind === "mosaic" ? { ...it, mosaic: { ...it.mosaic, dec_num: num(v) ?? 1 } } : it),
};
const mosaicOverlap: NumberFieldDef = {
  key: "mosaic.overlap_pct",
  type: "number",
  label: "Overlap",
  unit: "%",
  step: 1,
  min: 0,
  max: 90,
  read: (it) => (it.kind === "mosaic" ? it.mosaic.overlap_pct : null),
  write: (it, v) => (it.kind === "mosaic" ? { ...it, mosaic: { ...it.mosaic, overlap_pct: num(v) ?? 0 } } : it),
};
const autofocusBefore: BooleanFieldDef = {
  key: "autofocus_before",
  type: "boolean",
  label: "Autofocus before capture",
  onLabel: "Autofocus",
  offLabel: "No autofocus",
  hint: "Slew to this panel first, then autofocus before live stacking starts.",
  read: (it) => (it.kind === "goto_and_stack" ? it.autofocus_before : null),
  write: (it, value) => (it.kind === "goto_and_stack" ? { ...it, autofocus_before: value === true } : it),
};
const minAlt: NumberFieldDef = {
  key: "constraints.min_alt_deg",
  type: "number",
  label: "Minimum altitude",
  unit: "°",
  step: 1,
  min: 0,
  max: 90,
  nullable: true,
  hint: "Blank means no limit requested.",
  read: (it) => ("constraints" in it ? (it.constraints.min_alt_deg ?? null) : null),
  write: (it, v) => ("constraints" in it ? { ...it, constraints: { ...it.constraints, min_alt_deg: num(v) } } : it),
};
const minMoonSep: NumberFieldDef = {
  key: "constraints.min_moon_sep_deg",
  type: "number",
  label: "Minimum moon separation",
  unit: "°",
  step: 1,
  min: 0,
  max: 180,
  nullable: true,
  read: (it) => ("constraints" in it ? (it.constraints.min_moon_sep_deg ?? null) : null),
  write: (it, v) => ("constraints" in it ? { ...it, constraints: { ...it.constraints, min_moon_sep_deg: num(v) } } : it),
};
const exposureSec: NumberFieldDef = {
  key: "exposure_sec",
  type: "number",
  label: "Exposure",
  unit: "s",
  step: 0.1,
  min: 0,
  read: (it) => (it.kind === "set_exposure" ? it.exposure_sec : null),
  write: (it, v) => (it.kind === "set_exposure" ? { ...it, exposure_sec: num(v) ?? 0 } : it),
};
const gain: NumberFieldDef = {
  key: "gain",
  type: "number",
  label: "Gain",
  step: 1,
  min: 0,
  read: (it) => (it.kind === "set_gain" ? it.gain : null),
  write: (it, v) => (it.kind === "set_gain" ? { ...it, gain: num(v) ?? 0 } : it),
};
const filterPosition: SelectFieldDef = {
  key: "position",
  type: "select",
  label: "Filter",
  dynamic: "filter",
  hint: "Choose a filter-wheel position.",
  read: (it) => (it.kind === "filter" ? it.position : null),
  write: (it, v) => (it.kind === "filter" ? { ...it, position: str(v) } : it),
};
const slewUnparkBefore: BooleanFieldDef = {
  key: "unpark_before",
  type: "boolean",
  label: "Unpark before slew",
  onLabel: "Yes",
  offLabel: "No",
  read: (it) => (it.kind === "slew" ? it.unpark_before : null),
  write: (it, v) => (it.kind === "slew" ? { ...it, unpark_before: v === true } : it),
};
const slewDestination: SelectFieldDef = {
  key: "destination",
  type: "select",
  label: "Destination",
  options: [
    { value: "target", label: "Catalog or manual target" },
    { value: "polar_best", label: "Polar-alignment best field" },
  ],
  read: (it) => (it.kind === "slew" ? (it.destination ?? "target") : null),
  write: (it, v) => (it.kind === "slew" ? { ...it, destination: v === "polar_best" ? "polar_best" : "target" } : it),
};
const homeParkAfter: BooleanFieldDef = {
  key: "park_after",
  type: "boolean",
  label: "Park after home",
  onLabel: "Yes",
  offLabel: "No",
  read: (it) => (it.kind === "find_home" ? it.park_after : null),
  write: (it, v) => (it.kind === "find_home" ? { ...it, park_after: v === true } : it),
};
const polarPointCount: NumberFieldDef = { key: "point_count", type: "number", label: "Points", min: 3, step: 1, read: (it) => it.kind === "polar_alignment" ? it.point_count : null, write: (it, v) => it.kind === "polar_alignment" ? { ...it, point_count: num(v) ?? 3 } : it };
const polarStep: NumberFieldDef = { key: "step_deg", type: "number", label: "Separation", unit: "°", min: 1, step: 1, read: (it) => it.kind === "polar_alignment" ? it.step_deg : null, write: (it, v) => it.kind === "polar_alignment" ? { ...it, step_deg: num(v) ?? 30 } : it };
const polarExposure: NumberFieldDef = { key: "exposure_s", type: "number", label: "Exposure", unit: "s", min: 0.1, step: 0.1, read: (it) => it.kind === "polar_alignment" ? it.exposure_s : null, write: (it, v) => it.kind === "polar_alignment" ? { ...it, exposure_s: num(v) ?? 2 } : it };
const polarGain: NumberFieldDef = { key: "gain", type: "number", label: "Gain", min: 0, step: 1, read: (it) => it.kind === "polar_alignment" ? it.gain : null, write: (it, v) => it.kind === "polar_alignment" ? { ...it, gain: num(v) ?? 200 } : it };
const polarStartPoint: SelectFieldDef = { key: "start_point", type: "select", label: "Start position", options: [{ value: "best", label: "Best field" }, { value: "seestar", label: "Seestar field" }, { value: "current", label: "Current position" }, { value: "explicit", label: "Selected target" }], read: (it) => it.kind === "polar_alignment" ? it.start_point : null, write: (it, v) => it.kind === "polar_alignment" ? { ...it, start_point: v === "current" || v === "seestar" || v === "explicit" ? v : "best" } : it };
const polarRa: NumberFieldDef = { key: "start_ra_hours", type: "number", label: "Target RA", unit: "h", nullable: true, min: 0, max: 24, read: (it) => it.kind === "polar_alignment" ? (it.start_ra_hours ?? null) : null, write: (it, v) => it.kind === "polar_alignment" ? { ...it, start_ra_hours: num(v) } : it };
const polarDec: NumberFieldDef = { key: "start_dec_deg", type: "number", label: "Target Dec", unit: "°", nullable: true, min: -90, max: 90, read: (it) => it.kind === "polar_alignment" ? (it.start_dec_deg ?? null) : null, write: (it, v) => it.kind === "polar_alignment" ? { ...it, start_dec_deg: num(v) } : it };
const startupPointCount: NumberFieldDef = { key: "polar_point_count", type: "number", label: "PA points", min: 3, step: 1, read: (it) => it.kind === "startup" ? (it.polar_point_count ?? 3) : null, write: (it, v) => it.kind === "startup" ? { ...it, polar_point_count: num(v) ?? 3 } : it };
const startupStep: NumberFieldDef = { key: "polar_step_deg", type: "number", label: "PA separation", unit: "°", min: 1, step: 1, read: (it) => it.kind === "startup" ? (it.polar_step_deg ?? 30) : null, write: (it, v) => it.kind === "startup" ? { ...it, polar_step_deg: num(v) ?? 30 } : it };
const startupExposure: NumberFieldDef = { key: "polar_exposure_s", type: "number", label: "PA exposure", unit: "s", min: 0.1, step: 0.1, read: (it) => it.kind === "startup" ? (it.polar_exposure_s ?? 2) : null, write: (it, v) => it.kind === "startup" ? { ...it, polar_exposure_s: num(v) ?? 2 } : it };
const startupGain: NumberFieldDef = { key: "polar_gain", type: "number", label: "PA gain", min: 0, step: 1, read: (it) => it.kind === "startup" ? (it.polar_gain ?? 200) : null, write: (it, v) => it.kind === "startup" ? { ...it, polar_gain: num(v) ?? 200 } : it };
const startupStartPoint: SelectFieldDef = { key: "polar_start_point", type: "select", label: "PA start", options: [{ value: "best", label: "Best field" }, { value: "seestar", label: "Seestar field" }, { value: "current", label: "Current position" }, { value: "explicit", label: "Manual target" }], read: (it) => it.kind === "startup" ? (it.polar_start_point ?? "best") : null, write: (it, v) => it.kind === "startup" ? { ...it, polar_start_point: v === "current" || v === "seestar" || v === "explicit" ? v : "best" } : it };
const startupRa: NumberFieldDef = { key: "target_ra_hours", type: "number", label: "PA target RA", unit: "h", nullable: true, min: 0, max: 24, read: (it) => it.kind === "startup" ? (it.target_ra_hours ?? null) : null, write: (it, v) => it.kind === "startup" ? { ...it, target_ra_hours: num(v) } : it };
const startupDec: NumberFieldDef = { key: "target_dec_deg", type: "number", label: "PA target Dec", unit: "°", nullable: true, min: -90, max: 90, read: (it) => it.kind === "startup" ? (it.target_dec_deg ?? null) : null, write: (it, v) => it.kind === "startup" ? { ...it, target_dec_deg: num(v) } : it };
const heaterOn: BooleanFieldDef = {
  key: "heater_on",
  type: "boolean",
  label: "Dew heater",
  onLabel: "On",
  offLabel: "Off",
  read: (it) => (it.kind === "heater" ? it.heater_on : null),
  write: (it, v) => (it.kind === "heater" ? { ...it, heater_on: v === true } : it),
};
const ditherPixels: NumberFieldDef = {
  key: "dither_pixels",
  type: "number",
  label: "Dither",
  unit: "px",
  step: 1,
  min: 0,
  nullable: true,
  hint: "Blank keeps the device's own setting.",
  read: (it) => (it.kind === "dither" ? (it.dither_pixels ?? null) : null),
  write: (it, v) => (it.kind === "dither" ? { ...it, dither_pixels: num(v) } : it),
};
const autofocusDelta: NumberFieldDef = {
  key: "temperature_trigger_delta_c",
  type: "number",
  label: "Temperature trigger",
  unit: "°C",
  step: 0.5,
  min: 0,
  nullable: true,
  hint: "Refocus when the temperature has moved this much; blank means on the clock only.",
  read: (it) => (it.kind === "autofocus" ? (it.temperature_trigger_delta_c ?? null) : null),
  write: (it, v) => (it.kind === "autofocus" ? { ...it, temperature_trigger_delta_c: num(v) } : it),
};
const frameType: SelectFieldDef = {
  key: "frame_type",
  type: "select",
  label: "Frame type",
  options: [
    { value: "bias", label: "Bias" },
    { value: "dark", label: "Dark" },
    { value: "flat", label: "Flat" },
  ],
  read: (it) => (it.kind === "capture_calibration" ? it.frame_type : null),
  write: (it, v) =>
    it.kind === "capture_calibration"
      ? { ...it, frame_type: v === "bias" || v === "flat" ? v : "dark" }
      : it,
};
const frameCount: NumberFieldDef = {
  key: "count",
  type: "number",
  label: "Frame count",
  step: 1,
  min: 1,
  nullable: true,
  read: (it) => (it.kind === "capture_calibration" ? (it.count ?? null) : null),
  write: (it, v) => (it.kind === "capture_calibration" ? { ...it, count: num(v) } : it),
};

// --- take_exposures (DESIGN.md §4.3.0) --------------------------------- //

/** Fixed count vs. until-block-end (`ExposureCount`, §4.3.0) — a visible
 * either/or, the same discriminated-switch shape as `targetKindRequired`
 * above, not a checkbox modifying `exposureFrames`. Switching kind always
 * starts `frames` fresh at 60 rather than trying to remember a prior value,
 * matching `targetKindRequired`'s own precedent. */
const exposureCountKind: SelectFieldDef = {
  key: "count.kind",
  type: "select",
  label: "How many frames",
  options: [
    { value: "fixed", label: "Fixed count" },
    { value: "until_block_end", label: "Until the block ends" },
  ],
  hint: "“Until the block ends” needs this item to have its own end (duration, or an event) — without one there is no window to fill.",
  read: (it) => (it.kind === "take_exposures" ? it.count.kind : null),
  write: (it, v) =>
    it.kind === "take_exposures"
      ? { ...it, count: v === "until_block_end" ? { kind: "until_block_end" } : { kind: "fixed", frames: 60 } }
      : it,
};
const exposureFrames: NumberFieldDef = {
  key: "count.frames",
  type: "number",
  label: "Frame count",
  step: 1,
  min: 1,
  read: (it) => (it.kind === "take_exposures" && it.count.kind === "fixed" ? it.count.frames : null),
  write: (it, v) =>
    it.kind === "take_exposures" && it.count.kind === "fixed" ? { ...it, count: { kind: "fixed", frames: num(v) ?? 1 } } : it,
};
const blockExposureSec: NumberFieldDef = {
  key: "exposure_sec",
  type: "number",
  label: "Exposure",
  unit: "s",
  step: 0.1,
  min: 0.1,
  read: (it) => (it.kind === "take_exposures" || it.kind === "goto_and_stack" || it.kind === "mosaic" ? it.exposure_sec : null),
  write: (it, v) =>
    it.kind === "take_exposures" || it.kind === "goto_and_stack" || it.kind === "mosaic"
      ? { ...it, exposure_sec: num(v) ?? it.exposure_sec }
      : it,
};
const blockGain: NumberFieldDef = {
  key: "gain",
  type: "number",
  label: "Gain",
  step: 1,
  min: 0,
  read: (it) => (it.kind === "take_exposures" || it.kind === "goto_and_stack" || it.kind === "mosaic" ? it.gain : null),
  write: (it, v) =>
    it.kind === "take_exposures" || it.kind === "goto_and_stack" || it.kind === "mosaic"
      ? { ...it, gain: num(v) ?? it.gain }
      : it,
};
const filterName: SelectFieldDef = {
  key: "filter_name",
  type: "select",
  label: "Filter",
  dynamic: "filter",
  allowUnset: true,
  hint: "Choose a filter-wheel position. Left unchanged, this block images through the position already in place.",
  read: (it) => (it.kind === "take_exposures" || it.kind === "goto_and_stack" || it.kind === "mosaic" ? (it.filter_name ?? null) : null),
  write: (it, v) =>
    it.kind === "take_exposures" || it.kind === "goto_and_stack" || it.kind === "mosaic"
      ? { ...it, filter_name: str(v) === "" ? null : str(v) }
      : it,
};
/** A manual choice, never inferred (owner, 2026-09-10: "that would be
 * manual setting - when you go capture, you select which ones you're gonna
 * create"). Every kind `FrameType` actually defines is offered, including
 * flat and bias even though nothing captures them via a wizard yet — the
 * block itself does not know that, and hiding an option here would be this
 * file inventing a restriction the schema doesn't have. */
const exposureFrameType: SelectFieldDef = {
  key: "frame_type",
  type: "select",
  label: "Frame type",
  options: [
    { value: "light", label: "Light" },
    { value: "dark", label: "Dark" },
    { value: "flat", label: "Flat" },
    { value: "bias", label: "Bias" },
  ],
  read: (it) => (it.kind === "take_exposures" ? it.frame_type : null),
  write: (it, v) => {
    if (it.kind !== "take_exposures") return it;
    const options: readonly ExposureFrameType[] = ["light", "dark", "flat", "bias"];
    const frame_type = (options as readonly string[]).includes(v as string) ? (v as ExposureFrameType) : it.frame_type;
    return { ...it, frame_type };
  },
};
/** Dithering is optional on each imaging item (`dither: DitherSpec
 * | None`) — an on/off toggle for the whole nested spec, the same shape as
 * `targetKindOptional`'s "no target" option, rather than a lone nullable
 * number standing in for "off". */
const ditherEnabled: BooleanFieldDef = {
  key: "dither.enabled",
  type: "boolean",
  label: "Dithering",
  onLabel: "On",
  offLabel: "Off",
  read: (it) => (supportsDither(it) ? it.dither !== null && it.dither !== undefined : null),
  write: (it, v) => (supportsDither(it) ? { ...it, dither: v === true ? { pixels: null, min_pixels: 15, max_pixels: 200, every_n_frames: 6 } : null } : it),
};
const blockDitherMinPixels: NumberFieldDef = {
  key: "dither.min_pixels",
  type: "number",
  label: "Dither from",
  unit: "px",
  step: 1,
  min: 0,
  read: (it) => (supportsDither(it) && it.dither != null ? (it.dither.min_pixels ?? it.dither.pixels ?? null) : null),
  write: (it, v) => (supportsDither(it) && it.dither != null ? { ...it, dither: { ...it.dither, pixels: null, min_pixels: num(v) } } : it),
};
const blockDitherPixels: NumberFieldDef = {
  key: "dither.pixels",
  type: "number",
  label: "Dither amount",
  unit: "px",
  step: 1,
  min: 0,
  nullable: true,
  hint: "Legacy fixed amount from an older plan; new plans use a range.",
  read: (it) => (supportsDither(it) && it.dither != null ? it.dither.pixels ?? null : null),
  write: (it, v) => (supportsDither(it) && it.dither != null ? { ...it, dither: { ...it.dither, pixels: num(v) } } : it),
};
const blockDitherMaxPixels: NumberFieldDef = {
  key: "dither.max_pixels",
  type: "number",
  label: "Dither to",
  unit: "px",
  step: 1,
  min: 0,
  read: (it) => (supportsDither(it) && it.dither != null ? (it.dither.max_pixels ?? it.dither.pixels ?? null) : null),
  write: (it, v) => (supportsDither(it) && it.dither != null ? { ...it, dither: { ...it.dither, pixels: null, max_pixels: num(v) } } : it),
};
const blockDitherEveryNFrames: NumberFieldDef = {
  key: "dither.every_n_frames",
  type: "number",
  label: "Dither every",
  unit: "frames",
  step: 1,
  min: 1,
  read: (it) => (supportsDither(it) && it.dither != null ? it.dither.every_n_frames : null),
  write: (it, v) =>
    supportsDither(it) && it.dither != null ? { ...it, dither: { ...it.dither, every_n_frames: num(v) ?? 6 } } : it,
};
const centerAfterDrift: BooleanFieldDef = {
  key: "center_after_drift",
  type: "boolean",
  label: "Center after drift",
  onLabel: "On",
  offLabel: "Off",
  read: (it) => (it.kind === "take_exposures" ? it.center_after_drift != null : null),
  write: (it, v) => (it.kind === "take_exposures" ? { ...it, center_after_drift: v === true ? { enabled: true, threshold_pixels: it.center_after_drift?.threshold_pixels ?? 400 } : null } : it),
};
const driftThresholdPixels: NumberFieldDef = {
  key: "drift_threshold_pixels",
  type: "number",
  label: "Drift threshold",
  unit: "px",
  step: 1,
  min: 1,
  read: (it) => (it.kind === "take_exposures" ? it.center_after_drift?.threshold_pixels ?? null : null),
  write: (it, v) => (it.kind === "take_exposures" && it.center_after_drift != null ? { ...it, center_after_drift: { ...it.center_after_drift, threshold_pixels: num(v) ?? 400 } } : it),
};
const liveStackEnabled: BooleanFieldDef = {
  key: "live_stack",
  type: "boolean",
  label: "Live stacking",
  onLabel: "On",
  offLabel: "Off",
  read: (it) => supportsDither(it) ? it.live_stack : false,
  write: (it, v) => supportsDither(it) ? { ...it, live_stack: Boolean(v) } : it,
};
const waitConfirmation: BooleanFieldDef = {
  key: "requires_confirmation",
  type: "boolean",
  label: "Operator confirmation",
  onLabel: "Required",
  offLabel: "Timed wait only",
  read: (it) => it.kind === "wait" ? it.requires_confirmation : false,
  write: (it, v) => it.kind === "wait" ? { ...it, requires_confirmation: Boolean(v) } : it,
};
const waitInstructions: TextFieldDef = {
  key: "instructions",
  type: "text",
  label: "Operator instructions",
  nullable: true,
  read: (it) => it.kind === "wait" ? it.instructions ?? "" : "",
  write: (it, v) => it.kind === "wait" ? { ...it, instructions: str(v) || null } : it,
};
const calibrationExposure: NumberFieldDef = {
  key: "calibration.exposure_sec", type: "number", label: "Exposure", unit: "s", step: 0.1, min: 0.00001, nullable: true,
  read: (it) => it.kind === "capture_calibration" ? it.exposure_sec ?? null : null,
  write: (it, v) => it.kind === "capture_calibration" ? { ...it, exposure_sec: num(v) } : it,
};
const calibrationGain: NumberFieldDef = {
  key: "calibration.gain", type: "number", label: "Gain", step: 1, min: 0, nullable: true,
  read: (it) => it.kind === "capture_calibration" ? it.gain ?? null : null,
  write: (it, v) => it.kind === "capture_calibration" ? { ...it, gain: num(v) } : it,
};
const calibrationFilter: SelectFieldDef = {
  key: "calibration.filter_name", type: "select", label: "Filter", dynamic: "filter", allowUnset: true,
  read: (it) => it.kind === "capture_calibration" ? it.filter_name ?? "" : "",
  write: (it, v) => it.kind === "capture_calibration" ? { ...it, filter_name: str(v) || null } : it,
};
const calibrationOptic: TextFieldDef = {
  key: "calibration.optic_id", type: "text", label: "Optic", nullable: true,
  read: (it) => it.kind === "capture_calibration" ? it.optic_id ?? "" : "",
  write: (it, v) => it.kind === "capture_calibration" ? { ...it, optic_id: str(v) || null } : it,
};
const calibrationMaster: BooleanFieldDef = {
  key: "calibration.generate_master", type: "boolean", label: "Master", onLabel: "Create", offLabel: "Raw frames only",
  read: (it) => it.kind === "capture_calibration" ? it.generate_master : false,
  write: (it, v) => it.kind === "capture_calibration" ? { ...it, generate_master: Boolean(v) } : it,
};
const calibrationSafeState: BooleanFieldDef = {
  key: "calibration.safe_state", type: "boolean", label: "Safe dark transition", onLabel: "Required", offLabel: "Already safe",
  read: (it) => it.kind === "capture_calibration" ? it.safe_state : false,
  write: (it, v) => it.kind === "capture_calibration" ? { ...it, safe_state: Boolean(v) } : it,
};

const TARGET_FIELDS_CATALOG = [targetName, raRequired, decRequired] as const;
const TARGET_FIELDS_BODY = [targetBody] as const;
const FRAMING_FIELDS = [framingMode, framingPa, framingLens] as const;
const CONSTRAINT_FIELDS = [minAlt, minMoonSep] as const;

/** The catalogue/body-specific fields for one item's *current* target —
 * empty for a `sync` with no target set. */
function targetFields(target: TargetSpec | null | undefined): readonly FieldDef[] {
  if (target === null || target === undefined) return [];
  if (target.kind === "body") return TARGET_FIELDS_BODY;
  if (target.kind === "comet") return [];
  return TARGET_FIELDS_CATALOG;
}

/** The kind-specific fields for one item, in display order. `start`, `end`
 * and `enabled` are common to every kind and rendered by the editor itself.
 *
 * A function of the *item*, not a static per-kind table: a target-bearing
 * item's field list depends on whether its own target is presently a
 * catalogue object or a body (schema_version 2, DESIGN.md §3.6), which
 * static per-kind data (like `ITEM_KIND_LABELS`) cannot express. */
export function fieldsFor(item: PlanItem): readonly FieldDef[] {
  switch (item.kind) {
    case "startup":
      return [targetKindOptional, ...targetFields(item.target), startupPointCount, startupStep, startupExposure, startupGain, startupStartPoint, startupRa, startupDec];
    case "wait":
      return [waitConfirmation, ...(item.requires_confirmation ? [waitInstructions] : [])];
    case "autofocus":
      return [autofocusDelta];
    case "goto_and_stack":
      return [
        targetKindRequired,
        ...targetFields(item.target),
        ...FRAMING_FIELDS,
        autofocusBefore,
        blockExposureSec,
        blockGain,
        filterName,
        liveStackEnabled,
        ditherEnabled,
        ...(item.dither != null
          ? item.dither.min_pixels === undefined && item.dither.max_pixels === undefined
            ? [blockDitherPixels, blockDitherEveryNFrames]
            : [blockDitherMinPixels, blockDitherMaxPixels, blockDitherEveryNFrames]
          : []),
        ...CONSTRAINT_FIELDS,
      ];
    case "take_exposures":
      return [
        targetKindRequired,
        ...targetFields(item.target),
        ...FRAMING_FIELDS,
        ...CONSTRAINT_FIELDS,
        exposureCountKind,
        ...(item.count.kind === "fixed" ? [exposureFrames] : []),
        blockExposureSec,
        blockGain,
        filterName,
        exposureFrameType,
        liveStackEnabled,
        centerAfterDrift,
        ...(item.center_after_drift ? [driftThresholdPixels] : []),
        ditherEnabled,
        ...(item.dither != null
          ? item.dither.min_pixels === undefined && item.dither.max_pixels === undefined
            ? [blockDitherPixels, blockDitherEveryNFrames]
            : [blockDitherMinPixels, blockDitherMaxPixels, blockDitherEveryNFrames]
          : []),
      ];
    case "mosaic":
      return [
        targetKindRequired,
        ...targetFields(item.target),
        ...FRAMING_FIELDS,
        mosaicRa,
        mosaicDec,
        mosaicOverlap,
        blockExposureSec,
        blockGain,
        filterName,
        liveStackEnabled,
        ditherEnabled,
        ...(item.dither != null
          ? item.dither.min_pixels === undefined && item.dither.max_pixels === undefined
            ? [blockDitherPixels, blockDitherEveryNFrames]
            : [blockDitherMinPixels, blockDitherMaxPixels, blockDitherEveryNFrames]
          : []),
        ...CONSTRAINT_FIELDS,
      ];
    case "set_exposure":
      return [exposureSec];
    case "set_gain":
      return [gain];
    case "filter":
      return [filterPosition];
    case "heater":
      return [heaterOn];
    case "dither":
      return [ditherPixels];
    case "solve":
      return [];
    case "sync":
      return [targetKindOptional, ...targetFields(item.target)];
    case "slew":
      return [
        slewDestination,
        ...((item.destination ?? "target") === "polar_best"
          ? []
          : [targetKindRequired, ...targetFields(item.target)]),
        slewUnparkBefore,
      ];
    case "find_home":
      return [homeParkAfter];
    case "polar_alignment":
      return [polarPointCount, polarStep, polarExposure, polarGain, polarStartPoint, polarRa, polarDec];
    case "park":
      return [];
    case "unpark":
      return [];
    case "capture_calibration":
      return [frameType, frameCount, calibrationExposure, calibrationGain, calibrationFilter, calibrationOptic, calibrationMaster, calibrationSafeState];
    case "shutdown":
      return [];
  }
}

/** A fresh plan/item id: 32 hex chars, the same shape as the server's
 * `uuid4().hex`. `crypto.getRandomValues` rather than `crypto.randomUUID`,
 * which is secure-context only — and a phone reaching this app over a bare
 * LAN IP on http is not one (DESIGN.md §9). */
export function newId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** The start a freshly added item gets. The first item in a plan has no
 * predecessor to chain from, so it anchors on sunset — the one solar event
 * that is not a twilight opinion (DESIGN.md §4.1) — and everything after
 * it chains ASAP. Capture calibration is the exception §4.3 spells out:
 * "typically at nautical dawn", offered as the default, not the only choice. */
export function defaultStartFor(kind: PlanItemKind, isFirst: boolean): TimeSpec {
  if (kind === "capture_calibration") return { kind: "event", event: "nautical_dawn", offset_min: 0 };
  if (isFirst) return { kind: "event", event: "sunset", offset_min: 0 };
  return { kind: "asap" };
}

/** A brand-new, empty plan document. Timestamps are UTC ISO strings, as the
 * server requires (`Plan._require_aware_utc`). `schema_version` 7
 * (`CURRENT_PLAN_SCHEMA_VERSION`, `ssc.plan.models`) is the version that
 * carries `take_exposures` — an older build refuses a document stamped with
 * it, which is the point of the version, not a bug. */
export function newPlanDocument(name: string): Plan {
  const now = new Date().toISOString();
  return { schema_version: 7, id: newId(), name, items: [], created_at: now, updated_at: now };
}

/** A complete, editable reference night. Every safety- or image-critical
 * value is owned by its block; nothing depends on a setter hours earlier. */
export function newM31FirstLightPlan(): Plan {
  const plan = newPlanDocument("M31 first light");
  const m31: CatalogTargetSpec = { kind: "catalog", name: "M31", ra_j2000_h: 0.7123056, dec_j2000_deg: 41.269167 };
  const alignment = newItem("polar_alignment", newId(), { kind: "event", event: "nautical_dusk", offset_min: 0 });
  const checkpoint = newItem("wait", newId(), { kind: "asap" });
  const imaging = newItem("take_exposures", newId(), { kind: "asap" });
  const darks = newItem("capture_calibration", newId(), { kind: "event", event: "astronomical_dawn", offset_min: 0 });
  if (
    alignment.kind !== "polar_alignment" || checkpoint.kind !== "wait" ||
    imaging.kind !== "take_exposures" || darks.kind !== "capture_calibration"
  ) {
    throw new Error("scheduler item factory returned the wrong item kind");
  }
  return {
    ...plan,
    items: [
      {
        ...alignment,
        planning_estimate_min: 15,
        timeout_min: 30,
      },
      {
        ...checkpoint,
        requires_confirmation: true,
        instructions: "Use live polar refinement to adjust the mount physically. Resume only when the result is stable and acceptable; Resume is the durable acceptance.",
      },
      {
        ...imaging,
        target: m31,
        framing: { mode: "eq", pa_offset_deg: 0, lens: "tele" },
        count: { kind: "until_block_end" },
        end: { kind: "event", event: "astronomical_dawn", offset_min: 0 },
        exposure_sec: 60,
        gain: 200,
        filter_name: "LP",
        frame_type: "light",
        dither: { pixels: null, min_pixels: 30, max_pixels: 200, every_n_frames: 6 },
        center_after_drift: { enabled: true, threshold_pixels: 400 },
        live_stack: true,
      },
      {
        ...darks,
        frame_type: "dark",
        count: 50,
        exposure_sec: 60,
        gain: 200,
        filter_name: "Dark",
        optic_id: "tele",
        generate_master: true,
        safe_state: true,
        planning_estimate_min: 60,
        timeout_min: 75,
      },
      newItem("shutdown", newId(), { kind: "asap" }),
    ],
  };
}
