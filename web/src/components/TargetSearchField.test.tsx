// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { TargetResolutionResponse } from "../api/astro";
import { renderWithQueryClient } from "../test/queryClient";
import { TargetSearchField, type TargetSearchSelection } from "./TargetSearchField";

const M31: TargetResolutionResponse = {
  name: "M31", designations: ["M31", "NGC 224"], common_name: "Andromeda Galaxy",
  object_type: "galaxy", ra_j2000_h: 0.7123, dec_j2000_deg: 41.269,
  magnitude: 3.4, size_major_arcmin: 189.1, size_minor_arcmin: 61.7,
  position_angle_deg: 35, constellation: "And", source: "Messier", sources: ["Messier catalogue"],
};

function stubAstro(): void {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const path = new URL(input instanceof Request ? input.url : String(input), "http://localhost").pathname;
    const body = path.endsWith("/target-suggestions")
      ? { suggestions: [M31] }
      : path.endsWith("/bodies")
        ? { bodies: [] }
        : M31;
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));
  }));
}

describe("TargetSearchField", () => {
  it("selects a suggestion with a phone-sized touch target", async () => {
    stubAstro();
    const user = userEvent.setup();
    const onSelect = vi.fn<(selection: TargetSearchSelection) => void>();
    renderWithQueryClient(<TargetSearchField instant={new Date("2026-09-14T00:00:00Z")} onSelect={onSelect} />);

    await user.type(screen.getByRole("combobox", { name: "Search targets" }), "Andr");
    const option = await screen.findByRole("option", { name: /Andromeda Galaxy/i });
    expect(option).toHaveClass("min-h-[44px]");
    const listbox = screen.getByRole("listbox", { name: "Target suggestions" });
    expect(listbox.parentElement?.tagName).toBe("FORM");
    expect(listbox).toHaveClass("inset-x-0");
    expect(option.querySelectorAll(".break-words")).toHaveLength(2);
    expect(option.querySelector(".truncate")).toBeNull();
    await user.pointer({ target: option, keys: "[TouchA]" });
    expect(onSelect).toHaveBeenCalledWith({ kind: "catalog", result: M31 });
  });

  it("re-resolves unchanged text", async () => {
    stubAstro();
    const user = userEvent.setup();
    const onSelect = vi.fn<(selection: TargetSearchSelection) => void>();
    renderWithQueryClient(<TargetSearchField instant={null} onSelect={onSelect} />);
    await user.type(screen.getByLabelText("Search targets"), "M31");
    await user.click(screen.getByRole("button", { name: "Resolve" }));
    await user.click(screen.getByRole("button", { name: "Resolve" }));
    expect(onSelect).toHaveBeenCalledTimes(2);
  });
});
