// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { create } from "zustand";

interface StreamPipState {
  enabled: boolean;
  setEnabled: (enabled: boolean) => void;
}

/** Transient viewing preference for this browser session. The stream itself
 * remains server-owned; this only decides whether its existing preview is
 * carried between application routes. */
export const useStreamPipStore = create<StreamPipState>((set) => ({
  enabled: false,
  setEnabled: (enabled) => { set({ enabled }); },
}));
