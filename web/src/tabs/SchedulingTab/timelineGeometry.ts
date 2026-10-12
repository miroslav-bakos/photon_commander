// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The Gantt strip's arithmetic (DESIGN.md §4.4), free of React and the DOM
 * so it is testable against fixtures rather than eyeballed:
 *
 * - the axis (dusk → dawn, padded, never wider than the compiled window),
 *   and the one time ↔ pixel mapping every row, tick, band and drag uses;
 * - the twilight bands as a darkness step-function over the resolved solar
 *   events, honouring `outcome` — a dusk that "does not occur" is not a
 *   blank, it changes which band reaches the edge;
 * - the moon-up intervals from the resolved lunar events;
 * - the snap targets (every resolved event, all three twilights first-class)
 *   and the snap itself, in pixels, so the radius is a finger not a minute;
 * - UTC → the site zone's naive wall clock with the DST `fold`, for the
 *   `absolute` TimeSpec a released drag becomes.
 *
 * Nothing here solves an ephemeris: every instant is one the compiler
 * already resolved. UTC in, UTC out — the local edge is `formatClock`.
 */
import type { CompiledItem, Diagnostic, LunarEvent, NightWindow, ResolvedEvent, Severity, SolarEvent } from "../../api/plans";
import type { Outcome } from "../../api/astro";
import { SOLAR_EVENT_LABELS, outcomeMessage, type SolarEventName } from "../PlanningTab/twilight";
import { EVENT_LABELS } from "./timeSpecs";

export interface AxisRange {
  startMs: number;
  endMs: number;
}

/** Padding either side of the first/last thing on the axis, so a block that
 * starts exactly at sunset is not glued to the edge. */
const AXIS_PAD_MS = 30 * 60_000;

/** The strip's width before the `ResizeObserver` has reported (and in a DOM
 * that has no layout, e.g. tests). Tests compute expected x from this. */
export const FALLBACK_AXIS_PX = 800;

/** Pointer travel under this many pixels is a tap, not a drag. */
export const TAP_SLOP_PX = 4;
export const SNAP_RADIUS_PX = 8;
/** frontend-react.md: 44 px hit targets, everywhere. */
export const HIT_MIN_PX = 44;

const DUSK_TO_DAWN: readonly [SolarEventName, SolarEventName][] = [
  ["sunset", "sunrise"],
  ["civil_dusk", "civil_dawn"],
  ["nautical_dusk", "nautical_dawn"],
  ["astronomical_dusk", "astronomical_dawn"],
];

const OUTCOMES: readonly Outcome[] = ["occurs", "always_above", "always_below", "not_in_window"];

function ms(iso: string): number {
  return new Date(iso).getTime();
}

function findEvent(events: readonly ResolvedEvent[], name: string): ResolvedEvent | undefined {
  return events.find((e) => e.event === name);
}

function occursAt(event: ResolvedEvent | undefined): number | null {
  return event?.outcome === "occurs" && event.when_utc !== null ? ms(event.when_utc) : null;
}

// --- the axis ------------------------------------------------------------- //

/** Dusk → dawn: from sunset to sunrise when both occur, padded, and clipped
 * to the compiled window — never wider, however far a scheduled item runs
 * outside it. A polar night (no sunset) or midnight sun (no sunrise) falls
 * back to the window edge on that side rather than to a guessed time. An
 * item outside `[sunset - pad, sunrise + pad]` is clipped at that edge by
 * the strip that draws it, with its own "past dark window" diagnostic —
 * it does not get to drag the axis out to itself. `_items` is kept in the
 * signature for every existing call site; the axis no longer reads it. */
export function axisRange(night: NightWindow, _items: readonly CompiledItem[]): AxisRange {
  const windowStart = ms(night.window_start_utc);
  const windowEnd = ms(night.window_end_utc);
  const sunset = occursAt(findEvent(night.solar_events, "sunset")) ?? windowStart;
  const sunrise = occursAt(findEvent(night.solar_events, "sunrise")) ?? windowEnd;
  const start = Math.max(windowStart, sunset - AXIS_PAD_MS);
  const end = Math.min(windowEnd, sunrise + AXIS_PAD_MS);
  if (end <= start) return { startMs: windowStart, endMs: windowEnd };
  return { startMs: start, endMs: end };
}

export function timeToPx(atMs: number, range: AxisRange, lengthPx: number): number {
  return ((atMs - range.startMs) / (range.endMs - range.startMs)) * lengthPx;
}

export function pxToTime(px: number, range: AxisRange, lengthPx: number): number {
  return range.startMs + (px / lengthPx) * (range.endMs - range.startMs);
}

