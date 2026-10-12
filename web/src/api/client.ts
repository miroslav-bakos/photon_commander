// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The one REST client. Every path, param, request body and response shape
 * comes from `schema.d.ts`, which is generated from the backend's own OpenAPI
 * document (`npm run api:codegen`) — never hand-mirrored (frontend-react.md).
 *
 * `ssc-core` serves this app from the same origin in production, and the Vite
 * dev server proxies `/api` and `/ws` to it (vite.config.ts), so a relative
 * base URL is correct in both.
 */
import createClient from "openapi-fetch";
import type { paths } from "./schema";
import { timedApiFetch } from "../performance/diagnostics";

export const api = createClient<paths>({
  baseUrl: "",
  credentials: "same-origin",
  // Dispatch through `globalThis.fetch` on every call rather than letting
  // `openapi-fetch` capture it once at module load. Identical at runtime, and
  // it is what lets a test swap the global and still exercise this client —
  // status handling, `unwrap`, `ApiError` and all — instead of having to mock
  // the query hook and test nothing of the layer that actually breaks.
  fetch: timedApiFetch,
});

/**
 * Turns an `openapi-fetch` result into a value or a thrown `Error`. Routes
 * only document response models for the status codes their handler
 * deliberately returns (architecture policy invariant 7's honesty cuts both ways: an
 * auth failure or a rate limit isn't declared per-route), so `response.ok` —
 * not the schema-typed `error` field — is the real success signal here.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly path: string | undefined;
  constructor(status: number, error: unknown, path?: string) {
    super(describeError(status, error, path));
    this.status = status;
    this.path = path;
  }
}

/**
 * A 404 on a path this UI was generated against does not mean "not found" — it
 * means the route is absent from the server, i.e. ssc-core is running older
 * code than the bundle it is serving. That happens whenever a long-lived
 * process keeps serving a rebuilt `web/dist` from disk, and it cost an
 * evening once: the UI called an endpoint that did not exist and reported
 * "request failed with status 404", which reads as a bug in the feature
 * rather than a stale process.
 *
 * `web/src/api/schema.d.ts` is generated from the server's own OpenAPI, so
 * every path this client can construct is one the server is *supposed* to
 * have. Saying so is strictly more useful than the status code.
 */
function describeError(status: number, error: unknown, path?: string): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    const asValidation = describeValidationDetail(detail);
    if (asValidation !== null) return asValidation;
  }
  if (status === 404 && path !== undefined) {
    return (
      `This server has no ${path}. It is almost certainly running older code ` +
      `than the interface it is serving — restart ssc-core and reload.`
    );
  }
  return `request failed with status ${String(status)}`;
}

/**
 * A 422 whose rejection comes from a Pydantic model validator (e.g.
 * `HorizonRequest`'s "no duplicate azimuth" rule) does not carry a plain
 * string `detail` — FastAPI's default handler always shapes it as
 * `HTTPValidationError.detail: ValidationError[]`, one entry per failing
 * field, each with its own `msg`. The settings routes deliberately raise
 * `ValueError` inside the model so the rejection names the fix (e.g.
 * "duplicate azimuth 10.0 deg…") — extracting `msg` here is what actually
 * gets that text in front of the user instead of a bare "422". Pydantic
 * prefixes a custom validator's message with `"Value error, "`; that prefix
 * is stripped as noise, not content.
 */
function describeValidationDetail(detail: unknown): string | null {
  if (!Array.isArray(detail) || detail.length === 0) return null;
  const messages: string[] = [];
  for (const entry of detail) {
    if (typeof entry !== "object" || entry === null || !("msg" in entry)) return null;
    const msg = (entry as { msg?: unknown }).msg;
    if (typeof msg !== "string") return null;
    messages.push(msg.replace(/^Value error, /, ""));
  }
  return messages.join("; ");
}

/** The `/api/...` path of a response, for the stale-server message above. */
function pathOf(response: Response): string | undefined {
  try {
    return new URL(response.url).pathname;
  } catch {
    return undefined;
  }
}

export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok) throw new ApiError(response.status, error, pathOf(response));
  return data as T;
}

/** For endpoints whose success response has no body (204, or a bare 202). */
export async function unwrapVoid(
  call: Promise<{ error?: unknown; response: Response }>,
): Promise<void> {
  const { error, response } = await call;
  if (!response.ok) throw new ApiError(response.status, error, pathOf(response));
}
