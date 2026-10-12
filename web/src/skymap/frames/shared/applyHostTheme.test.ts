// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { ROLE_TOKEN } from "./roleStyle";
import { URL_THEME_KEYS } from "./applyHostTheme";

/** `applyHostTheme.ts`'s own doc comment: "Every token `roleStyle.ts`
 * resolves must be listed here or it silently reads an empty string
 * forever." This is the check that comment asks for, rather than relying on
 * the next person to reread it before adding a role. */
describe("URL_THEME_KEYS", () => {
  it("carries every --ssc-* token ROLE_TOKEN resolves", () => {
    const urlKeySet = new Set(URL_THEME_KEYS.map((key) => `--ssc-${key.replace(/[A-Z]/g, (l) => `-${l.toLowerCase()}`)}`));
    for (const [role, token] of Object.entries(ROLE_TOKEN)) {
      expect(urlKeySet.has(token), `${role} -> ${token} missing from URL_THEME_KEYS`).toBe(true);
    }
  });
});
