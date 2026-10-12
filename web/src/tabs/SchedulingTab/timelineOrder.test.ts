import { describe, expect, it } from "vitest";
import type { CompiledItem, PlanItem } from "../../api/plans";
import { newItem } from "./itemKinds";
import { orderAfterTimelineMove } from "./timelineOrder";

describe("timeline execution order", () => {
  it("moves dawn calibration behind night imaging and preserves chained starts", () => {
    const dark = { ...newItem("capture_calibration", "dark", { kind: "asap" }), start: { kind: "absolute" as const, local_datetime: "2026-10-03T05:05:00", fold: 0 as const } };
    const panel = newItem("goto_and_stack", "panel", { kind: "relative", offset_min: 0 });
    const compiled = new Map<string, CompiledItem>([
      ["dark", { start_utc: "2026-10-02T21:00:00Z" } as CompiledItem],
      ["panel", { start_utc: "2026-10-02T22:10:00Z" } as CompiledItem],
    ]);
    const result = orderAfterTimelineMove([dark, panel] as PlanItem[], "dark", compiled, "Europe/Bratislava");
    expect(result.map((item) => item.id)).toEqual(["panel", "dark"]);
    expect(result[0]?.start).toEqual({ kind: "absolute", local_datetime: "2026-10-03T00:10:00", fold: 0 });
  });
  it("reorders an event-snapped move by its resolved time while preserving the anchor", () => {
    const first = newItem("wait", "first", { kind: "absolute", local_datetime: "2026-10-06T21:00:00", fold: 0 });
    const moved = newItem("wait", "moved", { kind: "event", event: "astronomical_dusk", offset_min: 0 });
    const compiled = new Map<string, CompiledItem>([
      ["first", { start_utc: "2026-10-06T19:00:00Z" } as CompiledItem],
      ["moved", { start_utc: "2026-10-06T22:00:00Z" } as CompiledItem],
    ]);
    const result = orderAfterTimelineMove([first, moved], "moved", compiled, "Europe/Bratislava", "2026-10-06T18:02:02Z");
    expect(result.map((item) => item.id)).toEqual(["moved", "first"]);
    expect(result[0]?.start).toEqual(moved.start);
  });

});