// --- vertical-mode compression ---------------------------------------------- //
//
// A phone strip is literally 1 px/min end to end: a 7 h night is 420+ px of
// axis, and any `asap` gap or long exposure between two rows scrolls as
// mostly-blank space between their controls (real user report, 2026-09-07 —
// see DESIGN.md §4.4's amendment). The fix compresses *time the axis spends
// with nothing scheduled on it*, not the data: an item's own occupied
// [start,end] always draws at the literal rate (an in-progress block is
// content, not blankness), but a contiguous unoccupied stretch — before the
// first item, between two items, or after the last — longer than
// `VERTICAL_GAP_THRESHOLD_MIN` collapses to a fixed `VERTICAL_GAP_BREAK_PX`
// regardless of how many hours it actually spans. Short gaps (a normal
// slew) stay literal so the eye doesn't lose continuity for nothing.
//
// The mapping is piecewise-linear real-ms ↔ px, built once per schedule
// (`buildVerticalCompression`, memoised by the caller — never rebuilt per
// row, per drag step, per frame) from every compiled item's real span, then
// queried through `compressedTimeToPx`/`compressedPxToTime`, which are
// exact inverses of one another (tested each way) so a snapped drag/resize
// still commits the real timestamp under the pointer, not a compressed one.

/** A small fixed rate for genuinely eventful time (an item's own duration,
 * or a gap too short to bother collapsing) — unchanged from the original
 * literal scale, so an in-progress block still reads as real minutes. */
export const VERTICAL_PX_PER_MIN = 1;
/** A contiguous unoccupied stretch longer than this collapses to a fixed
 * height instead of scaling with its real duration. Below ~20 min a gap
 * reads as a normal slew/settle pause and stays literal; above ~30 min it
 * is dead time nobody needs to scroll through — 25 min splits the
 * difference. */
export const VERTICAL_GAP_THRESHOLD_MIN = 25;
/** The collapsed gap's fixed height: enough to draw a short dashed
 * connector as a "time passes here" affordance, not proportional blankness. */
export const VERTICAL_GAP_BREAK_PX = 28;

export interface CompressionBreakpoint {
  realMs: number;
  px: number;
}

/** A piecewise-linear real-ms ↔ px mapping, strictly increasing in both
 * fields. Built once per schedule; every row's strip queries it. */
export interface VerticalCompression {
  breakpoints: readonly CompressionBreakpoint[];
}

function mergeAndClip(intervals: readonly Interval[], range: AxisRange): Interval[] {
  const clipped = intervals
    .map((i) => ({ startMs: Math.max(i.startMs, range.startMs), endMs: Math.min(i.endMs, range.endMs) }))
    .filter((i) => i.endMs > i.startMs)
    .sort((a, b) => a.startMs - b.startMs);
  const merged: Interval[] = [];
  for (const iv of clipped) {
    const last = merged[merged.length - 1];
    if (last !== undefined && iv.startMs <= last.endMs) {
      last.endMs = Math.max(last.endMs, iv.endMs);
    } else {
      merged.push({ ...iv });
    }
  }
  return merged;
}

/**
 * The vertical axis's time↔px mapping, built from every compiled item's
 * real `[start,end]` span (`occupied`) so idle time between them can
 * collapse. `occupied` empty (no items, or none compiled yet) falls back to
 * the literal scale across the whole range — there is nothing to call
 * "idle" when nothing is scheduled at all.
 */
export function buildVerticalCompression(
  range: AxisRange,
  occupied: readonly Interval[],
  pxPerMin: number = VERTICAL_PX_PER_MIN,
  gapThresholdMin: number = VERTICAL_GAP_THRESHOLD_MIN,
  gapBreakPx: number = VERTICAL_GAP_BREAK_PX,
): VerticalCompression {
  const merged = mergeAndClip(occupied, range);
  if (merged.length === 0) {
    const totalMin = (range.endMs - range.startMs) / 60_000;
    return { breakpoints: [{ realMs: range.startMs, px: 0 }, { realMs: range.endMs, px: totalMin * pxPerMin }] };
  }
  const gapThresholdMs = gapThresholdMin * 60_000;
  const breakpoints: CompressionBreakpoint[] = [{ realMs: range.startMs, px: 0 }];
  let px = 0;
  let cursor = range.startMs;
  const pushLiteral = (fromMs: number, toMs: number) => {
    if (toMs <= fromMs) return;
    px += ((toMs - fromMs) / 60_000) * pxPerMin;
    breakpoints.push({ realMs: toMs, px });
  };
  const pushGap = (fromMs: number, toMs: number) => {
    if (toMs <= fromMs) return;
    if (toMs - fromMs > gapThresholdMs) {
      px += gapBreakPx;
      breakpoints.push({ realMs: toMs, px });
    } else {
      pushLiteral(fromMs, toMs);
    }
  };
  for (const occ of merged) {
    pushGap(cursor, occ.startMs);
    pushLiteral(occ.startMs, occ.endMs);
    cursor = occ.endMs;
  }
  pushGap(cursor, range.endMs);
  return { breakpoints };
}

