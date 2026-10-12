// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { safeLocalStorage } from "./safeStorage";

export type WedgeAxis = "altitude" | "azimuth";
export type WedgeStepArcsec = 3600 | 1800 | 60 | 30 | 10;

interface PolarDebugState {
  enabled: boolean;
  simulatePolarAlignment: boolean;
  altitudeAdjustmentArcsec: number;
  azimuthAdjustmentArcsec: number;
  stepArcsec: WedgeStepArcsec;
  setEnabled: (enabled: boolean) => void;
  setSimulatePolarAlignment: (enabled: boolean) => void;
  setStep: (step: WedgeStepArcsec) => void;
  move: (axis: WedgeAxis, direction: -1 | 1) => void;
  resetWedge: () => void;
}

/** Browser-local simulator state. No method here sends a mount command. */
export const usePolarDebugStore = create<PolarDebugState>()(persist((set) => ({
  enabled: false,
  simulatePolarAlignment: false,
  altitudeAdjustmentArcsec: 0,
  azimuthAdjustmentArcsec: 0,
  stepArcsec: 60,
  setEnabled: (enabled) => { set(enabled ? { enabled } : { enabled, simulatePolarAlignment: false }); },
  setSimulatePolarAlignment: (simulatePolarAlignment) => { set({ simulatePolarAlignment }); },
  setStep: (stepArcsec) => { set({ stepArcsec }); },
  move: (axis, direction) => {
    set((state) => {
      if (!state.enabled) return state;
      const key = axis === "altitude" ? "altitudeAdjustmentArcsec" : "azimuthAdjustmentArcsec";
      return { [key]: Math.max(-15 * 3600, Math.min(15 * 3600, state[key] + direction * state.stepArcsec)) };
    });
  },
  resetWedge: () => { set({ altitudeAdjustmentArcsec: 0, azimuthAdjustmentArcsec: 0 }); },
}), {
  name: "ssc.polar-debug.v1",
  version: 2,
  storage: createJSONStorage(safeLocalStorage),
  migrate: (persisted) => ({ ...(persisted as object), simulatePolarAlignment: false }),
  partialize: ({ enabled, simulatePolarAlignment, altitudeAdjustmentArcsec, azimuthAdjustmentArcsec, stepArcsec }) => ({
    enabled, simulatePolarAlignment, altitudeAdjustmentArcsec, azimuthAdjustmentArcsec, stepArcsec,
  }),
}));
