// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §4.1: "an autumn night can contain 01:30 twice." The fold
 * choice must appear exactly then — and the spring gap must be named, not
 * silently shifted.
 */
import { describe, expect, it } from "vitest";
import { formatMinutes, wallClockOccurrences } from "./localTime";

describe("wallClockOccurrences", () => {
  it("counts the autumn fold twice in a DST zone", () => {
    // Europe/London: clocks go back 02:00 → 01:00 on 2026-10-25.
    expect(wallClockOccurrences("2026-10-25T01:30", "Europe/London")).toBe(2);
  });

  it("counts the spring gap zero times", () => {
    // Europe/London: clocks go forward 01:00 → 02:00 on 2026-03-29.
    expect(wallClockOccurrences("2026-03-29T01:30", "Europe/London")).toBe(0);
  });

  it("counts an ordinary reading once, including in a quarter-hour zone", () => {
    expect(wallClockOccurrences("2026-09-07T22:00", "Europe/London")).toBe(1);
    expect(wallClockOccurrences("2026-09-07T22:00", "Asia/Kathmandu")).toBe(1);
    expect(wallClockOccurrences("2026-09-07T22:00", "UTC")).toBe(1);
  });

  it("returns null rather than guessing for an unknown zone or unparsable reading", () => {
    expect(wallClockOccurrences("2026-09-07T22:00", "Mars/Olympus_Mons")).toBeNull();
    expect(wallClockOccurrences("not a time", "UTC")).toBeNull();
  });
});

describe("formatMinutes", () => {
  it("reads as minutes below an hour and h:mm above", () => {
    expect(formatMinutes(45)).toBe("45 min");
    expect(formatMinutes(60)).toBe("1 h");
    expect(formatMinutes(95)).toBe("1 h 35");
  });
});
