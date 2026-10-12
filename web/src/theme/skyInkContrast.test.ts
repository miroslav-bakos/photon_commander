// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The defect this guards against: a sky-map role resolving to a real,
 * "correctly applied" colour token that is nevertheless indistinguishable
 * from `--ssc-bg` once actually drawn — `--ssc-border` (`#262c35`) over
 * `--ssc-bg` (`#0b0d10`) was "all but invisible even at 100% opacity" (owner,
 * 2026-09-10) despite being exactly the token `coordGrid` asked for. No
 * amount of type-checking or `getComputedStyle` catches that; only reading
 * the two colours and comparing them does.
 *
 * Parses `tokens.css` directly rather than rendering it: jsdom's
 * `getComputedStyle` does not resolve custom properties the way a real
 * browser paints them for *this* check's purposes (the raw hex is what
 * matters, not cascade resolution), and the file itself is the single
 * source of truth this test wants to catch drift in, in both themes.
 * Imported with Vite's `?raw` suffix (typed by `vite/client.d.ts`) rather
 * than `node:fs`, which `tsconfig.app.json` has no type declarations for.
 */
import { describe, expect, it } from "vitest";
import css from "./tokens.css?raw";

function extractThemeBlock(css: string, selectorTail: string): string {
  const startMarker = `[data-theme="${selectorTail}"]`;
  const start = css.indexOf(startMarker);
  if (start === -1) throw new Error(`extractThemeBlock: no block for ${selectorTail}`);
  const braceStart = css.indexOf("{", start);
  const braceEnd = css.indexOf("\n}", braceStart);
  if (braceStart === -1 || braceEnd === -1) throw new Error(`extractThemeBlock: malformed block for ${selectorTail}`);
  return css.slice(braceStart, braceEnd);
}

function tokenValue(block: string, name: `--ssc-${string}`): string {
  const declaration = new RegExp(`${name}:\\s*([^;]+);`).exec(block)?.[1];
  const value = declaration === undefined
    ? undefined
    : /#[0-9a-fA-F]{6}/.exec(declaration)?.[0];
  if (value === undefined) throw new Error(`tokenValue: no ${name} in block`);
  return value;
}

function hexToRgb(hex: string): [number, number, number] {
  const n = Number.parseInt(hex.slice(1), 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
}

/** Perceptual-ish luminance distance, 0-255ish scale — plenty for "is this
 * even findable over the background", not a WCAG contrast ratio. */
function luminance([r, g, b]: [number, number, number]): number {
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Chosen so `--ssc-border` against `--ssc-bg` in both themes — the actual
 * reported defect, `coordGrid`'s token before this fix — fails it (delta
 * ~31 dark, ~16 red), while every token meant to be legible over sky clears
 * it with room to spare (the closest, `--ssc-cardinal` in the red theme,
 * clears it by ~16). See the "demonstrates the defect" test below, which
 * checks this threshold actually separates the two rather than trusting the
 * arithmetic in this comment. */
const MIN_LUMINANCE_DELTA = 60;

/** Every role drawn directly over the sky background rather than over a UI
 * surface (`roleStyle.ts`'s `ROLE_TOKEN`) — the set this bug actually hit. */
const SKY_DRAWN_TOKENS: `--ssc-${string}`[] = [
  "--ssc-sky-ink",
  "--ssc-star",
  "--ssc-sun",
  "--ssc-moon",
  "--ssc-planet",
  "--ssc-cardinal",
  "--ssc-pointing",
  "--ssc-accent",
  "--ssc-stack",
  "--ssc-envelope",
  "--ssc-success",
  "--ssc-warning",
  "--ssc-danger",
];

describe.each([
  ["dark", "dark"],
  ["red", "red"],
] as const)("%s theme: sky-drawn tokens stay legible over --ssc-bg", (_label, themeName) => {
  const block = extractThemeBlock(css, themeName);
  const bg = luminance(hexToRgb(tokenValue(block, "--ssc-bg")));

  it.each(SKY_DRAWN_TOKENS)("%s is not close to --ssc-bg", (token) => {
    const value = luminance(hexToRgb(tokenValue(block, token)));
    expect(Math.abs(value - bg)).toBeGreaterThanOrEqual(MIN_LUMINANCE_DELTA);
  });

  it("--ssc-border (a UI chrome token, not a sky role) demonstrates the defect this guards against", () => {
    // Documents the actual regression rather than merely asserting an
    // abstract threshold: `coordGrid` used to point straight at this token,
    // which is exactly the pairing that was "all but invisible" (owner,
    // 2026-09-10). Not a `MIN_LUMINANCE_DELTA` assertion on `--ssc-sky-ink`
    // alone — this proves the metric actually distinguishes the reported
    // bad case from the fixed one, so a future change that quietly re-lowers
    // `MIN_LUMINANCE_DELTA` below the defect's own delta would be caught by
    // this failing to fail.
    const border = luminance(hexToRgb(tokenValue(block, "--ssc-border")));
    expect(Math.abs(border - bg)).toBeLessThan(MIN_LUMINANCE_DELTA);
  });
});
