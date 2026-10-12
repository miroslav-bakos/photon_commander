// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { LocationPanel } from "./LocationPanel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderLocationPanel(secure: boolean) {
  Object.defineProperty(window, "isSecureContext", { configurable: true, value: secure });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          location: { active: null, preferred_source: null, sources: {} },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    ),
  );
  return renderWithQueryClient(<LocationPanel />);
}

describe("LocationPanel browser geolocation availability", () => {
  it("accepts decimal commas and rejects missing or out-of-range coordinates", async () => {
    renderLocationPanel(false);
    await screen.findByText("Manual location");
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Latitude"), { target: { value: "49,5555" } });
    fireEvent.change(screen.getByLabelText("Longitude"), { target: { value: "18.5555" } });
    expect(save).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Latitude"), { target: { value: "91" } });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Latitude"), { target: { value: "" } });
    expect(save).toBeDisabled();
    expect(screen.getByRole("link", { name: "Open Google Maps" })).toHaveAttribute("href", "https://www.google.com/maps");
  });
  it("hides browser geolocation on an insecure HTTP origin", async () => {
    renderLocationPanel(false);
    expect(await screen.findByText("Manual location")).toBeInTheDocument();
    expect(screen.queryByText("Browser location")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Use this browser's location" })).not.toBeInTheDocument();
    expect(screen.getByText("No location set yet. Add one manually.")).toBeInTheDocument();
  });

  it("keeps browser geolocation in a secure context", async () => {
    renderLocationPanel(true);
    expect(await screen.findByText("Browser location")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use this browser's location" })).toBeInTheDocument();
  });
});
