// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The Gantt's arithmetic against a fixture night, not a screenshot: band
 * order and darkness, a dusk that does not occur, moon-up intervals in
 * every rise/set arrangement, the pixel snap, and the site-zone wall clock
 * with its DST fold.
 */
import { describe, expect, it } from "vitest";
import type { CompiledItem, CompiledSchedule } from "../../api/plans";
import { NIGHT, resolvedEvent as event } from "./testNight";
import {
  FALLBACK_AXIS_PX,
  VERTICAL_GAP_BREAK_PX,
  VERTICAL_GAP_THRESHOLD_MIN,
  assignLanes,
  axisRange,
  bandFill,
  buildVerticalCompression,
  compressedAxisPx,
  compressedPxToTime,
  compressedTimeToPx,
  labelLayout,
  linearAxisMap,
  missingSolarEvents,
  moonPhasePath,
  moonUpIntervals,
  resolveMoveDrag,
  resolveResizeDrag,
  snapTargets,
  snapTo,
  timeToPx,
  twilightBands,
  utcToSiteWallClock,
  verticalLaneSizing,
  type DragBounds,
  type Interval,
} from "./timelineGeometry";

type NightWindow = CompiledSchedule["night"];

const range = axisRange(NIGHT, []);

describe("axisRange", () => {
  it("runs from half an hour before sunset to half an hour after sunrise", () => {
    expect(new Date(range.startMs).toISOString()).toBe("2026-08-28T18:30:00.000Z");
    expect(new Date(range.endMs).toISOString()).toBe("2026-08-29T06:00:00.000Z");
  });

  it("does not widen past sunrise+30min for an item scheduled outside it — it clips there instead", () => {
    // An item that runs to 10:00, well past sunrise+30 min (06:00): the axis
    // must not stretch out to it (the bug this was: "the axis runs to 10:00").
    const late: CompiledItem[] = [
      {
        item_id: "late",
        kind: "wait",
        enabled: true,
        start_utc: "2026-08-29T05:00:00Z",
        end_utc: "2026-08-29T10:00:00Z",
        resolution: "after previous",
        end_resolution: "after previous",
        panels: [],
        diagnostics: [],
      },
    ];
    const clamped = axisRange(NIGHT, late);
    expect(clamped).toEqual(range);
    expect(new Date(clamped.endMs).toISOString()).toBe("2026-08-29T06:00:00.000Z");
  });
});

describe("twilightBands", () => {
  it("shades day → civil → nautical → astronomical → night and back, progressively darker", () => {
    const bands = twilightBands(NIGHT, range);
    expect(bands.map((b) => b.name)).toEqual([
      "day",
      "civil",
      "nautical",
      "astronomical",
      "night",
      "astronomical",
      "nautical",
      "civil",
      "day",
    ]);
    expect(bands.map((b) => b.level)).toEqual([0, 1, 2, 3, 4, 3, 2, 1, 0]);
    // Every level maps to its own fill token — five distinct hues (day red,
    // civil, nautical blue, astronomical, night black), not five points on
    // one opacity ramp.
    const fills = bands.slice(0, 5).map((b) => bandFill(b.level));
    expect(new Set(fills).size).toBe(5);
    expect(new Date(bands[4]?.startMs ?? 0).toISOString()).toBe("2026-08-28T21:00:00.000Z");
    expect(new Date(bands[4]?.endMs ?? 0).toISOString()).toBe("2026-08-29T03:30:00.000Z");
  });

  it("with no astronomical dusk/dawn the astronomical band reaches across the night — no blank, and the marker says so", () => {
    const summer: NightWindow = {
      ...NIGHT,
      solar_events: NIGHT.solar_events.map((e) =>
        e.event === "astronomical_dusk" || e.event === "astronomical_dawn" ? event(e.event, null, "always_above") : e,
      ),
    };
    const bands = twilightBands(summer, axisRange(summer, []));
    expect(bands.map((b) => b.name)).toEqual(["day", "civil", "nautical", "astronomical", "nautical", "civil", "day"]);
    expect(bands.map((b) => b.level)).not.toContain(4);
    expect(missingSolarEvents(summer)).toEqual([
      expect.objectContaining({ event: "astronomical_dusk", side: "start", message: expect.stringContaining("does not occur") as string }),
      expect.objectContaining({ event: "astronomical_dawn", side: "end" }),
    ]);
  });
});

