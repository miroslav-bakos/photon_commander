// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pins the brief's own verification requirement 3 — the toggle reflects the
 * server's own value rather than assuming it, off-by-default included — and
 * that the credit line comes from the live registry (`GET
 * /api/skymap/surveys`'s `cutout_sources`), never a literal in this panel
 * (`THIRD_PARTY_NOTICES.md` requirement 4). Field/path names
 * (`cutout_autofetch_enabled`, `/api/settings/skymap-autofetch`) are read
 * from `ssc/api/schemas.py`/`ssc/api/routes/settings.py`'s concurrent,
 * uncommitted implementation, not guessed — see `SkymapAutofetchPanel.tsx`'s
 * own header for why this panel is not called "DSS2".
 */
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CutoutSourceModel, SurveysResponse } from "../../api/skymap";
import { renderWithQueryClient } from "../../test/queryClient";
import { SkymapAutofetchPanel } from "./SkymapAutofetchPanel";

afterEach(cleanup);

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function unwiseSource(overrides: Partial<CutoutSourceModel> = {}): CutoutSourceModel {
  return {
    id: "unwise",
    label: "unWISE",
    credit: {
      label: "unWISE (Lang, Schlegel, Meisner, Dawson) — CC BY 4.0",
      url: "https://unwise.me/",
    },
    bands: ["w1", "w2"],
    default_bands: ["w1", "w2"],
    optional: false,
    max_pixels: 3000,
    ...overrides,
  };
}

function surveysResponse(cutoutSources: CutoutSourceModel[]): SurveysResponse {
  return { surveys: [], cache_bytes: 0, cache_max_bytes: 0, cutout_sources: cutoutSources };
}

/**
 * Routes by method + path. Two calling conventions land here: the
 * generated `api` client (`useSkymapSurveysQuery`) passes a `Request`
 * object; `SkymapAutofetchPanel`'s own hand-written `rawCall`
 * (`api/settings.ts` — no generated route for this endpoint yet, see that
 * file's own header) passes a plain path string and a separate `init`.
 * Both are normalised to one shape here rather than assuming either.
 */
function stubFetch(options: { enabled?: boolean } = {}) {
  const puts: unknown[] = [];
  let current = options.enabled ?? false;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (input: string | Request, init?: RequestInit) => {
      const isRequest = typeof input !== "string";
      const url = isRequest ? input.url : input;
      const method = (isRequest ? input.method : init?.method) ?? "GET";
      const { pathname } = new URL(url, "http://localhost");
      const readBody = async (): Promise<unknown> =>
        isRequest
          ? ((await input.clone().json()) as unknown)
          : (JSON.parse(typeof init?.body === "string" ? init.body : "{}") as unknown);

      if (method === "GET" && pathname === "/api/settings/skymap-autofetch") {
        return json({ cutout_autofetch_enabled: current }, 200);
      }
      if (method === "PUT" && pathname === "/api/settings/skymap-autofetch") {
        const body = (await readBody()) as { cutout_autofetch_enabled: boolean };
        puts.push(body);
        current = body.cutout_autofetch_enabled;
        return json({ cutout_autofetch_enabled: current }, 200);
      }
      if (method === "GET" && pathname === "/api/skymap/surveys") {
        return json(surveysResponse([unwiseSource()]), 200);
      }
      return json({ detail: `unexpected ${method} ${pathname}` }, 500);
    }),
  );
  return puts;
}

describe("SkymapAutofetchPanel", () => {
  it("shows the toggle unchecked when the server reports it off, without assuming a default", async () => {
    stubFetch({ enabled: false });
    renderWithQueryClient(<SkymapAutofetchPanel />);
    const toggle = await screen.findByRole("checkbox");
    expect(toggle).not.toBeChecked();
  });

  it("shows the toggle checked when the server reports it on", async () => {
    stubFetch({ enabled: true });
    renderWithQueryClient(<SkymapAutofetchPanel />);
    const toggle = await screen.findByRole("checkbox");
    expect(toggle).toBeChecked();
  });

  it("sends cutout_autofetch_enabled: true when switched on", async () => {
    const puts = stubFetch({ enabled: false });
    renderWithQueryClient(<SkymapAutofetchPanel />);
    const toggle = await screen.findByRole("checkbox");
    await userEvent.click(toggle);
    await waitFor(() => {
      expect(puts).toEqual([{ cutout_autofetch_enabled: true }]);
    });
  });

  it("shows the live credit line from the registry, not a literal", async () => {
    stubFetch({ enabled: false });
    renderWithQueryClient(<SkymapAutofetchPanel />);
    const link = await screen.findByRole("link", { name: /unWISE/ });
    expect(link).toHaveAttribute("href", "https://unwise.me/");
  });
});
