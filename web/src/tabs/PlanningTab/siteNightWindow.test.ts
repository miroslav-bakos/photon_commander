// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { describe, expect, it } from "vitest";
import { siteNightDate, siteNightWindow } from "./siteNightWindow";
describe("site night window", () => {
  it("uses Tokyo noon rather than the browser's noon", () => {
    const window = siteNightWindow("2026-10-07", "Asia/Tokyo");
    expect(window.start.toISOString()).toBe("2026-10-07T03:00:00.000Z");
    expect(window.end.toISOString()).toBe("2026-10-08T03:00:00.000Z");
    expect(siteNightDate(new Date("2026-10-07T01:00:00Z"), "Asia/Tokyo")).toBe("2026-10-06");
  });
  it("keeps 25-hour autumn and 23-hour spring nights in Bratislava", () => {
    const autumn = siteNightWindow("2026-10-24", "Europe/Bratislava");
    const spring = siteNightWindow("2026-03-28", "Europe/Bratislava");
    expect(autumn.end.getTime() - autumn.start.getTime()).toBe(25 * 3600_000);
    expect(spring.end.getTime() - spring.start.getTime()).toBe(23 * 3600_000);
  });
  it("supports quarter-hour site offsets", () => {
    expect(siteNightWindow("2026-10-07", "Asia/Kathmandu").start.toISOString()).toBe("2026-10-07T06:15:00.000Z");
  });
});