describe("bandFill", () => {
  it("maps day → the red token, nautical → the blue token, night → the black token", () => {
    expect(bandFill(0)).toBe("var(--ssc-band-day)");
    expect(bandFill(2)).toBe("var(--ssc-band-nautical)");
    expect(bandFill(4)).toBe("var(--ssc-band-night)");
  });

  it("civil and astronomical are their own tokens, distinct from every other band", () => {
    const fills = [0, 1, 2, 3, 4].map(bandFill);
    expect(fills).toEqual([
      "var(--ssc-band-day)",
      "var(--ssc-band-civil)",
      "var(--ssc-band-nautical)",
      "var(--ssc-band-astronomical)",
      "var(--ssc-band-night)",
    ]);
    expect(new Set(fills).size).toBe(5);
  });

  it("an out-of-range level falls back to the darkest band rather than an undefined fill", () => {
    expect(bandFill(5)).toBe("var(--ssc-band-night)");
    expect(bandFill(-1)).toBe("var(--ssc-band-night)");
  });
});

describe("moonUpIntervals", () => {
  it("rise then set is one interval", () => {
    expect(moonUpIntervals(NIGHT, range)).toEqual([
      { startMs: Date.parse("2026-08-28T22:00:00Z"), endMs: Date.parse("2026-08-29T02:00:00Z") },
    ]);
  });
  it("set then rise is two, from the axis edges", () => {
    const night = { ...NIGHT, lunar_events: [event("moonset", "2026-08-28T20:00:00Z"), event("moonrise", "2026-08-29T04:00:00Z")] };
    expect(moonUpIntervals(night, range)).toEqual([
      { startMs: range.startMs, endMs: Date.parse("2026-08-28T20:00:00Z") },
      { startMs: Date.parse("2026-08-29T04:00:00Z"), endMs: range.endMs },
    ]);
  });
  it("neither event and not always_above means down all night", () => {
    const night = { ...NIGHT, lunar_events: [event("moonrise", null, "not_in_window"), event("moonset", null, "not_in_window")] };
    expect(moonUpIntervals(night, range)).toEqual([]);
  });
});

describe("moonPhasePath", () => {
  it("is a closed path whose terminator collapses at half moon", () => {
    expect(moonPhasePath(10, 10, 6, 0.5, true)).toMatch(/A 0 6 0 0 [01] 10 4 Z$/);
    expect(moonPhasePath(10, 10, 6, 1, true)).toMatch(/A 6 6 0 0 1 10 16 A 6 6 0 0 1 10 4 Z$/);
  });
});

describe("snapTo", () => {
  it("snaps within the radius to the nearest resolved event, all twilights included, and not outside it", () => {
    const targets = snapTargets(NIGHT);
    expect(targets.map((t) => t.event)).toContain("astronomical_dusk");
    expect(targets.map((t) => t.event)).toContain("moonrise");
    const dusk = Date.parse("2026-08-28T21:00:00Z");
    const msPerPx = (range.endMs - range.startMs) / FALLBACK_AXIS_PX;
    expect(snapTo(dusk + 5 * msPerPx, targets, range, FALLBACK_AXIS_PX)?.event).toBe("astronomical_dusk");
    expect(snapTo(dusk + 20 * msPerPx, targets, range, FALLBACK_AXIS_PX)).toBeNull();
  });
  it("timeToPx is linear across the axis", () => {
    expect(timeToPx(range.startMs, range, 800)).toBe(0);
    expect(timeToPx((range.startMs + range.endMs) / 2, range, 800)).toBe(400);
  });
});