/** The mapping's total px length — the vertical axis's `axisPx`. */
export function compressedAxisPx(compression: VerticalCompression): number {
  return compression.breakpoints[compression.breakpoints.length - 1]?.px ?? 0;
}

/** Real ms → compressed px, extrapolating past either end at that edge
 * segment's own rate (never clamping) so a drag past the visible edge
 * still moves smoothly, the same way the linear axis always has. */
export function compressedTimeToPx(atMs: number, compression: VerticalCompression): number {
  const bps = compression.breakpoints;
  if (bps.length < 2) return 0;
  let i = 0;
  while (i < bps.length - 2 && (bps[i + 1]?.realMs ?? Infinity) <= atMs) i += 1;
  const a = bps[i];
  const b = bps[i + 1];
  if (a === undefined || b === undefined) return 0;
  const span = b.realMs - a.realMs;
  const rate = span === 0 ? 0 : (b.px - a.px) / span;
  return a.px + (atMs - a.realMs) * rate;
}

/** The exact inverse of `compressedTimeToPx`: compressed px → real ms. */
export function compressedPxToTime(px: number, compression: VerticalCompression): number {
  const bps = compression.breakpoints;
  if (bps.length < 2) return bps[0]?.realMs ?? 0;
  let i = 0;
  while (i < bps.length - 2 && (bps[i + 1]?.px ?? Infinity) <= px) i += 1;
  const a = bps[i];
  const b = bps[i + 1];
  if (a === undefined || b === undefined) return bps[0]?.realMs ?? 0;
  const span = b.px - a.px;
  const rate = span === 0 ? 0 : (b.realMs - a.realMs) / span;
  return a.realMs + (px - a.px) * rate;
}

/** One time↔px conversion every rectangle and drag goes through — linear on
 * desktop, compressed on a phone — so `TimelineStrip` has one code path for
 * both rather than an `orientation` branch scattered through its geometry. */
export interface AxisMap {
  toPx: (atMs: number) => number;
  toTime: (px: number) => number;
  lengthPx: number;
}

export function linearAxisMap(range: AxisRange, lengthPx: number): AxisMap {
  return {
    toPx: (atMs) => timeToPx(atMs, range, lengthPx),
    toTime: (px) => pxToTime(px, range, lengthPx),
    lengthPx,
  };
}

export function verticalAxisMap(compression: VerticalCompression): AxisMap {
  return {
    toPx: (atMs) => compressedTimeToPx(atMs, compression),
    toTime: (px) => compressedPxToTime(px, compression),
    lengthPx: compressedAxisPx(compression),
  };
}

/** Whole local hours inside the range, every `stepHours` — the tick marks.
 * Local because the labels are (display edge); the values stay UTC ms. */
export function hourTicks(range: AxisRange, stepHours: number): number[] {
  const first = new Date(range.startMs);
  first.setMinutes(0, 0, 0);
  const ticks: number[] = [];
  for (let t = first.getTime(); t <= range.endMs; ) {
    if (t >= range.startMs) ticks.push(t);
    const next = new Date(t);
    next.setHours(next.getHours() + stepHours);
    t = next.getTime();
  }
  return ticks;
}

// --- lane assignment (single-strip stacking) --------------------------------- //
//
// The S30 Pro is one resource: a compiled plan's items never truly run at
// once, so the strip they draw on is one shared track, not one per item
// (real user report, 2026-09-07 — "it can't capture two targets at once").
// Every item's block sits on lane 0 of that track by default; a genuine
// scheduling conflict — two items whose compiled `[start,end]` actually
// overlap — is the one case that earns a second lane, offsetting just the
// overlapping pair so the conflict is the visible exception, not the
// layout default. Classic interval-graph colouring: sorted by start, an
// item reuses the first lane whose last-placed end is at or before its own
// start, else opens a new one. Sequential items (the ordinary case) always
// land back on lane 0.

export interface LaneEntry {
  itemId: string;
  startMs: number;
  endMs: number;
}

export interface LaneAssignment {
  /** Every item that had a compiled span, mapped to its lane (0-based). An
   * item with no compiled span (unschedulable, or not compiled yet) is
   * simply absent — it draws no block and so needs no lane. */
  laneOf: ReadonlyMap<string, number>;
  /** At least 1, even with no entries — a strip always has one lane to lay
   * its (empty) block row against. */
  laneCount: number;
}

