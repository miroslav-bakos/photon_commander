// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

export interface DiagnosticEntry {
  id: number;
  kind: "screen" | "api";
  name: string;
  startedAt: number;
  durationMs: number | null;
  status: string;
}

const MAX_ENTRIES = 80;
let entries: DiagnosticEntry[] = [];
let nextId = 1;
const listeners = new Set<() => void>();
let pendingNavigation: { path: string; start: number } | null = null;

function publish(next: DiagnosticEntry[]) {
  entries = next.slice(0, MAX_ENTRIES);
  for (const listener of listeners) listener();
}

export function subscribeDiagnostics(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function getDiagnosticsSnapshot() { return entries; }

export function clearDiagnostics() { publish([]); }

export function beginNavigation(path: string) {
  pendingNavigation = { path, start: performance.now() };
}

export function finishNavigation(path: string) {
  const start = pendingNavigation?.path === path ? pendingNavigation.start : performance.now();
  pendingNavigation = null;
  const id = nextId++;
  let finished = false;
  publish([{ id, kind: "screen", name: path, startedAt: Date.now(), durationMs: null, status: "Painting" }, ...entries]);
  return {
    finish: () => {
      finished = true;
      publish(entries.map((entry) => entry.id === id
        ? { ...entry, durationMs: Math.round(performance.now() - start), status: "Painted" }
        : entry));
    },
    cancel: () => {
      if (!finished) publish(entries.filter((entry) => entry.id !== id));
    },
  };
}

function apiPath(request: Request): string {
  const url = new URL(request.url);
  // Never retain query parameters (tokens and user-entered coordinates can be there).
  return `${request.method} ${url.pathname}`;
}

export async function timedApiFetch(request: Request): Promise<Response> {
  const start = performance.now();
  const id = nextId++;
  publish([{ id, kind: "api", name: apiPath(request), startedAt: Date.now(), durationMs: null, status: "Pending" }, ...entries]);
  const controller = new AbortController();
  const abort = () => { controller.abort(request.signal.reason); };
  if (request.signal.aborted) abort();
  else request.signal.addEventListener("abort", abort, { once: true });
  let timedOut = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      timedOut = true;
      reject(new Error(request.method === "GET" || request.method === "HEAD"
        ? "The server did not respond within 30 seconds. Check the connection and try again."
        : "The server did not confirm the request within 30 seconds. Its result is unknown; reconnect and check the saved state before trying again."));
      controller.abort();
    }, 30_000);
  });
  try {
    const response = await Promise.race([deadline, (async () => {
      const received = await globalThis.fetch(new Request(request, { signal: controller.signal }));
      // Include a stalled JSON body in the deadline. File/media streams keep
      // their streaming behavior; these are not save acknowledgements.
      if (!(received.headers.get("content-type") ?? "").includes("application/json")) return received;
      const body = await received.arrayBuffer();
      const complete = new Response([204, 205, 304].includes(received.status) ? null : body,
        { status: received.status, statusText: received.statusText, headers: received.headers });
      Object.defineProperty(complete, "url", { value: received.url });
      return complete;
    })()]);
    publish(entries.map((entry) => entry.id === id
      ? { ...entry, durationMs: Math.round(performance.now() - start), status: String(response.status) }
      : entry));
    return response;
  } catch (error) {
    publish(entries.map((entry) => entry.id === id
      ? { ...entry, durationMs: Math.round(performance.now() - start), status: timedOut ? "Timed out" : "Network error" }
      : entry));
    throw error;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    request.signal.removeEventListener("abort", abort);
  }
}
