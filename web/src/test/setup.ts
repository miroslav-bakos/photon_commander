// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { afterEach } from "vitest";

import "@testing-library/jest-dom/vitest";

/**
 * jsdom implements no fetch stack, so vitest leaves Node's undici globals in
 * place — and undici's `Request` rejects a relative URL, while a browser's
 * resolves it against the document. `src/api/client.ts` deliberately uses a
 * relative `baseUrl` (same origin in production, Vite-proxied in dev), so
 * every request `openapi-fetch` builds is relative and dies in tests with
 * "Failed to parse URL from /api/…" — a failure of the environment, not of
 * the code under test.
 *
 * Resolving against `location.origin` is exactly what the browser does. This
 * is a jsdom shim, not a behaviour change: it must never be needed at runtime.
 */
const UndiciRequest = globalThis.Request;

class RelativeAwareRequest extends UndiciRequest {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    if (typeof input === "string" && input.startsWith("/")) {
      super(new URL(input, globalThis.location.origin), init);
      return;
    }
    super(input, init);
  }
}

globalThis.Request = RelativeAwareRequest;

import { disposeQueryClients } from "./queryClient";

// Global, not per-file: a client left running in ONE test file throws its
// unhandled error into whichever file happens to be running when the timer
// fires, so the cleanup has to be unconditional.
afterEach(() => {
  disposeQueryClients();
});