/** Two items whose gap is under this are "touching", not overlapping.
 * `start_utc`/`end_utc` each item carries are independently resolved
 * TimeSpecs — one item's end computed from its own duration, the next
 * item's start from its own event/target_event spec — so two items meant
 * to read as back-to-back can differ by more than a rounding error: two
 * *different* solar/lunar events (e.g. one item ending at
 * `moonrise + 57min`, the next starting at `astronomical_dawn`) can land
 * tens of seconds apart purely from real astronomical timing, not floating
 * point. A 1 second tolerance (this constant's original value) only
 * covered the sticky-edge-snap case (`resolveMoveDrag`/`resolveResizeDrag`
 * landing a dragged edge on a neighbour's edge exactly, then each item
 * re-resolving independently) and still split the event-based case above
 * onto two lanes — reading as two separate lines on the strip — for a gap
 * (25.6 s, real compiled schedule, 2026-09-07) invisible at any timeline
 * width this strip renders at (one pixel is tens of seconds on a
 * multi-hour night). One minute is still two orders of magnitude below the
 * gap the compiler's own `overlaps_previous` diagnostic considers
 * meaningful (`test_overlap_is_flagged`: a 10 minute overlap), so it can
 * never mask a real conflict worth a second lane. */
const LANE_TOUCH_TOLERANCE_MS = 60_000;

export function assignLanes(entries: readonly LaneEntry[]): LaneAssignment {
  const sorted = [...entries].sort((a, b) => a.startMs - b.startMs || a.endMs - b.endMs);
  const laneEndMs: number[] = [];
  const laneOf = new Map<string, number>();
  for (const entry of sorted) {
    let lane = laneEndMs.findIndex((end) => end - LANE_TOUCH_TOLERANCE_MS <= entry.startMs);
    if (lane === -1) {
      lane = laneEndMs.length;
      laneEndMs.push(entry.endMs);
    } else {
      laneEndMs[lane] = entry.endMs;
    }
    laneOf.set(entry.itemId, lane);
  }
  return { laneOf, laneCount: Math.max(1, laneEndMs.length) };
}

// --- vertical strip lane sizing (the non-time axis on a phone) -------------- //
//
// Before the single shared strip existed, a phone's one item-per-row track
// was drawn at the full measured container width (`TimelinePane`'s
// `paneWidth`) — plenty of room for the block's own label and the ticks/
// event labels the vertical strip overlays on top of it (this file's
// `labelLayout` positions rows assuming exactly that much room). Merging
// every item onto one shared strip introduced a *lane* concept that did not
// exist before (`assignLanes`), and that merge sized the phone's cross-axis
// from the lane count alone — `laneCount * ROW_LANE_PX`, 44 px for the
// overwhelmingly common single-lane case — instead of the measured
// container width, clipping almost every label and much of the block text
// against the SVG's own bounds (SVGs clip overflow by default; real user
// report, 2026-09-08: "the gantt chart on mobile is completely broken and
// unusable"). `verticalLaneSizing` restores "use the width you have": the
// common single-lane case takes the whole measured width back, and a
// genuine conflict divides it evenly across lanes, never shrinking below
// `minLanePx` (the 44 px finger target, `ROW_LANE_PX`) — past that point the
// lanes exceed the available width and the existing `overflow-x-auto`
// wrapper scrolls, rather than the lanes squeezing unreadably thin.

export interface LaneSizing {
  /** Each lane's own thickness, in px. */
  laneThicknessPx: number;
  /** `laneCount * laneThicknessPx` — every lane's combined extent. */
  lanesPx: number;
}

export function verticalLaneSizing(laneCount: number, availableCrossPx: number, minLanePx: number): LaneSizing {
  const count = Math.max(1, laneCount);
  const laneThicknessPx = Math.max(minLanePx, availableCrossPx / count);
  return { laneThicknessPx, lanesPx: laneThicknessPx * count };
}

// --- twilight bands ---------------------------------------------------------- //

export type BandName = "day" | "civil" | "nautical" | "astronomical" | "night";
export const BAND_NAMES: readonly BandName[] = ["day", "civil", "nautical", "astronomical", "night"];

export interface TwilightBand {
  startMs: number;
  endMs: number;
  /** 0 = day … 4 = astronomical night. */
  level: number;
  name: BandName;
}

/** Continuous twilight colours shared by Planning and Scheduling. */
export function twilightGradientStops(bands: readonly TwilightBand[], toPx: (ms: number) => number, lengthPx: number): { offset: number; level: number }[] {
  if (bands.length === 0 || lengthPx <= 0) return [];
  const stops = [{ offset: 0, level: bands[0]?.level ?? BAND_NAMES.length - 1 }];
  for (const band of bands) {
    const midpoint = (Math.max(toPx(band.startMs), 0) + Math.min(toPx(band.endMs), lengthPx)) / 2;
    stops.push({ offset: Math.min(1, Math.max(0, midpoint / lengthPx)), level: band.level });
  }
  stops.push({ offset: 1, level: bands[bands.length - 1]?.level ?? BAND_NAMES.length - 1 });
  return stops;
}

/** The fill token per band level (`---*`, `theme/tokens.css`): day
 * reads as a genuine red, nautical as a genuine blue, astronomical/full
 * night as near-black — three hues, not one darkening wash of `--ssc-bg`.
 * Civil (between day and nautical) and astronomical (between nautical and
 * night) are the transitions either side of full dark, blended toward the
 * hue they sit between rather than introducing a fourth unrelated colour.
 * In the red-light theme the same five tokens exist but are all the same
 * hue (0°), stepped by lightness/saturation only, so the three-way
 * distinction survives that theme's "no non-red pixel" constraint — see the
 * theme file's own comments. Levels are `BAND_NAMES`' indices; an
 * out-of-range level (should not happen — `twilightBands` only ever
 * produces 0..4) falls back to the darkest band rather than an undefined
 * fill. */
