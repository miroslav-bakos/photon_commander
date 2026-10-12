// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * §4.1's inline resolution: "Astronomical dusk +15 → 22:47", and — the case
 * that matters at 60° north in June — the compiler's own words when the
 * event does not occur, never a blank.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { CompiledItem } from "../../api/plans";
import { ResolvedTimeLabel } from "./ResolvedTimeLabel";

afterEach(cleanup);

function compiled(overrides: Partial<CompiledItem>): CompiledItem {
  return {
    item_id: "i1",
    kind: "goto_and_stack",
    enabled: true,
    start_utc: "2026-08-28T20:47:00Z",
    end_utc: "2026-08-28T22:47:00Z",
    resolution: "Astronomical dusk +15 → 22:47",
    end_resolution: "Duration 120 min → 22:47",
    panels: [],
    diagnostics: [],
    ...overrides,
  };
}

describe("ResolvedTimeLabel", () => {
  it("renders the compiler's label with the start as a local clock", () => {
    render(<ResolvedTimeLabel state={{ status: "compiled", item: compiled({}) }} />);
    const label = screen.getByTestId("resolved-time");
    expect(label).toHaveTextContent(/^Astronomical dusk \+15 → \d{2}:\d{2}$/);
  });

  it("shows the unschedulable diagnostic instead of a blank", () => {
    render(
      <ResolvedTimeLabel
        state={{
          status: "compiled",
          item: compiled({
            start_utc: null,
            end_utc: null,
            resolution: "Astronomical dusk +15 → unresolved",
            diagnostics: [
              {
                severity: "error",
                item_id: "i1",
                code: "event_does_not_occur",
                message: "astronomical dusk does not occur on this date at this latitude",
              },
            ],
          }),
        }}
      />,
    );
    expect(screen.getByText(/astronomical dusk does not occur on this date at this latitude/)).toBeInTheDocument();
    expect(screen.getByText(/→ unschedulable/)).toBeInTheDocument();
  });

  it("says when the compile itself failed, in the server's words", () => {
    render(<ResolvedTimeLabel state={{ status: "error", message: "no observing location is set" }} />);
    expect(screen.getByText(/no observing location is set/)).toBeInTheDocument();
  });

  it("shows the end's own resolution and clock when field is \"end\"", () => {
    render(<ResolvedTimeLabel state={{ status: "compiled", item: compiled({}) }} field="end" />);
    const label = screen.getByTestId("resolved-time-end");
    expect(label).toHaveTextContent(/^Duration 120 min → \d{2}:\d{2}$/);
  });
});