describe("resolveMoveDrag / resolveResizeDrag", () => {
  const axis = linearAxisMap(range, FALLBACK_AXIS_PX);
  const msPerPx = (range.endMs - range.startMs) / FALLBACK_AXIS_PX;
  const prevEndMs = range.startMs + 200 * msPerPx;
  const nextStartMs = range.startMs + 500 * msPerPx;
  const bounds: DragBounds = { prevEndMs, nextStartMs };
  const durationMs = 30 * 60_000;

  describe("resolveMoveDrag", () => {
    it("allows a start to cross the previous item when dragged beyond its snap radius", () => {
      const wanted = prevEndMs - 50 * msPerPx;
      const result = resolveMoveDrag(wanted, durationMs, [], axis, bounds);
      expect(result.proposedStartMs).toBe(wanted);
      expect(result.snappedToItemEdge).toBe(false);
      expect(result.snapped).toBeNull();
    });

    it("snaps to the previous item's end within the same radius as an event, even when not clamped", () => {
      const wanted = prevEndMs + 3 * msPerPx; // inside SNAP_RADIUS_PX, but still a legal (unclamped) position
      const result = resolveMoveDrag(wanted, durationMs, [], axis, bounds);
      expect(result.proposedStartMs).toBe(prevEndMs);
      expect(result.snappedToItemEdge).toBe(true);
    });

    it("does not snap to the previous item's end once outside the radius", () => {
      const wanted = prevEndMs + 30 * msPerPx;
      const result = resolveMoveDrag(wanted, durationMs, [], axis, bounds);
      expect(result.snappedToItemEdge).toBe(false);
      expect(result.proposedStartMs).toBe(wanted);
    });

    it("allows a whole item to cross the next item's start", () => {
      const wanted = nextStartMs; // would push the end well past nextStartMs
      const result = resolveMoveDrag(wanted, durationMs, [], axis, bounds);
      expect(result.proposedStartMs).toBe(nextStartMs);
      expect(result.snappedToItemEdge).toBe(false);
      expect(result.snapped).toBeNull();
    });

    it("snaps the moving item's end to the start of an item it crossed", () => {
      const wanted = nextStartMs - durationMs + 3 * msPerPx;
      const result = resolveMoveDrag(wanted, durationMs, [], axis, {
        ...bounds,
        moveSnapEdges: [{ atMs: nextStartMs, edge: "start" }],
      });
      expect(result.proposedStartMs).toBe(nextStartMs - durationMs);
      expect(result.snappedToItemEdge).toBe(true);
    });

    it("snaps the moving item's start to any other item's end", () => {
      const crossedEnd = range.startMs + 350 * msPerPx;
      const wanted = crossedEnd - 2 * msPerPx;
      const result = resolveMoveDrag(wanted, durationMs, [], axis, {
        ...bounds,
        moveSnapEdges: [{ atMs: crossedEnd, edge: "end" }],
      });
      expect(result.proposedStartMs).toBe(crossedEnd);
      expect(result.snappedToItemEdge).toBe(true);
    });

    it("prefers whichever of a resolved event or the previous item's end is closer", () => {
      const dusk = Date.parse("2026-08-28T21:00:00Z");
      const targets = snapTargets(NIGHT);
      const nearDusk: DragBounds = { prevEndMs: dusk - 1_000_000, nextStartMs: null };
      const wanted = dusk + 2 * msPerPx; // closer to the event than to the (far away) previous end
      const result = resolveMoveDrag(wanted, durationMs, targets, axis, nearDusk);
      expect(result.snapped?.event).toBe("astronomical_dusk");
      expect(result.snappedToItemEdge).toBe(false);
    });

    it("with no previous neighbour, does not clamp or snap on that side", () => {
      const result = resolveMoveDrag(range.startMs - 10 * msPerPx, durationMs, [], axis, { prevEndMs: null, nextStartMs: null });
      expect(result.proposedStartMs).toBe(range.startMs - 10 * msPerPx);
      expect(result.snappedToItemEdge).toBe(false);
    });
  });

  describe("resolveResizeDrag", () => {
    const startMs = range.startMs;

    it("clamps an end dragged past the next item's start to exactly that start — never later", () => {
      const wanted = nextStartMs + 50 * msPerPx;
      const result = resolveResizeDrag(wanted, startMs, [], axis, bounds);
      expect(result.proposedEndMs).toBe(nextStartMs);
      expect(result.snappedToNextStart).toBe(true);
    });

    it("snaps to the next item's start within radius, even when not clamped", () => {
      const wanted = nextStartMs - 3 * msPerPx;
      const result = resolveResizeDrag(wanted, startMs, [], axis, bounds);
      expect(result.proposedEndMs).toBe(nextStartMs);
      expect(result.snappedToNextStart).toBe(true);
    });

    it("does not snap once outside the radius", () => {
      const wanted = nextStartMs - 30 * msPerPx;
      const result = resolveResizeDrag(wanted, startMs, [], axis, bounds);
      expect(result.snappedToNextStart).toBe(false);
      expect(result.proposedEndMs).toBe(wanted);
    });

    it("with no next neighbour, does not clamp or snap on that side", () => {
      const wanted = range.endMs + 1000 * msPerPx;
      const result = resolveResizeDrag(wanted, startMs, [], axis, { prevEndMs: null, nextStartMs: null });
      expect(result.proposedEndMs).toBe(wanted);
      expect(result.snappedToNextStart).toBe(false);
    });
  });
});

