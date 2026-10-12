// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { safeLocalStorage } from "../store/safeStorage";
import type { FrameType } from "../api/capture";

interface CaptureFormDraft {
  frameType: FrameType;
  durationText: string;
  loopCountText: string;
  gainText: string;
  objectText: string;
  planningTargetName: string | null;
  flatSource: "panel" | "sky";
  opticId: string;
  calibrationCountText: string;
  selectedFilters: number[] | null;
  generateMaster: boolean;
  keepOnlyMaster: boolean;
}

interface CaptureFormState extends CaptureFormDraft {
  update: (patch: Partial<CaptureFormDraft>) => void;
  syncPlanningTarget: (name: string | null) => void;
  reset: () => void;
}

const DEFAULT_DRAFT: CaptureFormDraft = {
  frameType: "light",
  durationText: "60",
  loopCountText: "",
  gainText: "200",
  objectText: "",
  planningTargetName: null,
  flatSource: "panel",
  opticId: "tele",
  calibrationCountText: "30",
  selectedFilters: null,
  generateMaster: true,
  keepOnlyMaster: false,
};

/** Durable capture form draft. localStorage gives synchronous remounts and
 * `ClientConfigurationSync` mirrors it to backend configuration for fresh
 * browsers; transient dialog/pointer state is deliberately absent. */
export const useCaptureFormStore = create<CaptureFormState>()(persist((set) => ({
  ...DEFAULT_DRAFT,
  update: (patch) => { set(patch); },
  syncPlanningTarget: (name) => {
    set((state) => state.planningTargetName === name
      ? state
      : { planningTargetName: name, objectText: name ?? "" });
  },
  reset: () => { set({ ...DEFAULT_DRAFT }); },
}), {
  name: "ssc.capture-form.v1",
  version: 1,
  storage: createJSONStorage(safeLocalStorage),
  partialize: ({ update: _update, syncPlanningTarget: _sync, reset: _reset, ...draft }) => draft,
}));
