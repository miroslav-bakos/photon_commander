// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { afterEach, describe, expect, it, vi } from "vitest";
import { beginNavigation, clearDiagnostics, finishNavigation, getDiagnosticsSnapshot, subscribeDiagnostics, timedApiFetch } from "./diagnostics";

afterEach(() => {
  clearDiagnostics();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("loading diagnostics", () => {
  it("measures a screen paint and notifies subscribers", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeDiagnostics(listener);
    beginNavigation("/planning");
    const timing = finishNavigation("/planning");
    expect(getDiagnosticsSnapshot()[0]).toMatchObject({ kind: "screen", name: "/planning", status: "Painting", durationMs: null });
    timing.finish();
    expect(getDiagnosticsSnapshot()[0]).toMatchObject({ status: "Painted" });
    expect(getDiagnosticsSnapshot()[0]?.durationMs).toBeTypeOf("number");
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
  });

  it("discards an interrupted paint", () => {
    const timing = finishNavigation("/archive");
    timing.cancel();
    expect(getDiagnosticsSnapshot()).toEqual([]);
  });

  it("records API status and duration without query parameters", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("ok", { status: 200 })));
    const request = new Request("http://localhost/api/astro/twilight?secret=private");
    await timedApiFetch(request);
    expect(getDiagnosticsSnapshot()[0]).toMatchObject({ kind: "api", name: "GET /api/astro/twilight", status: "200" });
    expect(getDiagnosticsSnapshot()[0]?.durationMs).toBeTypeOf("number");
  });

  it("shows a pending request and records network errors", async () => {
    let rejectRequest: (error: Error) => void = () => { throw new Error("Request was not started"); };
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise<Response>((_resolve, reject: (error: Error) => void) => { rejectRequest = reject; })));
    const request = timedApiFetch(new Request("http://localhost/api/plans"));
    expect(getDiagnosticsSnapshot()[0]).toMatchObject({ status: "Pending", durationMs: null });
    rejectRequest(new Error("offline"));
    await expect(request).rejects.toThrow("offline");
    expect(getDiagnosticsSnapshot()[0]).toMatchObject({ status: "Network error" });
  });

  it("releases a hanging save with an unknown outcome and never repeats the write", async () => {
    vi.useFakeTimers();
    let release!: (response: Response) => void;
    const fetch = vi.fn(() => new Promise<Response>((resolve) => { release = resolve; }));
    vi.stubGlobal("fetch", fetch);
    let outcome: unknown;
    const pending = timedApiFetch(new Request("http://localhost/api/plans/example", { method: "PUT" }))
      .catch((error: unknown) => { outcome = error; });
    await vi.advanceTimersByTimeAsync(30_001);
    expect(outcome).toBeInstanceOf(Error);
    expect((outcome as Error).message).toContain("result is unknown");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(getDiagnosticsSnapshot()[0]?.status).toBe("Timed out");
    release(new Response("ok"));
    await pending;
  });

  it("bounds a JSON acknowledgement whose headers arrive but body stalls", async () => {
    vi.useFakeTimers();
    let streamController!: ReadableStreamDefaultController;
    const stream = new ReadableStream({ start(controller) { streamController = controller; } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(stream, { headers: { "Content-Type": "application/json" } })));
    let outcome: unknown;
    const pending = timedApiFetch(new Request("http://localhost/api/settings/site", { method: "PUT" }))
      .catch((error: unknown) => { outcome = error; });
    await vi.advanceTimersByTimeAsync(30_001);
    expect(outcome).toBeInstanceOf(Error);
    streamController.close();
    await pending;
  });

  it("returns readable JSON and clears the deadline after an acknowledged response", async () => {
    vi.useFakeTimers();
    const original = Response.json({ saved: true });
    Object.defineProperty(original, "url", { value: "http://localhost/api/settings/site" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(original));
    const response = await timedApiFetch(new Request(original.url, { method: "PUT" }));
    expect(await response.json()).toEqual({ saved: true });
    expect(response.url).toBe(original.url);
    expect(vi.getTimerCount()).toBe(0);
  });
});
