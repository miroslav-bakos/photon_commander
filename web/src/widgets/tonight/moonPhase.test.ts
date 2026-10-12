// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { isLitOnRight, moonPath, phaseName } from "./moonPhase";

/** `moonPath` always emits exactly two elliptical arcs (limb, then
 * terminator). Pulling out each arc's horizontal radius and sweep-flag is a
 * far more direct assertion of the geometry than trying to reconstruct an
 * area from the path string — the two arcs share both endpoints (top and
 * bottom of the disc), so the shape lives entirely in these two numbers. */
function arcs(d: string): [{ rx: number; sweep: number }, { rx: number; sweep: number }] {
  const found = [...d.matchAll(/A ([\d.]+) [\d.]+ 0 0 (\d)/g)].map((m) => ({
    rx: Number(m[1]),
    sweep: Number(m[2]),
  }));
  const [first, second] = found;
  if (found.length !== 2 || first === undefined || second === undefined) {
    throw new Error(`expected exactly 2 arcs in path, found ${found.length.toString()}: ${d}`);
  }
  return [first, second];
}

describe("isLitOnRight", () => {
  it("waxing lights the right in the northern hemisphere", () => {
    expect(isLitOnRight(true, "north")).toBe(true);
    expect(isLitOnRight(false, "north")).toBe(false);
  });

  it("flips for the southern hemisphere — the trap CLAUDE.md calls out", () => {
    expect(isLitOnRight(true, "south")).toBe(false);
    expect(isLitOnRight(false, "south")).toBe(true);
  });
});

describe("moonPath boundary cases (waxing, northern hemisphere, radius 10)", () => {
  it("new moon (f=0): both arcs share the limb's radius, on the same side — zero area", () => {
    const [limb, terminator] = arcs(
      moonPath({ illuminatedFraction: 0, waxing: true, hemisphere: "north", radius: 10 }),
    );
    expect(limb).toEqual({ rx: 10, sweep: 1 });
    // Same rx as the limb, complementary sweep-flag: per the sweep-flag/side
    // mapping (arc1 "right"=1, arc2 "right"=0), that is the *same* geometric
    // side — the terminator arc exactly retraces the limb arc.
    expect(terminator.rx).toBeCloseTo(10, 5);
    expect(terminator.sweep).toBe(0);
  });

  it("full moon (f=1): terminator radius grows back to the disc radius, opposite side", () => {
    const [limb, terminator] = arcs(
      moonPath({ illuminatedFraction: 1, waxing: true, hemisphere: "north", radius: 10 }),
    );
    expect(limb).toEqual({ rx: 10, sweep: 1 });
    expect(terminator.rx).toBeCloseTo(10, 5);
    // Same sweep-flag value as the limb this time — per the mapping, that's
    // the *opposite* side, so limb + terminator trace a full circle.
    expect(terminator.sweep).toBe(1);
  });

  it("exact quarter (f=0.5): terminator radius collapses to 0 — a straight edge", () => {
    const [, terminator] = arcs(
      moonPath({ illuminatedFraction: 0.5, waxing: true, hemisphere: "north", radius: 10 }),
    );
    expect(terminator.rx).toBeCloseTo(0, 5);
  });

  it("first quarter and last quarter are mirror images, not the same path", () => {
    // The exact trap named in the ticket: both are 50% illuminated, and
    // must not render identically.
    const firstQuarter = moonPath({ illuminatedFraction: 0.5, waxing: true, hemisphere: "north", radius: 10 });
    const lastQuarter = moonPath({ illuminatedFraction: 0.5, waxing: false, hemisphere: "north", radius: 10 });
    expect(firstQuarter).not.toBe(lastQuarter);
    const [limbFirst] = arcs(firstQuarter);
    const [limbLast] = arcs(lastQuarter);
    expect(limbFirst.sweep).not.toBe(limbLast.sweep);
  });

  it("clamps an out-of-range fraction rather than drawing something undefined", () => {
    const over = moonPath({ illuminatedFraction: 1.4, waxing: true, hemisphere: "north", radius: 10 });
    const atOne = moonPath({ illuminatedFraction: 1, waxing: true, hemisphere: "north", radius: 10 });
    expect(over).toBe(atOne);
    const under = moonPath({ illuminatedFraction: -0.4, waxing: true, hemisphere: "north", radius: 10 });
    const atZero = moonPath({ illuminatedFraction: 0, waxing: true, hemisphere: "north", radius: 10 });
    expect(under).toBe(atZero);
  });

  it("the southern hemisphere flips which side the limb sits on, not the terminator math", () => {
    const north = moonPath({ illuminatedFraction: 0.25, waxing: true, hemisphere: "north", radius: 10 });
    const south = moonPath({ illuminatedFraction: 0.25, waxing: true, hemisphere: "south", radius: 10 });
    const [limbNorth] = arcs(north);
    const [limbSouth] = arcs(south);
    expect(limbNorth.sweep).not.toBe(limbSouth.sweep);
  });
});

describe("phaseName", () => {
  it("names the ambiguous 50% cases from waxing, never from the fraction alone", () => {
    expect(phaseName(0.5, true)).toBe("First quarter");
    expect(phaseName(0.5, false)).toBe("Last quarter");
  });

  it("names the poles regardless of direction", () => {
    expect(phaseName(0, true)).toBe("New moon");
    expect(phaseName(1, false)).toBe("Full moon");
  });

  it("names crescent and gibbous on both sides of half", () => {
    expect(phaseName(0.2, true)).toBe("Waxing crescent");
    expect(phaseName(0.2, false)).toBe("Waning crescent");
    expect(phaseName(0.8, true)).toBe("Waxing gibbous");
    expect(phaseName(0.8, false)).toBe("Waning gibbous");
  });
});