describe("bounded event snapping", () => {
  it("does not resize beyond a neighbour for a closer event outside the permitted span", () => {
    const start = range.startMs;
    const next = start + 120 * 60_000;
    const axis = linearAxisMap(range, 800);
    const event = { atMs: next + 5000, event: "astronomical_dawn" as const, label: "Dawn" };
    const result = resolveResizeDrag(next - 1000, start, [event], axis, { prevEndMs: null, nextStartMs: next });
    expect(result.proposedEndMs).toBe(next);
    expect(result.snapped).toBeNull();
  });
});

describe("utcToSiteWallClock", () => {
  it("renders the site zone, not UTC, and marks the second 01:30 of the autumn night with fold=1", () => {
    expect(utcToSiteWallClock(Date.parse("2026-08-28T21:00:00Z"), "Europe/London")).toEqual({
      local_datetime: "2026-08-28T22:00:00",
      fold: 0,
    });
    // 2026-10-25: BST ends at 02:00 BST (01:00 UTC); 01:30 local happens
    // at 00:30 UTC (fold 0) and again at 01:30 UTC (fold 1).
    expect(utcToSiteWallClock(Date.parse("2026-10-25T00:30:00Z"), "Europe/London")).toEqual({
      local_datetime: "2026-10-25T01:30:00",
      fold: 0,
    });
    expect(utcToSiteWallClock(Date.parse("2026-10-25T01:30:00Z"), "Europe/London")).toEqual({
      local_datetime: "2026-10-25T01:30:00",
      fold: 1,
    });
    expect(utcToSiteWallClock(0, "Not/AZone")).toBeNull();
  });
});

