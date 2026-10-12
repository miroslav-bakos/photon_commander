// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";

import { ApiError } from "./client";

describe("ApiError", () => {
  it("names a stale server when a generated path 404s", () => {
    // The failure this exists for: ssc-core running older code while serving a
    // rebuilt web/dist. The UI called /api/astro/target, got 404, and rendered
    // "request failed with status 404" — which reads as a broken feature.
    const err = new ApiError(404, undefined, "/api/astro/target");
    expect(err.message).toContain("/api/astro/target");
    expect(err.message).toMatch(/older code/i);
    expect(err.message).toMatch(/restart ssc-core/i);
  });

  it("still prefers a server-supplied detail over the generic text", () => {
    const err = new ApiError(409, { detail: "no location set — Settings > Location" });
    expect(err.message).toBe("no location set — Settings > Location");
  });

  it("does not claim staleness for a 404 that carries its own detail", () => {
    // A route that exists and legitimately answers "not found" must not be
    // mistaken for an absent route.
    const err = new ApiError(404, { detail: "no catalogue match for 'NGC 99999'" });
    expect(err.message).toBe("no catalogue match for 'NGC 99999'");
    expect(err.message).not.toMatch(/older code/i);
  });

  it("extracts a Pydantic model-validator message from a 422's detail list", () => {
    // FastAPI's default validation-error shape is `detail: ValidationError[]`,
    // not a plain string — HorizonRequest's duplicate-azimuth check raises
    // through this path (ssc/api/schemas.py), and the point of that check is
    // that the rejection names the fix.
    const err = new ApiError(422, {
      detail: [
        {
          type: "value_error",
          loc: [],
          msg: "Value error, duplicate azimuth 10.0 deg at points 0 and 1: each azimuth may appear once in a horizon profile",
        },
      ],
    });
    expect(err.message).toBe(
      "duplicate azimuth 10.0 deg at points 0 and 1: each azimuth may appear once in a horizon profile",
    );
  });

  it("joins multiple validation-error messages", () => {
    const err = new ApiError(422, {
      detail: [
        { type: "too_short", loc: ["points"], msg: "List should have at least 2 items" },
        { type: "value_error", loc: ["points", 0, "altitude_deg"], msg: "Value error, out of range" },
      ],
    });
    expect(err.message).toBe("List should have at least 2 items; out of range");
  });

  it("falls back to the status when there is no path to name", () => {
    const err = new ApiError(500, undefined);
    expect(err.message).toBe("request failed with status 500");
  });

  it("keeps the status and path for callers that branch on them", () => {
    const err = new ApiError(404, undefined, "/api/astro/framing");
    expect(err.status).toBe(404);
    expect(err.path).toBe("/api/astro/framing");
  });
});
