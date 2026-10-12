// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { fireEvent, render, screen } from "@testing-library/react";
import { Link, MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { RouteScrollReset } from "./RouteScrollReset";

afterEach(() => vi.restoreAllMocks());

function Navigation() {
  const navigate = useNavigate();
  return <>
    <Link to="/help">Help</Link>
    <Link to="/help?section=manual">Manual</Link>
    <button onClick={() => { void navigate(-1); }}>Back</button>
  </>;
}

it("resets document, main, and persistent pane scroll on route changes and Back", () => {
  const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
  const { container, unmount } = render(<MemoryRouter initialEntries={["/planning"]}>
    <Navigation />
    <main id="main"><div data-panes><div data-testid="pane" /></div></main>
    <RouteScrollReset />
  </MemoryRouter>);
  const main = container.querySelector("main");
  if (main === null) throw new Error("Missing main scroll container");
  const pane = screen.getByTestId("pane");
  expect(window.history.scrollRestoration).toBe("manual");
  for (const name of ["Help", "Back"]) {
    main.scrollTop = 700;
    pane.scrollTop = 500;
    scroll.mockClear();
    fireEvent.click(screen.getByRole(name === "Back" ? "button" : "link", { name }));
    expect(scroll).toHaveBeenCalledWith({ top: 0, left: 0, behavior: "instant" });
    expect(main.scrollTop).toBe(0);
    expect(pane.scrollTop).toBe(0);
  }
  unmount();
});

it("does not reset scroll when changing a query on the same screen", () => {
  const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
  render(<MemoryRouter initialEntries={["/help"]}><Navigation /><RouteScrollReset /></MemoryRouter>);
  scroll.mockClear();
  fireEvent.click(screen.getByRole("link", { name: "Manual" }));
  expect(scroll).not.toHaveBeenCalled();
});
