// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Shell } from "./Shell";

vi.mock("react-router-dom", () => ({ useLocation: () => ({ pathname: "/planning" }), Link: ({ to, ...props }: { to: string; children: ReactNode }) => <a href={to} {...props} /> }));

vi.mock("../skymap/SkyMapCreditsFooter", () => ({ SkyMapCreditsFooter: () => <div data-testid="map-credits" /> }));
vi.mock("../widgets/weather/WeatherWidget", () => ({ WeatherWidget: () => <span data-testid="forecast">Forecast</span> }));
vi.mock("./ConnectionsFooter", () => ({ ConnectionsFooter: () => <div data-testid="connections-footer" /> }));
vi.mock("./TabNav", () => ({ BELOW_DESKTOP_NAV_QUERY: "(max-width: 767px)", TabNav: ({ docked }: { docked?: boolean }) => <nav aria-label="Main" data-docked={docked ? "yes" : "no"} />, WorkflowSubnav: ({ compact, docked }: { compact?: boolean; docked?: boolean }) => <nav aria-label="Current workflow" data-compact={compact ? "yes" : "no"} data-docked={docked ? "yes" : "no"} />, DesktopUtilityNav: () => <nav aria-label="Settings and help">Utilities</nav> }));
vi.mock("./GlobalOperationBanner", () => ({ GlobalOperationBanner: () => null }));

afterEach(() => vi.unstubAllGlobals());

describe("Shell", () => {
  it("places credits after planning inside the scrollable main, outside persistent chrome", () => {
    render(<Shell><p>Map contents</p></Shell>);
    const main = screen.getByRole("main");
    const credits = screen.getByTestId("map-credits");
    expect(main).toContainElement(credits);
    expect(screen.getByText("Map contents").compareDocumentPosition(credits) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(main).not.toContainElement(screen.getByTestId("connections-footer"));
  });

  it("uses the compact product icon as the accessible application brand", () => {
    render(<Shell><p>Page contents</p></Shell>);

    const brand = screen.getByRole("img", { name: "Photon Commander" });
    expect(brand).toHaveAttribute("title", "Photon Commander");
    expect(brand).toHaveAttribute("src", expect.stringContaining("data:image/svg+xml"));
    expect(screen.queryByText("Photon Commander")).not.toBeInTheDocument();
  });

  it("keeps desktop Settings and Help after Forecast without a fullscreen header action", () => {
    render(<Shell><p>Page contents</p></Shell>);
    const headerText = screen.getByRole("banner").textContent ?? "";

    expect(headerText.indexOf("Forecast")).toBeLessThan(headerText.indexOf("Utilities"));
    expect(screen.queryByRole("button", { name: /fullscreen/i })).not.toBeInTheDocument();
  });

  it("keeps the mobile fixed menu viewport-relative while blurring the desktop header", () => {
    render(<Shell><p>Page contents</p></Shell>);

    const header = screen.getByRole("banner");
    expect(header).toHaveClass("relative", "z-50", "overflow-visible", "bg-surface/35", "md:backdrop-blur-sm");
    expect(header).not.toHaveClass("backdrop-blur-sm");
  });

  it("keeps secondary navigation above content and the primary mobile bar below connection status", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query === "(max-width: 767px)",
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    const { unmount } = render(<Shell><p>Page contents</p></Shell>);

    const mobileHeader = screen.getByRole("banner");
    const mobileWorkflow = screen.getByRole("navigation", { name: "Current workflow" });
    const connectionsFooter = screen.getByTestId("connections-footer");
    expect(mobileHeader).not.toContainElement(mobileWorkflow);
    expect(mobileWorkflow).toHaveAttribute("data-compact", "yes");
    expect(mobileWorkflow.compareDocumentPosition(connectionsFooter) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(mobileWorkflow).toHaveAttribute("data-docked", "yes");
    const primary = screen.getByRole("navigation", { name: "Main" });
    expect(mobileWorkflow.compareDocumentPosition(screen.getByRole("main")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(connectionsFooter.compareDocumentPosition(primary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(primary).toHaveAttribute("data-docked", "yes");
    expect(mobileHeader).not.toContainElement(primary);
    expect(mobileHeader).toContainElement(screen.getByTestId("forecast"));
    unmount();

    vi.stubGlobal("matchMedia", () => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    render(<Shell><p>Page contents</p></Shell>);
    const desktopWorkflow = screen.getByRole("navigation", { name: "Current workflow" });
    expect(screen.getByRole("banner")).not.toContainElement(desktopWorkflow);
    expect(desktopWorkflow).toHaveAttribute("data-compact", "no");
  });
});

it("keeps the report bug icon accessible and large enough to tap", () => {
  render(<Shell><p>Page contents</p></Shell>);
  const link = screen.getByRole("link", { name: "Report a bug" });
  expect(link).toHaveAttribute("href", "/help#bug-report");
  expect(link).toHaveAttribute("title", "Report a bug");
  expect(link).toHaveClass("min-h-[44px]", "min-w-[44px]");
  expect(link.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  expect(link.textContent).toBe("");
});
