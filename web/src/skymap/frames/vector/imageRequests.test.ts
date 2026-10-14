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
  it("starts the globally lowest priorities across a batch, with latest requests winning ties", () => {
    const queue = new ImageRequestDispatcher(2);
    const outer = image(); const earlier = image(); const latest = image(); const center = image();
    queue.batch(() => {
      queue.request(center, "/center", 0);
      queue.request(earlier, "/earlier", 10);
      queue.request(latest, "/latest", 10);
      queue.request(outer, "/outer", 100);
      expect(center.src).toBe("");
    });
    expect(center.src).toBe("/center"); expect(latest.src).toBe("/latest");
    expect(earlier.src).toBe(""); expect(outer.src).toBe("");
    load(center);
    expect(earlier.src).toBe("/earlier"); expect(outer.src).toBe("");
    queue.cancelMany([outer, earlier, latest, center]);
  });

  it("reprioritizes queued images without replacing handlers or duplicating active requests", () => {
    const queue = new ImageRequestDispatcher(1);
    const active = image(); const promoted = image(); const other = image();
    const ready = vi.fn(); promoted.onload = ready;
    queue.request(active, "/active", 0);
    queue.request(promoted, "/promoted", 100);
    const handler = promoted.onload;
    queue.request(other, "/other", 10);
    queue.request(promoted, "/ignored-replacement", 1);
    expect(promoted.onload).toBe(handler);
    queue.request(active, "/ignored-active", -100);
    expect(active.src).toBe("/active");
    load(active);
    expect(promoted.src).toBe("/promoted"); expect(other.src).toBe("");
    load(promoted);
    expect(ready).toHaveBeenCalledOnce(); expect(other.src).toBe("/other");
    queue.cancelMany([other]);
  });

  it("keeps priority ordering and capacity when cancelling active and queued jobs", () => {
    const queue = new ImageRequestDispatcher(1);
    const active = image(); const cancelled = image(); const best = image(); const last = image();
    queue.request(active, "/active");
    queue.request(cancelled, "/cancelled", -1);
    queue.request(best, "/best", 0);
    queue.request(last, "/last", 1);
    queue.cancelMany([cancelled, active]);
    expect(cancelled.src).toBe(""); expect(active.src).toBe("");
    expect(best.src).toBe("/best"); expect(last.src).toBe("");
    load(best); expect(last.src).toBe("/last");
    queue.cancelMany([last]);
  });

  it("releases one slot on success and on failure, preferring latest visible requests", () => {
    const queue = new ImageRequestDispatcher();
    const active = Array.from({ length: 4 }, image);
    for (const [index, value] of active.entries()) queue.request(value, `/active/${String(index)}`);
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
    for (const [index, value] of active.entries()) queue.request(value, `/active/${String(index)}`);
    queue.request(obsolete, "/obsolete"); queue.request(next, "/next"); queue.request(current, "/current");
    const late = first.onload;
    queue.cancelMany([obsolete, first]);
    expect(obsolete.src).toBe(""); expect(first.src).toBe("");
    expect(current.src).toBe("/current"); expect(next.src).toBe("");
    late.call(first, new Event("load"));
    expect(ready).not.toHaveBeenCalled(); expect(next.src).toBe("");
    queue.cancelMany([...active, current, next]);
  });

  it("does not start obsolete queued images during a batch teardown", () => {
    const queue = new ImageRequestDispatcher();
    const images = Array.from({ length: 8 }, image);
    for (const [index, value] of images.entries()) queue.request(value, `/tile/${String(index)}`);
    expect(images.filter((value) => value.src !== "")).toHaveLength(4);
    queue.cancelMany(images);
    expect(images.every((value) => value.src === "")).toBe(true);
    const fresh = image(); queue.request(fresh, "/fresh");
    expect(fresh.src).toBe("/fresh");
    queue.cancelMany([fresh]);
  });
});
