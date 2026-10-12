// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { AboutPanel } from "./AboutPanel";

afterEach(cleanup);

describe("AboutPanel", () => {
  it("shows the current name and source licence", () => {
    renderWithQueryClient(<AboutPanel />);
    expect(screen.getByRole("img", { name: "Photon Commander" })).toHaveClass("w-full", "h-auto");
    expect(screen.getByAltText("Photon Commander")).toBeInTheDocument();
    expect(screen.getByText("Keep photons under your control—until they become electrons.")).toBeInTheDocument();
    expect(screen.getByText(/compatible with ZWO Seestar telescopes/)).toBeInTheDocument();
    expect(screen.getByText(/Free software under GPL-3.0-only/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Source code" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart backend + frontend" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reboot Raspberry Pi" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Shut down Raspberry Pi" })).toBeDisabled();
  });
});
