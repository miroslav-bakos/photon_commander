// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { ObjectInfoResponse } from "../api/astro";
import { resolveObjectInfoPanelState } from "./objectInfoPanelState";

function response(overrides: Partial<ObjectInfoResponse> = {}): ObjectInfoResponse {
  return {
    designation: "M31",
    configured: false,
    found: false,
    common_name: null,
    title: null,
    article_url: null,
    extract: null,
    wikidata_qid: null,
    instance_of: [],
    discovery_date: null,
    image: null,
    attributions: [],
    message: null,
    cached: false,
    fetched_at: null,
    ...overrides,
  };
}

describe("resolveObjectInfoPanelState", () => {
  it("reports not-configured when the feature itself is off, using the server's own message", () => {
    const state = resolveObjectInfoPanelState(
      response({ configured: false, found: false, message: "no catalogue source configured" }),
    );
    expect(state).toEqual({ kind: "not-configured", message: "no catalogue source configured" });
  });

  it("falls back to its own wording if a not-configured response somehow carries no message", () => {
    const state = resolveObjectInfoPanelState(response({ configured: false, found: false, message: null }));
    expect(state.kind).toBe("not-configured");
  });

  it("reports not-found (never an error) when configured but nothing was found", () => {
    const state = resolveObjectInfoPanelState(
      response({ designation: "NGC 1", configured: true, found: false, message: "no Wikipedia article found for NGC 1" }),
    );
    expect(state).toEqual({ kind: "not-found", message: "no Wikipedia article found for NGC 1" });
  });

  it("falls back to naming the designation if a not-found response somehow carries no message", () => {
    const state = resolveObjectInfoPanelState(
      response({ designation: "NGC 1", configured: true, found: false, message: null }),
    );
    expect(state).toEqual({ kind: "not-found", message: "no article found for NGC 1" });
  });

  it("reports found with the response attached verbatim — never re-derives content itself", () => {
    const full = response({
      configured: true,
      found: true,
      title: "Andromeda Galaxy",
      common_name: "Andromeda Galaxy",
      extract: "The Andromeda Galaxy is a barred spiral galaxy...",
      attributions: [{ text: "Wikipedia contributors", url: "https://en.wikipedia.org/wiki/Andromeda_Galaxy" }],
    });
    const state = resolveObjectInfoPanelState(full);
    expect(state).toEqual({ kind: "found", response: full });
  });
});
