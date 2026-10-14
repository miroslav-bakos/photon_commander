import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it } from "vitest";
import { Card } from "./Card";
import { PaneLayout } from "./PaneLayout";
import { LayoutRouteProvider } from "../layout/LayoutContext";
import { useLayoutStore } from "../layout/layoutStore";

afterEach(() => { cleanup(); useLayoutStore.getState().resetAll(); });
function board() {
  return render(<MemoryRouter initialEntries={["/planning"]}><LayoutRouteProvider><PaneLayout
    primary={<div className="contents"><Card title="Target" collapseKey="planning.target">Target body</Card><Card title="Frame from picture" collapseKey="planning.reference-image" defaultAfter="planning.target">Framing body</Card></div>}
    secondary={<Card title="Sky map">Map body</Card>}
  /></LayoutRouteProvider></MemoryRouter>);
}
it("places the new framing panel just after a saved Target placement", () => {
  useLayoutStore.getState().placeCards("desktop:/planning", "primary", ["other", "planning.target", "night"]);
  board();
  const card = screen.getByText("Framing body").closest("[data-layout-card-id]");
  expect(card).toHaveStyle({ order: "3" });
  expect(card?.parentElement).toHaveAttribute("data-layout-panel-target", "primary");
});
it("follows a relocated target until the framing panel is explicitly moved", () => {
  useLayoutStore.getState().placeCards("desktop:/planning", "secondary", ["planning.target"]);
  useLayoutStore.getState().placeCards("desktop:/planning", "primary", ["planning.reference-image"]);
  board();
  const card = screen.getByText("Framing body").closest("[data-layout-card-id]");
  expect(card?.parentElement).toHaveAttribute("data-layout-panel-target", "primary");
  expect(card).toHaveStyle({ order: "0" });
});
