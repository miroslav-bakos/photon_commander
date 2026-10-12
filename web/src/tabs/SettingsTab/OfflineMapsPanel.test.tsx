// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useConnectionStore } from "../../store/connectionStore";
import { renderWithQueryClient } from "../../test/queryClient";
import { OfflineMapsPanel } from "./OfflineMapsPanel";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const registry = {
  surveys: [
    {
      id: "legacy-surveys-dr11",
      label: "Legacy Surveys DR11",
      kind: "optical",
      scheme: "mercator",
      frame: "equatorial",
      tile_format: "jpg",
      max_order: 16,
      credit: { label: "Legacy Surveys", url: "https://www.legacysurvey.org/" },
      upstream_host: "www.legacysurvey.org",
      bundle_allowed: true,
      redistributable_pack_url: null,
      blend_only: false,
      bundled: false,
      enabled: true,
      reason: null,
      production_cleared: true,
      rights_notice: null,
      tile_url_template: "/tiles/{level}/{x}/{y}.jpg",
      properties_url: null,
      cached_tiles: 2,
      cached_bytes: 2048,
      missing_tiles: 0,
      pack_complete_order: null,
    },
  ],
  cache_bytes: 2048,
  cache_max_bytes: 1024 * 1024,
  cutout_sources: [],
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useConnectionStore.getState().reset();
});

describe("OfflineMapsPanel", () => {
  it("offers cache cleanup without any batch download controls", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(registry)));
    renderWithQueryClient(<OfflineMapsPanel />);

    expect(await screen.findByText("Map cache")).toBeInTheDocument();
    expect(screen.getByText(/does not download whole-sky, plan, or catalogue batches/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /download|prefetch|restart/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear" })).toBeInTheDocument();
  });

  it("requires confirmation before clearing a survey cache", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(registry))
      .mockResolvedValueOnce(response({ survey: "legacy-surveys-dr11", freed_bytes: 2048 }));
    vi.stubGlobal("fetch", fetch);
    renderWithQueryClient(<OfflineMapsPanel />);

    fireEvent.click(await screen.findByRole("button", { name: "Clear" }));
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    const confirmButton = screen.getAllByRole("button", { name: "Clear" }).at(0);
    if (confirmButton === undefined) throw new Error("Clear confirmation button is missing");
    fireEvent.click(confirmButton);

    await waitFor(() => {
      const request = fetch.mock.calls.at(-1)?.[0] as unknown;
      expect(request).toBeInstanceOf(Request);
      if (!(request instanceof Request)) throw new Error("Expected fetch to receive a Request");
      expect(request.url).toContain("/api/skymap/cache/legacy-surveys-dr11");
      expect(request.method).toBe("DELETE");
    });
  });
});
