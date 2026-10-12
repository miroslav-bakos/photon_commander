// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { usePolarDebugStore } from "../../store/polarDebugStore";
import { PolarDebugSettingsPanel } from "./PolarDebugSettingsPanel";

afterEach(() => {
  cleanup();
  usePolarDebugStore.setState({ enabled: false, altitudeAdjustmentArcsec: 0, azimuthAdjustmentArcsec: 0, stepArcsec: 60 });
});

it("enables debug controls without altering the simulated wedge position", () => {
  usePolarDebugStore.setState({ altitudeAdjustmentArcsec: -60 });
  render(<PolarDebugSettingsPanel />);
  fireEvent.click(screen.getByRole("checkbox", { name: "Enable PA debug simulator" }));
  expect(usePolarDebugStore.getState()).toMatchObject({ enabled: true, altitudeAdjustmentArcsec: -60 });
});
