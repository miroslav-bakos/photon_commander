// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { afterEach, describe, expect, it } from "vitest";
import { usePlanningUiStore } from "./planningUiStore";
import { nightWindowForAnchor } from "./twilight";

describe("Planning night default", () => {
  afterEach(() => {
    usePlanningUiStore.setState(usePlanningUiStore.getInitialState());
  });

  it("opens today's upcoming night before noon, with the field and query on the same date", () => {
    usePlanningUiStore.getState().resetObservationTime(new Date(2026, 8, 18, 8, 30));
    const anchor = usePlanningUiStore.getState().startAnchorDate;
    expect(anchor).toEqual(new Date(2026, 8, 18, 12, 0));
    expect(nightWindowForAnchor(anchor).start).toEqual(new Date(2026, 8, 18, 12, 0));
  });

  it("opens today's upcoming night after noon too", () => {
    usePlanningUiStore.getState().resetObservationTime(new Date(2026, 8, 18, 21, 0));
    expect(usePlanningUiStore.getState().startAnchorDate).toEqual(new Date(2026, 8, 18, 12, 0));
  });
});
