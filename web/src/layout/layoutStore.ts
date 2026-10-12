// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { safeLocalStorage } from "../store/safeStorage";

export type LayoutPanel = "primary" | "secondary" | "detail";

export interface CardPlacement {
  panel?: LayoutPanel;
  order?: number;
  collapsed?: boolean;
  height?: number;
}

export interface SavedTabLayout {
  cards: Record<string, CardPlacement>;
  primaryWidth?: number;
  detailWidth?: number;
  panelHeights?: Partial<Record<LayoutPanel, number>>;
  primaryCollapsed?: boolean;
  detailCollapsed?: boolean;
}

export interface SavedLayouts {
  version: 1;
  tabs: Record<string, SavedTabLayout>;
}

interface LayoutState {
  unlocked: boolean;
  saved: SavedLayouts;
  setUnlocked: (unlocked: boolean) => void;
  setCollapsed: (tab: string, card: string, collapsed: boolean) => void;
  setCardHeight: (tab: string, card: string, height: number | undefined) => void;
  placeCards: (tab: string, panel: LayoutPanel | undefined, cards: string[]) => void;
  setPanelWidth: (tab: string, panel: "primary" | "detail", width: number) => void;
  setPanelHeight: (tab: string, panel: LayoutPanel, height: number) => void;
  setPanelCollapsed: (tab: string, panel: "primary" | "detail", collapsed: boolean) => void;
  resetTab: (tab: string) => void;
  resetAll: () => void;
  replaceSaved: (saved: SavedLayouts) => void;
}

export const LAYOUT_STORAGE_KEY = "ssc.layout.v1";

function updateTab(
  saved: SavedLayouts,
  tab: string,
  update: (layout: SavedTabLayout) => SavedTabLayout,
): SavedLayouts {
  return {
    version: 1,
    tabs: {
      ...saved.tabs,
      [tab]: update(saved.tabs[tab] ?? { cards: {} }),
    },
  };
}

export const useLayoutStore = create<LayoutState>()(
  persist(
    (set) => ({
      unlocked: false,
      saved: { version: 1, tabs: {} },
      setUnlocked: (unlocked) => { set({ unlocked }); },
      setCollapsed: (tab, card, collapsed) => {
        set((state) => ({
          saved: updateTab(state.saved, tab, (layout) => ({
            ...layout,
            cards: {
              ...layout.cards,
              [card]: { ...layout.cards[card], collapsed },
            },
          })),
        }));
      },
      setCardHeight: (tab, card, height) => {
        set((state) => ({
          saved: updateTab(state.saved, tab, (layout) => ({
            ...layout,
            cards: {
              ...layout.cards,
              [card]: { ...layout.cards[card], height },
            },
          })),
        }));
      },
      placeCards: (tab, panel, cards) => {
        set((state) => ({
          saved: updateTab(state.saved, tab, (layout) => {
            const placements = { ...layout.cards };
            cards.forEach((card, order) => {
              placements[card] = { ...placements[card], panel, order };
            });
            return { ...layout, cards: placements };
          }),
        }));
      },
      setPanelWidth: (tab, panel, width) => {
        set((state) => ({
          saved: updateTab(state.saved, tab, (layout) => ({
            ...layout,
            [panel === "primary" ? "primaryWidth" : "detailWidth"]: Math.round(width),
          })),
        }));
      },
      setPanelHeight: (tab, panel, height) => {
        set((state) => ({
          saved: updateTab(state.saved, tab, (layout) => ({
            ...layout,
            panelHeights: { ...layout.panelHeights, [panel]: Math.round(height) },
          })),
        }));
      },
      setPanelCollapsed: (tab, panel, collapsed) => {
        set((state) => ({
          saved: updateTab(state.saved, tab, (layout) => ({
            ...layout,
            [panel === "primary" ? "primaryCollapsed" : "detailCollapsed"]: collapsed,
          })),
        }));
      },
      resetTab: (tab) => {
        set((state) => {
          const tabs = Object.fromEntries(Object.entries(state.saved.tabs).filter(([key]) => key !== tab));
          return { saved: { version: 1, tabs } };
        });
      },
      resetAll: () => { set({ saved: { version: 1, tabs: {} } }); },
      replaceSaved: (saved) => { set({ saved }); },
    }),
    {
      name: LAYOUT_STORAGE_KEY,
      version: 1,
      storage: createJSONStorage(safeLocalStorage),
      partialize: (state) => ({ saved: state.saved }),
    },
  ),
);

export function validateSavedLayouts(value: unknown): SavedLayouts {
  if (typeof value !== "object" || value === null) throw new Error("Layout configuration must be an object.");
  const candidate = value as { version?: unknown; tabs?: unknown };
  if (candidate.version !== 1 || typeof candidate.tabs !== "object" || candidate.tabs === null) {
    throw new Error("Unsupported layout configuration version.");
  }
  for (const [tab, rawLayout] of Object.entries(candidate.tabs)) {
    if (tab.length > 100 || typeof rawLayout !== "object" || rawLayout === null) {
      throw new Error("Invalid tab layout.");
    }
    const layout = rawLayout as { cards?: unknown; primaryWidth?: unknown; detailWidth?: unknown; panelHeights?: unknown; primaryCollapsed?: unknown; detailCollapsed?: unknown };
    if (typeof layout.cards !== "object" || layout.cards === null) throw new Error("Invalid card layout.");
    for (const [card, rawPlacement] of Object.entries(layout.cards)) {
      if (card.length > 160 || typeof rawPlacement !== "object" || rawPlacement === null) {
        throw new Error("Invalid card placement.");
      }
      const placement = rawPlacement as Record<string, unknown>;
      if (placement.panel !== undefined && (typeof placement.panel !== "string" || !["primary", "secondary", "detail"].includes(placement.panel))) {
        throw new Error("Invalid layout panel.");
      }
      if (placement.order !== undefined && (!Number.isInteger(placement.order) || Number(placement.order) < 0)) {
        throw new Error("Invalid card order.");
      }
      if (placement.collapsed !== undefined && typeof placement.collapsed !== "boolean") {
        throw new Error("Invalid collapsed state.");
      }
      if (placement.height !== undefined && (typeof placement.height !== "number" || !Number.isFinite(placement.height) || placement.height < 180 || placement.height > 1200)) {
        throw new Error("Invalid card height.");
      }
    }
    for (const width of [layout.primaryWidth, layout.detailWidth]) {
      if (width !== undefined && (typeof width !== "number" || !Number.isFinite(width) || width < 180 || width > 700)) {
        throw new Error("Invalid panel width.");
      }
    }
    if (layout.panelHeights !== undefined) {
      if (typeof layout.panelHeights !== "object" || layout.panelHeights === null) throw new Error("Invalid panel heights.");
      for (const [panel, height] of Object.entries(layout.panelHeights)) {
        if (!["primary", "secondary", "detail"].includes(panel) || typeof height !== "number" || !Number.isFinite(height) || height < 180 || height > 1600) {
          throw new Error("Invalid panel height.");
        }
      }
    }
    for (const collapsed of [layout.primaryCollapsed, layout.detailCollapsed]) {
      if (collapsed !== undefined && typeof collapsed !== "boolean") throw new Error("Invalid panel collapsed state.");
    }
  }
  return value as SavedLayouts;
}
