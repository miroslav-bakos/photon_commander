// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { describe, expect, it } from "vitest";
import css from "./tokens.css?raw";

function theme(name: string): string {
  const start = css.indexOf(`[data-theme="${name}"]`);
  return css.slice(start, css.indexOf("\n}", start));
}
function rgb(block: string, token: string): [number, number, number] {
  const hex = new RegExp(`--ssc-${token}: #([a-f0-9]{6});`).exec(block)?.[1];
  if (!hex) throw new Error(`Missing ${token}`);
  const channel = (index: number) => Number.parseInt(hex.slice(index, index + 2), 16);
  return [channel(0), channel(2), channel(4)];
}
function luminance(values: [number, number, number]): number {
  const linear = values.map((value) => { const x = value / 255; return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; });
  return (linear[0] ?? 0) * 0.2126 + (linear[1] ?? 0) * 0.7152 + (linear[2] ?? 0) * 0.0722;
}
function contrast(a: [number, number, number], b: [number, number, number]): number {
  const first = luminance(a);
  const second = luminance(b);
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
}
describe("interface text and field contrast", () => {
  it.each(["dark", "red"])("keeps readable text and distinguishable fields in %s", (name) => {
    const block = theme(name);
    for (const surface of ["bg", "surface", "surface-raised"]) {
      for (const text of ["fg", "fg-muted"]) expect(contrast(rgb(block, text), rgb(block, surface))).toBeGreaterThanOrEqual(4.5);
      expect(contrast(rgb(block, "border-strong"), rgb(block, surface))).toBeGreaterThanOrEqual(3);
    }
  });
  it("keeps the night interface text in a red-only palette", () => {
    for (const token of ["bg", "surface", "surface-raised", "fg", "fg-muted", "border-strong"]) {
      const [red, green, blue] = rgb(theme("red"), token);
      expect(green).toBe(blue);
      expect(red).toBeGreaterThan(green);
    }
  });
});
