// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The one shared Gantt strip (DESIGN.md §4.4): the twilight bands, event
 * lines, hour ticks and the moon band are drawn once, on a single shared
 * time axis, because the S30 Pro is one resource — a compiled plan's items
 * never truly run at once, so they never earned one full-width track each
 * (real user report, 2026-09-07: "only one combined timeline is needed for
 * one device — it can't capture two targets at once or doing two things at
 * once"). Every item's block sits at its own position along that one axis,
 * end to end in execution order; only a genuine scheduling conflict — two
 * items whose compiled `[start,end]` actually overlap — offsets that pair
 * onto a second lane (`assignLanes`), so the conflict is the visible
 * exception, never the default layout.
 *
 * `orientation` is the only thing that differs between desktop and phone:
 * every rectangle goes through `place()` and every point through `point()`,
 * so the two axes are one code path, not two components. On desktop, lanes
 * stack down the strip's height; on phone, lanes stack across its width —
 * the common case (`laneCount === 1`) is unaffected on either axis.
 *
 * Nothing here writes until the pointer is released; the live label while
 * dragging is the proposed time, and the commit is one `onCommit` with the
 * `TimeSpec`/`DurationEndSpec` patch the parent PUTs — a resize always
 * writes a plain duration, converting an anchored ("until event") end into
 * one on release, however it started (DESIGN.md §4.1 "Until" review). All
 * colours are tokens (`var(--ssc-*)`), per `no-literal-color`.
 *
 * Each item's block is its own `memo`d `ItemBlock`, keyed on primitives and
 * on stable references the parent memoises once per schedule (`axis`,
 * `laneBoxes`, `events`) — so selecting one item, or dragging one block,
 * re-renders that block alone, not every item on the strip (the render
 * budget below is timed against exactly this).
 */
import { memo, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { CloudFog, CloudMoon, CloudRain, Cloudy, MoonStar, Wind, type LucideIcon } from "lucide-react";
import type { CompiledItem, DurationEndSpec, PlanItem, Severity, TimeSpec } from "../../api/plans";
import type { ForecastPoint } from "../../api/weather";
import type { components } from "../../api/schema";
import { ABBREVIATED_EVENT_LABELS, EVENT_LABELS } from "./timeSpecs";
import { weatherDetail, weatherSlots, type WeatherIcon, type WeatherQuality } from "./weatherTimeline";
import { formatClock, resolutionLabel } from "./localTime";
import { useNow } from "../MountTab/useNow";
import {
  twilightGradientStops,
  HIT_MIN_PX,
  SNAP_RADIUS_PX,
  TAP_SLOP_PX,
  bandFill,
  diagnosticShortLabel,
  hourTicks,
  labelLayout,
  linearAxisMap,
  moonPhasePath,
  resolveMoveDrag,
  resolveResizeDrag,
  utcToSiteWallClock,
  verticalAxisMap,
  verticalLaneSizing,
  worstSeverity,
  type AxisMap,
  type AxisRange,
  type DragBounds,
  type Interval,
  type SnapTarget,
  type TwilightBand,
  type VerticalCompression,
} from "./timelineGeometry";

export type Orientation = "horizontal" | "vertical";
type AltitudeSample = components["schemas"]["AltitudeSampleModel"];
type MoonSeparationSample = components["schemas"]["MoonSeparationSampleModel"];
type RunItemStatus = components["schemas"]["RunItemStatus"];

function interpolateAt<T extends { when: string }>(samples: readonly T[] | undefined, atMs: number, value: (sample: T) => number): number | null {
  if (samples === undefined || samples.length === 0) return null;
  const first = samples[0];
  if (first === undefined) return null;
  if (atMs <= new Date(first.when).getTime()) return value(first);
  for (let i = 1; i < samples.length; i += 1) {
    const next = samples[i];
    const previous = samples[i - 1];
    if (next === undefined || previous === undefined) continue;
    const endMs = new Date(next.when).getTime();
    if (atMs <= endMs) {
      const startMs = new Date(previous.when).getTime();
      const fraction = endMs === startMs ? 0 : (atMs - startMs) / (endMs - startMs);
      return value(previous) + fraction * (value(next) - value(previous));
    }
  }
  const last = samples[samples.length - 1];
  return last === undefined ? null : value(last);
}

export type ItemPatch = { start: TimeSpec; end?: DurationEndSpec } | { end: DurationEndSpec };

/** A lane's thickness on desktop, fixed — a 44 px finger target. On phone
 * it is the *minimum* (`verticalLaneSizing`): the common single-lane case
 * fills the whole measured container width instead, so labels overlaid on
 * it have room; only a genuine conflict divides that width across lanes,
 * never below this floor. Most nights have exactly one lane. */
export const ROW_LANE_PX = 44;
/** The header region ahead of the item lanes on desktop: ticks, the moon
 * band and three staggered label rows. Unused on phone, where ticks and
 * labels overlay the lanes instead of reserving a region of their own. */
export const HEADER_LANE_PX = 96;
const BLOCK_INSET_PX = 4;
const MOON_BAND_PX = 8;
const WEATHER_BAND_PX = 8;
const FONT = "9px sans-serif";

const SEVERITY_STROKE: Record<Severity, string> = {
  info: "var(--ssc-border-strong)",
  warning: "var(--ssc-warning)",
  error: "var(--ssc-danger)",
};

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** [start, end) of a lane's own thickness, in the strip's shared
 * perpendicular-to-time coordinate — the same coordinate `place()`'s `l0`,
 * `l1` take. Memoised by lane index in the parent so every item on the
 * same lane (the overwhelming common case) shares one reference. */
export type LaneBox = readonly [number, number];

export interface MoonBand {
  intervals: readonly Interval[];
  /** `null` when no source reported it — the glyph is then neutral. */
  illuminatedFraction: number | null;
  waxing: boolean | null;
  /** The Moon's own altitude over the night, when a server endpoint
   * supplies it. Absent means no line at all — never a flat one. */
  altitude?: readonly AltitudeSample[];
}

export interface WeatherBand {
  points: readonly ForecastPoint[];
  stale: boolean;
}

/** One item's row on the shared strip. */
export interface StripItem {
  item: PlanItem;
  compiled?: CompiledItem;
  altitude?: readonly AltitudeSample[];
  moonSeparation?: readonly MoonSeparationSample[];
  runStatus?: RunItemStatus;
  label?: string;
  selected: boolean;
  /** The linked list row is hovered — highlights the block here too (and
   * vice versa, via `onHover`). */
  hovered?: boolean;
  /** Selected item whose compiled span overlaps another item. */
  overlapping?: boolean;
  laneIndex: number;
  /** Resize boundaries plus all enabled-item edges used by move snapping. */
  bounds: DragBounds;
}

export interface TimelineStripProps {
  orientation: Orientation;
  range: AxisRange;
  /** Pixels the whole `range` spans along the time axis (desktop: width;
   * phone: height, after vertical compression). */
  axisPx: number;
  /** How many lanes wide (phone) or tall (desktop) the strip's non-time
   * dimension is — `assignLanes(...).laneCount`, at least 1. */
  laneCount: number;
  /** The strip's own measured width when vertical (phone) — mirrors
   * `axisPx`'s role for the time dimension in horizontal mode, so the
   * common single-lane case fills the space the caller actually has rather
   * than shrinking to the bare lane thickness (`verticalLaneSizing`).
   * Ignored in horizontal orientation, where every lane is the fixed
   * `ROW_LANE_PX` finger-target thickness. */
  crossAxisPx: number;
  bands: readonly TwilightBand[];
  events: readonly SnapTarget[];
  /** The vertical axis's compressed time↔px mapping (`timelineGeometry`'s
   * "vertical-mode compression"), built once per schedule by the parent and
   * shared by the one strip. Ignored in horizontal orientation; a vertical
   * strip without one falls back to the literal 1 px/min scale. */
  verticalCompression?: VerticalCompression;
  /** The moon band (rise→set wash and phase glyph). */
  moon?: MoonBand;
  /** The Moon's altitude line, drawn once on the same 0–90° lane scale as
   * every item's own altitude curve so the two read against each other. */
  moonAltitude?: readonly AltitudeSample[];
  /** Hourly site forecast, rendered above the Moon lane. */
  weather?: WeatherBand;
  timeZone: string;
  /** Override for deterministic tests; production follows the live clock. */
  currentTimeMs?: number;
  /** Every item with a block to draw, in plan order. */
  items: readonly StripItem[];
  /** Stable across renders (the parent keys on `itemId`), so a memoised
   * block re-renders only when its own data changes. */
  onSelect?: (itemId: string) => void;
  /** The pointer entered or left an item's block — `null` on leave — so the
   * parent can highlight the linked gutter row too. */
  onHover?: (itemId: string | null) => void;
  onCommit?: (itemId: string, patch: ItemPatch) => void;
  /** Surfaced instead of a guessed wall clock when the site's zone is
   * unknown to this runtime. */
  onCommitError?: (message: string) => void;
}

function pointerAlong(e: ReactPointerEvent, orientation: Orientation): number {
  return orientation === "horizontal" ? e.clientX : e.clientY;
}

/** The item's own altitude-limit constraint (`ConstraintsSpec.min_alt_deg`)
 * — the same value the compiler's `below_min_altitude`/
 * `dips_below_min_altitude` diagnostics already key off. Only the item
 * kinds that carry `constraints` (a target-bearing item) have one; every
 * other kind, and an unset limit, draws no line — never an invented
 * default. */
function minAltDegOf(item: PlanItem): number | null {
  if (!("constraints" in item)) return null;
  const min = item.constraints.min_alt_deg;
  return typeof min === "number" ? min : null;
}

/** The lane boxes for `laneCount` lanes of thickness `laneThicknessPx`,
 * offset by `originPx` (the header region on desktop; 0 on phone) —
 * memoised by the caller so every item on the same lane shares one
 * `LaneBox` reference across renders. */
function laneBoxes(laneCount: number, laneThicknessPx: number, originPx: number): LaneBox[] {
  return Array.from({ length: laneCount }, (_, i) => [originPx + i * laneThicknessPx, originPx + (i + 1) * laneThicknessPx] as const);
}

export const TimelineStrip = memo(function TimelineStrip({
  orientation,
  range,
  axisPx,
  laneCount,
  crossAxisPx,
  bands,
  events,
  verticalCompression,
  moon,
  moonAltitude,
  weather,
  timeZone,
  currentTimeMs,
  items,
  onSelect,
  onHover,
  onCommit,
  onCommitError,
}: TimelineStripProps) {
  const liveNow = useNow(30_000);
  const nowMs = currentTimeMs ?? liveNow;
  const patternId = useId();
  const bandGradientId = useId();
  const horizontal = orientation === "horizontal";

  // The one time↔px conversion every rectangle and drag below goes
  // through — linear on desktop, the schedule's compressed mapping on a
  // phone — memoised so every `ItemBlock` shares one reference (a new
  // object every render would defeat their own memoisation).
  const axis: AxisMap = useMemo(
    () => (horizontal || verticalCompression === undefined ? linearAxisMap(range, axisPx) : verticalAxisMap(verticalCompression)),
    [horizontal, range, axisPx, verticalCompression],
  );
  const along = (atMs: number) => axis.toPx(atMs);
  const alongLengthPx = Math.max(HIT_MIN_PX, axisPx);
  const inRange = (atMs: number) => atMs >= range.startMs && atMs <= range.endMs;

  // Desktop: the header region (ticks, moon band, labels) sits ahead of the
  // item lanes, which stack downward. Phone: there is no separate header —
  // ticks and labels overlay the lanes, which stack across the width.
  const headerPx = horizontal ? HEADER_LANE_PX : 0;
  // Desktop: every lane is the fixed finger-target thickness. Phone: the
  // common single-lane case takes the whole measured container width back
  // (`verticalLaneSizing`'s own comment) — never the lane count alone.
  const laneThicknessPx = horizontal ? ROW_LANE_PX : verticalLaneSizing(laneCount, crossAxisPx, ROW_LANE_PX).laneThicknessPx;
  const totalLane = headerPx + laneCount * laneThicknessPx;
  const width = horizontal ? alongLengthPx : totalLane;
  const height = horizontal ? totalLane : alongLengthPx;
  const boxes = useMemo(() => laneBoxes(laneCount, laneThicknessPx, headerPx), [laneCount, laneThicknessPx, headerPx]);

  // One stop per band at its own midpoint, so each band's colour is fully
  // present in the middle of its own span and fades toward its neighbours
  // rather than stepping at the boundary; plus the strip's two ends, so the
  // first/last bands hold their colour there instead of fading to nothing.
  // Not memoised: `along` is itself a fresh closure every render (it reads
  // `axis`, not a prop), so a memo here would never actually hit.
  const bandGradientStops = twilightGradientStops(bands, along, alongLengthPx);

  const place = (a0: number, a1: number, l0: number, l1: number): Box =>
    horizontal
      ? { x: a0, y: l0, width: Math.max(0, a1 - a0), height: Math.max(0, l1 - l0) }
      : { x: l0, y: a0, width: Math.max(0, l1 - l0), height: Math.max(0, a1 - a0) };
  const point = (a: number, l: number): [number, number] => (horizontal ? [a, l] : [l, a]);

  const tickStepHours = horizontal ? (axisPx / ((range.endMs - range.startMs) / 3_600_000) < 44 ? 2 : 1) : 1;
  const ticks = hourTicks(range, tickStepHours).filter(inRange);
  const labelledEvents = events.filter((e) => inRange(e.atMs));
  const eventPlacements = labelLayout(labelledEvents, along, (event) => ABBREVIATED_EVENT_LABELS[event] ?? EVENT_LABELS[event]);
  const visibleVerticalTicks = new Set<number>();
  const visibleVerticalEvents = new Set<string>();
  if (!horizontal) {
    let lastTickPx = -Infinity;
    for (const tick of ticks) {
      const atPx = along(tick);
      if (atPx - lastTickPx >= 18) {
        visibleVerticalTicks.add(tick);
        lastTickPx = atPx;
      }
    }
    let lastEventPx = -Infinity;
    for (const event of labelledEvents) {
      const atPx = along(event.atMs);
      if (atPx - lastEventPx >= 16) {
        visibleVerticalEvents.add(event.event);
        lastEventPx = atPx;
      }
    }
  }

  // `slot`, when given, additionally bounds the curve to one item's own
  // scheduled span — the device is single-resource and slots never
  // overlap, so a target's altitude curve reads as "what it was doing
  // while this item captured it", not one continuous line threading
  // through every other item's block and the idle time between them (real
  // user report, 2026-09-07: "the gantt chart still is like two separate
  // lines"). The shared Moon curve has no slot — it is the one shared-axis
  // element that legitimately spans the whole night.
  const curvePoints = (samples: readonly AltitudeSample[], laneBox: LaneBox, slot?: Interval): string =>
    samples
      .filter((s) => inRange(new Date(s.when).getTime()))
      .filter((s) => slot === undefined || (new Date(s.when).getTime() >= slot.startMs && new Date(s.when).getTime() <= slot.endMs))
      .map((s) => {
        const a = along(new Date(s.when).getTime());
        const l = laneBox[1] - (Math.max(0, Math.min(90, s.altitude_deg)) / 90) * (laneBox[1] - laneBox[0]);
        const [x, y] = point(a, l);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");

  const weatherY = horizontal ? 31 : 0;
  const resolvedWeatherSlots = useMemo(() => weatherSlots(weather?.points ?? [], range), [weather, range]);
  const nowAlong = inRange(nowMs) ? along(nowMs) : null;

  const weatherColor = (quality: WeatherQuality): string => quality === "good"
    ? "var(--ssc-success)"
    : quality === "marginal"
      ? "var(--ssc-warning)"
      : quality === "poor" ? "var(--ssc-danger)" : "var(--ssc-border-strong)";
  const weatherIcon: Record<WeatherIcon, LucideIcon> = {
    clear: MoonStar,
    "partly-cloudy": CloudMoon,
    cloudy: Cloudy,
    rain: CloudRain,
    wind: Wind,
    fog: CloudFog,
  };

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${String(width)} ${String(height)}`}
      className="block select-none"
      data-orientation={orientation}
      data-testid="strip-axis"
    >
      <defs>
        <pattern id={patternId} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="6" stroke="var(--ssc-fg-muted)" strokeWidth="1.5" />
        </pattern>
        {/* One gradient, not five flat rects: day fades through civil into
            nautical into astronomical into full night and back, the way
            twilight actually happens, rather than stepping at each
            boundary. A stop sits at every band's own midpoint (its colour
            fully present there) plus the strip's two ends (so the first and
            last bands don't fade toward nothing at the edges); SVG
            interpolates linearly between them. `userSpaceOnUse` in pixels,
            through the same `along()` the bands/blocks/curves already use,
            so this respects the vertical axis's compressed mapping too —
            a naive time-fraction gradient would drift out of registration
            with the compressed strip on a phone. */}
        <linearGradient
          id={bandGradientId}
          gradientUnits="userSpaceOnUse"
          x1={point(0, totalLane / 2)[0]}
          y1={point(0, totalLane / 2)[1]}
          x2={point(alongLengthPx, totalLane / 2)[0]}
          y2={point(alongLengthPx, totalLane / 2)[1]}
        >
          {bandGradientStops.map(({ offset, level }, i) => (
            <stop key={i} offset={`${String(offset * 100)}%`} style={{ stopColor: bandFill(level) }} />
          ))}
        </linearGradient>
      </defs>

      {/* twilight bands: day red, nautical blue, astronomical/full-dark
          near-black, blended continuously (bandGradientId above) rather
          than stepped — see that gradient's own comment. One filled rect
          carries the colour; the per-band rects beneath stay transparent
          and exist only for their hover title and `data-band`/`data-level`
          (tests, and a place to hang the tooltip a gradient can't carry). */}
      {alongLengthPx > 0 && <rect {...place(0, alongLengthPx, 0, totalLane)} fill={`url(#${bandGradientId})`} />}
      {bands.map((band) => {
        const a0 = Math.max(along(band.startMs), 0);
        const a1 = Math.min(along(band.endMs), alongLengthPx);
        if (a1 <= a0) return null;
        const box = place(a0, a1, 0, totalLane);
        return (
          <rect
            key={`${band.name}-${String(band.startMs)}`}
            {...box}
            fill="transparent"
            data-band={band.name}
            data-level={band.level}
          >
            <title>{`${band.name}: ${formatClock(new Date(band.startMs).toISOString())} – ${formatClock(new Date(band.endMs).toISOString())}`}</title>
          </rect>
        );
      })}

      {/* hour ticks, once */}
      {ticks.map((t) => {
        const a = along(t);
        const [x1, y1] = point(a, 0);
        const [x2, y2] = point(a, horizontal ? 6 : totalLane);
        const [tx, ty] = horizontal ? [a, 16] : [4, a - 3];
        return (
          <g key={t} data-tick={t}>
            <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--ssc-border-strong)" />
            {(horizontal || visibleVerticalTicks.has(t)) && (
              <text x={tx} y={ty} textAnchor={horizontal ? "middle" : "start"} fill="var(--ssc-fg-muted)" style={{ font: FONT }} className="tabular-nums">
                {formatClock(new Date(t).toISOString())}
              </text>
            )}
          </g>
        );
      })}

      {/* Hourly forecast quality and conditions, once above the Moon lane. */}
      {resolvedWeatherSlots.length > 0 && (
        <g data-testid="weather-band" opacity={weather?.stale === true ? 0.5 : 1}>
          {resolvedWeatherSlots.map((slot) => {
            const a0 = Math.max(along(slot.startMs), 0);
            const a1 = Math.min(along(slot.endMs), alongLengthPx);
            if (a1 <= a0) return null;
            const box = horizontal
              ? place(a0, a1, weatherY, weatherY + WEATHER_BAND_PX)
              : place(a0, a1, 0, WEATHER_BAND_PX);
            return (
              <rect
                key={`weather-${slot.point.t}`}
                {...box}
                fill={weatherColor(slot.assessment.quality)}
                data-weather-quality={slot.assessment.quality}
              >
                <title>{`${formatClock(slot.point.t)} · ${weatherDetail(slot)}${weather?.stale === true ? " · stale forecast" : ""}`}</title>
              </rect>
            );
          })}
          {resolvedWeatherSlots.map((slot) => {
            const a = along(Date.parse(slot.point.t));
            if (a < 0 || a > alongLengthPx) return null;
            const Icon = weatherIcon[slot.assessment.icon];
            const [x, y] = horizontal ? [a - 7, 15] : [13, a - 7];
            return (
              <g key={`weather-icon-${slot.point.t}`} data-testid="weather-hour" data-weather-quality={slot.assessment.quality}>
                <Icon x={x} y={y} width={14} height={14} color={weatherColor(slot.assessment.quality)} strokeWidth={2} />
                <title>{`${formatClock(slot.point.t)} · ${weatherDetail(slot)}${weather?.stale === true ? " · stale forecast" : ""}`}</title>
              </g>
            );
          })}
        </g>
      )}

      {/* moon-up band with a phase glyph, once */}
      {moon !== undefined && (
        <g data-testid="moon-band">
          {moon.intervals.map((interval) => {
            const a0 = Math.max(along(interval.startMs), 0);
            const a1 = Math.min(along(interval.endMs), alongLengthPx);
            if (a1 <= a0) return null;
            const box = horizontal ? place(a0, a1, 45, 45 + MOON_BAND_PX) : place(a0, a1, 12, 12 + MOON_BAND_PX);
            return (
              <rect key={interval.startMs} {...box} fill="var(--ssc-fg-muted)" fillOpacity={0.5} data-moon-up>
                <title>{`Moon up ${formatClock(new Date(interval.startMs).toISOString())} – ${formatClock(new Date(interval.endMs).toISOString())}`}</title>
              </rect>
            );
          })}
          {moon.intervals.map((interval) => {
            const a0 = Math.max(along(interval.startMs), 0);
            const a1 = Math.min(along(interval.endMs), alongLengthPx);
            if (a1 <= a0) return null;
            const mid = (a0 + a1) / 2;
            const [cx, cy] = horizontal ? [mid, 45 + MOON_BAND_PX / 2] : [MOON_BAND_PX / 2 + 24, mid];
            const r = 6;
            return (
              <g key={`glyph-${String(interval.startMs)}`} data-testid="moon-glyph" data-illumination={moon.illuminatedFraction ?? "unknown"}>
                <circle cx={cx} cy={cy} r={r} fill="var(--ssc-surface)" stroke="var(--ssc-fg-muted)" />
                {moon.illuminatedFraction !== null && (
                  <path d={moonPhasePath(cx, cy, r, moon.illuminatedFraction, moon.waxing ?? true)} fill="var(--ssc-fg)" />
                )}
                <title>
                  {moon.illuminatedFraction === null
                    ? "Moon phase not reported"
                    : `Moon ${String(Math.round(moon.illuminatedFraction * 100))}% lit, ${moon.waxing === true ? "waxing" : "waning"}`}
                </title>
              </g>
            );
          })}
        </g>
      )}

      {/* resolved events: a line each, once, spanning every lane. Rows
          stagger close labels apart (civil/nautical/astronomical dusk are
          often minutes apart); a label too close to the previous one on
          its row abbreviates instead of overlapping it. */}
      {labelledEvents.map((event, index) => {
        const a = along(event.atMs);
        const [x1, y1] = point(a, horizontal ? 57 : 0);
        const [x2, y2] = point(a, totalLane);
        const placement = eventPlacements[index];
        const row = placement?.row ?? 0;
        const text = placement?.text ?? event.label;
        // Edge events (especially astronomical dusk/dawn on a phone's
        // horizontally scrolling strip) must not be drawn outside the SVG
        // viewport. Anchor the first label inward from the left edge and the
        // last label inward from the right edge; the event line stays at the
        // exact astronomical instant.
        const atRightEdge = horizontal && a > alongLengthPx - 110;
        const tx = horizontal
          ? atRightEdge
            ? Math.min(alongLengthPx - 4, a + 2)
            : Math.max(4, a + 2)
          : totalLane - 4 - row * 36;
        const ty = horizontal ? 69 + row * 11 : a - 3;
        return (
          <g key={event.event} data-event={event.event}>
            <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--ssc-border-strong)" strokeOpacity={0.9} strokeDasharray="2 3" />
            {(horizontal || visibleVerticalEvents.has(event.event)) && (
              <text x={tx} y={ty} textAnchor={horizontal && atRightEdge ? "end" : horizontal ? "start" : "end"} fill="var(--ssc-fg-muted)" style={{ font: FONT }}>
                {text}
              </text>
            )}
            <title>{`${event.label} ${formatClock(new Date(event.atMs).toISOString())}`}</title>
          </g>
        );
      })}

      {/* The Moon's altitude, using the Moon's own theme color — once,
          in the header lane box on desktop, lane 0's box on phone. */}
      {moonAltitude !== undefined && moonAltitude.length > 1 && (
        <polyline
          data-testid="moon-curve"
          fill="none"
          stroke="var(--ssc-moon)"
          strokeOpacity={0.95}
          strokeWidth={2}
          pointerEvents="none"
          points={curvePoints(moonAltitude, horizontal ? [0, headerPx] : (boxes[0] ?? [0, totalLane]))}
        >
          <title>Moon altitude throughout the night</title>
        </polyline>
      )}

      {[...items].sort((a, b) => Number(a.selected) - Number(b.selected)).map((entry) => (
        <ItemBlock
          key={entry.item.id}
          orientation={orientation}
          axis={axis}
          range={range}
          axisPx={axisPx}
          laneBox={boxes[entry.laneIndex] ?? boxes[0] ?? [headerPx, headerPx + laneThicknessPx]}
          events={events}
          timeZone={timeZone}
          item={entry.item}
          compiled={entry.compiled}
          altitude={entry.altitude}
          moonSeparation={entry.moonSeparation}
          moonAltitude={moonAltitude}
          runStatus={entry.runStatus}
          label={entry.label}
          selected={entry.selected}
          hovered={entry.hovered ?? false}
          bounds={entry.bounds}
          onSelect={onSelect}
          onHover={onHover}
          onCommit={onCommit}
          onCommitError={onCommitError}
          patternId={patternId}
          curvePoints={curvePoints}
        />
      ))}

      {/* Draw after blocks: the previous placement under their opaque fills
          made the current-time line vanish exactly where a running item was. */}
      {nowAlong !== null && (() => {
        const [x1, y1] = point(nowAlong, 0);
        const [x2, y2] = point(nowAlong, totalLane);
        const [tx, ty] = horizontal ? [Math.min(nowAlong + 4, alongLengthPx - 24), 42] : [totalLane - 3, nowAlong - 4];
        return (
          <g data-testid="current-time-marker" pointerEvents="none">
            <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--ssc-bg)" strokeWidth={5} />
            <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--ssc-accent)" strokeWidth={2.5} />
            <text x={tx} y={ty} textAnchor={horizontal ? "start" : "end"} fill="var(--ssc-accent)" style={{ font: FONT }}>
              Now
            </text>
            <title>{`Current time ${formatClock(new Date(nowMs).toISOString())}`}</title>
          </g>
        );
      })()}
    </svg>
  );
});

