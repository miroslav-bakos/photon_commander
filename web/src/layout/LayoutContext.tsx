// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
/* eslint-disable react-refresh/only-export-components -- providers and their hooks form one context API */

import { createContext, useContext, useSyncExternalStore, type ReactNode } from "react";
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

// Matches TabNav's desktop boundary. One provider scopes every layout consumer.
export const MOBILE_LAYOUT_QUERY = "(max-width: 767px)";

function subscribeViewport(onChange: () => void) {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const query = window.matchMedia(MOBILE_LAYOUT_QUERY);
  query.addEventListener("change", onChange);
  return () => { query.removeEventListener("change", onChange); };
}

function isMobileViewport() {
  return typeof window.matchMedia === "function" && window.matchMedia(MOBILE_LAYOUT_QUERY).matches;
}

export function LayoutRouteProvider({ children, tab }: { children: ReactNode; tab?: string }) {
  const location = useLocation();
  const mobile = useSyncExternalStore(subscribeViewport, isMobileViewport, () => false);
  return (
    <LayoutRouteContext.Provider value={{ enabled: true, tab: `${mobile ? "mobile" : "desktop"}:${tab ?? location.pathname}` }}>
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
