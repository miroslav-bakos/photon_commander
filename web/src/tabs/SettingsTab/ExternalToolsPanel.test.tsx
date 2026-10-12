// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { renderWithQueryClient } from "../../test/queryClient";
import { ExternalToolsPanel } from "./ExternalToolsPanel";

vi.mock("../../api/settings", () => ({
  useExternalToolsQuery: () => ({
    isPending: false,
    isError: false,
    data: {
      platform: "linux",
      architecture: "aarch64",
      tools: [{
        id: "dwarf-alpaca",
        name: "dwarfAlp",
        purpose: "Experimental Alpaca bridge for DWARFLAB telescopes",
        status: "not_installed",
        detail: "Optional unofficial bridge; hardware support varies by model.",
        version: null,
        detected_path: null,
        license: "GPL-3.0-only (separate process)",
        distribution: "separate_process",
        source_url: "https://github.com/acocalypso/dwarfAlp",
        guide_article_id: "external-tools",
        supported_platforms: ["Linux", "Raspberry Pi OS"],
      }],
    },
  }),
}));

describe("ExternalToolsPanel", () => {
  it("shows detection, licensing, mobile-sized actions, and setup guidance", () => {
    renderWithQueryClient(<MemoryRouter><ExternalToolsPanel /></MemoryRouter>);
    expect(screen.getByRole("heading", { name: "dwarfAlp" })).toBeInTheDocument();
    expect(screen.getByText("Not installed")).toBeInTheDocument();
    expect(screen.getByText(/GPL-3.0-only/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Setup guide" })).toHaveAttribute(
      "href", "/help?section=knowledge&article=external-tools",
    );
    expect(screen.getByRole("link", { name: "Upstream source" })).toHaveAttribute(
      "href", "https://github.com/acocalypso/dwarfAlp",
    );
  });
});