interface Drag {
  mode: "move" | "resize-start" | "resize-end";
  originPx: number;
  startMs: number;
  endMs: number;
  proposedStartMs: number;
  proposedEndMs: number;
  snapped: SnapTarget | null;
  /** Snapped to an item edge instead of a resolved event. Arbitrary move
   * edges commit as absolute time; resize-start's original previous edge
   * remains relative, and resize-end changes only duration. */
  neighborSnap: "previous" | "next" | "edge" | null;
  moved: boolean;
}

interface ItemBlockProps {
  orientation: Orientation;
  axis: AxisMap;
  range: AxisRange;
  axisPx: number;
  laneBox: LaneBox;
  events: readonly SnapTarget[];
  timeZone: string;
  item: PlanItem;
  compiled?: CompiledItem;
  altitude?: readonly AltitudeSample[];
  moonSeparation?: readonly MoonSeparationSample[];
  moonAltitude?: readonly AltitudeSample[];
  runStatus?: RunItemStatus;
  label?: string;
  selected: boolean;
  hovered: boolean;
  overlapping?: boolean;
  /** Resize boundaries and whole-timeline move snap edges. */
  bounds: DragBounds;
  onSelect?: (itemId: string) => void;
  onHover?: (itemId: string | null) => void;
  onCommit?: (itemId: string, patch: ItemPatch) => void;
  onCommitError?: (message: string) => void;
  patternId: string;
  curvePoints: (samples: readonly AltitudeSample[], laneBox: LaneBox, slot?: Interval) => string;
}

