// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The "Until" review (user: "duration should be accompanied with 'until
 * event' and be possible to select e.g. astronomical dawn, or another
 * event"): each of the three `EndSpec` kinds round-trips to the schema's
 * shape, "Until target event" is offered only on a target-bearing item, and
 * clearing the Duration field commits `null` (instantaneous) without the
 * field itself vanishing mid-edit.
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { EndSpec } from "../../api/plans";
import { EndSpecPicker } from "./EndSpecPicker";

afterEach(cleanup);

function renderPicker(value: EndSpec | null, hasTarget = true) {
  const onChange = vi.fn<(end: EndSpec | null) => void>();
  render(<EndSpecPicker value={value} onChange={onChange} hasTarget={hasTarget} />);
  return onChange;
}

describe("EndSpecPicker", () => {
  it("round-trips each kind to the schema shape", async () => {
    const user = userEvent.setup();
    const onChange = renderPicker(null);

    await user.click(screen.getByRole("radio", { name: "Duration" }));
    expect(onChange).toHaveBeenLastCalledWith({ kind: "duration", minutes: 30 });

    await user.click(screen.getByRole("radio", { name: "Until event" }));
    expect(onChange).toHaveBeenLastCalledWith({ kind: "event", event: "sunset", offset_min: 0 });

    await user.click(screen.getByRole("radio", { name: "Until target event" }));
    expect(onChange).toHaveBeenLastCalledWith({ kind: "target_event", event: "sets_below", alt_deg: 30, offset_min: 0 });
  });

  it("offers 'Until target event' only on a target-bearing item", () => {
    renderPicker({ kind: "duration", minutes: 10 }, false);
    expect(screen.queryByRole("radio", { name: "Until target event" })).not.toBeInTheDocument();
    cleanup();
    renderPicker({ kind: "duration", minutes: 10 }, true);
    expect(screen.getByRole("radio", { name: "Until target event" })).toBeInTheDocument();
  });

  it("clearing the duration field commits null without hiding the field", async () => {
    const user = userEvent.setup();
    const onChange = renderPicker({ kind: "duration", minutes: 10 });
    const minutes = screen.getByLabelText("Duration (minutes)");
    await user.clear(minutes);
    expect(onChange).toHaveBeenLastCalledWith(null);
    // Still on screen — the section did not collapse under a now-null value,
    // so the field the user was mid-edit of does not vanish under them.
    expect(screen.getByLabelText("Duration (minutes)")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Duration" })).toHaveAttribute("aria-checked", "true");
  });

  it("changes the event and the offset for 'Until event' without dropping the other", async () => {
    const user = userEvent.setup();
    const onChange = renderPicker({ kind: "event", event: "sunset", offset_min: -15 });
    await user.selectOptions(screen.getByLabelText("Event"), "astronomical_dawn");
    expect(onChange).toHaveBeenLastCalledWith({ kind: "event", event: "astronomical_dawn", offset_min: -15 });
  });

  it("drops alt_deg for transits and restores it for rises/sets on 'Until target event'", async () => {
    const user = userEvent.setup();
    const onChange = renderPicker({ kind: "target_event", event: "sets_below", alt_deg: 20, offset_min: 0 });
    await user.selectOptions(screen.getByLabelText("Target event"), "transits");
    expect(onChange).toHaveBeenLastCalledWith({ kind: "target_event", event: "transits", alt_deg: null, offset_min: 0 });
  });

  it("shows a mosaic's duration as per panel when the caller says so", () => {
    render(
      <EndSpecPicker
        value={{ kind: "duration", minutes: 5 }}
        onChange={vi.fn()}
        hasTarget
        durationLabel="Duration per panel (minutes)"
      />,
    );
    expect(screen.getByLabelText("Duration per panel (minutes)")).toHaveValue(5);
  });
});
