// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/** Planning's target and Moon timeline shares Scheduling's twilight geometry,
 * colour gradient and Moon-up band. Its measured SVG width keeps time/event
 * labels readable both in a narrow sidebar and across the full workspace. */
import { SegmentedControl } from "../../components/SegmentedControl";
import { useId, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { useAnnualAltitudeQuery, type AnnualAltitudeNightModel, type AnnualAltitudeRequest, type AnnualAltitudeResponse, type NightResponse } from "../../api/astro";
import type { components } from "../../api/schema";
import { Card, CardTitle } from "../../components/Card";
import { useLayoutRoute } from "../../layout/LayoutContext";
import { useLayoutStore } from "../../layout/layoutStore";
import { ABBREVIATED_EVENT_LABELS, EVENT_LABELS } from "../SchedulingTab/timeSpecs";
import { axisRange, bandFill, hourTicks, labelLayout, linearAxisMap, moonPhasePath, moonUpIntervals, twilightBands, twilightGradientStops } from "../SchedulingTab/timelineGeometry";
import {
  altitudeCurvePoints,
  altitudeToY,
  annualDarkPeakAltitude,
  nightAltitudeHover,
  nightAltitudeMarkers,
  nightWindowFromNightResponse,
  pointerPxToClampedMs,
} from "./nightAltitudeGeometry";

type AltitudeSample = components["schemas"]["AltitudeSampleModel"];
type TimelineView = "daily" | "yearly";

// Initial width before the first measurement; the rendered axis uses actual pixels.
const AXIS_PX = 1280;
// Match Scheduling's time ruler: hour labels live above the plotted lane.
const LANE_TOP_PX = 22;
// Keep the altitude lane compact enough to leave the sky map visible below
// it. The previous 312px lane dominated the Planning screen; 150px makes the
// complete chart (axis and event labels included) roughly half as tall while
// retaining enough separation for the target and Moon curves.
const LANE_HEIGHT_PX = 150;
const LANE_BOTTOM_PX = LANE_TOP_PX + LANE_HEIGHT_PX;
const TICK_LABEL_Y = 16;
const EVENT_LABEL_BASE_Y = LANE_BOTTOM_PX + 16;
const EVENT_LABEL_ROWS = 2;
const EVENT_LABEL_ROW_PX = 11;
const HEIGHT = EVENT_LABEL_BASE_Y + EVENT_LABEL_ROWS * EVENT_LABEL_ROW_PX + 2;
// Match the scheduler’s compact, readable time ruler.
const FONT = "10px sans-serif";

function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

export function NightAltitudeChart({
  night,
  moonAltitude,
  annualAltitudeRequest,
  instantMs,
  onInstantChange,
}: {
  night: NightResponse;
  /** The Moon's own altitude across the same window (`useMoonAltitudeQuery`,
   * mapped to `{ when, altitude_deg }` the same way `TimelinePane.tsx` does
   * for the Scheduling strip). Absent while it is loading or has not been
   * asked for yet — never a guessed flat line (DESIGN.md §4.4's own
   * convention for this curve). */
  moonAltitude?: readonly AltitudeSample[];
  /** One per-night target altitude reduction from `/api/astro/annual-altitude`.
   * Used only by the Yearly view; the Daily view remains the interactive
   * whole-night scrubber with Moon altitude. */
  annualAltitudeRequest: AnnualAltitudeRequest | null;
  /** The scrubbed instant, drawn as a solid cursor across the night — the
   * one the rest of the tab is showing. `null` when the session start has
   * not resolved yet, in which case no cursor is drawn rather than one at a
   * guessed time. */
  instantMs?: number | null;
  /** Makes the chart the time control: dragging anywhere sets the instant
   * (owner: "whole night timeline with dragable current time line would be
   * imo great addition, below the skychart"). Omitted, the chart stays a
   * read-only readout, which is what it was built as. */
  onInstantChange?: (atMs: number) => void;
}) {
  const [chartWidth, setChartWidth] = useState(AXIS_PX);
  const axisPx = chartWidth;
  const gradientId = useId();
  const nightWindow = useMemo(() => nightWindowFromNightResponse(night), [night]);
  const range = useMemo(() => axisRange(nightWindow, []), [nightWindow]);
  const axis = useMemo(() => linearAxisMap(range, axisPx), [range, axisPx]);
  const bands = useMemo(() => twilightBands(nightWindow, range), [nightWindow, range]);

  // --- hover / touch-scrub readout ---------------------------------------- //
  //
  // A mouse reports hover for free (`pointermove` with no button down); a
  // touch does not, so a finger has to actively drag before there is
  // anything to read — `scrubbing` is that latch, set only for a non-mouse
  // pointer. `touchAction: "pan-y"` on the `<svg>` is what keeps that drag
  // from being ambiguous with the page's own vertical scroll: it tells the
  // browser only vertical panning is its own gesture to claim, so a
  // vertical touch scrolls the page exactly as it would over any other
  // element (delivered to us as `pointercancel`, which drops the readout),
  // while a horizontal drag is left for our own `pointermove` handler — the
  // same "don't trap the scroll" fix already applied to the sky map's
  // iframes, here for a one-axis chart instead of a two-axis pan/zoom.
  const svgRef = useRef<SVGSVGElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const layoutRoute = useLayoutRoute();
  const cardHeight = useLayoutStore((state) => state.saved.tabs[layoutRoute.tab]?.cards["planning.timeline"]?.height);
  const [view, setView] = useState<TimelineView>("daily");
  const annualAltitude = useAnnualAltitudeQuery(view === "yearly" ? annualAltitudeRequest : null);
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (viewport === null || typeof ResizeObserver === "undefined") return;
    const update = () => {
      const width = viewport.getBoundingClientRect().width;
      if (width > 0) setChartWidth(width);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(viewport);
    return () => { observer.disconnect(); };
  }, []);
  // The card's editor height grows the plotted altitude lane, rather than
  // leaving an empty area beneath a fixed SVG. Both views keep an explicit
  // height and measured width: width changes must not resize the altitude lane.
  const chartHeight = cardHeight === undefined ? HEIGHT : Math.max(HEIGHT - LANE_HEIGHT_PX + 80, cardHeight - 100);
  const laneHeight = chartHeight - (HEIGHT - LANE_HEIGHT_PX);
  const laneBottom = LANE_TOP_PX + laneHeight;
  const eventLabelBaseY = laneBottom + 16;
  const totalHeight = eventLabelBaseY + EVENT_LABEL_ROWS * EVENT_LABEL_ROW_PX + 2;
  const [pointerAtMs, setPointerAtMs] = useState<number | null>(null);
  const [scrubbing, setScrubbing] = useState(false);

  const updateHoverFromEvent = (e: ReactPointerEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (rect === undefined || rect.width === 0) return;
    const px = ((e.clientX - rect.left) / rect.width) * axisPx;
    const atMs = pointerPxToClampedMs(px, range, axis);
    setPointerAtMs(atMs);
    // Only while a button/finger is actually down: a bare mouse hover reads
    // values out, it does not move the session's instant out from under the
    // person reading them.
    if (onInstantChange !== undefined && (e.buttons !== 0 || e.pointerType !== "mouse")) {
      onInstantChange(atMs);
    }
  };
  const handlePointerMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (e.pointerType === "mouse" || scrubbing) updateHoverFromEvent(e);
  };
  const handlePointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (e.pointerType !== "mouse") {
      setScrubbing(true);
      // A genuine touch always has an active pointer to capture, but a
      // pointer that already released between the browser dispatching this
      // event and this handler running (a very fast tap) does not — this
      // would otherwise throw and abort the handler *before* the line below
      // ever set the readout, silently losing the tap's own hover update.
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        // No active pointer to capture — fall through and still read it.
      }
    }
    updateHoverFromEvent(e);
  };
  const handlePointerUp = () => {
    setScrubbing(false);
  };
  // A cancelled gesture (the browser took it as a scroll instead) leaves no
  // instant worth reporting — unlike a deliberate release, which keeps the
  // last read value on screen so a finger doesn't have to stay down in the
  // dark to read it.
  const handlePointerCancel = () => {
    setScrubbing(false);
    setPointerAtMs(null);
  };
  const handlePointerLeave = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (e.pointerType === "mouse") setPointerAtMs(null);
  };

  const hover = useMemo(
    () => (pointerAtMs === null ? null : nightAltitudeHover(pointerAtMs, night.altitude_curve ?? [], moonAltitude ?? [])),
    [pointerAtMs, night.altitude_curve, moonAltitude],
  );
  const moonIntervals = moonUpIntervals(nightWindow, range);
  const gradientStops = twilightGradientStops(bands, axis.toPx, axisPx);
  const markers = useMemo(
    () => nightAltitudeMarkers(nightWindow).filter((m) => m.atMs >= range.startMs && m.atMs <= range.endMs),
    [nightWindow, range],
  );

  const tickStepHours = axisPx / ((range.endMs - range.startMs) / 3_600_000) < 44 ? 2 : 1;
  const ticks = useMemo(
    () => hourTicks(range, tickStepHours).filter((t) => t >= range.startMs && t <= range.endMs),
    [range, tickStepHours],
  );
  const markerPlacements = labelLayout(markers, axis.toPx, (event) => ABBREVIATED_EVENT_LABELS[event] ?? EVENT_LABELS[event], EVENT_LABEL_ROWS);

  const targetCurve = night.altitude_curve ?? [];
  const targetPoints = targetCurve.length > 1 ? altitudeCurvePoints(targetCurve, range, axis, LANE_TOP_PX, laneBottom) : "";
  const moonPoints =
    moonAltitude !== undefined && moonAltitude.length > 1
      ? altitudeCurvePoints(moonAltitude, range, axis, LANE_TOP_PX, laneBottom)
      : "";

  return (
    <Card collapseKey="planning.timeline" className="space-y-1">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <CardTitle>Planning timeline</CardTitle>
        <SegmentedControl label="Timeline view" value={view} onChange={setView}
          options={[{ value: "daily", label: "Daily" }, { value: "yearly", label: "Yearly" }]} />
      </div>

      <div ref={viewportRef}>
      {view === "daily" ? (
        <>
          <svg
            ref={svgRef}
            viewBox={`0 0 ${String(axisPx)} ${String(totalHeight)}`}
            className="block w-full shrink-0 select-none"
            style={{ touchAction: "pan-y", height: chartHeight }}
            role="img"
            aria-label="This target's altitude and the Moon's altitude across the session, over twilight-shaded background bands, with astronomical dusk/dawn and moonrise/moonset marked. Hover or drag to read the values at a given time."
            onPointerMove={handlePointerMove}
            onPointerDown={handlePointerDown}
            onPointerUp={handlePointerUp}
            onPointerCancel={handlePointerCancel}
            onPointerLeave={handlePointerLeave}
          >
            <defs>
              <linearGradient id={gradientId} gradientUnits="userSpaceOnUse" x1={0} y1={0} x2={axisPx} y2={0}>
                {gradientStops.map((stop, index) => <stop key={index} offset={`${String(stop.offset * 100)}%`} stopColor={bandFill(stop.level)} />)}
              </linearGradient>
            </defs>
            <rect x={0} y={LANE_TOP_PX} width={axisPx} height={laneHeight} fill={`url(#${gradientId})`} data-testid="planning-twilight-gradient" />
            {bands.map((band) => {
              const x0 = Math.max(axis.toPx(band.startMs), 0);
              const x1 = Math.min(axis.toPx(band.endMs), axisPx);
              if (x1 <= x0) return null;
              return (
                <rect key={`${band.name}-${String(band.startMs)}`} x={x0} y={LANE_TOP_PX} width={x1 - x0} height={laneHeight} fill="transparent" data-band={band.name}>
                  <title>{`${band.name}: ${formatClock(new Date(band.startMs).toISOString())} – ${formatClock(new Date(band.endMs).toISOString())}`}</title>
                </rect>
              );
            })}

            <g data-testid="planning-moon-band" pointerEvents="none">
              {moonIntervals.map(interval => {
                const x0 = axis.toPx(interval.startMs), x1 = axis.toPx(interval.endMs);
                const cx = (x0 + x1) / 2, cy = LANE_TOP_PX + 9;
                return <g key={interval.startMs}>
                  <rect x={x0} y={LANE_TOP_PX + 3} width={x1 - x0} height={12} fill="var(--ssc-fg-muted)" fillOpacity={0.5} />
                  <circle cx={cx} cy={cy} r={5} fill="var(--ssc-surface)" stroke="var(--ssc-fg-muted)" />
                  <path d={moonPhasePath(cx, cy, 5, night.moon.illuminated_fraction, night.moon.waxing)} fill="var(--ssc-fg)" />
                  <title>{`Moon up ${formatClock(new Date(interval.startMs).toISOString())} – ${formatClock(new Date(interval.endMs).toISOString())}`}</title>
                </g>;
              })}
            </g>
            {[0, 30, 60, 90].map((deg) => {
              const y = laneBottom - (deg / 90) * laneHeight;
              return (
                <g key={deg}>
                  <line x1={0} y1={y} x2={axisPx} y2={y} stroke="var(--ssc-border)" strokeWidth={deg === 0 ? 1 : 0.5} />
                  <text x={2} y={y - 2} fill="var(--ssc-fg-muted)" style={{ font: FONT }}>{`${String(deg)}°`}</text>
                </g>
              );
            })}

            {ticks.map((t) => {
              const x = axis.toPx(t);
              return (
                <g key={t}>
                  <line x1={x} y1={0} x2={x} y2={6} stroke="var(--ssc-border-strong)" />
                  <line x1={x} y1={LANE_TOP_PX} x2={x} y2={laneBottom} stroke="var(--ssc-border)" strokeOpacity={0.6} />
                  <text x={Math.min(axisPx - 2, Math.max(2, x))} y={TICK_LABEL_Y} textAnchor={x < 24 ? "start" : x > axisPx - 24 ? "end" : "middle"} fill="var(--ssc-fg-muted)" style={{ font: FONT }} className="tabular-nums">
                    {formatClock(new Date(t).toISOString())}
                  </text>
                </g>
              );
            })}

            {markers.map((marker, index) => {
              const x = axis.toPx(marker.atMs);
              const placement = markerPlacements[index];
              const row = placement?.row ?? 0;
              const text = placement?.text ?? marker.label;
              return (
                <g key={marker.event}>
                  <line x1={x} y1={LANE_TOP_PX} x2={x} y2={laneBottom} stroke="var(--ssc-border-strong)" strokeOpacity={0.9} strokeDasharray="2 3" />
                  <text x={Math.min(axisPx - 4, Math.max(4, x + 2))} textAnchor={x > axisPx - 110 ? "end" : "start"} y={eventLabelBaseY + row * EVENT_LABEL_ROW_PX} fill="var(--ssc-fg-muted)" style={{ font: FONT }}>
                    {text}
                  </text>
                  <title>{`${marker.label} ${formatClock(new Date(marker.atMs).toISOString())}`}</title>
                </g>
              );
            })}

            {moonPoints !== "" && <polyline fill="none" stroke="var(--ssc-moon)" strokeOpacity={0.95} strokeWidth={2} points={moonPoints} />}
            {targetPoints !== "" && <polyline fill="none" stroke="var(--ssc-accent)" strokeOpacity={0.9} strokeWidth={1.5} points={targetPoints} />}
            {instantMs !== null && instantMs !== undefined && instantMs >= range.startMs && instantMs <= range.endMs && (
              <line
                x1={axis.toPx(instantMs)}
                y1={LANE_TOP_PX}
                x2={axis.toPx(instantMs)}
                y2={laneBottom}
                stroke="var(--ssc-accent)"
                strokeWidth={2}
              />
            )}
            {hover !== null && (
              <g>
                <line
                  x1={axis.toPx(hover.atMs)}
                  y1={LANE_TOP_PX}
                  x2={axis.toPx(hover.atMs)}
                  y2={laneBottom}
                  stroke="var(--ssc-fg)"
                  strokeOpacity={0.8}
                  strokeWidth={1}
                />
                {hover.target !== null && (
                  <circle
                    cx={axis.toPx(hover.target.atMs)}
                    cy={altitudeToY(hover.target.altitudeDeg, LANE_TOP_PX, laneBottom)}
                    r={2.5}
                    fill="var(--ssc-accent)"
                  />
                )}
                {hover.moon !== null && (
                  <circle
                    cx={axis.toPx(hover.moon.atMs)}
                    cy={altitudeToY(hover.moon.altitudeDeg, LANE_TOP_PX, laneBottom)}
                    r={2.5}
                    fill="var(--ssc-fg-muted)"
                  />
                )}
              </g>
            )}
          </svg>

          <div className="min-h-6 rounded border border-border bg-surface-raised px-2 py-0.5 text-[9px]">
            {hover === null ? (
              <p className="text-fg-muted">Hover, or drag on touch, to read the altitude at a given time.</p>
            ) : (
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                <span className="font-semibold tabular-nums text-fg">{formatClock(new Date(hover.atMs).toISOString())}</span>
                <span className="inline-flex items-center gap-1 tabular-nums text-accent">
                  <span aria-hidden className="inline-block h-0.5 w-3 bg-accent align-middle" />
                  Target {hover.target === null ? "—" : `${hover.target.altitudeDeg.toFixed(0)}°`}
                </span>
                <span className="inline-flex items-center gap-1 tabular-nums text-fg-muted">
                  <span aria-hidden className="inline-block h-0.5 w-3 bg-fg-muted align-middle" />
                  Moon {hover.moon === null ? "—" : `${hover.moon.altitudeDeg.toFixed(0)}°`}
                </span>
              </div>
            )}
          </div>

          <p className="text-[9px] text-fg-muted">
            <span className="mr-3 inline-flex items-center gap-1">
              <span aria-hidden className="inline-block h-0.5 w-3 bg-accent align-middle" /> Target
            </span>
            <span className="inline-flex items-center gap-1">
              <span aria-hidden className="inline-block h-0.5 w-3 bg-fg-muted align-middle" /> Moon
            </span>
            {night.moon.separation_deg !== null && (
              <span className="ml-3">
                Moon separation (session midpoint): <span className="tabular-nums text-fg">{night.moon.separation_deg.toFixed(1)}°</span>
              </span>
            )}
          </p>
        </>
      ) : (
        <AnnualAltitudeChart
          data={annualAltitude.data}
          pending={annualAltitude.isPending && annualAltitudeRequest !== null}
          error={annualAltitude.isError}
          errorMessage={annualAltitude.error instanceof Error ? annualAltitude.error.message : undefined}
          chartHeight={chartHeight}
          axisPx={axisPx}
        />
      )}
      </div>
    </Card>
  );
}

