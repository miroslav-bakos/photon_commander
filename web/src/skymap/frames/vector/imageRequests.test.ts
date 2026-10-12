// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { describe, expect, it, vi } from "vitest";
import { ImageRequestDispatcher } from "./imageRequests";

function image(): HTMLImageElement {
  return { src: "", onload: null, onerror: null } as HTMLImageElement;
}
function load(value: HTMLImageElement): void { value.onload?.call(value, new Event("load")); }
function error(value: HTMLImageElement): void { value.onerror?.call(value, new Event("error")); }

describe("global imagery capacity", () => {
  it("releases one slot on success and on failure, preferring latest visible requests", () => {
    const queue = new ImageRequestDispatcher();
    const active = Array.from({ length: 4 }, image);
    for (const [index, value] of active.entries()) queue.request(value, `/active/${index}`);
    const earlier = image(); const latest = image();
    queue.request(earlier, "/earlier"); queue.request(latest, "/latest");
    expect(earlier.src).toBe(""); expect(latest.src).toBe("");
    const first = active[0]; const second = active[1];
    if (first === undefined || second === undefined) throw new Error("Missing active requests");
    load(first);
    expect(latest.src).toBe("/latest"); expect(earlier.src).toBe("");
    error(second);
    expect(earlier.src).toBe("/earlier");
    queue.cancelMany([...active, earlier, latest]);
  });

  it("cancels obsolete queued requests without starting them and ignores late active callbacks", () => {
    const queue = new ImageRequestDispatcher();
    const active = Array.from({ length: 4 }, image);
    const obsolete = image(); const current = image(); const next = image();
    const ready = vi.fn();
    const first = active[0]; if (first === undefined) throw new Error("Active missing");
    first.onload = ready;
    for (const [index, value] of active.entries()) queue.request(value, `/active/${index}`);
    queue.request(obsolete, "/obsolete"); queue.request(next, "/next"); queue.request(current, "/current");
    const late = first.onload;
    queue.cancelMany([obsolete, first]);
    expect(obsolete.src).toBe(""); expect(first.src).toBe("");
    expect(current.src).toBe("/current"); expect(next.src).toBe("");
    late?.call(first, new Event("load"));
    expect(ready).not.toHaveBeenCalled(); expect(next.src).toBe("");
    queue.cancelMany([...active, current, next]);
  });

  it("does not start obsolete queued images during a batch teardown", () => {
    const queue = new ImageRequestDispatcher();
    const images = Array.from({ length: 8 }, image);
    for (const [index, value] of images.entries()) queue.request(value, `/tile/${index}`);
    expect(images.filter((value) => value.src !== "")).toHaveLength(4);
    queue.cancelMany(images);
    expect(images.every((value) => value.src === "")).toBe(true);
    const fresh = image(); queue.request(fresh, "/fresh");
    expect(fresh.src).toBe("/fresh");
    queue.cancelMany([fresh]);
  });
});
