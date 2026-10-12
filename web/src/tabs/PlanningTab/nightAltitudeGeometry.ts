// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pure geometry for `NightAltitudeChart` — Planning's single-target echo of
 * the Scheduling tab's Gantt strip (`SchedulingTab/timelineGeometry.ts`,
 * `TimelineStrip.tsx`). No React/DOM here, so it is testable against
 * fixtures the same way `timelineGeometry.test.ts` already tests its own
 * pure functions.
 *
 * `NightResponse` (`/api/astro/night`, this tab's own query) and
 * `NightWindow` (`CompiledSchedule["night"]`, Scheduling's) both describe
 * the same server-side solar/lunar solve but do not share field names —
 * `window_start`/`window_end` vs `window_start_utc`/`window_end_utc`,
 * `EventTimeModel.when` vs `ResolvedEvent.when_utc` — so `axisRange`/
 * `twilightBands`/`snapTargets` (Scheduling's, unmodified) cannot be called
 * on a `NightResponse` directly. `nightWindowFromNightResponse` is the small
 * adapter that reconciles the two shapes, here rather than in
 * `timelineGeometry.ts`, which stays exactly as Scheduling's own callers
 * already use it.
 */
import type { AnnualAltitudeNightModel, EventTimeModel, NightResponse } from "../../api/astro";
import type { NightWindow, ResolvedEvent } from "../../api/plans";
import { type AxisMap, type AxisRange, type SnapTarget, snapTargets } from "../SchedulingTab/timelineGeometry";

export function annualDarkPeakAltitude(night: AnnualAltitudeNightModel): number | null {
  return typeof night.peak_altitude_deg === "number" && Number.isFinite(night.peak_altitude_deg)
    ? night.peak_altitude_deg
    : null;
}

function toResolvedEvents(events: readonly EventTimeModel[]): ResolvedEvent[] {
  return events.map((e) => ({ event: e.event, when_utc: e.when, outcome: e.outcome, threshold_deg: e.threshold_deg }));
}

/**
 * `NightResponse` reshaped into `NightWindow` so Scheduling's own
 * `axisRange`/`twilightBands`/`snapTargets` can be reused unmodified.
 * `date`/`tz`/`dark_start_utc`/`dark_end_utc` are not read by any of those
 * three functions (checked against their bodies, not assumed) — filled with
 * honest placeholders rather than left to a type-level lie, but never
 * consumed here.
 */
export function nightWindowFromNightResponse(night: NightResponse): NightWindow {
  return {
    date: night.window_start.slice(0, 10),
    tz: night.location.timezone ?? "UTC",
    window_start_utc: night.window_start,
    window_end_utc: night.window_end,
    dark_start_utc: null,
    dark_end_utc: null,
    solar_events: toResolvedEvents(night.solar_events),
    lunar_events: toResolvedEvents(night.lunar_events),
  };
}

/**
 * The three events the live report named explicitly ("dusk dawn moon
 * set") — astronomical dusk/dawn (the same "dark" boundary
 * `ssc/scheduler/compiler.py` itself uses for `dark_start_utc`/
 * `dark_end_utc`, not the more ambiguous plain "dusk"/"dawn") plus
 * moonrise and moonset for symmetry, in this fixed order so the chart's
 * markers never reshuffle between renders. The twilight bands already
 * encode every dusk/dawn tier as a background colour change; a labelled
 * tick is still worth it for astronomical dusk/dawn specifically because
 * "dusk"/"dawn" on their own are ambiguous which tier they mean — the
 * boundary the bands draw needs a name attached somewhere.
 */
const NIGHT_ALTITUDE_MARKER_EVENTS = ["astronomical_dusk", "astronomical_dawn", "moonrise", "moonset"] as const;

/** Reuses `snapTargets`'s own event resolution (occurs-only, already
 * tested) rather than re-deriving "does this event occur" a second way,
 * filtered down to the four named above. */
export function nightAltitudeMarkers(night: NightWindow): SnapTarget[] {
  const wanted: readonly string[] = NIGHT_ALTITUDE_MARKER_EVENTS;
  return snapTargets(night).filter((t) => wanted.includes(t.event));
}

/** Anything with a UTC timestamp and an altitude — `AltitudeSampleModel`
 * (`night.altitude_curve`) and the Moon-altitude samples once adapted to
 * the same shape (`{ when: s.at, altitude_deg: s.alt_deg }`, the same
 * mapping `TimelinePane.tsx` already does for the Scheduling strip). */
export interface AltitudeLikeSample {
  when: string;
  altitude_deg: number;
}

/** 0-90 deg (clamped) → the lane's y pixel, 90 at `laneTopPx`. The one
 * altitude→y mapping both `altitudeCurvePoints` and the hover readout's own
 * on-curve dot use, so a hover dot is guaranteed to land exactly on the
 * polyline it is marking rather than a second, independently-rounded copy
 * of the same formula drifting from it. */
export function altitudeToY(altitudeDeg: number, laneTopPx: number, laneBottomPx: number): number {
  return laneBottomPx - (Math.max(0, Math.min(90, altitudeDeg)) / 90) * (laneBottomPx - laneTopPx);
}

/**
 * An altitude series as an SVG `points` string against a 0-90 lane
 * `[laneTopPx, laneBottomPx]` (90 deg at the top), clipped to `range` and
 * projected through `axis` — the same technique `TimelineStrip.tsx`'s own
 * (unexported) `curvePoints` uses, without that function's item-slot
 * clipping and dual orientation, neither of which this single always-
 * horizontal chart needs.
 */
export function altitudeCurvePoints(
  samples: readonly AltitudeLikeSample[],
  range: AxisRange,
  axis: AxisMap,
  laneTopPx: number,
  laneBottomPx: number,
): string {
  return samples
    .filter((s) => {
      const atMs = new Date(s.when).getTime();
      return atMs >= range.startMs && atMs <= range.endMs;
    })
    .map((s) => {
      const x = axis.toPx(new Date(s.when).getTime());
      const y = altitudeToY(s.altitude_deg, laneTopPx, laneBottomPx);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

// --- hover / scrub readout -------------------------------------------------- //
//
// The live report asked for "onhover data shown when hovering over the
// curve": the local time plus the target's and the Moon's altitude at the
// hovered instant. Both curves are server-sampled at a fixed cadence (145
// points across the window for the target, one every `step_min` — 10 by
// default — for the Moon: `useNightQuery`/`useMoonAltitudeQuery`), so a
// pointer position between two samples has no real value to show — only
// two real ones either side of it. Snapping to the nearer of those two
// (never interpolating) is what keeps this readout from ever showing a
// number the data does not contain, the same principle `altitudeCurvePoints`
// already applies by clamping rather than inventing altitude.

/** The nearest sample to `atMs` by `when` distance, or `null` for an empty
 * series. Ties (equidistant on both sides — only reachable with
 * artificially coincident sampling, never the server's own even cadence)
 * resolve to the earlier sample, deterministically, rather than whichever
 * `Array.prototype` iteration order happens to visit last. */
export function nearestAltitudeSample<T extends AltitudeLikeSample>(samples: readonly T[], atMs: number): T | null {
  let best: T | null = null;
  let bestDistMs = Infinity;
  for (const s of samples) {
    const distMs = Math.abs(new Date(s.when).getTime() - atMs);
    if (distMs < bestDistMs) {
      bestDistMs = distMs;
      best = s;
    }
  }
  return best;
}

/** A pointer/touch x position (SVG user-space px, already scaled from
 * client px by the caller) to a time clamped to `range` — a drag past
 * either edge of the chart reads as that edge's own instant rather than
 * extrapolating off the axis or leaving the readout stuck off-screen. */
export function pointerPxToClampedMs(px: number, range: AxisRange, axis: AxisMap): number {
  const clampedPx = Math.max(0, Math.min(axis.lengthPx, px));
  const atMs = axis.toTime(clampedPx);
  return Math.max(range.startMs, Math.min(range.endMs, atMs));
}

/** One snapped sample per series, at their own nearest instant to the
 * pointer — the two curves are not resampled onto a shared timeline, so
 * "the Moon's altitude at 21:04" and "the target's altitude at 21:03"
 * appearing together is honest, not a bug. */
export interface NightAltitudeHoverPoint {
  atMs: number;
  altitudeDeg: number;
}

export interface NightAltitudeHover {
  /** The target sample's own instant when one exists (the target curve is
   * the finer-grained, primary series), else the Moon's — this is what the
   * cursor line and the "local time" readout both anchor to, so the line
   * always points at an instant one of the two numbers shown was actually
   * sampled at. */
  atMs: number;
  target: NightAltitudeHoverPoint | null;
  moon: NightAltitudeHoverPoint | null;
}

/** `null` only when both series are empty — nothing anywhere to report. */
export function nightAltitudeHover(
  pointerAtMs: number,
  targetSamples: readonly AltitudeLikeSample[],
  moonSamples: readonly AltitudeLikeSample[],
): NightAltitudeHover | null {
  const targetSample = nearestAltitudeSample(targetSamples, pointerAtMs);
  const moonSample = nearestAltitudeSample(moonSamples, pointerAtMs);
  const anchor = targetSample ?? moonSample;
  if (anchor === null) return null;
  return {
    atMs: new Date(anchor.when).getTime(),
    target: targetSample === null ? null : { atMs: new Date(targetSample.when).getTime(), altitudeDeg: targetSample.altitude_deg },
    moon: moonSample === null ? null : { atMs: new Date(moonSample.when).getTime(), altitudeDeg: moonSample.altitude_deg },
  };
}
