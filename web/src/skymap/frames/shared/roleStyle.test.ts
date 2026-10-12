// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";

import { ROLE_TOKEN } from "./roleStyle";

/** The five FOV shapes are drawn on top of one another around the same
 * target, so they have to stay separable by colour. Everything else — grid
 * lines, boundaries, catalogue outlines — is background and may share.
 * `pointing` (the believed-pointing indicator, DESIGN.md §3.5.1) joins this
 * set for the same reason: it can be drawn over a planning session's own FOV
 * shapes around the same target, and must not read as any of them. */
const FOV_ROLES = ["primary", "ghost", "stack", "union", "keep", "pointing"] as const;

describe("ROLE_TOKEN", () => {
  it("gives each FOV role a colour token no other FOV role uses", () => {
    // `stack` was `--ssc-accent`, the same token as `primary`, so the alt-az
    // rotation indicator drew in the FOV rectangle's own colour and read as a
    // broken duplicate of the frame rather than as rotation (owner,
    // 2026-09-10).
    const tokens = FOV_ROLES.map((role) => ROLE_TOKEN[role]);
    expect(new Set(tokens).size).toBe(FOV_ROLES.length);
  });

  it("keeps the pointing indicator off both existing red tokens", () => {
    // `danger` and `cardinal` are already red in the dark theme; `pointing`
    // answers a third question ("where is the scope right now") and must
    // not silently share either's token.
    expect(ROLE_TOKEN.pointing).not.toBe(ROLE_TOKEN.danger);
    expect(ROLE_TOKEN.pointing).not.toBe(ROLE_TOKEN.cardinal);
  });

  it("resolves every role to a token, so none silently reads as empty", () => {
    for (const [role, token] of Object.entries(ROLE_TOKEN)) {
      expect(token, `${role} has no token`).toMatch(/^--ssc-/);
    }
  });
});
