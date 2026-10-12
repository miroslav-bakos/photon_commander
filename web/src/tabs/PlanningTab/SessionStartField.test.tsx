// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Behaviour that matters most here: the resolved-time inline display
 * (DESIGN.md §4.1: "the abstraction never hides what will actually happen")
 * and the high-latitude "does not occur" case (architecture policy: "the UI must say
 * ... rather than rendering a blank, an epoch date, or silently falling
 * back to another twilight").
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { EventTimeModel } from "../../api/astro";
import { resolveEventStart } from "./twilight";
import { SessionStartField } from "./SessionStartField";

// `globals: false` in vite.config.ts means `@testing-library/react`'s own
// auto-cleanup (which checks for a *global* `afterEach`) never registers —
// done explicitly here instead, since several tests below render more than
// once.
afterEach(cleanup);

const bratislavaEvents: EventTimeModel[] = [
  { event: "astronomical_dusk", when: "2026-08-28T19:36:43Z", outcome: "occurs", threshold_deg: -18 },
];

const tromsoEvents: EventTimeModel[] = [
  { event: "astronomical_dusk", when: null, outcome: "always_above", threshold_deg: -18 },
];

function baseProps() {
  return {
    mode: "event" as const,
    onModeChange: vi.fn(),
    absoluteLocalValue: "2026-08-28T22:00",
    onAbsoluteLocalChange: vi.fn(),
    anchorDateLocalValue: "2026-08-28",
    onAnchorDateLocalChange: vi.fn(),
    eventName: "astronomical_dusk" as const,
    onEventChange: vi.fn(),
    offsetMin: 15,
    onOffsetChange: vi.fn(),
    isError: false,
  };
}

describe("SessionStartField", () => {
  it("shows the resolved absolute time inline for an occurring event", () => {
    render(
      <SessionStartField
        {...baseProps()}
        solarEvents={bratislavaEvents}
        resolution={resolveEventStart(bratislavaEvents, "astronomical_dusk", 15)}
      />,
    );
    // 19:36:43Z + 15 min = 19:51:43Z; asserting on the "label +offset →"
    // shape rather than the exact localised clock string, which depends on
    // the test runner's time zone.
    expect(screen.getByText(/astronomical dusk \+15/i)).toBeInTheDocument();
    expect(screen.getByText("→", { exact: false })).toBeInTheDocument();
  });

  it("says the event does not occur, never a blank or epoch date, at high latitude in summer", () => {
    render(
      <SessionStartField
        {...baseProps()}
        solarEvents={tromsoEvents}
        resolution={resolveEventStart(tromsoEvents, "astronomical_dusk", 15)}
      />,
    );
    expect(screen.getByText(/does not occur — the sun stays/i)).toBeInTheDocument();
    expect(screen.queryByText("→", { exact: false })).not.toBeInTheDocument();
  });

  it("prompts for a choice before any event is selected, rather than defaulting to one", () => {
    render(
      <SessionStartField
        {...baseProps()}
        eventName={null}
        solarEvents={bratislavaEvents}
        resolution={resolveEventStart(bratislavaEvents, null, 0)}
      />,
    );
    expect(screen.getByText(/choose a solar event/i)).toBeInTheDocument();
  });

  it("offers civil, nautical and astronomical as equal options, not one narrowed default", () => {
    render(
      <SessionStartField
        {...baseProps()}
        solarEvents={bratislavaEvents}
        resolution={resolveEventStart(bratislavaEvents, "astronomical_dusk", 15)}
      />,
    );
    const select = screen.getByLabelText(/solar event/i);
    const optionText = Array.from(select.querySelectorAll("option")).map((o) => o.textContent ?? "");
    expect(optionText.some((t) => /civil dusk/i.test(t))).toBe(true);
    expect(optionText.some((t) => /nautical dusk/i.test(t))).toBe(true);
    expect(optionText.some((t) => /astronomical dusk/i.test(t))).toBe(true);
  });

  it("shows the absolute datetime-local field, and no event picker, in absolute mode", () => {
    render(
      <SessionStartField
        {...baseProps()}
        mode="absolute"
        solarEvents={undefined}
        resolution={{ status: "resolved", eventWhen: new Date(), when: new Date() }}
      />,
    );
    expect(screen.getByLabelText(/absolute time/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/solar event/i)).not.toBeInTheDocument();
  });

  it("asks to switch mode when the event-relative toggle is clicked from absolute mode", async () => {
    const user = userEvent.setup();
    const onModeChange = vi.fn();
    const { container } = render(
      <SessionStartField
        {...baseProps()}
        mode="absolute"
        onModeChange={onModeChange}
        solarEvents={undefined}
        resolution={{ status: "resolved", eventWhen: new Date(), when: new Date() }}
      />,
    );
    await user.click(within(container).getByRole("radio", { name: /event-relative/i }));
    expect(onModeChange).toHaveBeenCalledWith("event");
  });
});
