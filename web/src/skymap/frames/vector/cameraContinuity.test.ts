// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { heldTargetNeedsReanchor } from "./cameraContinuity";

describe("heldTargetNeedsReanchor", () => {
  const instant = new Date("2026-09-15T20:00:00Z");

  it("preserves the drag-established camera on a same-instant target refresh", () => {
    expect(heldTargetNeedsReanchor(new Date(instant), instant, true)).toBe(false);
  });

  it("reanchors a held target only when the timeline instant changes", () => {
    expect(heldTargetNeedsReanchor(new Date("2026-09-15T19:59:59Z"), instant, true)).toBe(true);
    expect(heldTargetNeedsReanchor(new Date("2026-09-15T19:59:59Z"), instant, false)).toBe(false);
    expect(heldTargetNeedsReanchor(null, instant, true)).toBe(false);
  });
});