/** One item's block, altitude curve and drag handles, on its own lane box
 * of the shared strip. `memo`d so a selection or drag elsewhere on a
 * 20-item plan redraws this block only when its own props actually
 * change — the render-budget test in `TimelinePane.test.tsx` is timed
 * against exactly this path. */
const ItemBlock = memo(function ItemBlock({
  orientation,
  axis,
  range,
  axisPx,
  laneBox,
  events,
  timeZone,
  item,
  compiled,
  altitude,
  moonSeparation,
  moonAltitude,
  runStatus,
  label,
  selected,
  hovered,
  overlapping,
  bounds,
  onSelect,
  onHover,
  onCommit,
  onCommitError,
  patternId,
  curvePoints,
}: ItemBlockProps) {
  const [drag, setDragState] = useState<Drag | null>(null);
  // Native lostcapture and pointermove can arrive in one React batch. A
  // cancelled gesture must disappear before the next callback reads it.
  const dragRef = useRef<Drag | null>(null);
  const setDrag = (next: Drag | null) => {
    dragRef.current = next;
    setDragState(next);
  };
  const [hoveredResize, setHoveredResize] = useState<"start" | "end" | null>(null);
  const [hoverTimeMs, setHoverTimeMs] = useState<number | null>(null);
  const horizontal = orientation === "horizontal";
  const [lane0, lane1] = laneBox;
  const laneThickness = lane1 - lane0;

  const along = (atMs: number) => axis.toPx(atMs);
  const place = (a0: number, a1: number, l0: number, l1: number): Box =>
    horizontal
      ? { x: a0, y: l0, width: Math.max(0, a1 - a0), height: Math.max(0, l1 - l0) }
      : { x: l0, y: a0, width: Math.max(0, l1 - l0), height: Math.max(0, a1 - a0) };
  const point = (a: number, l: number): [number, number] => (horizontal ? [a, l] : [l, a]);

  // Desktop's rate is constant end to end, so a drag step there stays the
  // one-division arithmetic it always was — the render-budget test is timed
  // against exactly this path. Only the compressed vertical axis (where a
  // fixed pixel delta is not a fixed time delta once it crosses a collapsed
  // gap) needs the full toPx→toTime round trip.
  const pxPerMs = axisPx / (range.endMs - range.startMs);
  const fromPxDelta = (fromMs: number, deltaPx: number): number =>
    horizontal ? fromMs + deltaPx / pxPerMs : axis.toTime(axis.toPx(fromMs) + deltaPx);

  const scheduled = compiled?.start_utc != null && compiled.end_utc !== null;
  const startMs = scheduled ? new Date(compiled.start_utc ?? 0).getTime() : null;
  const endMs = scheduled ? new Date(compiled.end_utc ?? 0).getTime() : null;
  const shownStartMs = drag?.proposedStartMs ?? startMs;
  const shownEndMs = drag?.proposedEndMs ?? endMs;
  const diagnostics = compiled?.diagnostics ?? [];
  const severity = overlapping ? "error" : worstSeverity(diagnostics);
  const editable = onCommit !== undefined && scheduled;
  const panelCount = item.kind === "mosaic" ? (compiled?.panels.length ?? 0) : 0;
  /** The item's own `end` names an event rather than a plain duration — the
   * block shows an anchor glyph at its right edge, and dragging that edge
   * converts it to a duration only on release, never silently. */
  const endAnchored = item.end != null && item.end.kind !== "duration";
  const minAltDeg = minAltDegOf(item);

  const beginDrag = (e: ReactPointerEvent<SVGElement>, mode: Drag["mode"]) => {
    if (!editable || startMs === null || endMs === null) return;
    if (e.button !== 0 && e.pointerType === "mouse") return;
    e.preventDefault();
    const target = e.currentTarget;
    if (typeof target.setPointerCapture === "function" && Number.isFinite(e.pointerId)) {
      target.setPointerCapture(e.pointerId);
    }
    setDrag({
      mode,
      originPx: pointerAlong(e, orientation),
      startMs,
      endMs,
      proposedStartMs: startMs,
      proposedEndMs: endMs,
      snapped: null,
      neighborSnap: null,
      moved: false,
    });
  };

  const updateDrag = (e: ReactPointerEvent<SVGElement>) => {
    const drag = dragRef.current;
    if (drag === null) return;
    const deltaPx = pointerAlong(e, orientation) - drag.originPx;
    const moved = drag.moved || Math.abs(deltaPx) >= TAP_SLOP_PX;
    // Re-derived from the pointer's actual pixel position, not accumulated
    // in ms: under compression a fixed pixel delta is not a fixed time
    // delta once it crosses a collapsed gap. Clamped to the immediate
    // neighbours the whole time — a single-resource schedule's slots never
    // overlap while moving; that is intentional so an item can cross an
    // existing item and be placed before it. Resizes remain neighbour-bound.
    // The compiler highlights any temporary overlap in red.
    if (drag.mode === "move") {
      const wanted = fromPxDelta(drag.startMs, deltaPx);
      const result = resolveMoveDrag(wanted, drag.endMs - drag.startMs, events, axis, bounds, SNAP_RADIUS_PX);
      setDrag({
        ...drag,
        moved,
        snapped: result.snapped,
        neighborSnap: result.snappedToItemEdge
          ? result.proposedStartMs === bounds.prevEndMs && bounds.prevEndCommitsRelative !== false ? "previous" : "edge"
          : null,
        proposedStartMs: result.proposedStartMs,
        proposedEndMs: result.proposedStartMs + (drag.endMs - drag.startMs),
      });
    } else if (drag.mode === "resize-end") {
      const wanted = fromPxDelta(drag.endMs, deltaPx);
      const result = resolveResizeDrag(wanted, drag.startMs, events, axis, bounds, SNAP_RADIUS_PX);
      setDrag({
        ...drag,
        moved,
        snapped: result.snapped,
        neighborSnap: result.snappedToNextStart ? "next" : null,
        proposedEndMs: result.proposedEndMs,
      });
    } else {
      const wanted = fromPxDelta(drag.startMs, deltaPx);
      const minStart = bounds.prevEndMs ?? range.startMs;
      const maxStart = drag.endMs - 60_000;
      const clamped = Math.max(minStart, Math.min(maxStart, wanted));
      const snapped = events
        .filter((event) => event.atMs >= minStart && event.atMs <= maxStart)
        .map((event) => ({ event, distance: Math.abs(axis.toPx(event.atMs) - axis.toPx(wanted)) }))
        .filter(({ distance }) => distance <= SNAP_RADIUS_PX)
        .sort((a, b) => a.distance - b.distance)[0]?.event;
      const previousDistance = bounds.prevEndMs === null ? Infinity : Math.abs(axis.toPx(bounds.prevEndMs) - axis.toPx(clamped));
      const eventDistance = snapped === undefined ? Infinity : Math.abs(axis.toPx(snapped.atMs) - axis.toPx(clamped));
      const atPreviousEdge = bounds.prevEndMs !== null && bounds.prevEndMs <= maxStart
        && previousDistance <= SNAP_RADIUS_PX && previousDistance <= eventDistance;
      const proposedStartMs = atPreviousEdge && bounds.prevEndMs !== null ? bounds.prevEndMs : snapped?.atMs ?? clamped;
      setDrag({
        ...drag,
        moved,
        snapped: !atPreviousEdge && snapped?.atMs === proposedStartMs ? snapped : null,
        neighborSnap: atPreviousEdge ? (bounds.prevEndCommitsRelative === false ? "edge" : "previous") : null,
        proposedStartMs,
      });
    }
  };

  const endDrag = () => {
    const drag = dragRef.current;
    if (drag === null) return;
    setDrag(null);
    if (!drag.moved) {
      onSelect?.(item.id);
      return;
    }
    if (onCommit === undefined) return;
    if (drag.mode === "move" || drag.mode === "resize-start") {
      let start: TimeSpec;
      if (drag.neighborSnap === "previous") {
        start = { kind: "relative", offset_min: 0 };
      } else if (drag.snapped !== null) {
        start = { kind: "event", event: drag.snapped.event, offset_min: 0 };
      } else {
        const wall = utcToSiteWallClock(drag.proposedStartMs, timeZone);
        if (wall === null) {
          onCommitError?.(`This browser does not know the site's time zone (${timeZone}); the start was not changed.`);
          return;
        }
        start = { kind: "absolute", local_datetime: wall.local_datetime, fold: wall.fold };
      }
      if (drag.mode === "resize-start") {
        const totalMin = (drag.proposedEndMs - drag.proposedStartMs) / 60_000;
        const minutes = totalMin / Math.max(1, panelCount);
        onCommit(item.id, { start, end: { kind: "duration", minutes: Math.max(1 / Math.max(1, panelCount), minutes) } });
      } else {
        onCommit(item.id, { start });
      }
      return;
    }
    // End resize always writes a plain duration (DESIGN.md §4.1) — snapping
    // to the next item's start just means `proposedEndMs` already lands
    // exactly there.
    const totalMin = (drag.proposedEndMs - drag.startMs) / 60_000;
    const minutes = totalMin / Math.max(1, panelCount);
    onCommit(item.id, { end: { kind: "duration", minutes: Math.max(1 / Math.max(1, panelCount), minutes) } });
  };

  const onKeyDown = (e: KeyboardEvent<SVGElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect?.(item.id);
    }
  };

  // --- geometry of the block in its lane ---------------------------------- //
  let block: Box | null = null;
  let blockA0 = 0;
  let blockA1 = 0;
  if (shownStartMs !== null && shownEndMs !== null) {
    blockA0 = along(shownStartMs);
    blockA1 = Math.max(blockA0 + 2, along(shownEndMs));
    block = place(blockA0, blockA1, lane0 + BLOCK_INSET_PX, lane1 - BLOCK_INSET_PX);
  }
  const blockLength = blockA1 - blockA0;
  const moveHitA0 = blockLength >= HIT_MIN_PX ? blockA0 : (blockA0 + blockA1) / 2 - HIT_MIN_PX / 2;
  const moveHitA1 = blockLength >= HIT_MIN_PX ? blockA1 : (blockA0 + blockA1) / 2 + HIT_MIN_PX / 2;
  // Keep both edge targets inside their block. The old outward 44px
  // targets covered adjacent short blocks and each other after snapping.
  const resizeHitSize = Math.min(HIT_MIN_PX, blockLength / 3);
  const resizeHitA0 = blockA1 - resizeHitSize;
  const resizeStartHitA0 = blockA0;

  const hoverAltitude = hoverTimeMs === null ? null : interpolateAt(altitude, hoverTimeMs, (s) => s.altitude_deg);
  const hoverSeparation = hoverTimeMs === null ? null : interpolateAt(moonSeparation, hoverTimeMs, (s) => s.separation_deg);
  const hoverMoonAltitude = hoverTimeMs === null ? null : interpolateAt(moonAltitude, hoverTimeMs, (s) => s.altitude_deg);
  const hoverDetails = hoverTimeMs === null ? "" :
    `${formatClock(new Date(hoverTimeMs).toISOString())} · Target alt ${hoverAltitude === null ? "unavailable" : `${hoverAltitude.toFixed(1)}°`} · Moon separation ${hoverSeparation === null ? "unavailable" : `${hoverSeparation.toFixed(1)}°`} · Moon alt ${hoverMoonAltitude === null ? "unavailable" : `${hoverMoonAltitude.toFixed(1)}°`}`;
  const title = [label ?? item.kind, hoverDetails, compiled?.resolution ?? "", compiled?.end_resolution ?? "", ...diagnostics.map((d) => `${d.severity}: ${d.message}`)]
    .filter((s) => s !== "")
    .join("\n");

  const updateHoverTime = (e: ReactPointerEvent<SVGGElement>) => {
    const svg = e.currentTarget.ownerSVGElement;
    if (svg === null || !Number.isFinite(e.clientX)) return;
    const atMs = axis.toTime(horizontal ? e.clientX - svg.getBoundingClientRect().left : e.clientY - svg.getBoundingClientRect().top);
    const bounded = Math.max(shownStartMs ?? range.startMs, Math.min(shownEndMs ?? range.endMs, atMs));
    setHoverTimeMs(Math.round(bounded / 60_000) * 60_000);
  };

  const cursor = drag !== null ? "grabbing" : "grab";
  const draggedTotalMinutes = drag === null
    ? null
    : Math.max(1, Math.round((drag.proposedEndMs - drag.proposedStartMs) / 60_000));
  const draggedPerPanelMinutes = draggedTotalMinutes === null || panelCount <= 1
    ? null
    : Math.max(1, Math.round(draggedTotalMinutes / panelCount));

  return (
    <g data-testid={`strip-${item.id}`}>
      {block !== null && compiled !== undefined && (
        <g
          role="button"
          tabIndex={0}
          aria-pressed={selected}
          aria-label={`${label ?? item.kind}, ${compiled.resolution}`}
          data-testid={`block-${item.id}`}
          data-start-utc={compiled.start_utc ?? undefined}
          data-end-utc={compiled.end_utc ?? undefined}
          data-run-status={runStatus ?? "pending"}
          onKeyDown={onKeyDown}
          onPointerEnter={(e) => {
            onHover?.(item.id);
            updateHoverTime(e);
          }}
          onPointerMove={(e) => {
            updateHoverTime(e);
          }}
          onPointerLeave={() => {
            onHover?.(null);
            setHoverTimeMs(null);
          }}
          style={{ outline: "none" }}
        >
          <title>{title}</title>
          <rect
            {...block}
            rx={4}
            data-testid="block-rect"
            data-severity={severity ?? "none"}
            fill={runStatus === "done" ? "var(--ssc-success)" : runStatus === "running" ? "var(--ssc-accent)" : runStatus === "failed" ? "var(--ssc-danger)" : selected ? "var(--ssc-accent-muted)" : "var(--ssc-surface-raised)"}
            fillOpacity={runStatus === "done" || runStatus === "running" || runStatus === "failed" ? 0.35 : 1}
            stroke={severity !== null ? SEVERITY_STROKE[severity] : selected ? "var(--ssc-accent)" : "var(--ssc-border-strong)"}
            strokeWidth={selected || severity === "error" ? 2 : hovered ? 1.5 : 1}
            data-hovered={hovered || undefined}
          />
          {blockLength < HIT_MIN_PX && (
            <circle
              {...(() => {
                const [cx, cy] = point((blockA0 + blockA1) / 2, lane0 + laneThickness / 2);
                return { cx, cy };
              })()}
              r={selected ? 6 : 4}
              fill={selected ? "var(--ssc-accent)" : "var(--ssc-fg-muted)"}
              pointerEvents="none"
              data-testid="short-action-marker"
            />
          )}
          {!item.enabled && <rect {...block} rx={4} fill={`url(#${patternId})`} fillOpacity={0.6} data-testid="block-disabled" />}
          {/* the target's altitude curve, clipped to this item's own
              scheduled span (`shownStartMs`/`shownEndMs`, live while
              dragging) so it reads as a bounded arc for this capture, not
              one line threading through every other item's block and the
              idle time between them. Drawn *over* the block's own opaque
              fill (its clipped span is now exactly the block's own width,
              so "behind" the fill would mean invisible) but still behind
              the label/badge/hit-areas below. */}
          {altitude !== undefined && altitude.length > 1 && shownStartMs !== null && shownEndMs !== null && (
            <polyline
              data-testid="altitude-curve"
              fill="none"
              stroke="var(--ssc-accent)"
              strokeOpacity={0.8}
              strokeWidth={1.5}
              pointerEvents="none"
              points={curvePoints(altitude, laneBox, { startMs: shownStartMs, endMs: shownEndMs })}
            />
          )}
          {/* the item's own altitude-limit constraint, drawn as a red
              reference line across the same clipped span as its altitude
              curve — so a dip below it is visible right against the line
              that causes the `below_min_altitude`/`dips_below_min_altitude`
              diagnostic, not only inferable from the badge text. No line
              when the item sets no limit — never an invented default. */}
          {minAltDeg !== null && shownStartMs !== null && shownEndMs !== null && (
            <line
              data-testid="min-alt-line"
              {...(() => {
                const l = laneBox[1] - (Math.max(0, Math.min(90, minAltDeg)) / 90) * laneThickness;
                const [x1, y1] = point(along(shownStartMs), l);
                const [x2, y2] = point(along(shownEndMs), l);
                return { x1, y1, x2, y2 };
              })()}
              stroke="var(--ssc-danger)"
              strokeWidth={1}
              strokeDasharray="3 2"
              pointerEvents="none"
            >
              <title>{`Altitude limit ${String(minAltDeg)}°`}</title>
            </line>
          )}
          {compiled.panels.map((panel) => {
            const p0 = along(new Date(panel.start_utc).getTime() + (shownStartMs ?? 0) - (startMs ?? 0));
            const p1 = along(new Date(panel.end_utc).getTime() + (shownStartMs ?? 0) - (startMs ?? 0));
            const box = place(p0 + 1, p1 - 1, lane0 + BLOCK_INSET_PX + 3, lane1 - BLOCK_INSET_PX - 3);
            return (
              <rect key={`${String(panel.row)}-${String(panel.col)}`} {...box} fill="none" stroke="var(--ssc-border-strong)" strokeOpacity={0.8} data-testid="panel">
                <title>{`Panel ${String(panel.row)},${String(panel.col)}: ${formatClock(panel.start_utc)} – ${formatClock(panel.end_utc)}`}</title>
              </rect>
            );
          })}
          <BlockLabel horizontal={horizontal} block={block} a0={blockA0} a1={blockA1} text={label ?? item.kind} enabled={item.enabled} />
          {overlapping ? (
            <Badge
              horizontal={horizontal}
              a0={blockA0}
              a1={blockA1}
              laneBox={laneBox}
              severity="error"
              text="OVERLAPPING"
              titleText="This item overlaps another scheduled item."
            />
          ) : severity !== null && diagnostics[0] !== undefined ? (
            <Badge
              horizontal={horizontal}
              a0={blockA0}
              a1={blockA1}
              laneBox={laneBox}
              severity={severity}
              text={`${severity}: ${diagnosticShortLabel(diagnostics[0].code)}${diagnostics.length > 1 ? ` +${String(diagnostics.length - 1)}` : ""}`}
              titleText={diagnostics.map((d) => `${d.severity}: ${d.message}`).join("\n")}
            />
          ) : null}
          {/* hit areas: move (the body, at least 44 px) then resize (the end edge) */}
          <rect
            {...place(moveHitA0, moveHitA1, lane0, lane1)}
            fill="transparent"
            data-testid="block-move-handle"
            style={{ cursor: editable ? cursor : "pointer", touchAction: "none" }}
            onPointerDown={(e) => {
              if (editable) beginDrag(e, "move");
              else onSelect?.(item.id);
            }}
            onPointerMove={updateDrag}
            onPointerUp={endDrag}
            onLostPointerCapture={() => {
              setDrag(null);
            }}
            onPointerCancel={() => {
              setDrag(null);
            }}
          />
          {endAnchored && (
            <g data-testid="end-anchor" pointerEvents="none">
              {(() => {
                const [ax, ay] = point(blockA1 - 2, lane0 + laneThickness / 2);
                const s = 4;
                const d = horizontal
                  ? `M ${String(ax - s)} ${String(ay - s)} L ${String(ax)} ${String(ay)} L ${String(ax - s)} ${String(ay + s)} Z`
                  : `M ${String(ax - s)} ${String(ay - s)} L ${String(ax)} ${String(ay)} L ${String(ax + s)} ${String(ay - s)} Z`;
                return <path d={d} fill="var(--ssc-fg-muted)" />;
              })()}
              <title>{`Ends ${resolutionLabel(compiled.end_resolution)} — not a fixed duration.`}</title>
            </g>
          )}
          {editable && hoveredResize === "start" && (
            <line
              {...(() => {
                const [x1, y1] = point(blockA0, lane0 + BLOCK_INSET_PX + 6);
                const [x2, y2] = point(blockA0, lane1 - BLOCK_INSET_PX - 6);
                return { x1, y1, x2, y2 };
              })()}
              stroke="var(--ssc-accent)"
              strokeWidth={4}
              pointerEvents="none"
            />
          )}
          {editable && (hovered || selected || drag?.mode === "resize-start") && (
            <rect
              {...place(resizeStartHitA0, resizeStartHitA0 + resizeHitSize, lane0, lane1)}
              fill="transparent"
              data-testid="block-resize-start-handle"
              aria-hidden
              style={{ cursor: horizontal ? "ew-resize" : "ns-resize", touchAction: "none" }}
              onPointerDown={(e) => {
                beginDrag(e, "resize-start");
              }}
              onPointerEnter={() => { setHoveredResize("start"); }}
              onPointerLeave={() => { setHoveredResize(null); }}
              onPointerMove={updateDrag}
              onPointerUp={endDrag}
              onLostPointerCapture={() => {
                setDrag(null);
              }}
              onPointerCancel={() => {
                setDrag(null);
              }}
            />
          )}
          {editable && hoveredResize === "end" && (
            <line
              {...(() => {
                const [x1, y1] = point(blockA1, lane0 + BLOCK_INSET_PX + 6);
                const [x2, y2] = point(blockA1, lane1 - BLOCK_INSET_PX - 6);
                return { x1, y1, x2, y2 };
              })()}
              stroke="var(--ssc-accent)"
              strokeWidth={4}
              pointerEvents="none"
            />
          )}
          {editable && (
            <rect
              {...place(resizeHitA0, resizeHitA0 + resizeHitSize, lane0, lane1)}
              fill="transparent"
              data-testid="block-resize-handle"
              aria-hidden
              style={{ cursor: horizontal ? "ew-resize" : "ns-resize", touchAction: "none" }}
              onPointerDown={(e) => {
                beginDrag(e, "resize-end");
              }}
              onPointerEnter={() => { setHoveredResize("end"); }}
              onPointerLeave={() => { setHoveredResize(null); }}
              onPointerMove={updateDrag}
              onPointerUp={endDrag}
              onLostPointerCapture={() => {
                setDrag(null);
              }}
              onPointerCancel={() => {
                setDrag(null);
              }}
            >
              {endAnchored && <title>{`Converts "until ${resolutionLabel(compiled.end_resolution)}" to a duration.`}</title>}
            </rect>
          )}
          {editable && (
            <line
              {...(() => {
                const [x1, y1] = point(blockA1 - 1, lane0 + BLOCK_INSET_PX + 6);
                const [x2, y2] = point(blockA1 - 1, lane1 - BLOCK_INSET_PX - 6);
                return { x1, y1, x2, y2 };
              })()}
              stroke="var(--ssc-fg-muted)"
              strokeWidth={2}
              pointerEvents="none"
            />
          )}
        </g>
      )}

      {hoverTimeMs !== null && block !== null && drag === null && (
        <g data-testid="hover-details" pointerEvents="none" {...(() => {
          const x = horizontal ? Math.max(2, Math.min(axisPx - 260, along(hoverTimeMs) + 8)) : 4;
          const y = horizontal ? Math.max(2, lane0 - 35) : Math.max(2, Math.min(axisPx - 34, along(hoverTimeMs) + 8));
          return { transform: `translate(${String(x)} ${String(y)})` };
        })()}>
          <rect width={258} height={32} rx={4} fill="var(--ssc-surface-raised)" stroke="var(--ssc-border-strong)" />
          <text x={6} y={12} fill="var(--ssc-fg)" style={{ font: "600 10px sans-serif" }}>
            {formatClock(new Date(hoverTimeMs).toISOString())} · Target altitude {hoverAltitude === null ? "—" : `${hoverAltitude.toFixed(1)}°`}
          </text>
          <text x={6} y={25} fill="var(--ssc-moon)" style={{ font: "10px sans-serif" }}>
            Moon separation {hoverSeparation === null ? "—" : `${hoverSeparation.toFixed(1)}°`} · Moon altitude {hoverMoonAltitude === null ? "—" : `${hoverMoonAltitude.toFixed(1)}°`}
          </text>
        </g>
      )}

      {/* live time while dragging — nothing is written until release */}
      {drag !== null && drag.moved && (
        <text
          data-testid="drag-label"
          {...(() => {
            const middle = Math.max(32, Math.min(axisPx - 32, (blockA0 + blockA1) / 2));
            const [x, y] = horizontal
              ? [middle, lane0 + laneThickness / 2 + 4]
              : [lane0 + laneThickness / 2, middle + 4];
            return { x, y };
          })()}
          fill="var(--ssc-fg)"
          style={{ font: FONT }}
          className="tabular-nums"
          textAnchor="middle"
          pointerEvents="none"
        >
          {drag.mode === "move"
            ? `${formatClock(new Date(drag.proposedStartMs).toISOString())} – ${formatClock(new Date(drag.proposedEndMs).toISOString())}`
            : draggedPerPanelMinutes === null
              ? `${String(draggedTotalMinutes)} min total`
              : `${String(draggedPerPanelMinutes)} min/panel · ${String(draggedTotalMinutes)} min total`}
          {drag.snapped !== null
            ? ` · ${drag.snapped.label}`
            : drag.neighborSnap === "previous"
              ? " · touching previous item"
              : drag.neighborSnap === "next"
                ? " · touching next item"
                : drag.neighborSnap === "edge"
                  ? " · snapped to item"
                : ""}
        </text>
      )}
    </g>
  );
});