describe("labelLayout", () => {
  it("puts civil/nautical/astronomical dusk on three distinct rows on a narrow axis", () => {
    const narrow = axisRange(NIGHT, []);
    const targets = snapTargets(NIGHT).filter((t) =>
      (["civil_dusk", "nautical_dusk", "astronomical_dusk"] as const).includes(
        t.event as "civil_dusk" | "nautical_dusk" | "astronomical_dusk",
      ),
    );
    const placements = labelLayout(targets, (atMs) => timeToPx(atMs, narrow, 200), (e) => e);
    const rows = new Set(placements.map((p) => p.row));
    expect(rows.size).toBe(3);
  });

  it("abbreviates a label too close to the previous one on its own row", () => {
    const targets = [
      { event: "sunset" as const, label: "Sunset", atMs: 0 },
      { event: "civil_dusk" as const, label: "Civil dusk", atMs: 3_600_000 },
      { event: "nautical_dusk" as const, label: "Nautical dusk", atMs: 7_200_000 },
      // Same row as "Sunset" (index 0, rows=3 → row 0), and close enough in
      // pixels that "Astronomical dusk" would overlap "Sunset"'s tail.
      { event: "astronomical_dusk" as const, label: "Astronomical dusk", atMs: 7_260_000 },
    ];
    const px = (atMs: number) => atMs / 60_000; // 1 px/min, so 60px between sunset and the 4th event
    const placements = labelLayout(targets, px, () => "Astro.");
    expect(placements[0]?.row).toBe(0);
    expect(placements[3]?.row).toBe(0);
    expect(placements[3]?.text).toBe("Astro.");
  });
});

// A 200-minute range with one 10-minute occupied item at minute 10, so the
// axis is exactly [idle][item][idle] — the shape a real `asap` chain makes.
const minutes = (n: number) => n * 60_000;
const compressionRange = { startMs: 0, endMs: minutes(200) };
const oneItem: Interval[] = [{ startMs: minutes(10), endMs: minutes(20) }];

describe("buildVerticalCompression / compressedTimeToPx / compressedPxToTime", () => {
  it("draws an occupied span and a gap under the threshold literally, and collapses the gap over it to a fixed height", () => {
    const compression = buildVerticalCompression(compressionRange, oneItem);
    // 10 min lead-in (literal, under the threshold) + 10 min occupied
    // (always literal) + a 180 min trailing gap (well over the threshold,
    // collapsed to VERTICAL_GAP_BREAK_PX) — not 190 min of trailing space.
    expect(compressedAxisPx(compression)).toBeCloseTo(10 + 10 + VERTICAL_GAP_BREAK_PX, 6);
    expect(compressedTimeToPx(minutes(10), compression)).toBeCloseTo(10, 6);
    expect(compressedTimeToPx(minutes(20), compression)).toBeCloseTo(20, 6);
    expect(compressedTimeToPx(compressionRange.endMs, compression)).toBeCloseTo(20 + VERTICAL_GAP_BREAK_PX, 6);
  });

  it("keeps a gap exactly at the threshold literal, and collapses one minute past it", () => {
    const atThreshold: Interval[] = [{ startMs: minutes(VERTICAL_GAP_THRESHOLD_MIN), endMs: minutes(VERTICAL_GAP_THRESHOLD_MIN + 1) }];
    const compression = buildVerticalCompression(compressionRange, atThreshold);
    // The lead-in gap is exactly VERTICAL_GAP_THRESHOLD_MIN long: still literal.
    expect(compressedTimeToPx(minutes(VERTICAL_GAP_THRESHOLD_MIN), compression)).toBeCloseTo(VERTICAL_GAP_THRESHOLD_MIN, 6);

    const pastThreshold: Interval[] = [
      { startMs: minutes(VERTICAL_GAP_THRESHOLD_MIN + 1), endMs: minutes(VERTICAL_GAP_THRESHOLD_MIN + 2) },
    ];
    const collapsed = buildVerticalCompression(compressionRange, pastThreshold);
    // One minute longer: the lead-in gap now collapses to the fixed break
    // height instead of scaling to VERTICAL_GAP_THRESHOLD_MIN + 1 px.
    expect(compressedTimeToPx(minutes(VERTICAL_GAP_THRESHOLD_MIN + 1), collapsed)).toBeCloseTo(VERTICAL_GAP_BREAK_PX, 6);
  });

  it("compressedPxToTime exactly inverts compressedTimeToPx, at gap sizes below, at, and above the threshold, and inside the occupied span", () => {
    const compression = buildVerticalCompression(compressionRange, oneItem);
    const samples = [
      0, // range start
      minutes(5), // inside the short (literal) lead-in gap
      minutes(10), // the occupied span's own start (a breakpoint)
      minutes(15), // inside the occupied span
      minutes(20), // the occupied span's own end (a breakpoint)
      minutes(60), // inside the collapsed trailing gap
      minutes(150), // also inside the collapsed trailing gap, closer to the end
      compressionRange.endMs, // range end
    ];
    for (const atMs of samples) {
      const px = compressedTimeToPx(atMs, compression);
      expect(compressedPxToTime(px, compression)).toBeCloseTo(atMs, 6);
    }
  });

  it("falls back to the literal scale when nothing is scheduled — there is nothing to call idle", () => {
    const compression = buildVerticalCompression(compressionRange, []);
    expect(compressedAxisPx(compression)).toBeCloseTo(200, 6);
    expect(compressedTimeToPx(minutes(100), compression)).toBeCloseTo(100, 6);
  });

  it("merges overlapping occupied spans rather than double-counting them", () => {
    const overlapping: Interval[] = [
      { startMs: minutes(10), endMs: minutes(20) },
      { startMs: minutes(15), endMs: minutes(30) },
    ];
    const compression = buildVerticalCompression(compressionRange, overlapping);
    // One 20-minute occupied span (10→30), not two overlapping ones.
    expect(compressedTimeToPx(minutes(30), compression)).toBeCloseTo(10 + 20, 6);
  });
});

