// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { HelpTab } from "./HelpTab";
import { manualTopics } from "./helpContent";

function renderHelp(initialEntry = "/help") {
  return render(<MemoryRouter initialEntries={[initialEntry]}><HelpTab /></MemoryRouter>);
}

describe("HelpTab", () => {
  it("provides guide, manual, and knowledge-base sections", () => {
    renderHelp();
    expect(screen.getByRole("tab", { name: "Guide" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Manual" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Knowledge base" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Set up Photon Commander for the first night" })).toBeInTheDocument();
  });

  it("indexes every primary screen and opens its reference", async () => {
    const user = userEvent.setup();
    renderHelp();
    await user.click(screen.getByRole("tab", { name: "Manual" }));

    for (const topic of manualTopics) {
      expect(screen.getByRole("button", { name: topic.title })).toBeInTheDocument();
    }

    await user.click(screen.getByRole("button", { name: "Live stacking" }));
    expect(screen.getByRole("heading", { name: "Live stacking" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Live stacking" })).toHaveAttribute("href", "/live-stacking");
    expect(screen.getByRole("heading", { name: "What it can do" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "How to use it" })).toBeInTheDocument();
  });

  it("searches article text and keywords and links back to the manual", async () => {
    const user = userEvent.setup();
    renderHelp();
    await user.click(screen.getByRole("tab", { name: "Knowledge base" }));
    await user.type(screen.getByRole("searchbox", { name: "Search the knowledge base" }), "privacy");

    expect(screen.getByRole("heading", { name: "YouTube privacy and stream keys" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "When plate solving fails" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("1 article");

    await user.click(screen.getByRole("button", { name: "Streaming" }));
    expect(screen.getByRole("tabpanel", { name: "Manual" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Streaming" })).toBeInTheDocument();
  });

  it("opens the external-tools guide directly from Settings", () => {
    renderHelp("/help?section=knowledge&article=external-tools");
    expect(screen.getByRole("tabpanel", { name: "Knowledge base" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Install optional tools and telescope bridges" })).toBeInTheDocument();
  });

  it("documents tailnet-private remote access and warns against Funnel", async () => {
    const user = userEvent.setup();
    renderHelp("/help?section=knowledge&article=remote-access");
    expect(screen.getByRole("heading", { name: "Private remote access with Tailscale" })).toBeInTheDocument();
    expect(screen.getByText(/sudo tailscale serve --bg 127\.0\.0\.1:7777/)).toBeInTheDocument();
    expect(screen.getByText(/Funnel is different/)).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Guide" }));
    expect(screen.getByRole("heading", { name: "Use Photon Commander securely away from home" })).toBeInTheDocument();
  });
});