const BAND_FILL_VARS: readonly string[] = [
  "var(--ssc-band-day)",
  "var(--ssc-band-civil)",
  "var(--ssc-band-nautical)",
  "var(--ssc-band-astronomical)",
  "var(--ssc-band-night)",
];

export function bandFill(level: number): string {
  return BAND_FILL_VARS[level] ?? BAND_FILL_VARS[BAND_FILL_VARS.length - 1] ?? "var(--ssc-band-night)";
}

/** The interval in which the sun is below one threshold, from its dusk/dawn
 * pair, clipped to the range. `null` when the sun never gets that low. */
function belowThresholdInterval(
  dusk: ResolvedEvent | undefined,
  dawn: ResolvedEvent | undefined,
  range: AxisRange,
): [number, number] | null {
  const duskAt = occursAt(dusk);
  const dawnAt = occursAt(dawn);
  if (duskAt !== null && dawnAt !== null) return [duskAt, dawnAt];
  if (duskAt !== null) return [duskAt, range.endMs];
  if (dawnAt !== null) return [range.startMs, dawnAt];
  // Neither crossing occurs: the sun stayed on one side of the threshold
  // all window. `always_below` (polar night) is dark throughout; anything
  // else (`always_above`, or two `not_in_window`s) is never below it.
  if (dusk?.outcome === "always_below" || dawn?.outcome === "always_below") return [range.startMs, range.endMs];
  return null;
}

export function twilightBands(night: NightWindow, range: AxisRange): TwilightBand[] {
  const intervals: [number, number][] = [];
  for (const [duskName, dawnName] of DUSK_TO_DAWN) {
    const interval = belowThresholdInterval(
      findEvent(night.solar_events, duskName),
      findEvent(night.solar_events, dawnName),
      range,
    );
    if (interval !== null) intervals.push(interval);
  }
  const edges = new Set<number>([range.startMs, range.endMs]);
  for (const [a, b] of intervals) {
    if (a > range.startMs && a < range.endMs) edges.add(a);
    if (b > range.startMs && b < range.endMs) edges.add(b);
  }
  const sorted = [...edges].sort((x, y) => x - y);
  const bands: TwilightBand[] = [];
  for (let i = 0; i + 1 < sorted.length; i += 1) {
    const startMs = sorted[i] ?? range.startMs;
    const endMs = sorted[i + 1] ?? range.endMs;
    const mid = (startMs + endMs) / 2;
    const level = intervals.filter(([a, b]) => mid >= a && mid < b).length;
    const previous = bands[bands.length - 1];
    if (previous?.level === level) {
      previous.endMs = endMs;
    } else {
      bands.push({ startMs, endMs, level, name: BAND_NAMES[level] ?? "night" });
    }
  }
  return bands;
}

/** A solar event the night does not contain, with the message DESIGN.md §4.1
 * demands instead of a blank, and which edge of the axis it belongs at. */
export interface MissingEvent {
  event: string;
  label: string;
  side: "start" | "end";
  message: string;
}

export function missingSolarEvents(night: NightWindow): MissingEvent[] {
  const missing: MissingEvent[] = [];
  for (const [duskName, dawnName] of DUSK_TO_DAWN) {
    for (const [name, side] of [
      [duskName, "start"],
      [dawnName, "end"],
    ] as const) {
      const event = findEvent(night.solar_events, name);
      if (event === undefined || event.outcome === "occurs") continue;
      const label = SOLAR_EVENT_LABELS[name];
      const message = (OUTCOMES as readonly string[]).includes(event.outcome)
        ? outcomeMessage(event.outcome as Outcome, label)
        : `${label} does not occur (${event.outcome}).`;
      missing.push({ event: name, label, side, message });
    }
  }
  return missing;
}

// --- the moon ------------------------------------------------------------------ //

export interface Interval {
  startMs: number;
  endMs: number;
}

/** When the Moon is above the horizon inside the range. Both events may
 * occur in either order (set then rise is a Moon that was up at dusk and
 * comes back before dawn); one alone runs to the axis edge; neither means
 * the lunar outcome decides — `always_above` is up all night, anything else
 * is down all night. Clipped to the range; empty intervals dropped. */
