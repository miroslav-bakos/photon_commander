// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { testConnectedDevices, testSnapshot } from "../../test/fixtures";
import { useConnectionStore } from "../../store/connectionStore";
import { FilterWheelPanel } from "./FilterWheelPanel";

afterEach(() => {
  vi.unstubAllGlobals();
  useConnectionStore.getState().reset();
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("FilterWheelPanel", () => {
  beforeEach(() => {
    useConnectionStore.setState({ snapshot: testSnapshot({ devices: testConnectedDevices("filterwheel") }) });
  });

  it("shows Disconnected without polling when the rig is off", () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    useConnectionStore.setState({ snapshot: testSnapshot({ devices: null }) });

    renderWithQueryClient(<FilterWheelPanel />);

    expect(screen.getByText("Disconnected")).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("shows the live slot and sends the selected ASCOM index", async () => {
    let putRequest: Request | undefined;
    const fetch = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(input, init);
      if (request.method === "GET") {
        return Promise.resolve(
          json({
            names: ["Dark", "LP", "IR"],
            position: 1,
            names_note: null,
            position_note: null,
          }),
        );
      }
      if (request.method === "PUT") {
        putRequest = request;
        return Promise.resolve(new Response(null, { status: 202 }));
      }
      return Promise.resolve(json({ detail: "unexpected request" }, 500));
    });
    vi.stubGlobal("fetch", fetch);
    renderWithQueryClient(<FilterWheelPanel />);

    expect(await screen.findByRole("button", { name: "LP" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const selectedSlot = screen.getByRole("button", { name: "LP" });
    selectedSlot.focus();
    fireEvent.keyDown(selectedSlot, { key: "ArrowRight" });
    expect(screen.getByRole("button", { name: "IR" })).toHaveFocus();
    expect(selectedSlot).toHaveAttribute("aria-pressed", "true");
    expect(putRequest).toBeUndefined();
    fireEvent.click(screen.getByRole("button", { name: "IR" }));

    await vi.waitFor(() => {
      expect(putRequest).toBeInstanceOf(Request);
    });
    await expect(putRequest?.clone().json()).resolves.toEqual({ position: 2 });
  });

  it("collapses Connected=True refusals to a concise disconnected state", async () => {
    const namesError =
      "filterwheel.names: [0x407] filterwheel.names requires Connected = True (this device is not connected)";
    const positionError =
      "filterwheel.position: [0x407] filterwheel.position requires Connected = True (this device is not connected)";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        json({
          names: null,
          position: null,
          names_note: namesError,
          position_note: positionError,
        }),
      ),
    );
    renderWithQueryClient(<FilterWheelPanel />);

    expect(await screen.findByText("Disconnected")).toBeInTheDocument();
    expect(screen.queryByText(namesError)).not.toBeInTheDocument();
    expect(screen.queryByText(positionError)).not.toBeInTheDocument();
    expect(screen.queryByText(/Current:/)).not.toBeInTheDocument();
  });

  it("retains a useful non-connection diagnostic", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        json({
          names: null,
          position: null,
          names_note: "FilterWheel.Names: not implemented",
          position_note: null,
        }),
      ),
    );
    renderWithQueryClient(<FilterWheelPanel />);

    expect(await screen.findByText("FilterWheel.Names: not implemented")).toBeInTheDocument();
  });
});
