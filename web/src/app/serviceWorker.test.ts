// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
// @vitest-environment node
/// <reference types="node" />

import { readFileSync } from "node:fs";
import vm from "node:vm";
import { afterEach, describe, expect, it, type Mock, vi } from "vitest";

type FetchListener = (event: {
  request: { method: string; mode: string; url: string };
  respondWith: (response: Promise<Response>) => void;
}) => void;

interface Harness {
  fetch: Mock<(request: unknown, init?: { signal?: AbortSignal }) => Promise<Response>>;
  listener: FetchListener;
  match: ReturnType<typeof vi.fn>;
  put: ReturnType<typeof vi.fn>;
}

function harness(cached: Response | undefined): Harness {
  const listeners = new Map<string, (...args: never[]) => void>();
  const fetch = vi.fn<(request: unknown, init?: { signal?: AbortSignal }) => Promise<Response>>();
  const match = vi.fn().mockResolvedValue(cached);
  const put = vi.fn().mockResolvedValue(undefined);
  const cache = { addAll: vi.fn(), match, put };
  const context = {
    AbortController,
    Promise,
    Response,
    Set,
    URL,
    caches: {
      delete: vi.fn(),
      keys: vi.fn().mockResolvedValue([]),
      open: vi.fn().mockResolvedValue(cache),
    },
    clearTimeout,
    fetch,
    setTimeout,
    self: {
      addEventListener: (name: string, listener: (...args: never[]) => void) => {
        listeners.set(name, listener);
      },
      clients: { claim: vi.fn() },
      location: { origin: "http://photon.local" },
      skipWaiting: vi.fn(),
    },
  };
  const source = readFileSync(new URL("../../public/sw.js", import.meta.url), "utf8");
  vm.runInNewContext(source, context);
  const listener = listeners.get("fetch") as FetchListener | undefined;
  if (listener === undefined) throw new Error("service worker did not register a fetch listener");
  return { fetch, listener, match, put };
}

function navigate(listener: FetchListener): Promise<Response> {
  let response: Promise<Response> | undefined;
  listener({
    request: { method: "GET", mode: "navigate", url: "http://photon.local/" },
    respondWith: (value) => {
      response = value;
    },
  });
  if (response === undefined) throw new Error("navigation was not intercepted");
  return response;
}

describe("app-shell service worker", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns the cached shell after the navigation network deadline", async () => {
    vi.useFakeTimers();
    const cached = new Response("cached shell", { status: 200 });
    const app = harness(cached);
    app.fetch.mockImplementation(
      (_request, init) =>
        new Promise((_resolve, reject) => {
          if (init?.signal !== undefined) {
            init.signal.addEventListener("abort", () => {
              reject(new Error("aborted"));
            });
          }
        }),
    );

    const response = navigate(app.listener);
    await vi.advanceTimersByTimeAsync(1_999);
    let settled = false;
    void response.then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    expect(await (await response).text()).toBe("cached shell");
  });

  it("uses and caches a responsive network navigation", async () => {
    const app = harness(new Response("old shell", { status: 200 }));
    app.fetch.mockResolvedValue(new Response("current shell", { status: 200 }));

    expect(await (await navigate(app.listener)).text()).toBe("current shell");
    expect(app.put).toHaveBeenCalledOnce();
  });

  it("waits for the network when no cached shell exists", async () => {
    vi.useFakeTimers();
    const app = harness(undefined);
    let answer: ((response: Response) => void) | undefined;
    app.fetch.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    );

    const response = navigate(app.listener);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(answer).toBeDefined();
    answer?.(new Response("first shell", { status: 200 }));
    expect(await (await response).text()).toBe("first shell");
  });

  it.each(["/api/state", "/ws"])("does not intercept %s", (path) => {
    const app = harness(new Response("cached", { status: 200 }));
    const respondWith = vi.fn();

    app.listener({
      request: { method: "GET", mode: "cors", url: `http://photon.local${path}` },
      respondWith,
    });

    expect(respondWith).not.toHaveBeenCalled();
    expect(app.fetch).not.toHaveBeenCalled();
  });
});