export function moonUpIntervals(night: NightWindow, range: AxisRange): Interval[] {
  const rise = findEvent(night.lunar_events, "moonrise");
  const set = findEvent(night.lunar_events, "moonset");
  const riseAt = occursAt(rise);
  const setAt = occursAt(set);
  let raw: Interval[];
  if (riseAt !== null && setAt !== null) {
    raw =
      riseAt <= setAt
        ? [{ startMs: riseAt, endMs: setAt }]
        : [
            { startMs: range.startMs, endMs: setAt },
            { startMs: riseAt, endMs: range.endMs },
          ];
  } else if (riseAt !== null) {
    raw = [{ startMs: riseAt, endMs: range.endMs }];
  } else if (setAt !== null) {
    raw = [{ startMs: range.startMs, endMs: setAt }];
  } else if (rise?.outcome === "always_above" || set?.outcome === "always_above") {
    raw = [{ startMs: range.startMs, endMs: range.endMs }];
  } else {
    raw = [];
  }
  return raw
    .map((i) => ({ startMs: Math.max(i.startMs, range.startMs), endMs: Math.min(i.endMs, range.endMs) }))
    .filter((i) => i.endMs > i.startMs);
}

/** SVG path of a Moon disc of radius `r` lit by `fraction` (0 dark … 1
 * full), lit limb on the right when waxing — the northern-hemisphere
 * evening convention; the glyph is a phase indicator, not an orientation.
 * The terminator is the half-ellipse of a sphere seen side-on. */
export function moonPhasePath(cx: number, cy: number, r: number, fraction: number, waxing: boolean): string {
  const f = Math.min(1, Math.max(0, fraction));
  const litSide = waxing ? 1 : -1;
  // Half-disc on the lit side, then the terminator back: bulging into the
  // lit side when less than half lit, into the dark side when more.
  const rx = Math.abs(2 * f - 1) * r;
  const bulgeSide = f >= 0.5 ? -litSide : litSide;
  // SVG sweep=1 is clockwise on screen: top→bottom via the right, and
  // bottom→top via the left.
  return [
    `M ${String(cx)} ${String(cy - r)}`,
    `A ${String(r)} ${String(r)} 0 0 ${litSide === 1 ? "1" : "0"} ${String(cx)} ${String(cy + r)}`,
    `A ${String(rx)} ${String(r)} 0 0 ${bulgeSide === -1 ? "1" : "0"} ${String(cx)} ${String(cy - r)}`,
    "Z",
  ].join(" ");
}

// --- snapping -------------------------------------------------------------------- //

export interface SnapTarget {
  event: SolarEvent | LunarEvent;
  label: string;
  atMs: number;
}

/** Every resolved solar and lunar event that occurs, as a snap target. */
export function snapTargets(night: NightWindow): SnapTarget[] {
  const targets: SnapTarget[] = [];
  for (const e of [...night.solar_events, ...night.lunar_events]) {
    const at = occursAt(e);
    if (at === null || !(e.event in EVENT_LABELS)) continue;
    const event = e.event as SolarEvent | LunarEvent;
    targets.push({ event, label: EVENT_LABELS[event], atMs: at });
  }
  return targets.sort((a, b) => a.atMs - b.atMs);
}

/** The nearest target within `radiusPx` of `atMs` on this axis map — linear
 * or compressed, whichever the caller is dragging on — or null. */
export function snapToAxis(
  atMs: number,
  targets: readonly SnapTarget[],
  axis: AxisMap,
  radiusPx: number = SNAP_RADIUS_PX,
): SnapTarget | null {
  const px = axis.toPx(atMs);
  let best: SnapTarget | null = null;
  let bestDistance = radiusPx;
  for (const target of targets) {
    const distance = Math.abs(axis.toPx(target.atMs) - px);
    if (distance <= bestDistance) {
      best = target;
      bestDistance = distance;
    }
  }
  return best;
}

/** The nearest target within `radiusPx` of `atMs` on the linear axis, or
 * null — `snapToAxis` on a `linearAxisMap`, kept as its own entry point for
 * the desktop (always-linear) call sites and their fixtures. */
export function snapTo(
  atMs: number,
  targets: readonly SnapTarget[],
  range: AxisRange,
  lengthPx: number,
  radiusPx: number = SNAP_RADIUS_PX,
): SnapTarget | null {
  return snapToAxis(atMs, targets, linearAxisMap(range, lengthPx), radiusPx);
}

// --- free item moves, constrained resizes, and sticky neighbour edges ------------ //
//
// Whole-item moves may cross other items so users can place an item before
// or after an existing one. Temporary overlap is surfaced by the compiler's
// red diagnostic. Resizing remains constrained by neighbouring spans.

export interface DragBounds {
  /** The nearest *enabled* previous item's compiled end, or `null`. Moves
   * may snap here but can continue through it; start-resize uses it as a
   * hard boundary. */
  prevEndMs: number | null;
  /** Whether snapping a start resize to `prevEndMs` can truthfully be
   * stored as "ASAP after previous item". False when the chronologically
   * previous block is not the previous enabled item in plan order. */
  prevEndCommitsRelative?: boolean;
  /** The nearest *enabled* next item's compiled start, or `null`. End-resize
   * may not carry its handle past it; whole-item moves may cross it. */
  nextStartMs: number | null;
  /** Every other enabled item's temporal edge. A moving block can align its
   * start to an `end` edge or its end to a `start` edge. */
  moveSnapEdges?: readonly { atMs: number; edge: "start" | "end" }[];
}

