import type { CompiledItem, PlanItem } from "../../api/plans";
import { utcIsoToSiteWallClock } from "./timelineGeometry";

/** Keep execution order aligned with a dragged start without shifting chained neighbours. */
export function orderAfterTimelineMove(items: PlanItem[], movedId: string, compiled: Map<string, CompiledItem>, timezone: string, movedStartUtc?: string): PlanItem[] {
  const readings = new Map(items.map((item) => {
    const start = item.id === movedId && movedStartUtc !== undefined ? movedStartUtc : compiled.get(item.id)?.start_utc;
    const wall = start == null ? null : utcIsoToSiteWallClock(start, timezone);
    return [item.id, item.id === movedId && item.start.kind === "absolute" ? item.start : wall];
  }));
  const ordered = [...items].sort((a, b) => {
    const left = readings.get(a.id);
    const right = readings.get(b.id);
    if (left == null || right == null) return 0;
    return left.local_datetime.localeCompare(right.local_datetime) || left.fold - right.fold;
  });
  if (ordered.every((item, index) => item.id === items[index]?.id)) return items;
  return ordered.map((item) => {
    const wall = readings.get(item.id);
    return wall != null && (item.start.kind === "relative" || item.start.kind === "asap")
      ? { ...item, start: { kind: "absolute", local_datetime: wall.local_datetime, fold: wall.fold } }
      : item;
  });
}
