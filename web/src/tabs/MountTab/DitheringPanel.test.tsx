// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { DitheringPanel } from "./DitheringPanel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DitheringPanel", () => {
  it("loads and persists the bounded capture-loop dither policy", async () => {
    const updates: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(new URL(String(input), "http://localhost"), init);
      if (request.method === "PUT") updates.push(await request.clone().json());
      return new Response(JSON.stringify({
        enabled: request.method === "PUT",
        every_n_frames: 6,
        min_pixels: 30,
        max_pixels: 200,
      }), { status: 200, headers: { "content-type": "application/json" } });
    }));

    renderWithQueryClient(<DitheringPanel />);

    const toggle = await screen.findByRole("button", { name: "Disabled" });
    await userEvent.click(toggle);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(updates).toEqual([{
        enabled: true,
        every_n_frames: 6,
        min_pixels: 30,
        max_pixels: 200,
      }]);
    });
  });
});