export interface MoveDragResult {
  proposedStartMs: number;
  /** The nearest resolved solar/lunar event snapped to, if any. */
  snapped: SnapTarget | null;
  /** A start/end edge of another scheduled item won the snap. */
  snappedToItemEdge: boolean;
}

/** One move-drag step: keep the wanted start unconstrained so the block can
 * cross its neighbours, then snap its start/end to the closest edge of any
 * other enabled item, or its start to a resolved event. */
export function resolveMoveDrag(
  wantedStartMs: number,
  durationMs: number,
  events: readonly SnapTarget[],
  axis: AxisMap,
  bounds: DragBounds,
  radiusPx: number = SNAP_RADIUS_PX,
): MoveDragResult {
  const eventSnap = snapToAxis(wantedStartMs, events, axis, radiusPx);
  const eventPx = eventSnap === null ? null : Math.abs(axis.toPx(eventSnap.atMs) - axis.toPx(wantedStartMs));
  const fallbackEdges = bounds.prevEndMs === null ? [] : [{ atMs: bounds.prevEndMs, edge: "end" as const }];
  const closestEdge = (bounds.moveSnapEdges ?? fallbackEdges)
    .map((edge) => {
      const movingEdgeMs = edge.edge === "end" ? wantedStartMs : wantedStartMs + durationMs;
      return {
        proposedStartMs: edge.edge === "end" ? edge.atMs : edge.atMs - durationMs,
        distancePx: Math.abs(axis.toPx(edge.atMs) - axis.toPx(movingEdgeMs)),
      };
    })
    .sort((a, b) => a.distancePx - b.distancePx)[0];
  if (closestEdge !== undefined && closestEdge.distancePx <= radiusPx && (eventPx === null || closestEdge.distancePx <= eventPx)) {
    return { proposedStartMs: closestEdge.proposedStartMs, snapped: null, snappedToItemEdge: true };
  }
  if (eventSnap !== null) return { proposedStartMs: eventSnap.atMs, snapped: eventSnap, snappedToItemEdge: false };
  return { proposedStartMs: wantedStartMs, snapped: null, snappedToItemEdge: false };
}

export interface ResizeDragResult {
  proposedEndMs: number;
  snapped: SnapTarget | null;
  /** The next enabled item's start snapped to instead — the resize still
   * commits a plain `duration` (DESIGN.md §4.1), computed so the end lands
   * exactly there. */
  snappedToNextStart: boolean;
}

/** One resize-drag step: clamp the wanted end to `[startMs + 1 min,
 * nextStartMs]`, then snap — to a resolved event after `startMs`, or to the
 * next item's own start — whichever is closer within `radiusPx`. */
export function resolveResizeDrag(
  wantedEndMs: number,
  startMs: number,
  events: readonly SnapTarget[],
  axis: AxisMap,
  bounds: DragBounds,
  radiusPx: number = SNAP_RADIUS_PX,
): ResizeDragResult {
  const minEnd = startMs + 60_000;
  const maxEnd = bounds.nextStartMs ?? Infinity;
  const clamped = Math.min(Math.max(wantedEndMs, minEnd), Math.max(minEnd, maxEnd));
  const rawEventSnap = snapToAxis(clamped, events, axis, radiusPx);
  const eventSnap = rawEventSnap !== null && rawEventSnap.atMs >= minEnd && rawEventSnap.atMs <= maxEnd ? rawEventSnap : null;
  const eventPx = eventSnap === null ? null : Math.abs(axis.toPx(eventSnap.atMs) - axis.toPx(clamped));
  const nextStartMs = bounds.nextStartMs;
  if (nextStartMs !== null) {
    const neighborPx = Math.abs(axis.toPx(nextStartMs) - axis.toPx(clamped));
    if (neighborPx <= radiusPx && (eventPx === null || neighborPx <= eventPx)) {
      return { proposedEndMs: nextStartMs, snapped: null, snappedToNextStart: true };
    }
  }
  if (eventSnap !== null) return { proposedEndMs: eventSnap.atMs, snapped: eventSnap, snappedToNextStart: false };
  return { proposedEndMs: clamped, snapped: null, snappedToNextStart: false };
}

// --- axis label layout ------------------------------------------------------------ //

export interface LabelPlacement {
  event: SolarEvent | LunarEvent;
  /** Which of `rows` staggered lines (desktop: below the axis; phone: out
   * from it) this label sits on. */
  row: number;
  /** The label's own text — abbreviated when the gap since the previous
   * label on the same row is narrower than the full text. */
  text: string;
}

