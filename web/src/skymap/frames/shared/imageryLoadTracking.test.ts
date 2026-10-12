// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { trackImageryLoading } from "./imageryLoadTracking";

describe("trackImageryLoading", () => {
  let stop: () => void;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    stop();
    vi.useRealTimers();
  });

  it("reports true the instant an image's src is set, before it has loaded", () => {
    const seen: boolean[] = [];
    stop = trackImageryLoading((loading) => seen.push(loading));

    const img = new Image();
    img.src = "/api/skymap/tiles/legacy-surveys-dr11/mercator/2/3/1.jpg";

    expect(seen).toEqual([true]);
  });

  it("reports false only after SETTLE_MS with nothing left in flight", () => {
    const seen: boolean[] = [];
    stop = trackImageryLoading((loading) => seen.push(loading));

    const img = new Image();
    img.src = "/api/skymap/tiles/legacy-surveys-dr11/mercator/2/3/1.jpg";
    img.dispatchEvent(new Event("load"));

    expect(seen).toEqual([true]); // not yet — still inside the settle window
    vi.advanceTimersByTime(199);
    expect(seen).toEqual([true]);
    vi.advanceTimersByTime(1);
    expect(seen).toEqual([true, false]);
  });

  it("never reports false while any of several images is still in flight", () => {
    const seen: boolean[] = [];
    stop = trackImageryLoading((loading) => seen.push(loading));

    const a = new Image();
    const b = new Image();
    a.src = "/api/skymap/tiles/legacy-surveys-dr11/mercator/2/3/1.jpg";
    b.src = "/api/skymap/tiles/legacy-surveys-dr11/mercator/2/3/2.jpg";
    a.dispatchEvent(new Event("load"));
    vi.advanceTimersByTime(500);

    expect(seen).toEqual([true]); // b is still pending — the settle timer for a alone never fires false
  });

  it("treats a failed load the same as a successful one — the fetch attempt is over either way", () => {
    const seen: boolean[] = [];
    stop = trackImageryLoading((loading) => seen.push(loading));

    const img = new Image();
    img.src = "/api/skymap/tiles/legacy-surveys-dr11/mercator/2/3/1.jpg";
    img.dispatchEvent(new Event("error"));
    vi.advanceTimersByTime(200);

    expect(seen).toEqual([true, false]);
  });

  it("stays balanced across a retry that reassigns src on the same element (Tile.requestImage's own proxy-retry pattern)", () => {
    const seen: boolean[] = [];
    stop = trackImageryLoading((loading) => seen.push(loading));

    const img = new Image();
    img.src = "/api/skymap/tiles/legacy-surveys-dr11/mercator/2/3/1.jpg";
    img.dispatchEvent(new Event("error")); // triggers a retry in the real engine
    img.src = "/api/skymap/tiles/legacy-surveys-dr11/mercator/2/3/1.jpg?retry=1";
    vi.advanceTimersByTime(200);
    expect(seen).toEqual([true]); // the retry's own assignment is still pending

    img.dispatchEvent(new Event("load"));
    vi.advanceTimersByTime(200);
    expect(seen).toEqual([true, false]);
  });

  it("never reports the same value twice in a row", () => {
    const seen: boolean[] = [];
    stop = trackImageryLoading((loading) => seen.push(loading));

    const a = new Image();
    a.src = "/api/skymap/tiles/legacy-surveys-dr11/mercator/2/3/1.jpg";
    const b = new Image();
    b.src = "/api/skymap/tiles/legacy-surveys-dr11/mercator/2/3/2.jpg"; // already loading — no redundant "true"

    expect(seen).toEqual([true]);
  });

  it("settles after canceling a pending tile with an empty source", () => {
    const seen: boolean[] = [];
    stop = trackImageryLoading((loading) => seen.push(loading));
    const img = new Image();
    img.src = "/api/tile.jpg";
    img.src = "";
    img.dispatchEvent(new Event("error"));
    vi.advanceTimersByTime(200);
    expect(seen).toEqual([true, false]);
  });

  it("counts a replacement source as one pending image", () => {
    const seen: boolean[] = [];
    stop = trackImageryLoading((loading) => seen.push(loading));
    const img = new Image();
    img.src = "/api/old.jpg";
    img.src = "/api/new.jpg";
    img.dispatchEvent(new Event("load"));
    vi.advanceTimersByTime(200);
    expect(seen).toEqual([true, false]);
  });

  it("does not finish another image when a completed image emits another event", () => {
    const seen: boolean[] = [];
    stop = trackImageryLoading((loading) => seen.push(loading));
    const a = new Image();
    const b = new Image();
    a.src = "/api/a.jpg";
    b.src = "/api/b.jpg";
    a.dispatchEvent(new Event("load"));
    a.dispatchEvent(new Event("error"));
    vi.advanceTimersByTime(200);
    expect(seen).toEqual([true]);
  });
});
