// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Durable session-setup state for the Planning tab: the target, mount mode,
 * profile/optic choice, session start/length, scrub instant, mosaic grid and
 * altitude floor. It is cached synchronously outside the tab's component tree
 * (`schedulingUiStore.ts`'s own pattern), so a resolved target and its whole
 * session setup survive `<Routes>` unmounting `PlanningTab` on a tab switch
 * instead of resetting to defaults every time (frontend-react.md). The
 * astro/framing responses computed from these inputs stay in TanStack Query;
 * nothing here copies them.
 *
 * `ClientConfigurationSync` mirrors the persisted value to the backend so a
 * fresh browser restores it too. Open dialogs, pointer gestures, pan/zoom,
 * server-owned plans, and computed responses remain outside this store.
 */
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { safeLocalStorage } from "../../store/safeStorage";
import type { MountMode } from "../../api/astro";
import type { StartMode } from "./SessionStartField";
import type { SolarEventName } from "./twilight";
import type { PlanningTarget } from "./types";

const DEFAULT_SESSION_LENGTH_HOURS = 4;
/** Planning-tab-local floor for the altitude-limit ring and the
 * `/api/astro/night` visibility fallback (SessionControls.tsx's own doc
 * comment on `altitudeLimitDeg` explains why this lives here rather than as
 * a persisted setting). 20deg is a common backyard-imaging convention —
 * roughly where atmospheric extinction and light-dome glow both start
 * costing real signal — not an `astro-math` number. */
const DEFAULT_ALTITUDE_LIMIT_DEG = 20;
const DEFAULT_CENTERING_TOLERANCE_TEXT = "30";

export type BestTargetCategory =
  | "galaxies"
  | "emission_nebulae"
  | "reflection_nebulae"
  | "dark_nebulae"
  | "planetary_nebulae"
  | "supernova_remnants"
  | "star_clusters";

export const DEFAULT_BEST_TARGET_CATEGORIES: BestTargetCategory[] = [
  "galaxies",
  "emission_nebulae",
  "reflection_nebulae",
  "dark_nebulae",
  "planetary_nebulae",
  "supernova_remnants",
];

export interface MosaicSpec {
  raNum: number;
  decNum: number;
  overlapPct: number;
}

interface PlanningUiState {
  target: PlanningTarget | null;
  // EQ by default: the S30 Pro ships with a wedge and the owner plans in EQ
  // (user request, 2026-09-07). Alt-az stays one tap away for the tripod
  // case; the framing endpoint warns when the plan and the device disagree.
  mount: MountMode;
  // `null` means "no explicit user choice yet" — the *effective* id used
  // everywhere below falls back to the bundled list's first entry, computed
  // at render time rather than seeded by an effect (no state to desync).
  chosenProfileId: string | null;
  chosenOpticId: string | null;
  // Session start is a `TimeSpec` (DESIGN.md §4.1), not a bare `Date`:
  // either the absolute value below, or an event-relative pick against
  // `twilightNight`'s solar events, resolved further down. `startAbsolute`
  // is the mode's own value AND the seed a switch into event mode copies
  // its anchor date from — kept as one `Date` rather than two so the two
  // modes cannot drift apart while unused.
  startMode: StartMode;
  startAbsolute: Date;
  startAnchorDate: Date;
  startEventName: SolarEventName | null;
  startEventOffsetMin: number;
  sessionLengthHours: number;
  instantSeconds: number;
  mosaicSpec: MosaicSpec;
  altitudeLimitDeg: number;
  centeringToleranceText: string;
  bestTargetCategories: BestTargetCategory[];

  setTarget: (target: PlanningTarget | null) => void;
  setMount: (mount: MountMode) => void;
  setChosenProfileId: (id: string | null) => void;
  setChosenOpticId: (id: string | null) => void;
  setStartMode: (mode: StartMode) => void;
  setStartAbsolute: (when: Date) => void;
  setStartAnchorDate: (when: Date) => void;
  setStartEventName: (name: SolarEventName | null) => void;
  setStartEventOffsetMin: (min: number) => void;
  setSessionLengthHours: (hours: number) => void;
  setInstantSeconds: (seconds: number) => void;
  resetObservationTime: (when: Date) => void;
  setMosaicSpec: (spec: MosaicSpec) => void;
  setAltitudeLimitDeg: (deg: number) => void;
  setCenteringToleranceText: (value: string) => void;
  setBestTargetCategories: (categories: BestTargetCategory[]) => void;
}

export const usePlanningUiStore = create<PlanningUiState>()(persist((set) => ({
  target: null,
  mount: "eq",
  chosenProfileId: null,
  chosenOpticId: null,
  // Reported live: "make the astronomical dusk the default option selected"
  // and then, because the first attempt only set `startEventName` below:
  // "Session start does not start by default from astronomical dusk". Both
  // halves are needed — the event name alone is inert while the *mode* is
  // "absolute", which pins the session to `startAbsolute` (i.e. whenever the
  // tab happened to be opened) and does not even show the event picker.
  // Persisted state keeps the choice until the user selects something else.
  startMode: "event",
  startAbsolute: new Date(),
  startAnchorDate: new Date(),
  startEventName: "astronomical_dusk",
  startEventOffsetMin: 0,
  sessionLengthHours: DEFAULT_SESSION_LENGTH_HOURS,
  instantSeconds: 0,
  mosaicSpec: { raNum: 1, decNum: 1, overlapPct: 20 },
  altitudeLimitDeg: DEFAULT_ALTITUDE_LIMIT_DEG,
  centeringToleranceText: DEFAULT_CENTERING_TOLERANCE_TEXT,
  bestTargetCategories: DEFAULT_BEST_TARGET_CATEGORIES,

  setTarget: (target) => { set({ target }); },
  setMount: (mount) => { set({ mount }); },
  setChosenProfileId: (chosenProfileId) => { set({ chosenProfileId }); },
  setChosenOpticId: (chosenOpticId) => { set({ chosenOpticId }); },
  setStartMode: (startMode) => { set({ startMode }); },
  setStartAbsolute: (startAbsolute) => { set({ startAbsolute }); },
  setStartAnchorDate: (startAnchorDate) => { set({ startAnchorDate }); },
  setStartEventName: (startEventName) => { set({ startEventName }); },
  setStartEventOffsetMin: (startEventOffsetMin) => { set({ startEventOffsetMin }); },
  setSessionLengthHours: (sessionLengthHours) => { set({ sessionLengthHours }); },
  setInstantSeconds: (instantSeconds) => { set({ instantSeconds }); },
  resetObservationTime: (when) => {
    // A fresh Planning session always targets today's upcoming evening,
    // including when opened before noon. Keep the calendar-date anchor at
    // local noon so it cannot be mistaken for an overnight observation time.
    const startAnchorDate = new Date(when);
    startAnchorDate.setHours(12, 0, 0, 0);
    set({
      startAbsolute: new Date(when),
      startAnchorDate,
      instantSeconds: 0,
    });
  },
  setMosaicSpec: (mosaicSpec) => { set({ mosaicSpec }); },
  setAltitudeLimitDeg: (altitudeLimitDeg) => { set({ altitudeLimitDeg }); },
  setCenteringToleranceText: (centeringToleranceText) => { set({ centeringToleranceText }); },
  setBestTargetCategories: (bestTargetCategories) => { set({ bestTargetCategories }); },
}), {
  name: "ssc.planning-ui.v1",
  version: 1,
  storage: createJSONStorage(safeLocalStorage),
  partialize: (state) => ({
    target: state.target,
    mount: state.mount,
    chosenProfileId: state.chosenProfileId,
    chosenOpticId: state.chosenOpticId,
    startMode: state.startMode,
    startAbsolute: state.startAbsolute,
    startAnchorDate: state.startAnchorDate,
    startEventName: state.startEventName,
    startEventOffsetMin: state.startEventOffsetMin,
    sessionLengthHours: state.sessionLengthHours,
    instantSeconds: state.instantSeconds,
    mosaicSpec: state.mosaicSpec,
    altitudeLimitDeg: state.altitudeLimitDeg,
    centeringToleranceText: state.centeringToleranceText,
    bestTargetCategories: state.bestTargetCategories,
  }),
  merge: (persisted, current) => {
    const saved = persisted as Partial<PlanningUiState>;
    return {
      ...current,
      ...saved,
      startAbsolute: saved.startAbsolute === undefined ? current.startAbsolute : new Date(saved.startAbsolute),
      startAnchorDate: saved.startAnchorDate === undefined ? current.startAnchorDate : new Date(saved.startAnchorDate),
    };
  },
}));