describe("assignLanes", () => {
  it("keeps every item on lane 0 when the plan's spans are sequential — the ordinary single-resource case", () => {
    const { laneOf, laneCount } = assignLanes([
      { itemId: "a", startMs: minutes(0), endMs: minutes(10) },
      { itemId: "b", startMs: minutes(10), endMs: minutes(20) },
      { itemId: "c", startMs: minutes(20), endMs: minutes(30) },
    ]);
    expect(laneCount).toBe(1);
    expect([...laneOf.values()]).toEqual([0, 0, 0]);
  });

  it("offsets only a genuinely overlapping pair onto a second lane, not every item", () => {
    const { laneOf, laneCount } = assignLanes([
      { itemId: "a", startMs: minutes(0), endMs: minutes(10) },
      // b overlaps a (starts before a ends) — the exception.
      { itemId: "b", startMs: minutes(5), endMs: minutes(15) },
      // c starts after both a and b have ended — back on lane 0.
      { itemId: "c", startMs: minutes(15), endMs: minutes(25) },
    ]);
    expect(laneCount).toBe(2);
    expect(laneOf.get("a")).toBe(0);
    expect(laneOf.get("b")).toBe(1);
    expect(laneOf.get("c")).toBe(0);
  });

  it("reuses a freed lane rather than growing without bound", () => {
    const { laneCount } = assignLanes([
      { itemId: "a", startMs: minutes(0), endMs: minutes(10) },
      { itemId: "b", startMs: minutes(5), endMs: minutes(15) },
      // d starts after a's lane (0) is free again.
      { itemId: "d", startMs: minutes(11), endMs: minutes(20) },
    ]);
    expect(laneCount).toBe(2);
  });

  it("an empty plan still has one lane for the (empty) block row", () => {
    expect(assignLanes([]).laneCount).toBe(1);
  });

  it("treats a near-zero gap as touching, not a second-lane overlap", () => {
    // The bug this guards: two items meant to be back-to-back (the
    // sticky-edge snap put them there) but whose independently-resolved
    // start_utc/end_utc differ by a few hundred ms — read live as "split
    // to two lines" before the tolerance existed.
    const { laneOf, laneCount } = assignLanes([
      { itemId: "a", startMs: minutes(0), endMs: minutes(10) },
      { itemId: "b", startMs: minutes(10) + 400, endMs: minutes(20) },
    ]);
    expect(laneCount).toBe(1);
    expect(laneOf.get("a")).toBe(0);
    expect(laneOf.get("b")).toBe(0);
  });

  it("treats two independently event-resolved items tens of seconds apart as touching", () => {
    // The bug the 1 s tolerance did not cover: a real compiled schedule
    // ("One shared strip check", 2026-09-07) had a mosaic item ending at
    // moonrise + 57 min and the next item starting at astronomical_dawn —
    // two different solar/lunar events that happened to land 25.6 s apart
    // that night, still read live as "split to two lines".
    const { laneOf, laneCount } = assignLanes([
      { itemId: "a", startMs: minutes(0), endMs: minutes(10) },
      { itemId: "b", startMs: minutes(10) + 25_600, endMs: minutes(20) },
    ]);
    expect(laneCount).toBe(1);
    expect(laneOf.get("a")).toBe(0);
    expect(laneOf.get("b")).toBe(0);
  });

  it("still splits a real overlap far past the touch tolerance", () => {
    const { laneCount } = assignLanes([
      { itemId: "a", startMs: minutes(0), endMs: minutes(10) },
      // b starts 5 min before a ends — far past the 1 min touch tolerance.
      { itemId: "b", startMs: minutes(5), endMs: minutes(20) },
    ]);
    expect(laneCount).toBe(2);
  });
});

