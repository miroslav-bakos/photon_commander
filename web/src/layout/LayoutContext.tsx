// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
/* eslint-disable react-refresh/only-export-components -- providers and their hooks form one context API */

import { createContext, useContext, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import type { LayoutPanel } from "./layoutStore";

interface LayoutRouteContextValue {
  enabled: boolean;
  tab: string;
}

interface LayoutBoardContextValue {
  defaultPanel?: LayoutPanel;
  panels?: Partial<Record<LayoutPanel, HTMLDivElement>>;
}

const LayoutRouteContext = createContext<LayoutRouteContextValue>({ enabled: false, tab: "global" });
const LayoutBoardContext = createContext<LayoutBoardContextValue>({});

export function LayoutRouteProvider({ children, tab }: { children: ReactNode; tab?: string }) {
  const location = useLocation();
  return (
    <LayoutRouteContext.Provider value={{ enabled: true, tab: tab ?? location.pathname }}>
      {children}
    </LayoutRouteContext.Provider>
  );
}

export function LayoutPanelProvider({
  panel,
  panels,
  children,
}: {
  panel: LayoutPanel;
  panels: Partial<Record<LayoutPanel, HTMLDivElement>>;
  children: ReactNode;
}) {
  const parent = useLayoutBoard();
  return (
    <LayoutBoardContext.Provider value={{ defaultPanel: panel, panels: { ...parent.panels, ...panels } }}>
      {children}
    </LayoutBoardContext.Provider>
  );
}

export function useLayoutRoute() {
  return useContext(LayoutRouteContext);
}

export function useLayoutBoard() {
  return useContext(LayoutBoardContext);
}
