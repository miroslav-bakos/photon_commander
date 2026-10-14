// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { LayoutRouteProvider, useLayoutRoute } from "./LayoutContext";
import { useLayoutStore, validateSavedLayouts } from "./layoutStore";

function Probe() {
  const { tab } = useLayoutRoute();
  return <div><output>{tab}</output><input aria-label="Draft" defaultValue="" /></div>;
}

afterEach(() => {
  cleanup();
  useLayoutStore.setState({ saved: { version: 1, tabs: {} } });
  vi.unstubAllGlobals();
});

it("selects independent responsive route keys without remounting drafts", () => {
  let mobile = false;
  const listeners = new Set<() => void>();
  vi.stubGlobal("matchMedia", vi.fn((query: string) => {
    expect(query).toBe("(max-width: 767px)");
    return { get matches() { return mobile; }, addEventListener: (_: string, listener: () => void) => listeners.add(listener), removeEventListener: (_: string, listener: () => void) => listeners.delete(listener) };
  }));
  render(<MemoryRouter initialEntries={["/capture"]}><LayoutRouteProvider><Probe /></LayoutRouteProvider></MemoryRouter>);
  expect(screen.getByText("desktop:/capture")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Draft"), { target: { value: "unsaved target" } });
  act(() => { mobile = true; listeners.forEach((listener) => { listener(); }); });
  expect(screen.getByText("mobile:/capture")).toBeInTheDocument();
  expect(screen.getByLabelText("Draft")).toHaveValue("unsaved target");
  act(() => { mobile = false; listeners.forEach((listener) => { listener(); }); });
  expect(screen.getByText("desktop:/capture")).toBeInTheDocument();
  expect(screen.getByLabelText("Draft")).toHaveValue("unsaved target");
});

it("imports legacy layouts into both views, preserving modern overrides and independent reset/export", () => {
  const legacy = { cards: { alpha: { panel: "detail" as const, order: 2, collapsed: true, height: 320 } }, primaryWidth: 410, detailWidth: 470, panelHeights: { secondary: 640 }, primaryCollapsed: true, detailCollapsed: false };
  useLayoutStore.getState().replaceSaved({ version: 1, tabs: { "/capture": legacy, "mobile:/capture": { cards: {} } } });
  const desktop = useLayoutStore.getState().saved.tabs["desktop:/capture"];
  expect(desktop).toEqual(legacy);
  expect(useLayoutStore.getState().saved.tabs["mobile:/capture"]).toEqual({ cards: {} });
  useLayoutStore.getState().setCollapsed("mobile:/capture", "alpha", false);
  useLayoutStore.getState().setPanelWidth("mobile:/capture", "primary", 250);
  useLayoutStore.getState().setPanelHeight("mobile:/capture", "secondary", 300);
  useLayoutStore.getState().setPanelCollapsed("mobile:/capture", "primary", false);
  useLayoutStore.getState().setCardHeight("mobile:/capture", "alpha", 220);
  useLayoutStore.getState().placeCards("mobile:/capture", "primary", ["alpha"]);
  expect(useLayoutStore.getState().saved.tabs["desktop:/capture"]).toEqual(legacy);
  const exported = validateSavedLayouts(JSON.parse(JSON.stringify(useLayoutStore.getState().saved)) as unknown);
  expect(validateSavedLayouts(exported)).toEqual(useLayoutStore.getState().saved);
  useLayoutStore.getState().resetTab("mobile:/capture");
  expect(useLayoutStore.getState().saved.tabs["mobile:/capture"]).toBeUndefined();
  expect(useLayoutStore.getState().saved.tabs["desktop:/capture"]).toEqual(legacy);
  useLayoutStore.getState().replaceSaved(exported);
  expect(useLayoutStore.getState().saved).toEqual(exported);
  useLayoutStore.getState().resetAll();
  expect(useLayoutStore.getState().saved.tabs).toEqual({});
});


it("hydrates existing persisted v1 layouts into both views before they render", async () => {
  const storage = useLayoutStore.persist.getOptions().storage;
  const legacy = { cards: { target: { collapsed: true } }, primaryWidth: 350 };
  useLayoutStore.persist.setOptions({ storage: {
    getItem: () => ({ state: { saved: { version: 1, tabs: { "/planning": legacy } } }, version: 1 }),
    setItem: () => undefined,
    removeItem: () => undefined,
  } });
  try {
    await useLayoutStore.persist.rehydrate();
    expect(useLayoutStore.getState().saved.tabs).toEqual({ "desktop:/planning": legacy, "mobile:/planning": legacy });
    useLayoutStore.getState().setCollapsed("desktop:/planning", "target", false);
    expect(useLayoutStore.getState().saved.tabs["mobile:/planning"]?.cards.target?.collapsed).toBe(true);
  } finally {
    useLayoutStore.persist.setOptions({ storage });
  }
});

it.each([
  null,
  { version: 1 },
  { version: 1, tabs: null },
  { version: 2, tabs: {} },
  { version: 1, tabs: { "/planning": { cards: { target: { panel: "invalid" } } } } },
])("retains current layouts when persisted storage is malformed: %j", (saved) => {
  useLayoutStore.getState().setPanelWidth("desktop:/planning", "primary", 350);
  const current = useLayoutStore.getState();
  const merge = useLayoutStore.persist.getOptions().merge;
  if (!merge) throw new Error("Missing persistence merge handler");
  expect(merge({ saved }, current)).toBe(current);
});
