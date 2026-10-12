// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The night timeline (DESIGN.md §4.4) — and the plan's item list, merged
 * onto one shared strip: the S30 Pro is one resource, so a compiled plan's
 * items never truly run at once and never earned one full-width track each
 * (real user report, 2026-09-07 — "only one combined timeline is needed for
 * one device"). `TimelineStrip` draws the axis, twilight bands, moon band
 * and every item's block, altitude curve and diagnostics *once*; every
 * item's block sits at its own real time position on it, stacked onto a
 * second lane only where two items' compiled spans genuinely overlap
 * (`assignLanes`) — the conflict is the visible exception, never the
 * layout default. Altitude curves are clipped to each item's own scheduled
 * span (not the whole night) and always drawn, selected or not — they are
 * part of the timeline's normal read, the same as the twilight bands.
 *
 * Reorder handles remain available on every collapsed row. The enable switch
 * and edit chevron appear only on the selected row. Collapsed rows retain a
 * clickable name and status line (kept, rather than removed
 * outright, because it is also the only way to reach an item with no block
 * to click — e.g. one that failed to compile a start/end — and because
 * `onReorderItem`'s drag needs real row geometry to hit-test against, even
 * for a row that is not expanded). The editor opens inline beneath the
 * selected row (one at a time; the selection *is* the accordion), and the
 * add-item menu lives in the header.
 *
 * Selection is a toggle: a click on a row's name, its block on the strip,
 * or its chevron opens that item's editor and closes any other; a second
 * click (or Escape) closes it. Hovering a row or its block highlights both
 * (`hoveredItemId`) — they are two views of one thing. A pointer that
 * travels past `TAP_SLOP_PX` is a drag, not a click, and writes only on
 * release.
 *
 * Three gestures, three writes, none before release:
 * - drag the block along the time axis → `onEditItem` with a new `start`
 *   (`event` when snapped to a resolved event, else `absolute` in the site's
 *   zone) or, from the end handle, a new duration `end` — even a block whose
 *   `end` named an event converts to a plain duration this way (its own
 *   anchor glyph and the resize handle's tooltip say so up front);
 * - drag the gutter handle up/down (or Alt+↑/↓) → `onReorderItem`, which is
 *   what `relative`/`asap` chains follow;
 * - the switch → `onToggleItem`.
 *
 * Desktop and phone both keep time left→right. On phones the strip has a
 * deliberate minimum width inside a native horizontal scroller, so one-finger
 * touch panning remains predictable. Times stay UTC; `formatClock` is the
 * display edge.
 *
 * Altitude curves come from `/api/astro/night` per distinct target (TanStack
 * cached, one request per target per night); the moon phase from the same
 * endpoint over the window. Nothing astronomical is computed here.
 */
import { useQueries } from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { api, unwrap } from "../../api/client";
import { useMoonAltitudeQuery, useNightQuery, type MoonAltitudeRequest, type NightRequest } from "../../api/astro";
import type { CompiledItem, CompiledSchedule, PlanItem, PlanItemKind } from "../../api/plans";
import { useForecastQuery } from "../../api/weather";
import type { RunState } from "../../api/runs";
import { AddIcon, EnabledIcon } from "../../components/ActionIcons";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { ADVANCED_ITEM_KINDS, ITEM_KIND_LABELS, NORMAL_ITEM_KINDS, itemSummary } from "./itemKinds";
import { estimateIntegration, formatIntegrationDuration } from "./integrationEstimate";
import { formatClock } from "./localTime";
import {
  FALLBACK_AXIS_PX,
  axisRange,
  missingSolarEvents,
  moonUpIntervals,
  snapTargets,
  timeToPx,
  twilightBands,
  type AxisRange,
  type DragBounds,
} from "./timelineGeometry";
import { TimelineStrip, type ItemPatch, type MoonBand, type Orientation, type StripItem } from "./TimelineStrip";
import { BELOW_SM_QUERY, useMediaQuery } from "./useMediaQuery";

export type { ItemPatch } from "./TimelineStrip";

export interface TimelinePaneProps {
  schedule: CompiledSchedule | undefined;
  /** Journal-derived run state, ignored when it belongs to another plan/night. */
  run?: RunState | null;
  isPending: boolean;
  isError: boolean;
  /** Why there is no schedule, when `isError` — the server's own words. */
  errorMessage?: string;
  selectedItemId: string | null;
  /** `null` collapses the open editor. */
  onSelectItem: (itemId: string | null) => void;
  /** The stored plan's items, in order — the rows. Absent until a plan is open. */
  items?: readonly PlanItem[];
  /** The item with unsaved edits, if any — marked visibly. */
  dirtyItemId?: string | null;
  /** A save is in flight: structural gestures are disabled meanwhile. */
  busy?: boolean;
  /** The editor for the selected item, rendered beneath its row. */
  editor?: ReactNode;
  onEditItem?: (itemId: string, patch: ItemPatch) => void;
  onReorderItem?: (itemId: string, newIndex: number) => void;
  onToggleItem?: (itemId: string, enabled: boolean) => void;
  onAddItem?: (kind: PlanItemKind) => void;
}

const SELECT_CLASSES = "min-h-[44px] w-full min-w-0 rounded-md border border-border-strong bg-bg px-3 text-base text-fg lg:min-h-11";

function nightLine(night: CompiledSchedule["night"]): string {
  const dark =
    night.dark_start_utc !== null && night.dark_end_utc !== null
      ? `${formatClock(night.dark_start_utc)} – ${formatClock(night.dark_end_utc)}`
      : "— no astronomical night on this date at this latitude";
  return `Night of ${night.date} (${night.tz}) · dark ${dark}`;
}

function moonLine(moon: MoonBand): string {
  const phase =
    moon.illuminatedFraction === null
      ? "Moon: phase not reported"
      : `Moon ${String(Math.round(moon.illuminatedFraction * 100))}% lit, ${moon.waxing === true ? "waxing" : "waning"}`;
  return moon.intervals.length === 0 ? `${phase} · down all night` : phase;
}

function rowLabel(item: PlanItem): string {
  const summary = itemSummary(item);
  return summary === "" ? ITEM_KIND_LABELS[item.kind] : `${ITEM_KIND_LABELS[item.kind]} · ${summary}`;
}

/** A fixed J2000 position for the altitude curve request below — only a
 * catalogue target has one; a body target moves, so it is not requested
 * here (DESIGN.md §3.6, `ssc.plan.models.TargetFields.ra_j2000_h`: `None`
 * for a body). */
function targetOf(item: PlanItem): { ra_j2000_h: number; dec_j2000_deg: number } | null {
  const target =
    item.kind === "goto_and_stack" || item.kind === "take_exposures" || item.kind === "mosaic" || item.kind === "slew" || item.kind === "sync"
      ? item.target
      : undefined;
  if (target?.kind !== "catalog") return null;
  // Fresh target-bearing items use the schema's visible placeholder
  // (null, 0, 0). It is not a real object and must not trigger a misleading
  // altitude request/curve at the celestial origin.
  if ((target.name === null || target.name === "") && target.ra_j2000_h === 0 && target.dec_j2000_deg === 0) return null;
  return { ra_j2000_h: target.ra_j2000_h, dec_j2000_deg: target.dec_j2000_deg };
}

/** The strip column's live width — `ResizeObserver` where there is one,
 * the fallback where there is no layout (tests). */
function useMeasuredWidth(ref: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(FALLBACK_AXIS_PX);
  useLayoutEffect(() => {
    const element = ref.current;
    if (element === null || typeof ResizeObserver !== "function") return;
    const update = () => {
      const measured = element.getBoundingClientRect().width;
      if (measured > 0) setWidth(measured);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, [ref]);
  return width;
}

interface Reorder {
  itemId: string;
  fromIndex: number;
  toIndex: number;
}

export function TimelinePane({
  schedule,
  run = null,
  isPending,
  isError,
  errorMessage,
  selectedItemId,
  onSelectItem,
  items = [],
  dirtyItemId = null,
  busy = false,
  editor,
  onEditItem,
  onReorderItem,
  onToggleItem,
  onAddItem,
}: TimelinePaneProps) {
  // Phones use the same left-to-right axis as desktop, inside a deliberate
  // horizontal scroller. The previous vertical transpose made event labels,
  // times and item blocks compete for a narrow column and was difficult to
  // drag accurately with a finger.
  const mobile = useMediaQuery(BELOW_SM_QUERY);
  const orientation: Orientation = "horizontal";
  const listRef = useRef<HTMLOListElement>(null);
  const paneWidth = useMeasuredWidth(listRef);
  const [addKind, setAddKind] = useState<PlanItemKind>("polar_alignment");
  // Tied to the schedule it arose under: a recompile clears it, without an
  // effect.
  const [commitErrorState, setCommitErrorState] = useState<{ message: string; schedule: CompiledSchedule | undefined } | null>(null);
  const commitError = commitErrorState !== null && commitErrorState.schedule === schedule ? commitErrorState.message : null;
  const setCommitError = useCallback(
    (message: string) => {
      setCommitErrorState({ message, schedule });
    },
    [schedule],
  );
  const [reorder, setReorder] = useState<Reorder | null>(null);
  const rowRefs = useRef(new Map<string, HTMLLIElement>());
  // The gutter row and the strip's block for the same item are two views of
  // one thing (DESIGN.md §4.4's single shared strip); hovering either
  // highlights both.
  const [hoveredItemId, setHoveredItemId] = useState<string | null>(null);

  // Stable handlers for the memoised strips: they read the latest props
  // through a ref, so a selection change re-renders one strip, not twenty.
  const latest = useRef({ selectedItemId, onSelectItem, onEditItem, busy });
  useEffect(() => {
    latest.current = { selectedItemId, onSelectItem, onEditItem, busy };
  });
  const handleSelect = useCallback((itemId: string) => {
    const { selectedItemId: current, onSelectItem: select } = latest.current;
    select(current === itemId ? null : itemId);
  }, []);
  const handleCommit = useCallback((itemId: string, patch: ItemPatch) => {
    const { onEditItem: edit, busy: saving } = latest.current;
    if (edit !== undefined && !saving) edit(itemId, patch);
  }, []);
  const canEdit = onEditItem !== undefined && !busy;

  // Escape collapses the open editor, from wherever focus is.
  useEffect(() => {
    if (selectedItemId === null) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") onSelectItem(null);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [selectedItemId, onSelectItem]);

  const compiledById = useMemo(() => {
    const map = new Map<string, CompiledItem>();
    for (const item of schedule?.items ?? []) map.set(item.item_id, item);
    return map;
  }, [schedule]);
  const matchingRun = run === null || schedule === undefined ? null :
    run.plan_id === schedule.plan_id && run.night_date === schedule.night.date ? run : null;
  const runItemStatus = useMemo(
    () => new Map(matchingRun?.items?.map((item) => [item.item_id, item.status]) ?? []),
    [matchingRun],
  );

  // --- the axis, once per schedule ------------------------------------- //
  const night = schedule?.night;
  const baseRange: AxisRange | null = useMemo(
    () => (schedule === undefined ? null : axisRange(schedule.night, schedule.items)),
    [schedule],
  );
  const zoomScope = schedule === undefined ? null : `${schedule.plan_id}:${schedule.night.date}`;
  const [zoomState, setZoomState] = useState<{ scope: string | null; range: AxisRange } | null>(null);
  const zoomRange = zoomState?.scope === zoomScope ? zoomState.range : null;
  const setZoomRange = useCallback((value: AxisRange) => {
    setZoomState({ scope: zoomScope, range: value });
  }, [zoomScope]);
  const range = zoomRange ?? baseRange;
  const bands = useMemo(() => (night !== undefined && range !== null ? twilightBands(night, range) : []), [night, range]);
  const events = useMemo(() => (night !== undefined ? snapTargets(night) : []), [night]);
  const missing = useMemo(() => (night !== undefined ? missingSolarEvents(night) : []), [night]);

  // Every compiled item's real span — the vertical axis's "occupied" set,
  // so idle time between them (an `asap` chain's slew, a long wait) can
  // collapse instead of scaling 1:1 with real minutes (timelineGeometry.ts
  // "vertical-mode compression"). Built once per schedule, not per row, per
  // drag step, or per frame.
  // The shared strip is the full pane width now — no gutter beside it to
  // subtract for (the gutter list lives below it instead).
  const axisPx = Math.max(mobile ? 800 : 120, paneWidth);
  const timelineViewportRef = useRef<HTMLDivElement>(null);
  const centeredTimelineScopeRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    const viewport = timelineViewportRef.current;
    if (!mobile || viewport === null || range === null || zoomScope === null) return;
    if (centeredTimelineScopeRef.current === zoomScope) return;
    const nowMs = Date.now();
    if (nowMs < range.startMs || nowMs > range.endMs) {
      centeredTimelineScopeRef.current = zoomScope;
      return;
    }
    const viewportWidth = viewport.clientWidth;
    if (viewportWidth <= 0) return;
    const nowPx = timeToPx(nowMs, range, axisPx);
    viewport.scrollLeft = Math.max(0, Math.min(axisPx - viewportWidth, nowPx - viewportWidth / 2));
    centeredTimelineScopeRef.current = zoomScope;
  }, [axisPx, mobile, range, zoomScope]);
  const zoomPointers = useRef(new Map<number, number>());
  const panGesture = useRef<{ pointerId: number; lastPx: number } | null>(null);
  const zoomAround = useCallback((anchorPx: number, factor: number) => {
    if (range === null) return;
    const duration = range.endMs - range.startMs;
    const baseDuration = baseRange === null ? duration : baseRange.endMs - baseRange.startMs;
    const nextDuration = Math.max(15 * 60_000, Math.min(baseDuration, duration * factor));
    const fraction = Math.max(0, Math.min(1, anchorPx / Math.max(1, axisPx)));
    const anchorMs = range.startMs + duration * fraction;
    let startMs = anchorMs - nextDuration * fraction;
    let endMs = startMs + nextDuration;
    if (baseRange !== null) {
      if (startMs < baseRange.startMs) { startMs = baseRange.startMs; endMs = startMs + nextDuration; }
      if (endMs > baseRange.endMs) { endMs = baseRange.endMs; startMs = endMs - nextDuration; }
    }
    setZoomRange({ startMs, endMs });
  }, [axisPx, baseRange, range, setZoomRange]);
  const onTimelineWheel = useCallback((e: WheelEvent) => {
    const viewport = timelineViewportRef.current;
    if (range === null || viewport === null) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = viewport.getBoundingClientRect();
    const anchorPx = Math.max(0, Math.min(axisPx, e.clientX - rect.left + viewport.scrollLeft));
    zoomAround(anchorPx, Math.exp(e.deltaY * 0.001));
  }, [axisPx, range, zoomAround]);
  useEffect(() => {
    const viewport = timelineViewportRef.current;
    if (viewport === null) return;
    // React/browser delegated wheel handlers may be passive. Register this
    // interaction explicitly as non-passive so timeline zoom cannot also
    // move the surrounding scheduling page.
    viewport.addEventListener("wheel", onTimelineWheel, { passive: false });
    return () => { viewport.removeEventListener("wheel", onTimelineWheel); };
  }, [onTimelineWheel]);
  const onTimelinePointerDown = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    const target = e.target as Element;
    const onItem = target.closest('[data-testid^="block-"]') !== null;
    // A new item gesture owns this pointer, even if a previous background
    // pan lost capture and its release happened outside this viewport.
    if (onItem) panGesture.current = null;
    if (range !== null && zoomRange !== null && !onItem && e.pointerType !== "touch") {
      panGesture.current = { pointerId: e.pointerId, lastPx: e.clientX };
      e.currentTarget.setPointerCapture(e.pointerId);
      e.preventDefault();
      return;
    }
    if (e.pointerType !== "touch") return;
    if (range !== null && zoomRange !== null && !onItem) panGesture.current = { pointerId: e.pointerId, lastPx: e.clientX };
    const rect = e.currentTarget.getBoundingClientRect();
    zoomPointers.current.set(e.pointerId, e.clientX - rect.left + e.currentTarget.scrollLeft);
  }, [range, zoomRange]);
  const onTimelinePointerMove = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (panGesture.current?.pointerId === e.pointerId && range !== null && baseRange !== null &&
        (e.pointerType !== "touch" || zoomPointers.current.size === 1)) {
      const dx = e.clientX - panGesture.current.lastPx;
      panGesture.current.lastPx = e.clientX;
      const shift = -(dx / Math.max(1, axisPx)) * (range.endMs - range.startMs);
      const duration = range.endMs - range.startMs;
      let startMs = range.startMs + shift;
      let endMs = startMs + duration;
      if (startMs < baseRange.startMs) { startMs = baseRange.startMs; endMs = startMs + duration; }
      if (endMs > baseRange.endMs) { endMs = baseRange.endMs; startMs = endMs - duration; }
      setZoomRange({ startMs, endMs });
      e.preventDefault();
      return;
    }
    if (e.pointerType !== "touch" || !zoomPointers.current.has(e.pointerId)) return;
    const rect = e.currentTarget.getBoundingClientRect();
    zoomPointers.current.set(e.pointerId, e.clientX - rect.left + e.currentTarget.scrollLeft);
    if (zoomPointers.current.size !== 2 || range === null) return;
    const points = [...zoomPointers.current.values()];
    const distance = Math.abs((points[1] ?? 0) - (points[0] ?? 0));
    const previous = e.currentTarget.dataset.pinchDistance;
    if (previous !== undefined) {
      const oldDistance = Number(previous);
      const [first, second] = points;
      if (first !== undefined && second !== undefined && oldDistance > 1 && distance > 1) {
        zoomAround((first + second) / 2, oldDistance / distance);
      }
    }
    e.currentTarget.dataset.pinchDistance = String(distance);
  }, [axisPx, baseRange, range, setZoomRange, zoomAround]);
  const onTimelinePointerUp = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (panGesture.current?.pointerId === e.pointerId) {
      panGesture.current = null;
      if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    }
    zoomPointers.current.delete(e.pointerId);
    if (zoomPointers.current.size < 2) delete e.currentTarget.dataset.pinchDistance;
  }, []);

  const onTimelineLostPointerCapture = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    // Child resize handles also emit a bubbling lostcapture event. Only
    // release this viewport's own pan/pinch state here.
    if (e.target !== e.currentTarget) return;
    if (panGesture.current?.pointerId === e.pointerId) panGesture.current = null;
    zoomPointers.current.delete(e.pointerId);
    if (zoomPointers.current.size < 2) delete e.currentTarget.dataset.pinchDistance;
  }, []);

  // One resource, one strip: every item's block sits on lane 0 by default;
  // only a genuine scheduling conflict — two items whose compiled spans
  // actually overlap — earns a second lane, so the conflict is the visible
  // exception rather than N tracks by default (real user report,
  // 2026-09-07).
  const overlappingIds = useMemo(() => {
    const ids = new Set<string>();
    const spans = items.flatMap((item) => {
      const compiled = compiledById.get(item.id);
      return compiled?.start_utc != null && compiled.end_utc != null
        ? [{ id: item.id, start: Date.parse(compiled.start_utc), end: Date.parse(compiled.end_utc) }]
        : [];
    });
    for (let i = 0; i < spans.length; i += 1) {
      for (let j = i + 1; j < spans.length; j += 1) {
        const a = spans[i];
        const b = spans[j];
        if (a !== undefined && b !== undefined && a.start < b.end && b.start < a.end) {
          ids.add(a.id);
          ids.add(b.id);
        }
      }
    }
    return ids;
  }, [items, compiledById]);

  // Resize boundaries follow the blocks' actual chronological positions,
  // not plan order. Whole-item moves are intentionally allowed to cross
  // other items, so after such a move the next row in the document may be
  // behind this block on the timeline. Using that row as a resize boundary
  // made the handle clamp backwards and made the visible next block fail to
  // stick. Disabled blocks are not boundaries. A small existing overlap must
  // retain its neighbour so resizing can repair it instead of growing it.
  const neighborBounds = useMemo(() => {
    const bounds = new Map<string, DragBounds>();
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      if (item === undefined) continue;
      const current = compiledById.get(item.id);
      const currentStartMs = current?.start_utc == null ? null : new Date(current.start_utc).getTime();
      const currentEndMs = current?.end_utc == null ? null : new Date(current.end_utc).getTime();
      let previousPlanItemId: string | null = null;
      for (let j = i - 1; j >= 0; j -= 1) {
        const neighbor = items[j];
        if (!neighbor?.enabled) continue;
        previousPlanItemId = neighbor.id;
        break;
      }
      const otherSpans = items.flatMap((other) => {
        if (other.id === item.id || !other.enabled) return [];
        const compiled = compiledById.get(other.id);
        if (compiled?.start_utc == null || compiled.end_utc == null) return [];
        return [{ id: other.id, startMs: new Date(compiled.start_utc).getTime(), endMs: new Date(compiled.end_utc).getTime() }];
      });
      const previous = currentStartMs === null
        ? undefined
        : otherSpans.filter((span) => span.startMs < currentStartMs && span.endMs < (currentEndMs ?? Infinity)).sort((a, b) => b.endMs - a.endMs)[0];
      const next = currentEndMs === null
        ? undefined
        : otherSpans.filter((span) => span.startMs > (currentStartMs ?? -Infinity) && span.endMs >= currentEndMs).sort((a, b) => a.startMs - b.startMs)[0];
      const prevEndMs = previous?.endMs ?? null;
      const nextStartMs = next?.startMs ?? null;
      const moveSnapEdges = items.flatMap((other) => {
        if (other.id === item.id || !other.enabled) return [];
        const compiled = compiledById.get(other.id);
        if (compiled?.start_utc == null || compiled.end_utc == null) return [];
        return [
          { atMs: new Date(compiled.start_utc).getTime(), edge: "start" as const },
          { atMs: new Date(compiled.end_utc).getTime(), edge: "end" as const },
        ];
      });
      bounds.set(item.id, {
        prevEndMs,
        prevEndCommitsRelative: previous?.id === previousPlanItemId,
        nextStartMs,
        moveSnapEdges,
      });
    }
    return bounds;
  }, [items, compiledById]);

  // --- moon phase and altitude curves, from /api/astro/night --------------- //
  const moonRequest: NightRequest | null =
    night === undefined
      ? null
      : { start_time: night.window_start_utc, end_time: night.window_end_utc, min_altitude_deg: 0, altitude_samples: 2 };
  const moonQuery = useNightQuery(moonRequest);
  const moonAltitudeRequest: MoonAltitudeRequest | null =
    night === undefined ? null : { start: night.window_start_utc, end: night.window_end_utc };
  const moonAltitudeQuery = useMoonAltitudeQuery(moonAltitudeRequest);
  const weatherQuery = useForecastQuery(night !== undefined);
  // Absent (not an empty/flat series) whenever the request has not resolved
  // or failed — `TimelineStrip` only draws the polyline when it is given
  // real samples, never a guessed flat line (DESIGN.md §4.4).
  const moonAltitude = useMemo(
    () => moonAltitudeQuery.data?.samples.map((s) => ({ when: s.at, altitude_deg: s.alt_deg })),
    [moonAltitudeQuery.data],
  );
  const moon: MoonBand | undefined = useMemo(() => {
    if (night === undefined || range === null) return undefined;
    return {
      intervals: moonUpIntervals(night, range),
      illuminatedFraction: moonQuery.data?.moon.illuminated_fraction ?? null,
      waxing: moonQuery.data?.moon.waxing ?? null,
      altitude: moonAltitude,
    };
  }, [night, range, moonQuery.data, moonAltitude]);

  const targetBodies = useMemo(() => {
    const bodies = new Map<string, NightRequest>();
    if (range === null) return bodies;
    for (const item of items) {
      const target = targetOf(item);
      if (target === null) continue;
      const key = `${String(target.ra_j2000_h)}/${String(target.dec_j2000_deg)}`;
      if (bodies.has(key)) continue;
      bodies.set(key, {
        start_time: new Date(range.startMs).toISOString(),
        end_time: new Date(range.endMs).toISOString(),
        ...target,
        min_altitude_deg: 0,
        altitude_samples: 145,
      });
    }
    return bodies;
  }, [items, range]);
  const targetKeys = useMemo(() => [...targetBodies.keys()], [targetBodies]);
  const altitudeQueries = useQueries({
    queries: targetKeys.map((key) => {
      const body = targetBodies.get(key);
      return {
        queryKey: ["astro", "night", body] as const,
        queryFn: async () => {
          if (body === undefined) throw new Error("TimelinePane: altitude query ran without a body");
          return unwrap(api.POST("/api/astro/night", { body }));
        },
        staleTime: Infinity,
      };
    }),
  });
  const altitudeByTarget = useMemo(
    () => new Map(targetKeys.map((key, i) => [key, altitudeQueries[i]?.data?.altitude_curve ?? undefined])),
    [targetKeys, altitudeQueries],
  );
  const separationByTarget = useMemo(
    () => new Map(targetKeys.map((key, i) => [key, altitudeQueries[i]?.data?.moon_separation_curve ?? undefined])),
    [targetKeys, altitudeQueries],
  );
  const altitudeError =
    altitudeQueries.find((q) => q.isError)?.error ??
    (moonQuery.isError ? moonQuery.error : moonAltitudeQuery.isError ? moonAltitudeQuery.error : null);

  // --- the shared strip's rows: one entry per item, positioned on the one //
  // axis above (real compiled time, not a per-row segment) and stacked //
  // onto its assigned lane. //
  const stripItems: StripItem[] = useMemo(
    () =>
      items.map((item) => {
        const compiled = compiledById.get(item.id);
        const target = targetOf(item);
        const altitude = target === null ? undefined : altitudeByTarget.get(`${String(target.ra_j2000_h)}/${String(target.dec_j2000_deg)}`);
        const moonSeparation = target === null ? undefined : separationByTarget.get(`${String(target.ra_j2000_h)}/${String(target.dec_j2000_deg)}`);
        return {
          item,
          compiled,
          altitude,
          moonSeparation,
          runStatus: runItemStatus.get(item.id) ?? "pending",
          label: itemSummary(item) || ITEM_KIND_LABELS[item.kind],
          selected: item.id === selectedItemId,
          hovered: item.id === hoveredItemId,
          laneIndex: 0,
          overlapping: item.id === selectedItemId && overlappingIds.has(item.id),
          bounds: neighborBounds.get(item.id) ?? { prevEndMs: null, nextStartMs: null },
        };
      }),
    [items, compiledById, altitudeByTarget, separationByTarget, runItemStatus, selectedItemId, hoveredItemId, overlappingIds, neighborBounds],
  );

  // --- reorder by dragging the gutter handle --------------------------------- //
  const beginReorder = (e: ReactPointerEvent<HTMLButtonElement>, itemId: string, index: number) => {
    if (onReorderItem === undefined || busy) return;
    // Default-prevented so a touch drag scrolls nothing; that also skips the
    // browser's focus-on-mousedown, so focus is taken explicitly for Alt+↑/↓.
    e.preventDefault();
    e.currentTarget.focus();
    if (typeof e.currentTarget.setPointerCapture === "function" && Number.isFinite(e.pointerId)) {
      e.currentTarget.setPointerCapture(e.pointerId);
    }
    setReorder({ itemId, fromIndex: index, toIndex: index });
  };
  const updateReorder = (e: ReactPointerEvent<HTMLButtonElement>) => {
    if (reorder === null) return;
    let toIndex = 0;
    for (const item of items) {
      if (item.id === reorder.itemId) continue;
      const row = rowRefs.current.get(item.id);
      if (row === undefined) continue;
      const rect = row.getBoundingClientRect();
      if (e.clientY > rect.top + rect.height / 2) toIndex += 1;
    }
    if (toIndex !== reorder.toIndex) setReorder({ ...reorder, toIndex });
  };
  const endReorder = () => {
    if (reorder === null) return;
    setReorder(null);
    if (reorder.toIndex !== reorder.fromIndex) onReorderItem?.(reorder.itemId, reorder.toIndex);
  };
  const onHandleKeyDown = (e: KeyboardEvent<HTMLButtonElement>, itemId: string, index: number) => {
    if (!e.altKey || onReorderItem === undefined || busy) return;
    if (e.key === "ArrowUp" && index > 0) {
      e.preventDefault();
      onReorderItem(itemId, index - 1);
    } else if (e.key === "ArrowDown" && index < items.length - 1) {
      e.preventDefault();
      onReorderItem(itemId, index + 1);
    }
  };

  const insertionIndex = reorder === null ? null : reorder.toIndex + (reorder.toIndex > reorder.fromIndex ? 1 : 0);

  return (
    <Card resizableHeight className="space-y-3">
      {(busy || (schedule !== undefined && isPending)) && (
        <div role="status" aria-label="Applying plan changes" aria-live="polite"
          className="pointer-events-none fixed bottom-14 left-1/2 z-50 -translate-x-1/2 rounded-lg border border-border bg-surface-raised px-4 py-3 text-sm shadow-lg">
          {busy ? "Saving changes…" : "Recalculating plan…"}
        </div>
      )}
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <CardTitle>Night timeline</CardTitle>
        {/* On a phone this line folds into the moon-summary line below
            instead (one line of header before the axis, not two). */}
        {schedule !== undefined && <p className="text-sm text-fg-muted tabular-nums">{nightLine(schedule.night)}</p>}
      </div>

      {isError && (
        <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-xs text-danger" role="alert">
          {errorMessage ?? "The plan could not be compiled."}
        </p>
      )}
      {!isError && isPending && schedule === undefined && <p className="text-sm text-fg-muted">Calculating schedule…</p>}
      {!isError && schedule === undefined && !isPending && items.length === 0 && (
        <p className="text-sm text-fg-muted">Open a plan to see its night.</p>
      )}
      {commitError !== null && (
        <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-xs text-danger" role="alert">
          {commitError}
        </p>
      )}
      {altitudeError !== null && (
        <p className="text-xs text-fg-muted">
          Altitude curves and moon phase unavailable: {altitudeError instanceof Error ? altitudeError.message : "request failed"}
        </p>
      )}

      {missing.length > 0 && (
        <ul className="flex flex-row flex-wrap justify-between gap-2 text-xs text-warning" aria-label="Events that do not occur">
          {missing.map((m) => (
            <li key={m.event} className={m.side === "end" ? "ml-auto text-right" : ""} data-testid={`missing-${m.event}`} title={m.message}>
              {m.side === "start" ? "◀ " : ""}
              {m.label} does not occur
              {m.side === "end" ? " ▶" : ""}
            </li>
          ))}
        </ul>
      )}

      {/* The one shared strip: axis, bands, moon and every item's block, at
          its own real time position, stacked onto a second lane only where
          two items' compiled spans genuinely overlap. The interactive
          gutter — reorder, enable, expand — lives in the list below it;
          hovering or selecting a row highlights its block here too. */}
      {schedule !== undefined && range !== null && moon !== undefined && (
        <div className="flex flex-col gap-1">
          <p className="flex flex-wrap items-center gap-2 text-xs text-fg-muted tabular-nums" data-testid="moon-summary">
            {weatherQuery.isError && <span className="text-warning">Hourly weather unavailable</span>}
            {weatherQuery.data?.stale_since !== null && weatherQuery.data?.stale_since !== undefined && (
              <span className="text-warning">Weather forecast is stale</span>
            )}
            <span>{moonLine(moon)}</span>
            {moon.altitude !== undefined && moon.altitude.length > 1 && (
              <span className="inline-flex items-center gap-1" data-testid="moon-curve-legend">
                <span className="inline-block w-5 border-t-2" style={{ borderColor: "var(--ssc-moon)" }} />
                Moon altitude curve
              </span>
            )}
          </p>
          <div
            ref={timelineViewportRef}
            data-testid="timeline-viewport"
            className="overflow-x-auto"
            onPointerDown={onTimelinePointerDown}
            onPointerMove={onTimelinePointerMove}
            onPointerUp={onTimelinePointerUp}
            onPointerCancel={onTimelinePointerUp}
            onLostPointerCapture={onTimelineLostPointerCapture}
            style={{
              // The phone layout deliberately overflows horizontally. Let the
              // browser own ordinary one-finger scrolling there; disabling
              // touch actions on the whole viewport made that overflow
              // impossible to reach. Individual block drag/resize handles
              // still use `touch-action: none` in TimelineStrip.
              touchAction: mobile ? "auto" : "none",
              overscrollBehavior: "contain",
            }}
          >
            <TimelineStrip
              orientation={orientation}
              range={range}
              axisPx={axisPx}
              laneCount={1}
              crossAxisPx={paneWidth}
              bands={bands}
              events={events}
              moon={moon}
              moonAltitude={moon.altitude}
              weather={weatherQuery.data === undefined ? undefined : {
                points: weatherQuery.data.points,
                stale: weatherQuery.data.stale_since !== null,
              }}
              timeZone={schedule.night.tz}
              items={stripItems}
              onSelect={handleSelect}
              onHover={setHoveredItemId}
              onCommit={canEdit ? handleCommit : undefined}
              onCommitError={setCommitError}
            />
          </div>
        </div>
      )}

      {items.length === 0 && schedule !== undefined && (
        <p className="text-sm text-fg-muted">No items. Every plan should start with Start-up and end with Shutdown.</p>
      )}

      <ol ref={listRef} className="flex flex-col gap-1" aria-label="Plan items">
        {items.map((item, index) => {
          const compiled = compiledById.get(item.id);
          const firstTargetIndex = items.findIndex((candidate) => candidate.enabled && candidate.kind === "goto_and_stack");
          const estimate = estimateIntegration(item, compiled, index === firstTargetIndex);
          const selected = item.id === selectedItemId;
          const hovered = item.id === hoveredItemId;
          const label = rowLabel(item);
          const number = String(index + 1);
          const status =
            compiled === undefined ? "not compiled" : compiled.start_utc === null ? `unschedulable — ${compiled.resolution}` : compiled.resolution;
          const execution = runItemStatus.get(item.id) ?? "pending";
          const executionLabel = execution === "done" ? "COMPLETED" : execution === "running" ? "IN PROGRESS" : execution.toUpperCase();
          const executionTone = execution === "done" ? "text-success" : execution === "running" ? "text-accent" :
            execution === "failed" ? "text-danger" : "text-fg-muted";
          return (
            <li
              key={item.id}
              ref={(el) => {
                if (el === null) rowRefs.current.delete(item.id);
                else rowRefs.current.set(item.id, el);
              }}
              data-index={index}
              data-testid={`row-${item.id}`}
              data-hovered={hovered || undefined}
              className={`rounded-md border ${selected ? "border-accent" : hovered ? "border-border-strong" : "border-border"} ${
                hovered && !selected ? "bg-surface-raised" : ""
              } ${insertionIndex === index ? "border-t-2 border-t-accent" : ""} ${
                insertionIndex === items.length && index === items.length - 1 ? "border-b-2 border-b-accent" : ""
              } ${reorder?.itemId === item.id ? "opacity-60" : ""}`}
              onPointerEnter={() => {
                setHoveredItemId(item.id);
              }}
              onPointerLeave={() => {
                setHoveredItemId((current) => (current === item.id ? null : current));
              }}
            >
              <div className="flex min-h-[44px] items-center gap-0.5 px-1 lg:min-h-11">
                {/* Reordering remains accessible without opening the editor. */}
                    <button
                      type="button"
                      className="flex h-[44px] w-[44px] shrink-0 cursor-grab items-center justify-center rounded-md text-fg-muted hover:bg-surface-raised disabled:cursor-not-allowed disabled:opacity-50 lg:h-11 lg:w-11"
                      aria-label={`Reorder item ${number}`}
                      title="Drag to reorder (Alt+↑/↓)"
                      disabled={busy || onReorderItem === undefined}
                      style={{ touchAction: "none" }}
                      onPointerDown={(e) => {
                        beginReorder(e, item.id, index);
                      }}
                      onPointerMove={updateReorder}
                      onPointerUp={endReorder}
                      onPointerCancel={() => {
                        setReorder(null);
                      }}
                      onKeyDown={(e) => {
                        onHandleKeyDown(e, item.id, index);
                      }}
                    >
                      ⋮⋮
                    </button>
                {selected && (
                  <>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={item.enabled}
                      aria-label={`${item.enabled ? "Disable" : "Enable"} item ${number}`}
                      disabled={busy || onToggleItem === undefined}
                      className={`inline-flex h-[44px] w-[44px] shrink-0 flex-col items-center justify-center rounded-md border text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50 lg:h-11 lg:w-11 ${
                        item.enabled ? "border-accent bg-accent-muted text-accent" : "border-border bg-surface-raised text-fg-muted"
                      }`}
                      onClick={() => {
                        onToggleItem?.(item.id, !item.enabled);
                      }}
                    >
                      <EnabledIcon />
                      {item.enabled ? "On" : "Off"}
                    </button>
                    <button
                      type="button"
                      aria-expanded={selected}
                      aria-label={`Edit item ${number}`}
                      className="h-[44px] w-[44px] shrink-0 rounded-md text-fg-muted hover:bg-surface-raised lg:h-11 lg:w-11"
                      onClick={() => {
                        onSelectItem(null);
                      }}
                    >
                      ▾
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="flex min-h-[44px] min-w-0 flex-1 flex-col justify-center px-1 text-left lg:min-h-11"
                  aria-pressed={selected}
                  onClick={() => {
                    onSelectItem(selected ? null : item.id);
                  }}
                >
                  <span className="flex w-full min-w-0 items-center justify-between gap-2">
                    <span className={`truncate text-sm font-medium ${selected ? "text-accent" : item.enabled ? "text-fg" : "text-fg-muted line-through"}`}>
                      {number}. {label}
                    </span>
                    <span className={`shrink-0 text-[10px] font-semibold tracking-wide ${executionTone}`} data-testid={`run-status-${item.id}`}>
                      {executionLabel}
                    </span>
                  </span>
                  <span className="flex items-center gap-1 truncate text-xs text-fg-muted tabular-nums">
                    {status}
                    {estimate !== null && (
                      <> · ≈ {formatIntegrationDuration(estimate.cleanIntegrationS)} clean · {estimate.frames} frames · {Math.round(estimate.efficiencyPct)}%</>
                    )}
                    {item.id === dirtyItemId && <Badge tone="warning">unsaved</Badge>}
                  </span>
                </button>
              </div>
              {selected && editor !== undefined && <div className="border-t border-border p-2">{editor}</div>}
            </li>
          );
        })}
      </ol>

      {onAddItem !== undefined && (
        <form
          className="flex flex-col gap-1.5 border-t border-border pt-3"
          onSubmit={(e) => {
            e.preventDefault();
            onAddItem(addKind);
          }}
        >
          <label className="text-sm font-medium text-fg-muted" htmlFor="add-item-kind">
            Add item
          </label>
          <div className="flex gap-2">
            <select
              id="add-item-kind"
              className={SELECT_CLASSES}
              value={addKind}
              onChange={(e) => {
                setAddKind(e.target.value as PlanItemKind);
              }}
            >
              <optgroup label="Night blocks">
                {NORMAL_ITEM_KINDS.map((kind) => <option key={kind} value={kind}>{ITEM_KIND_LABELS[kind]}</option>)}
              </optgroup>
              <optgroup label="Advanced device actions">
                {ADVANCED_ITEM_KINDS.map((kind) => <option key={kind} value={kind}>{ITEM_KIND_LABELS[kind]}</option>)}
              </optgroup>
            </select>
            <Button type="submit" variant="primary" disabled={busy} className="shrink-0">
              <AddIcon />
              Add
            </Button>
          </div>
        </form>
      )}
    </Card>
  );
}