/**
 * Row + text for every axis label, so close events (civil/nautical/
 * astronomical dusk are often minutes apart) do not draw on top of one
 * another. Greedy in position order: label `i` goes on row `i % rows`, so
 * consecutive close events never share a row; if even that row's last
 * label is still closer than this label's own width, this one abbreviates
 * instead of overlapping it. Pure pixel arithmetic — no DOM measurement —
 * so a text's width is estimated from its length at a fixed average glyph
 * width for the 10 px axis font.
 */
export function labelLayout(
  events: readonly SnapTarget[],
  positionPx: (atMs: number) => number,
  abbreviate: (event: SolarEvent | LunarEvent) => string,
  rows = 3,
  charWidthPx = 5.5,
): LabelPlacement[] {
  const rowEndPx = new Array<number>(rows).fill(-Infinity);
  return events.map((target, index) => {
    const x = positionPx(target.atMs);
    const row = index % rows;
    const rowEnd = rowEndPx[row] ?? -Infinity;
    const fullWidth = target.label.length * charWidthPx;
    const fits = x - rowEnd >= fullWidth;
    const text = fits ? target.label : abbreviate(target.event);
    rowEndPx[row] = x + (fits ? fullWidth : text.length * charWidthPx);
    return { event: target.event, row, text };
  });
}

// --- UTC → the site's wall clock --------------------------------------------------- //

/**
 * A UTC instant as the naive wall-clock reading `AbsoluteTimeSpec` wants,
 * in the *site's* zone (`NightWindow.tz` — the plan compiles there, not in
 * the browser), with `fold=1` when the reading also names an earlier
 * instant (the autumn hour that happens twice). `null` when the runtime
 * does not know the zone — the caller refuses rather than guesses.
 */
/** Freeze an existing compiled UTC instant without truncating server microseconds. */
export function utcIsoToSiteWallClock(iso: string, timeZone: string): { local_datetime: string; fold: 0 | 1 } | null {
  const wall = utcToSiteWallClock(Date.parse(iso), timeZone);
  if (wall === null) return null;
  const fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(iso)?.[1];
  return fraction === undefined ? wall : { ...wall, local_datetime: `${wall.local_datetime.replace(/\.\d+$/, "")}.${fraction}` };
}

export function utcToSiteWallClock(atMs: number, timeZone: string): { local_datetime: string; fold: 0 | 1 } | null {
  let formatter: Intl.DateTimeFormat;
  try {
    formatter = new Intl.DateTimeFormat("en-GB", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return null;
  }
  const reading = (t: number): string => {
    const parts = new Map<string, string>(formatter.formatToParts(new Date(t)).map((p) => [p.type, p.value]));
    const get = (type: string) => parts.get(type) ?? "";
    return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}:${get("second")}`;
  };
  const milliseconds = new Date(atMs).getUTCMilliseconds();
  const localReading = reading(atMs);
  const local_datetime = localReading + (milliseconds === 0 ? "" : `.${String(milliseconds).padStart(3, "0")}`);
  // A transition shifts the clock by at most 2 h; if any earlier instant in
  // that span reads the same, this one is the second occurrence.
  let fold: 0 | 1 = 0;
  for (let back = 15; back <= 180; back += 15) {
    if (reading(atMs - back * 60_000) === localReading) {
      fold = 1;
      break;
    }
  }
  return { local_datetime, fold };
}

// --- diagnostics on the block ------------------------------------------------------ //

const SEVERITY_RANK: Record<Severity, number> = { info: 0, warning: 1, error: 2 };

export function worstSeverity(diagnostics: readonly Diagnostic[]): Severity | null {
  let worst: Severity | null = null;
  for (const d of diagnostics) {
    if (worst === null || SEVERITY_RANK[d.severity] > SEVERITY_RANK[worst]) worst = d.severity;
  }
  return worst;
}

/** The compiler's codes (`ssc/scheduler/compiler.py`) as badge text; an
 * unknown code is shown with its underscores as spaces, never hidden. */
const DIAGNOSTIC_SHORT_LABELS: Record<string, string> = {
  overlaps_previous: "overlaps previous",
  exceeds_dark_window: "past dark window",
  below_min_altitude: "below min altitude",
  dips_below_min_altitude: "dips below min alt",
  moon_too_close: "moon too close",
  high_altitude_meridian_crossing: "high transit: rotation spike",
  event_does_not_occur: "event does not occur",
  target_event_does_not_occur: "target event does not occur",
  target_event_needs_target: "needs a target",
  polar_best_field_unavailable: "PA field unavailable",
  nonexistent_local_time: "time does not exist (DST)",
  mosaic_invalid_grid: "invalid mosaic grid",
  mosaic_panels_not_expanded: "panels not laid out",
  unknown_lens: "unknown lens",
};

export function diagnosticShortLabel(code: string): string {
  return DIAGNOSTIC_SHORT_LABELS[code] ?? code.replaceAll("_", " ");
}
