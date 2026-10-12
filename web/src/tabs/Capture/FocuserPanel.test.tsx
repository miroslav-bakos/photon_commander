// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import type { StateSnapshot } from "../../api/types";
import { useConnectionStore } from "../../store/connectionStore";
import { testConnectedDevices, testSnapshot } from "../../test/fixtures";
import { FocuserPanel } from "./FocuserPanel";

vi.mock("./AutofocusChart", () => ({ AutofocusChart: ({ embedded }: { embedded?: boolean }) => <p>{embedded ? "Embedded autofocus curve" : "Autofocus curve"}</p> }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useConnectionStore.getState().reset();
});

describe("FocuserPanel device state", () => {
  beforeEach(() => {
    useConnectionStore.setState({ snapshot: testSnapshot({ devices: testConnectedDevices("focuser") }) });
  });

  it("keeps the autofocus curve inside the focuser card", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ position: 1200, note: null, is_moving: false, is_moving_note: null, step_size: null, step_size_note: null }), { status: 200, headers: { "content-type": "application/json" } })));
    renderWithQueryClient(<FocuserPanel />);
    expect(await screen.findByText("Embedded autofocus curve")).toBeInTheDocument();
  });
  it("shows only Disconnected for an Alpaca Connected=True refusal", async () => {
    const rawNote =
      "focuser.position: [0x407] focuser.position requires Connected = True (this device is not connected)";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            position: null,
            note: rawNote,
            is_moving: null,
            is_moving_note: rawNote,
            step_size: null,
            step_size_note: rawNote,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    renderWithQueryClient(<FocuserPanel />);
    expect(await screen.findByText("Disconnected")).toBeInTheDocument();
    expect(screen.queryByText(rawNote)).not.toBeInTheDocument();
    expect(screen.queryByText(/Position:/)).not.toBeInTheDocument();
  });

  it("recovers a completed autofocus result from backend state after remount", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      position: 123,
      note: null,
      is_moving: false,
      is_moving_note: null,
      step_size: null,
      step_size_note: null,
    }), { status: 200, headers: { "content-type": "application/json" } })));
    useConnectionStore.setState({
      snapshot: {
        ...testSnapshot({ devices: testConnectedDevices("focuser") }),
        operations: [{
          id: "af-1",
          kind: "autofocus",
          status: "succeeded",
          message: "Completed.",
          progress_current: null,
          progress_total: null,
          result: null,
          error: null,
          idempotency_key: "request-1",
          cancellable: true,
          created_at: "2026-09-15T00:00:00Z",
          updated_at: "2026-09-15T00:01:00Z",
          finished_at: "2026-09-15T00:01:00Z",
        }],
      } as unknown as StateSnapshot,
      seq: 1,
    });

    renderWithQueryClient(<FocuserPanel />);

    expect(await screen.findByText(/Autofocus complete/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start autofocus" })).toBeEnabled();
  });

  it("recovers an autofocus error from backend state after remount", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      position: 123,
      note: null,
      is_moving: false,
      is_moving_note: null,
      step_size: null,
      step_size_note: null,
    }), { status: 200, headers: { "content-type": "application/json" } })));
    useConnectionStore.setState({
      snapshot: {
        ...testSnapshot({ devices: testConnectedDevices("focuser") }),
        operations: [{
          id: "af-2",
          kind: "autofocus",
          status: "failed",
          message: "Failed.",
          progress_current: null,
          progress_total: null,
          result: null,
          error: "AutofocusError: no measurable stars",
          idempotency_key: "request-2",
          cancellable: true,
          created_at: "2026-09-15T00:00:00Z",
          updated_at: "2026-09-15T00:01:00Z",
          finished_at: "2026-09-15T00:01:00Z",
        }],
      } as unknown as StateSnapshot,
      seq: 1,
    });

    renderWithQueryClient(<FocuserPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent("no measurable stars");
  });
});
