// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Card, CardTitle } from "../components/Card";
import { PaneLayout } from "../components/PaneLayout";
import { LayoutRouteProvider } from "./LayoutContext";
import { useLayoutStore, validateSavedLayouts } from "./layoutStore";

function Board() {
  return (
    <MemoryRouter initialEntries={["/capture"]}>
      <LayoutRouteProvider>
        <PaneLayout
          fill
          primary={<div className="space-y-3"><Card><CardTitle>Alpha</CardTitle><p>Alpha body</p></Card><Card><CardTitle>Beta</CardTitle><p>Beta body</p></Card></div>}
          secondary={<Card><CardTitle>Preview</CardTitle><p>Preview body</p></Card>}
          detail={<><Card><CardTitle>Timeline</CardTitle><p>Timeline body</p></Card><p>Owned notice</p><Card><CardTitle>Options</CardTitle><p>Options body</p></Card></>}
        />
      </LayoutRouteProvider>
    </MemoryRouter>
  );
}

function PlanningLikeBoard() {
  return (
    <MemoryRouter initialEntries={["/planning"]}>
      <LayoutRouteProvider>
        <PaneLayout
          primary={
            <div className="flex flex-col gap-4">
              <Card title="Target"><p>Target body</p></Card>
              <div className="contents"><Card title="Night summary"><p>Night body</p></Card></div>
            </div>
          }
          secondary={<Card title="Sky map"><p>Map body</p></Card>}
          detail={<Card title="Framing"><p>Framing body</p></Card>}
        />
      </LayoutRouteProvider>
    </MemoryRouter>
  );
}

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(document, "elementFromPoint");
  useLayoutStore.setState({ unlocked: false, saved: { version: 1, tabs: {} } });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("customizable layouts", () => {
  it("keeps card collapse and expand controls available while the layout is locked", async () => {
    render(<Board />);
    expect(screen.getByRole("heading", { name: "Alpha" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Collapse Alpha" }));
    expect(screen.queryByText("Alpha body")).not.toBeInTheDocument();
    expect(useLayoutStore.getState().saved.tabs["desktop:/capture"]?.cards.alpha?.collapsed).toBe(true);
    const collapsedButton = screen.getByRole("button", { name: /Alpha/ });
    expect(collapsedButton.closest("[data-layout-card-id]")).toHaveClass("h-auto", "min-h-0", "self-start");

    fireEvent.click(screen.getByRole("button", { name: /Alpha/ }));
    expect(await screen.findByText("Alpha body")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Alpha" })).toBeInTheDocument();
  });

  it("is locked by default and exposes arrange controls only when unlocked", async () => {
    render(<Board />);
    expect(screen.queryByRole("button", { name: "Move Alpha down" })).not.toBeInTheDocument();
    useLayoutStore.getState().setUnlocked(true);
    expect(await screen.findByRole("button", { name: "Move Alpha down" })).toBeInTheDocument();
  });

  it("resizes only an opted-in card while editing and persists its height", async () => {
    const { rerender } = render(
      <MemoryRouter initialEntries={["/planning"]}>
        <LayoutRouteProvider>
          <PaneLayout secondary={<Card title="Night timeline" collapseKey="planning.timeline" resizableHeight><p>Chart</p></Card>} />
        </LayoutRouteProvider>
      </MemoryRouter>,
    );
    expect(screen.queryByRole("button", { name: /resize night timeline height/i })).not.toBeInTheDocument();
    useLayoutStore.getState().setUnlocked(true);
    const handle = await screen.findByRole("button", { name: /resize night timeline height/i });
    fireEvent.keyDown(handle, { key: "ArrowDown" });
    expect(useLayoutStore.getState().saved.tabs["desktop:/planning"]?.cards["planning.timeline"]?.height).toBe(320);
    expect(screen.getByText("Chart").closest("[data-layout-card-id]")).toHaveStyle({ height: "320px" });
    expect(validateSavedLayouts(useLayoutStore.getState().saved)).toEqual(useLayoutStore.getState().saved);
    useLayoutStore.getState().setUnlocked(false);
    await waitFor(() => { expect(screen.queryByRole("button", { name: /resize night timeline height/i })).not.toBeInTheDocument(); });
    expect(screen.getByText("Chart").closest("[data-layout-card-id]")).toHaveStyle({ height: "320px" });
    rerender(
      <MemoryRouter initialEntries={["/planning"]}>
        <LayoutRouteProvider><PaneLayout secondary={<Card title="Night timeline" collapseKey="planning.timeline" resizableHeight><p>Chart</p></Card>} /></LayoutRouteProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText("Chart").closest("[data-layout-card-id]")).toHaveStyle({ height: "320px" });
  });

  it("moves cards among desktop panels and persists their destination", async () => {
    useLayoutStore.getState().setUnlocked(true);
    render(<Board />);
    fireEvent.click(await screen.findByRole("button", { name: "Move Alpha to detail panel" }));
    await waitFor(() => {
      expect(screen.getByText("Alpha body").closest("[data-layout-card-id]")).toHaveAttribute("data-layout-panel", "detail");
    });
    expect(useLayoutStore.getState().saved.tabs["desktop:/capture"]?.cards.alpha?.panel).toBe("detail");
  });

  it("supports button-based ordering for touch screens and saves collapse state", async () => {
    useLayoutStore.getState().setUnlocked(true);
    render(<Board />);
    fireEvent.click(await screen.findByRole("button", { name: "Move Alpha down" }));
    expect(useLayoutStore.getState().saved.tabs["desktop:/capture"]?.cards.alpha?.order).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Collapse Alpha" }));
    expect(screen.queryByText("Alpha body")).not.toBeInTheDocument();
    expect(useLayoutStore.getState().saved.tabs["desktop:/capture"]?.cards.alpha?.collapsed).toBe(true);
  });

  it("reorders Night summary through Planning-style nested wrappers", async () => {
    useLayoutStore.getState().setUnlocked(true);
    render(<PlanningLikeBoard />);

    fireEvent.click(await screen.findByRole("button", { name: "Move Night summary up" }));

    expect(useLayoutStore.getState().saved.tabs["desktop:/planning"]?.cards["night-summary"]?.order).toBe(0);
    expect(useLayoutStore.getState().saved.tabs["desktop:/planning"]?.cards.target?.order).toBe(1);
    expect(screen.getByText("Night body").closest("[data-layout-card-id]")?.parentElement).toHaveAttribute("data-layout-panel-target", "primary");
  });

  it("pointer-drags a nested Planning card before another card", async () => {
    useLayoutStore.getState().setUnlocked(true);
    render(<PlanningLikeBoard />);

    const nightCard = (await screen.findByText("Night body")).closest("[data-layout-card-id]");
    expect(nightCard).not.toBeNull();
    const targetCard = screen.getByText("Target body").closest("[data-layout-card-id]");
    expect(targetCard).not.toBeNull();
    Object.defineProperty(document, "elementFromPoint", {
      configurable: true,
      value: vi.fn(() => targetCard as HTMLElement),
    });
    const handle = within(nightCard as HTMLElement).getByTitle("Drag this card");
    fireEvent.pointerDown(handle, { pointerId: 7, button: 0, clientX: 200, clientY: 300 });
    fireEvent.pointerUp(handle, { pointerId: 7, button: 0, clientX: 200, clientY: 200 });

    expect(useLayoutStore.getState().saved.tabs["desktop:/planning"]?.cards["night-summary"]?.order).toBe(0);
    expect(useLayoutStore.getState().saved.tabs["desktop:/planning"]?.cards.target?.order).toBe(1);
  });

  it("gives a titleless skymap card edit chrome and controls", async () => {
    useLayoutStore.getState().setUnlocked(true);
    render(
      <MemoryRouter initialEntries={["/planning"]}>
        <LayoutRouteProvider>
          <PaneLayout secondary={<Card layoutId="sky-map" layoutLabel="Sky map" padded={false}><p>Map body</p></Card>} />
        </LayoutRouteProvider>
      </MemoryRouter>,
    );

    const mapCard = screen.getByText("Map body").closest("[data-layout-card-id]");
    expect(mapCard).toHaveAttribute("data-layout-card-id", "sky-map");
    expect(mapCard).toHaveClass("ring-1", "ring-accent/60");
    expect(await screen.findByTitle("Drag this card")).toHaveTextContent("Sky map");
    expect(screen.getByRole("button", { name: "Move Sky map up" })).toBeInTheDocument();
  });

  it("continues touch ordering across panel boundaries", async () => {
    useLayoutStore.getState().setUnlocked(true);
    render(<Board />);
    fireEvent.click(await screen.findByRole("button", { name: "Move Beta down" }));
    expect(useLayoutStore.getState().saved.tabs["desktop:/capture"]?.cards.beta?.panel).toBe("secondary");
    expect(useLayoutStore.getState().saved.tabs["desktop:/capture"]?.cards.beta?.order).toBe(0);
  });

  it("stops desktop arrow ordering at the selected panel boundary", async () => {
    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: query === "(min-width: 640px)", addEventListener: () => undefined, removeEventListener: () => undefined })));
    useLayoutStore.getState().setUnlocked(true);
    render(<Board />);
    fireEvent.click(await screen.findByRole("button", { name: "Move Timeline up" }));
    expect(useLayoutStore.getState().saved.tabs["desktop:/capture"]?.cards.timeline).toBeUndefined();
    expect(screen.getByText("Timeline body").closest("[data-layout-card-id]")).toHaveAttribute("data-layout-panel", "detail");
  });

  it("preserves source order around non-card panel content", () => {
    render(<Board />);
    const panelText = screen.getByText("Owned notice").parentElement?.textContent ?? "";
    expect(panelText.indexOf("Timeline body")).toBeLessThan(panelText.indexOf("Owned notice"));
  });

  it("persists collapsed side panels per screen", () => {
    render(<Board />);
    fireEvent.click(screen.getByRole("button", { name: "Hide the left panel" }));
    expect(useLayoutStore.getState().saved.tabs["desktop:/capture"]?.primaryCollapsed).toBe(true);
  });

  it("places directional pane toggles at the measured inter-panel gutter centers", async () => {
    const rect = (left: number, right: number): DOMRect => ({
      x: left, y: 80, left, right, top: 80, bottom: 700, width: right - left, height: 620,
      toJSON: () => ({}),
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.dataset.panes === "1") return rect(16, 1264);
      if (this.dataset.paneCell === "primary") return rect(16, 288);
      if (this.dataset.paneCell === "secondary") return rect(304, 894);
      if (this.dataset.paneCell === "detail") return rect(910, 1264);
      return rect(0, 0);
    });
    const { container } = render(<Board />);
    const panes = container.querySelector<HTMLElement>("[data-panes]");
    const left = screen.getByRole("button", { name: "Hide the left panel" });
    const right = screen.getByRole("button", { name: "Hide the right panel" });

    expect(panes?.style.getPropertyValue("--ssc-primary-track")).toBe("272px");
    expect(panes?.style.getPropertyValue("--ssc-detail-track")).toBe("354px");
    await waitFor(() => {
      expect(left).toHaveStyle({ left: "296px" });
      expect(right).toHaveStyle({ left: "902px" });
    });
    expect(left).toHaveAttribute("data-pane-toggle", "left");
    expect(left).toHaveTextContent("▸");
    expect(left.querySelector("span")).toHaveClass("rotate-180");
    expect(left).toHaveClass("fixed", "top-1/2", "-translate-y-1/2");
    expect(left).not.toHaveClass("rounded-full", "border", "bg-surface-raised", "shadow-md");
    expect(right).toHaveAttribute("data-pane-toggle", "right");
    expect(right).toHaveTextContent("▸");
    expect(right.querySelector("span")).not.toHaveClass("rotate-180");
    expect(container.querySelector("[data-panes] > [data-pane-toggle='left']")).toBe(left);
    expect(container.querySelector("[data-panes] > [data-pane-toggle='right']")).toBe(right);

    fireEvent.click(left);
    expect(screen.getByRole("button", { name: "Show the left panel" }).querySelector("span")).not.toHaveClass("rotate-180");

    fireEvent.click(right);
    const reopenRight = screen.getByRole("button", { name: "Show the right panel" });
    expect(reopenRight).toHaveClass("fixed", "sm:flex");
    expect(reopenRight).not.toHaveClass("lg:flex");
  });

  it("keeps a card moved out of the right panel visible when that panel collapses", async () => {
    useLayoutStore.getState().setUnlocked(true);
    render(<Board />);
    fireEvent.click(await screen.findByRole("button", { name: "Move Timeline to primary panel" }));
    fireEvent.click(screen.getByRole("button", { name: "Hide the right panel" }));

    const moved = screen.getByText("Timeline body").closest("[data-layout-card-id]");
    expect(moved).toHaveAttribute("data-layout-panel", "primary");
    expect(moved).toBeVisible();
  });

  it("uses persisted panel widths", () => {
    useLayoutStore.getState().setPanelWidth("desktop:/capture", "primary", 410);
    useLayoutStore.getState().setPanelWidth("desktop:/capture", "detail", 470);
    const { container } = render(<Board />);
    const grid = container.querySelector<HTMLElement>("[data-panes]");
    expect(grid?.style.getPropertyValue("--ssc-pane-cols-lg")).toContain("410px");
    expect(grid?.style.getPropertyValue("--ssc-pane-cols-lg")).toContain("470px");
  });

  it("keeps fill panels tall and persists an explicit panel height", async () => {
    const { container } = render(<Board />);
    expect(container.querySelector('[data-layout-panel-target="secondary"]')).toHaveClass("h-full", "min-h-0");
    useLayoutStore.getState().setPanelHeight("desktop:/capture", "secondary", 640);
    await waitFor(() => {
      expect(container.querySelector<HTMLElement>('[data-layout-panel-target="secondary"]')?.style.getPropertyValue("--ssc-panel-height")).toBe("640px");
    });
  });
});


it("offers all panel destinations for untitled cards on a single-panel screen", async () => {
  useLayoutStore.setState({ unlocked: true });
  render(<MemoryRouter><LayoutRouteProvider><PaneLayout secondary={<Card layoutId="notice" layoutLabel="Notice"><p>Body</p></Card>} /></LayoutRouteProvider></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: "Move Notice to primary panel" }));
  await waitFor(() => expect(document.querySelector('[data-layout-panel-target="primary"]')).toContainElement(screen.getByText("Body")));
  fireEvent.click(screen.getByRole("button", { name: "Move Notice to detail panel" }));
  await waitFor(() => expect(document.querySelector('[data-layout-panel-target="detail"]')).toContainElement(screen.getByText("Body")));
  useLayoutStore.setState({ unlocked: false });
  await waitFor(() => expect(document.querySelector('[data-layout-panel-target="detail"]')).toContainElement(screen.getByText("Body")));
});
