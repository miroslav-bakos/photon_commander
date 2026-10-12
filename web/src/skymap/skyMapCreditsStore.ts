// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Cross-component channel for the sky map's own survey/catalogue credit
 * line: written by whichever `SkyMap` is currently mounted, read by the
 * app-shell footer (`Shell.tsx`) that renders it outside the map's own
 * bounding box (reported live: the in-map credit bar blocking the view on a
 * small screen). Matches `useConnectionStore`'s "one authoritative value,
 * several readers" shape rather than a callback prop, because the footer
 * lives in `Shell.tsx`, above and outside whichever tab currently mounts a
 * `SkyMap` — there is no shared parent to thread a callback through short of
 * `Shell` itself, which does not (and should not) know which tab is active.
 *
 * `credits` is only meaningful while `mounted` is true. `setMounted(false)`
 * clears `credits` in the same update, so a tab switch that unmounts
 * `SkyMap` cannot leave a stale credit on screen for a map that is no longer
 * showing.
 */
import { create } from "zustand";
import type { SurveyCredit } from "./types";

interface SkyMapCreditsState {
  /** Whether a `SkyMap` is currently mounted anywhere in the tree. `false`
   * means the footer renders nothing at all — not even a pending message. */
  mounted: boolean;
  /** `null` while mounted and the frame has not reported its first
   * `creditChange` yet ("Loading survey attribution…" territory).
   * Meaningless (and ignored by the footer) while `mounted` is false. */
  credits: SurveyCredit[] | null;
  setMounted: (mounted: boolean) => void;
  setCredits: (credits: SurveyCredit[] | null) => void;
}

export const useSkyMapCreditsStore = create<SkyMapCreditsState>((set) => ({
  mounted: false,
  credits: null,
  setMounted: (mounted) => {
    set(mounted ? { mounted: true } : { mounted: false, credits: null });
  },
  setCredits: (credits) => {
    set({ credits });
  },
}));
