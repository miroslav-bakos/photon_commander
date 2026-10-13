// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Durable selection state for the Scheduling tab: which plan is open and
 * which item is selected. It is outside the tab's component
 * tree, so the Planning tab's "Send to Scheduling" can select the item it
 * just appended before navigating, and the selection survives a tab switch
 * or application restart. `ClientConfigurationSync` mirrors the cache to the
 * backend so a fresh browser restores the same selection.
 * The plan *documents* stay in TanStack Query; nothing here copies them.
 */
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { safeLocalStorage } from "../../store/safeStorage";
import type { RunState } from "../../api/runs";

interface EditLock { runId: string | null; locked: boolean }

export function planEditRunId(planId: string | null, run: RunState | null): string | null {
  return run?.plan_id === planId && (run.status === "running" || run.status === "paused") ? run.run_id : null;
}

export function isPlanEditLocked(planId: string | null, run: RunState | null, locks: Record<string, EditLock>): boolean {
  if (planId === null) return false;
  const runId = planEditRunId(planId, run);
  const lock = locks[planId];
  return lock?.runId === runId ? lock.locked : runId !== null;
}

interface SchedulingUiState {
  selectedPlanId: string | null;
  selectedItemId: string | null;
  selectedNightDate: string | null;
  /** Editing permission is session-local; a fresh client must unlock an active run again. */
  editLocks: Record<string, EditLock>;
  setPlanEditLock: (planId: string, runId: string | null, locked: boolean) => void;
  selectPlan: (planId: string | null) => void;
  selectItem: (itemId: string | null) => void;
  selectNightDate: (nightDate: string | null) => void;
  /** One call for the handoff: open this plan on this item. */
  select: (planId: string, itemId: string | null, nightDate?: string | null) => void;
}

export const useSchedulingUiStore = create<SchedulingUiState>()(persist((set) => ({
  selectedPlanId: null,
  selectedItemId: null,
  selectedNightDate: null,
  editLocks: {},
  setPlanEditLock: (planId, runId, locked) => {
    set((state) => ({ editLocks: { ...state.editLocks, [planId]: { runId, locked } } }));
  },
  selectPlan: (selectedPlanId) => {
    set({ selectedPlanId, selectedItemId: null, selectedNightDate: null });
  },
  selectItem: (selectedItemId) => {
    set({ selectedItemId });
  },
  selectNightDate: (selectedNightDate) => {
    set({ selectedNightDate });
  },
  select: (selectedPlanId, selectedItemId, selectedNightDate = null) => {
    set({ selectedPlanId, selectedItemId, selectedNightDate });
  },
}), {
  name: "ssc.scheduling-ui.v1",
  version: 1,
  storage: createJSONStorage(safeLocalStorage),
  partialize: ({ selectedPlanId, selectedItemId, selectedNightDate }) => ({ selectedPlanId, selectedItemId, selectedNightDate }),
}));