function formatMonthDay(iso: string): string {
  return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });
}

function annualY(altitudeDeg: number, topPx: number, bottomPx: number): number {
  return bottomPx - (Math.max(0, Math.min(90, altitudeDeg)) / 90) * (bottomPx - topPx);
}

function annualX(index: number, count: number): number {
  if (count <= 1) return 0;
  return (index / (count - 1)) * AXIS_PX;
}

function nearestAnnualNight(nights: readonly AnnualAltitudeNightModel[], px: number): AnnualAltitudeNightModel | null {
  if (nights.length === 0) return null;
  const index = Math.max(0, Math.min(nights.length - 1, Math.round((px / AXIS_PX) * (nights.length - 1))));
  return nights[index] ?? null;
}

function annualNowX(nights: readonly AnnualAltitudeNightModel[], nowMs: number = Date.now()): number | null {
  if (nights.length === 0) return null;
  const startMs = new Date(nights[0]?.window_start ?? "").getTime();
  const lastMs = new Date(nights[nights.length - 1]?.window_start ?? "").getTime();
  if (!Number.isFinite(startMs) || !Number.isFinite(lastMs) || nowMs < startMs || nowMs > lastMs) return null;
  if (lastMs <= startMs) return 0;
  return ((nowMs - startMs) / (lastMs - startMs)) * AXIS_PX;
}