describe("verticalLaneSizing", () => {
  it("gives the one shared lane the whole phone-width container, not the 44 px finger-target floor", () => {
    // The bug this guards: merging every item onto one shared strip sized
    // the phone's cross-axis from `laneCount * ROW_LANE_PX` alone (44 px
    // for the common single-lane case) instead of the measured container
    // width, clipping every overlaid label against the SVG's own bounds
    // (real user report, 2026-09-08: "the gantt chart on mobile is
    // completely broken and unusable"). A 390 px phone viewport's card
    // interior (comparable to the width `TimelinePane` actually measures)
    // must fill that width back, not 44 px of it.
    const { laneThicknessPx, lanesPx } = verticalLaneSizing(1, 324, 44);
    expect(laneThicknessPx).toBe(324);
    expect(lanesPx).toBe(324);
  });

  it("divides the available width evenly across a genuine conflict's lanes", () => {
    const { laneThicknessPx, lanesPx } = verticalLaneSizing(2, 324, 44);
    expect(laneThicknessPx).toBe(162);
    expect(lanesPx).toBe(324);
  });

  it("never shrinks a lane below the finger-target floor, even when the width does not divide evenly", () => {
    // 8 conflicting lanes on a 390 px phone would be under 41 px each —
    // the floor wins, and the lanes' combined extent legitimately exceeds
    // the measured width (the existing `overflow-x-auto` wrapper scrolls).
    const { laneThicknessPx, lanesPx } = verticalLaneSizing(8, 324, 44);
    expect(laneThicknessPx).toBe(44);
    expect(lanesPx).toBe(352);
  });

  it("treats a lane count under 1 the same as exactly 1", () => {
    expect(verticalLaneSizing(0, 324, 44)).toEqual(verticalLaneSizing(1, 324, 44));
  });
});

describe("fractional sticky starts", () => {
  it("preserves fractional seconds when moving immediately after another item", () => {
    expect(utcToSiteWallClock(Date.parse("2026-08-28T21:00:37.250Z"), "Europe/London")).toEqual({ local_datetime: "2026-08-28T22:00:37.250", fold: 0 });
  });
});

// The compiler emits microseconds; Date-based absolute conversion must not lose them.
describe("compiled instant precision", () => {
  it("preserves six fractional digits when freezing a chained neighbour", async () => {
    const { utcIsoToSiteWallClock } = await import("./timelineGeometry");
    expect(utcIsoToSiteWallClock("2026-10-04T22:19:17.868765Z", "Europe/Bratislava"))
      .toEqual({ local_datetime: "2026-10-05T00:19:17.868765", fold: 0 });
  });
});
