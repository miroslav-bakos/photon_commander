// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Two properties DESIGN.md §4.1 makes non-negotiable: every event the
 * schema enumerates is offered — civil, nautical *and* astronomical, both
 * dusk and dawn — and each of the five kinds round-trips to exactly the
 * schema's shape. Plus the DST fold: shown only on an ambiguous reading.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TimeSpec } from "../../api/plans";
import { TimeSpecPicker } from "./TimeSpecPicker";
import { defaultTimeSpec, EVENT_LABELS, LUNAR_EVENTS, SOLAR_EVENTS } from "./timeSpecs";

afterEach(cleanup);

function renderPicker(value: TimeSpec, siteTimeZone = "Europe/London") {
  const onChange = vi.fn<(spec: TimeSpec) => void>();
  render(<TimeSpecPicker value={value} onChange={onChange} hasTarget siteTimeZone={siteTimeZone} />);
  return onChange;
}

describe("TimeSpecPicker", () => {
  it("offers every solar and lunar event, all three twilights included", () => {
    renderPicker({ kind: "event", event: "sunset", offset_min: 0 });
    const select = screen.getByLabelText("Event");
    const values = within(select)
      .getAllByRole("option")
      .map((o) => (o as HTMLOptionElement).value);
    for (const name of [...SOLAR_EVENTS, ...LUNAR_EVENTS]) expect(values).toContain(name);
    expect(values).toEqual(expect.arrayContaining(["civil_dusk", "nautical_dusk", "astronomical_dusk"]));
    expect(values).toEqual(expect.arrayContaining(["civil_dawn", "nautical_dawn", "astronomical_dawn"]));
    expect(values).toContain("astronomical_midnight");
    expect(values).toHaveLength(13);
    expect(within(select).getByRole("option", { name: EVENT_LABELS.nautical_dusk })).toBeInTheDocument();
  });

  it("round-trips each kind to the schema shape", async () => {
    const user = userEvent.setup();
    const onChange = renderPicker({ kind: "asap" });

    await user.click(screen.getByRole("radio", { name: "Absolute" }));
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ kind: "absolute", fold: 0, local_datetime: expect.stringMatching(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/) as string }),
    );

    await user.click(screen.getByRole("radio", { name: "Sun / Moon event" }));
    expect(onChange).toHaveBeenLastCalledWith({ kind: "event", event: "sunset", offset_min: 0 });

    await user.click(screen.getByRole("radio", { name: "Target event" }));
    expect(onChange).toHaveBeenLastCalledWith({ kind: "target_event", event: "rises_above", alt_deg: 30, offset_min: 0 });

    await user.click(screen.getByRole("radio", { name: "After previous" }));
    expect(onChange).toHaveBeenLastCalledWith({ kind: "relative", offset_min: 0 });

    // Already ASAP: re-selecting the current kind must not reset anything.
    const calls = onChange.mock.calls.length;
    await user.click(screen.getByRole("radio", { name: "ASAP" }));
    expect(onChange.mock.calls.length).toBe(calls);
    expect(defaultTimeSpec("asap")).toEqual({ kind: "asap" });
  });

  it("changes the event and the offset without dropping the other", async () => {
    const user = userEvent.setup();
    const onChange = renderPicker({ kind: "event", event: "sunset", offset_min: 15 });
    await user.selectOptions(screen.getByLabelText("Event"), "astronomical_dusk");
    expect(onChange).toHaveBeenLastCalledWith({ kind: "event", event: "astronomical_dusk", offset_min: 15 });
  });

  it("drops alt_deg for transits and restores it for rises/sets", async () => {
    const user = userEvent.setup();
    const onChange = renderPicker({ kind: "target_event", event: "rises_above", alt_deg: 25, offset_min: 0 });
    await user.selectOptions(screen.getByLabelText("Target event"), "transits");
    expect(onChange).toHaveBeenLastCalledWith({ kind: "target_event", event: "transits", alt_deg: null, offset_min: 0 });
  });

  it("shows the first/second occurrence choice only for an ambiguous local time", () => {
    renderPicker({ kind: "absolute", local_datetime: "2026-10-25T01:30:00", fold: 0 });
    expect(screen.getByRole("radiogroup", { name: "Which occurrence" })).toBeInTheDocument();
    cleanup();
    renderPicker({ kind: "absolute", local_datetime: "2026-09-07T22:00:00", fold: 0 });
    expect(screen.queryByRole("radiogroup", { name: "Which occurrence" })).not.toBeInTheDocument();
  });

  it("maps the second occurrence to fold=1", async () => {
    const user = userEvent.setup();
    const onChange = renderPicker({ kind: "absolute", local_datetime: "2026-10-25T01:30:00", fold: 0 });
    await user.click(screen.getByRole("radio", { name: /second/i }));
    expect(onChange).toHaveBeenLastCalledWith({ kind: "absolute", local_datetime: "2026-10-25T01:30:00", fold: 1 });
  });

  it("names the spring gap rather than hiding it", () => {
    renderPicker({ kind: "absolute", local_datetime: "2026-03-29T01:30:00", fold: 0 });
    expect(screen.getByText(/does not exist at the site/i)).toBeInTheDocument();
  });
});
