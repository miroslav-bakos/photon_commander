// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ObservingModeSwitch } from "./ObservingModeSwitch";

describe("ObservingModeSwitch", () => {
  it("switches between Capture and Live stack while marking the current view", () => {
    render(<MemoryRouter initialEntries={["/live-stacking"]}><ObservingModeSwitch /></MemoryRouter>);

    expect(screen.getByRole("link", { name: "Capture" })).toHaveAttribute("href", "/live");
    expect(screen.getByRole("link", { name: "Live stack" })).toHaveAttribute("aria-current", "page");
  });
});
