// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { Link, MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { useLayoutRoute } from "../layout/LayoutContext";
import { PersistentPlanningTab } from "./PersistentPlanningTab";

const mounted = vi.hoisted(() => vi.fn());
const unmounted = vi.hoisted(() => vi.fn());

vi.mock("../tabs/PlanningTab/PlanningTab", () => ({
  PlanningTab: () => {
    const route = useLayoutRoute();
    useEffect(() => {
      mounted();
      return unmounted;
    }, []);
    return <><span data-testid="layout-route">{route.tab}</span><iframe title="Sky map" src="/skymap-vector.html" /></>;
  },
}));

function Navigation() {
  return <>
    <Link to="/planning">Plan</Link>
    <Link to="/scheduling">Schedule</Link>
  </>;
}

afterEach(() => {
  mounted.mockClear();
  unmounted.mockClear();
  vi.restoreAllMocks();
});

it("keeps the same Planning iframe mounted across route switches", () => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 720, height: 480 } as DOMRect);
  render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <Navigation />
      <PersistentPlanningTab />
    </MemoryRouter>,
  );
  expect(screen.queryByTitle("Sky map")).toBeNull();

  fireEvent.click(screen.getByRole("link", { name: "Plan" }));
  const frame = screen.getByTitle("Sky map");
  expect(mounted).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("link", { name: "Schedule" }));
  expect(screen.getByTitle("Sky map")).toBe(frame);
  expect(frame.parentElement).toHaveAttribute("aria-hidden", "true");
  expect(frame.parentElement).toHaveStyle({ width: "720px", height: "480px" });
  expect(frame.parentElement?.parentElement).toHaveClass("h-0", "w-0", "overflow-hidden");
  expect(screen.getByTestId("layout-route")).toHaveTextContent("/planning");
  expect(unmounted).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("link", { name: "Plan" }));
  expect(screen.getByTitle("Sky map")).toBe(frame);
  expect(mounted).toHaveBeenCalledTimes(1);
  expect(frame.parentElement?.parentElement).not.toHaveClass("h-0");
});
