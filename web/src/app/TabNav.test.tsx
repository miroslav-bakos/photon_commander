// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BELOW_DESKTOP_NAV_QUERY, DesktopUtilityNav, TabNav, WorkflowSubnav } from "./TabNav";

function useCompactViewport() {
  const matchMedia = vi.fn((query: string) => ({
    matches: query === BELOW_DESKTOP_NAV_QUERY,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  vi.stubGlobal("matchMedia", matchMedia);
  return matchMedia;
}

afterEach(() => vi.unstubAllGlobals());

describe("TabNav", () => {
  it("keeps phone controls in the shell dock without viewport-fixed offsets or a spacer", () => {
    useCompactViewport();
    render(<MemoryRouter initialEntries={["/live"]}><WorkflowSubnav compact docked /><TabNav docked /></MemoryRouter>);
    const primary = screen.getByRole("navigation", { name: "Main" });
    const workflow = screen.getByRole("navigation", { name: "Current workflow" });
    expect(primary).toHaveClass("relative", "shrink-0");
    expect(workflow).toHaveClass("relative", "shrink-0");
    expect(primary).not.toHaveClass("fixed");
    expect(workflow).not.toHaveClass("fixed");
    expect(document.querySelector("[data-mobile-workflow-spacer]")).toBeNull();
  });

  it("uses the agreed concise desktop labels and routes", () => {
    render(<MemoryRouter initialEntries={["/planning"]}><TabNav /></MemoryRouter>);
    const nav = screen.getByRole("navigation", { name: "Main" });

    expect(within(nav).getByRole("link", { name: "Observe" })).toHaveAttribute("href", "/dashboard");
    expect(within(nav).getByRole("link", { name: "Plan" })).toHaveAttribute("href", "/planning");
    expect(within(nav).getByRole("link", { name: "Telescope" })).toHaveAttribute("href", "/mount");
    expect(within(nav).getByRole("link", { name: "Library" })).toHaveAttribute("href", "/archive");
    expect(within(nav).getByRole("link", { name: "System" })).toHaveAttribute("href", "/system");
    expect(within(nav).queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Help" })).not.toBeInTheDocument();
    expect(nav).toHaveClass("flex-nowrap");
    expect(within(nav).getByRole("link", { name: "Plan" })).toHaveAttribute("title", "Plan");
    expect(within(nav).getByRole("link", { name: "Plan" }).querySelector("span")).not.toHaveClass("sr-only");
  });

  it("renders Settings and Help as icon-only desktop utility actions", () => {
    render(<MemoryRouter initialEntries={["/planning"]}><DesktopUtilityNav /></MemoryRouter>);
    const nav = screen.getByRole("navigation", { name: "Settings and help" });
    const settings = within(nav).getByRole("link", { name: "Settings" });
    const help = within(nav).getByRole("link", { name: "Help" });

    expect(settings).toHaveAttribute("title", "Settings");
    expect(settings.querySelector(".lucide-settings")).toBeInTheDocument();
    expect(help).toHaveAttribute("title", "Help");
    expect(settings.querySelector("span")).toHaveClass("sr-only");
    expect(help.querySelector("span")).toHaveClass("sr-only");
  });

  it("keeps all five phone destinations in a stable equal-width row without scrolling", () => {
    useCompactViewport();
    render(<MemoryRouter initialEntries={["/planning"]}><TabNav /></MemoryRouter>);
    const nav = screen.getByRole("navigation", { name: "Main" });

    expect(nav).toHaveClass("ssc-mobile-tabbar", "grid", "grid-cols-5", "h-[3.5rem]", "inset-x-2");
    expect(nav.className).toContain("bottom-[env(safe-area-inset-bottom)]");
    expect(nav).not.toHaveAttribute("data-scrollable");
    expect(nav.querySelector("[data-mobile-nav-scroll]")).toBeNull();
    expect(nav.querySelector("[data-scroll-indicator]")).toBeNull();
    expect(within(nav).queryByRole("button")).not.toBeInTheDocument();
    const links = within(nav).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/dashboard", "/planning", "/mount", "/archive", "/system",
    ]);
    for (const link of links) {
      expect(link).toHaveClass("ssc-mobile-tab-item", "min-w-0", "min-h-11");
      expect(link).not.toHaveClass("min-w-20", "shrink-0");
      expect(link.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
      expect(link.querySelector("span")).toHaveClass("max-w-full", "truncate");
    }
  });

  it("keeps a deep-linked destination active in the single mobile row", () => {
    useCompactViewport();
    render(<MemoryRouter initialEntries={["/streaming"]}><><TabNav /><WorkflowSubnav /></></MemoryRouter>);
    const nav = screen.getByRole("navigation", { name: "Main" });
    const subnav = screen.getByRole("navigation", { name: "Current workflow" });

    expect(within(nav).getByRole("link", { name: "Observe" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Telescope" })).toHaveAttribute("aria-current", "page");
    expect(within(subnav).getByRole("link", { name: "Stream" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getAllByRole("link")).toHaveLength(5);
  });

  it.each([
    ["/scheduling", "Plan"],
    ["/polar-align", "Telescope"],
    ["/live", "Telescope"],
    ["/live-stacking", "Telescope"],
    ["/streaming", "Telescope"],
    ["/settings", "System"],
    ["/help", "System"],
  ])("keeps the owning mobile destination selected for %s", (route, label) => {
    useCompactViewport();
    render(<MemoryRouter initialEntries={[route]}><TabNav /></MemoryRouter>);
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
    expect(nav.querySelectorAll('[aria-current="page"]')).toHaveLength(1);
    expect(within(nav).getAllByRole("link")).toHaveLength(5);
  });

  it("scrolls the compact workflow row to reveal a newly active tab", () => {
    render(<MemoryRouter initialEntries={["/mount"]}><WorkflowSubnav compact /></MemoryRouter>);
    const nav = screen.getByRole("navigation", { name: "Current workflow" });
    const stream = within(nav).getByRole("link", { name: "Stream" });
    Object.defineProperties(nav, {
      clientWidth: { configurable: true, value: 120 },
      scrollLeft: { configurable: true, writable: true, value: 0 },
    });
    Object.defineProperties(stream, {
      offsetLeft: { configurable: true, value: 200 },
      offsetWidth: { configurable: true, value: 60 },
    });

    fireEvent.click(stream);

    expect(stream).toHaveAttribute("aria-current", "page");
    expect(nav.scrollLeft).toBe(140);
    expect(nav).toHaveClass("fixed", "inset-x-0", "h-[2.75rem]", "min-w-0", "overflow-x-auto", "border-t");
    expect(nav.className).toContain("bottom-[calc(5.5rem+env(safe-area-inset-bottom))]");
    expect(nav).not.toHaveClass("flex-1", "border-b");
    expect(document.querySelector("[data-mobile-workflow-spacer]")).toHaveClass("h-[2.75rem]", "shrink-0");
  });

  it("uses the bottom navigation only below the tablet breakpoint", () => {
    const matchMedia = useCompactViewport();
    render(<MemoryRouter initialEntries={["/planning"]}><TabNav /></MemoryRouter>);

    expect(matchMedia).toHaveBeenCalledWith("(max-width: 767px)");
    expect(screen.getByRole("navigation", { name: "Main" })).toHaveClass("ssc-mobile-tabbar");
  });

  it("keeps Live stack in the Telescope workflow", () => {
    render(<MemoryRouter initialEntries={["/live-stacking"]}><WorkflowSubnav /></MemoryRouter>);
    const link = screen.getByRole("link", { name: "Live stack" });

    expect(link).toHaveAttribute("href", "/live-stacking");
    expect(link).toHaveAttribute("aria-current", "page");
  });

  it("names the equipment workflow and its device destination distinctly", () => {
    render(<MemoryRouter initialEntries={["/mount"]}><><TabNav /><WorkflowSubnav /></></MemoryRouter>);

    expect(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Telescope" })).toHaveAttribute("aria-current", "page");
    expect(within(screen.getByRole("navigation", { name: "Current workflow" })).getByRole("link", { name: "Control" })).toHaveAttribute("href", "/mount");
    expect(within(screen.getByRole("navigation", { name: "Current workflow" })).getByRole("link", { name: "Polar align" })).toHaveAttribute("href", "/polar-align");
  });

  it("keeps every screen one tap away inside the active workflow", () => {
    render(<MemoryRouter initialEntries={["/live"]}><WorkflowSubnav /></MemoryRouter>);
    const nav = screen.getByRole("navigation", { name: "Current workflow" });
    expect(within(nav).getAllByRole("link").map((link) => link.textContent)).toEqual(["Control", "Polar align", "Capture", "Live stack", "Stream"]);
    expect(within(nav).getByRole("link", { name: "Capture" })).toHaveAttribute("href", "/live");
    expect(within(nav).getByRole("link", { name: "Live stack" })).toHaveAttribute("href", "/live-stacking");
    expect(within(nav).getByRole("link", { name: "Stream" })).toHaveAttribute("href", "/streaming");
  });
});

it("calls the execution destination Execute and keeps its route", () => {
  render(<MemoryRouter initialEntries={["/scheduling"]}><WorkflowSubnav /></MemoryRouter>);
  expect(screen.getByRole("link", { name: "Execute" })).toHaveAttribute("href", "/scheduling");
  expect(screen.getByRole("link", { name: "Execute" })).toHaveAttribute("aria-current", "page");
});

it("leaves Observe without telescope subnavigation", () => {
  render(<MemoryRouter initialEntries={["/dashboard"]}><TabNav /><WorkflowSubnav /></MemoryRouter>);
  expect(screen.queryByRole("navigation", { name: "Current workflow" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Observe" })).toHaveAttribute("aria-current", "page");
});