function AnnualAltitudeChart({
  data,
  pending,
  error,
  errorMessage,
  chartHeight,
  axisPx,
}: {
  data?: AnnualAltitudeResponse;
  pending: boolean;
  error: boolean;
  errorMessage?: string;
  chartHeight: number;
  axisPx: number;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [pointerNight, setPointerNight] = useState<AnnualAltitudeNightModel | null>(null);
  const laneTop = LANE_TOP_PX;
  const laneHeight = chartHeight - (HEIGHT - LANE_HEIGHT_PX);
  const laneBottom = laneTop + laneHeight;
  const height = chartHeight;
  const xForNight = (index: number, count: number) => annualX(index, count) * axisPx / AXIS_PX;
  const nights = data?.nights ?? [];
  const usable = nights.filter((night) => annualDarkPeakAltitude(night) !== null);
  const peakPoints = usable
    .map((night) => {
      const index = nights.indexOf(night);
      const peak = annualDarkPeakAltitude(night);
      return peak === null
        ? ""
        : `${xForNight(index, nights.length).toFixed(1)},${annualY(peak, laneTop, laneBottom).toFixed(1)}`;
    })
    .filter((point) => point !== "")
    .join(" ");
  const monthTicks = nights
    .map((night, index) => ({ night, index, day: new Date(night.window_start).getUTCDate() }))
    .filter((tick) => tick.index === 0 || tick.day === 1);
  const unscaledNowX = annualNowX(nights);
  const nowX = unscaledNowX === null ? null : unscaledNowX * axisPx / AXIS_PX;

  const updatePointer = (event: ReactPointerEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (rect === undefined || rect.width === 0) return;
    const px = ((event.clientX - rect.left) / rect.width) * AXIS_PX;
    setPointerNight(nearestAnnualNight(nights, px));
  };

  const status = error
    ? errorMessage ?? "Yearly altitude request failed."
    : pending ? "Loading yearly altitude…"
      : data === undefined ? "Pick a target to see yearly altitude." : null;

  return (
    <>
      <div className="relative">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${String(axisPx)} ${String(height)}`}
        className="block w-full shrink-0 select-none"
        style={{ touchAction: "pan-y", height: chartHeight }}
        role="img"
        aria-label="This target's highest altitude during astronomical darkness for each night across the year. No Moon curves are shown in the yearly view."
        onPointerMove={updatePointer}
        onPointerDown={updatePointer}
        onPointerLeave={() => { setPointerNight(null); }}
      >
        <rect x={0} y={laneTop} width={axisPx} height={laneHeight} fill="var(--ssc-bg)" opacity={0.35} />
        {[0, 30, 60, 90].map((deg) => {
          const y = annualY(deg, laneTop, laneBottom);
          return (
            <g key={deg}>
              <line x1={0} y1={y} x2={axisPx} y2={y} stroke="var(--ssc-border)" strokeWidth={deg === 0 ? 1 : 0.5} />
              <text x={2} y={y - 2} fill="var(--ssc-fg-muted)" style={{ font: FONT }}>{`${String(deg)}°`}</text>
            </g>
          );
        })}
        {monthTicks.map(({ night, index }) => {
          const x = xForNight(index, nights.length);
          return (
            <g key={night.window_start}>
              <line x1={x} y1={laneTop} x2={x} y2={laneBottom} stroke="var(--ssc-border)" strokeOpacity={0.5} />
              <text x={x + 2} y={laneBottom + 13} fill="var(--ssc-fg-muted)" style={{ font: FONT }}>
                {new Date(night.window_start).toLocaleDateString([], { month: "short" })}
              </text>
            </g>
          );
        })}
        {usable.map((night) => {
          const index = nights.indexOf(night);
          const x = xForNight(index, nights.length);
          const peak = annualDarkPeakAltitude(night);
          if (peak === null) return null;
          const hours = night.hours_above_limit ?? 0;
          const opacity = Math.max(0.18, Math.min(0.7, hours / 6));
          return (
            <line
              key={night.window_start}
              x1={x}
              y1={laneBottom}
              x2={x}
              y2={annualY(peak, laneTop, laneBottom)}
              stroke="var(--ssc-accent)"
              strokeOpacity={opacity}
              strokeWidth={Math.max(1, axisPx / Math.max(1, nights.length) - 0.5)}
            />
          );
        })}
        {peakPoints !== "" && <polyline fill="none" stroke="var(--ssc-accent)" strokeOpacity={0.95} strokeWidth={1.4} points={peakPoints} />}
        <line
          x1={0}
          y1={annualY((data?.min_altitude_deg ?? 0), laneTop, laneBottom)}
          x2={axisPx}
          y2={annualY((data?.min_altitude_deg ?? 0), laneTop, laneBottom)}
          stroke="var(--ssc-warning)"
          strokeDasharray="4 3"
        />
        {nowX !== null && (
          <g>
            <line
              x1={nowX}
              y1={laneTop}
              x2={nowX}
              y2={laneBottom}
              stroke="var(--ssc-accent)"
              strokeWidth={2}
            />
            <text x={Math.min(nowX + 4, axisPx - 20)} y={laneTop + 10} fill="var(--ssc-accent)" style={{ font: FONT }}>
              NOW
            </text>
          </g>
        )}
        {pointerNight !== null && (
          <g>
            <line
              x1={xForNight(nights.indexOf(pointerNight), nights.length)}
              y1={laneTop}
              x2={xForNight(nights.indexOf(pointerNight), nights.length)}
              y2={laneBottom}
              stroke="var(--ssc-fg)"
              strokeOpacity={0.75}
            />
            {annualDarkPeakAltitude(pointerNight) !== null && (
              <circle
                cx={xForNight(nights.indexOf(pointerNight), nights.length)}
                cy={annualY(annualDarkPeakAltitude(pointerNight) ?? 0, laneTop, laneBottom)}
                r={2.5}
                fill="var(--ssc-accent)"
              />
            )}
          </g>
        )}
      </svg>
      {status !== null && <p role={error ? "alert" : "status"} className={`absolute inset-0 flex items-center justify-center rounded border bg-surface-raised px-2 py-1 text-xs ${error ? "border-danger text-danger" : "border-border text-fg-muted"}`}>{status}</p>}
      </div>
      <div className="min-h-6 rounded border border-border bg-surface-raised px-2 py-0.5 text-[9px]">
        {pointerNight === null ? (
          <p className="text-fg-muted">Hover, or drag on touch, to read the yearly altitude summary.</p>
        ) : (
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
            <span className="font-semibold tabular-nums text-fg">{formatMonthDay(pointerNight.window_start)}</span>
            <span className="tabular-nums text-accent">
              Max {pointerNight.max_altitude_deg.toFixed(0)}° {formatClock(pointerNight.max_when)}
            </span>
            <span className="tabular-nums text-fg-muted">
              Dark peak {pointerNight.peak_altitude_deg === null || pointerNight.peak_altitude_deg === undefined ? "—" : `${pointerNight.peak_altitude_deg.toFixed(0)}° ${formatClock(pointerNight.peak_when ?? pointerNight.max_when)}`}
            </span>
            <span className="tabular-nums text-fg-muted">
              Above {(data?.min_altitude_deg ?? 0).toFixed(0)}°: {pointerNight.hours_above_limit === null || pointerNight.hours_above_limit === undefined ? "—" : `${pointerNight.hours_above_limit.toFixed(1)} h`}
            </span>
          </div>
        )}
      </div>
      <p className="text-[9px] text-fg-muted">
        <span className="mr-3 inline-flex items-center gap-1">
          <span aria-hidden className="inline-block h-0.5 w-3 bg-accent align-middle" /> Peak during darkness
        </span>
        <span className="inline-flex items-center gap-1">
          <span aria-hidden className="inline-block h-0.5 w-3 bg-warning align-middle" /> Minimum altitude
        </span>
        <span className="ml-3 inline-flex items-center gap-1">
          <span aria-hidden className="inline-block h-0.5 w-3 bg-accent align-middle" /> Now
        </span>
      </p>
    </>
  );
}
