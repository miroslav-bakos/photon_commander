// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { mountPositionToJ2000 } from "./mountFrame";

describe("mountPositionToJ2000", () => {
  it("passes a position through unchanged once the frame is already J2000", () => {
    const result = mountPositionToJ2000({ raHours: 10.5, decDeg: 41.2, equatorialSystem: "J2000" });
    expect(result).toEqual({ status: "ok", raHours: 10.5, decDeg: 41.2 });
  });

  it("matches the J2000 label case-insensitively", () => {
    const result = mountPositionToJ2000({ raHours: 1, decDeg: 2, equatorialSystem: "j2000" });
    expect(result.status).toBe("ok");
  });

  // The device this app targets: refusing here, not silently drawing the
  // mount's of-date position as if it were J2000, is the whole point of
  // this module — a straight plot lands ~20' off and reads as pointing
  // error (DESIGN.md §3.5.1's frame trap).
  it("refuses a topocentric (of-date) position rather than passing it through", () => {
    const result = mountPositionToJ2000({ raHours: 10.5, decDeg: 41.2, equatorialSystem: "topocentric" });
    expect(result.status).toBe("unavailable");
    if (result.status === "unavailable") {
      expect(result.reason).toMatch(/topocentric/);
    }
  });

  it("refuses rather than guesses when the equatorial system is unknown", () => {
    const result = mountPositionToJ2000({ raHours: 10.5, decDeg: 41.2, equatorialSystem: null });
    expect(result.status).toBe("unavailable");
  });

  it("refuses when the mount has not reported a position yet", () => {
    expect(mountPositionToJ2000({ raHours: null, decDeg: 41.2, equatorialSystem: "J2000" }).status).toBe(
      "unavailable",
    );
    expect(mountPositionToJ2000({ raHours: 10.5, decDeg: null, equatorialSystem: "J2000" }).status).toBe(
      "unavailable",
    );
  });
});