function BlockLabel({
  horizontal,
  block,
  a0,
  a1,
  text,
  enabled,
}: {
  horizontal: boolean;
  block: Box;
  a0: number;
  a1: number;
  text: string;
  enabled: boolean;
}) {
  // Inside the block when it is long enough to hold the text, else just
  // past its end — a 0-minute shutdown is a 2 px sliver with a name beside it.
  let x: number;
  let y: number;
  if (horizontal) {
    x = a1 - a0 >= 64 ? a0 + 6 : a1 + 6;
    y = block.y + block.height / 2 + 4;
  } else {
    x = block.x + 8;
    y = a1 - a0 >= 18 ? a0 + 13 : a1 + 12;
  }
  return (
    <text x={x} y={y} fill={enabled ? "var(--ssc-fg)" : "var(--ssc-fg-muted)"} style={{ font: "500 10px sans-serif" }} pointerEvents="none" data-testid="block-label">
      {text}
      {!enabled ? " (off)" : ""}
    </text>
  );
}

function Badge({
  horizontal,
  a0,
  a1,
  laneBox,
  severity,
  text,
  titleText,
}: {
  horizontal: boolean;
  a0: number;
  a1: number;
  laneBox: LaneBox;
  severity: Severity;
  text: string;
  titleText: string;
}) {
  const [lane0, lane1] = laneBox;
  const badgeWidth = text.length * 5.8 + 10;
  const inside = a1 - a0 >= badgeWidth + 90;
  const [x, y] = horizontal
    ? [inside ? a1 - badgeWidth - 6 : a1 + 6, lane0 + (lane1 - lane0) / 2 - 8]
    : [lane1 - badgeWidth - 8, a1 - a0 >= 44 ? a1 - 22 : a1 + 4];
  return (
    <g data-testid="block-badge" data-severity={severity} pointerEvents="none">
      <title>{titleText}</title>
      <rect x={x} y={y} width={badgeWidth} height={16} rx={8} fill="var(--ssc-surface-raised)" stroke={SEVERITY_STROKE[severity]} />
      <text x={x + 5} y={y + 11.5} fill={SEVERITY_STROKE[severity]} style={{ font: FONT }}>
        {text}
      </text>
    </g>
  );
}
