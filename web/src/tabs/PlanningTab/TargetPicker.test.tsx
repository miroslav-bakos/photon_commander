// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Reported live: "resolve button does not work properly for the same object
 * already resolved - it should re-resolve". Pressing Go a second time
 * with unchanged search text used to do nothing at all — the submitted-query
 * state update was a no-op (same string), so the query key never changed,
 * nothing refetched, and the effect that pushes the resolved target upwards
 * never re-fired. If the target had since been cleared, it stayed cleared.
 *
 * Exercised through the real `useAstroTargetQuery` with only `fetch` stubbed
 * (`client.ts`'s own doc comment: swap the global, keep the layer that
 * actually breaks), because the bug lives precisely in TanStack Query's
 * caching behaviour — a mocked hook would test nothing of it.
 */
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AstroBodyModel, CometSuggestionModel, TargetResolutionResponse } from "../../api/astro";
import { renderWithQueryClient } from "../../test/queryClient";
import { TargetPicker } from "./TargetPicker";
import type { PlanningTarget } from "./types";

afterEach(cleanup);

const M31: TargetResolutionResponse = {
  name: "M31",
  designations: ["M31", "NGC 224"],
  common_name: "Andromeda Galaxy",
  object_type: "galaxy",
  ra_j2000_h: 0.7123,
  dec_j2000_deg: 41.269,
  magnitude: 3.4,
  size_major_arcmin: 189.1,
  size_minor_arcmin: 61.7,
  position_angle_deg: 35,
  constellation: "And",
  source: "Messier",
  sources: ["Messier catalogue"],
};

const SPIRAL_M31: TargetResolutionResponse = { ...M31, object_type: "galaxy_spiral" };

const M31_TARGET: PlanningTarget = {
  name: M31.name,
  raH: M31.ra_j2000_h,
  decDeg: M31.dec_j2000_deg,
  magnitude: M31.magnitude,
  sizeMajorArcmin: M31.size_major_arcmin,
  positionAngleDeg: M31.position_angle_deg,
  objectType: M31.object_type,
};

const MARS: AstroBodyModel = {
  id: "mars",
  name: "Mars",
  kind: "planet",
  ra_j2000_h: 8.25,
  dec_j2000_deg: 20.5,
  apparent_diameter_arcmin: 0.14,
  illuminated_fraction: 0.91,
  phase_angle_deg: 24,
};

const HALLEY: CometSuggestionModel = {
  spk_id: 1000036,
  designation: "1P",
  name: "Halley",
  full_name: "1P/Halley",
  ra_j2000_h: 8.5,
  dec_j2000_deg: 2.25,
  resolved_at: "2026-09-13T20:00:00Z",
  element_epoch_jd_tdb: 2439875.5,
  element_age_days: 21421,
  solution_date: "2025-11-21 15:57:34",
  execution_safe: false,
  execution_rejection: "orbit elements are too old for execution",
};

/** A real `Response`, for the reasons `TonightWidget.test.tsx` documents.
 * Returns the same body every time and counts the calls — the count is the
 * assertion: a second Go must reach the server again. */
function requestPath(input: RequestInfo | URL): string {
  return new URL(input instanceof Request ? input.url : String(input), "http://localhost").pathname;
}

function stubFetchCounting(
  suggestionResults: TargetResolutionResponse[] = [],
  bodyResults: AstroBodyModel[] = [],
  cometResults: CometSuggestionModel[] = [],
): { calls: () => number; paths: () => string[] } {
  let targetCalls = 0;
  const requestedPaths: string[] = [];
  const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const path = requestPath(input);
    requestedPaths.push(path);
    const isSuggestions = path.endsWith("/target-suggestions");
    const isBodies = path.endsWith("/bodies");
    const isComets = path.endsWith("/comet-suggestions");
    if (!isSuggestions && !isBodies && !isComets) targetCalls += 1;
    const payload = isSuggestions
      ? { suggestions: suggestionResults }
      : isBodies
        ? { instant: "2026-09-13T20:00:00Z", bodies: bodyResults, credit: "JPL" }
        : isComets
          ? {
              comets: cometResults,
              unavailable: [],
              source: "NASA/JPL Small-Body Dynamics",
              source_url: "https://ssd-api.jpl.nasa.gov/sbdb_query.api?fields=test",
              snapshot_fetched_at: "2026-09-13T19:00:00Z",
              snapshot_age_days: 0.05,
              stale: false,
              accuracy_note: "Two-body preview; scheduling is not yet enabled.",
            }
        : M31;
    const response = new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    Object.defineProperty(response, "url", {
      value: `http://localhost/api/astro/${isSuggestions ? "target-suggestions" : isBodies ? "bodies" : isComets ? "comet-suggestions" : "target"}`,
    });
    return Promise.resolve(response);
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    calls: () => targetCalls,
    paths: () => requestedPaths,
  };
}

describe("TargetPicker", () => {
  it("places Clear beside Go when a target is selected", () => {
    stubFetchCounting();
    const onTargetChange = vi.fn<(target: PlanningTarget | null) => void>();

    renderWithQueryClient(<TargetPicker target={M31_TARGET} onTargetChange={onTargetChange} />);

    const go = screen.getByRole("button", { name: "Go" });
    const clear = screen.getByRole("button", { name: "Clear M31" });
    expect(go.closest("form")).toContainElement(clear);
    expect(go).toHaveTextContent(/^$/);
    expect(clear).toHaveTextContent(/^$/);
    expect(go).toHaveAttribute("title", "Resolve and go to target");
    expect(clear).toHaveAttribute("title", "Clear target");
    fireEvent.click(clear);
    expect(onTargetChange).toHaveBeenCalledWith(null);
  });

  it("re-resolves when Go is pressed again with the same search text", async () => {
    const user = userEvent.setup();
    const fetches = stubFetchCounting();
    const onTargetChange = vi.fn<(target: PlanningTarget | null) => void>();

    renderWithQueryClient(<TargetPicker target={null} onTargetChange={onTargetChange} />);

    await user.type(screen.getByLabelText("Search targets"), "M31");
    await user.click(screen.getByRole("button", { name: "Go" }));

    await waitFor(() => {
      expect(onTargetChange).toHaveBeenCalledTimes(1);
    });
    expect(fetches.calls()).toBe(1);
    expect(onTargetChange.mock.calls[0]?.[0]?.name).toBe("M31");

    // The same text, untouched, pressed again: this is the reported case.
    await user.click(screen.getByRole("button", { name: "Go" }));

    await waitFor(() => {
      expect(fetches.calls()).toBe(2);
    });
    await waitFor(() => {
      expect(onTargetChange).toHaveBeenCalledTimes(2);
    });
    expect(onTargetChange.mock.calls[1]?.[0]?.name).toBe("M31");
  });

  it("still resolves normally when the search text changes", async () => {
    const user = userEvent.setup();
    const fetches = stubFetchCounting();
    const onTargetChange = vi.fn<(target: PlanningTarget | null) => void>();

    renderWithQueryClient(<TargetPicker target={null} onTargetChange={onTargetChange} />);

    const field = screen.getByLabelText("Search targets");
    await user.type(field, "M31");
    await user.click(screen.getByRole("button", { name: "Go" }));
    await waitFor(() => {
      expect(onTargetChange).toHaveBeenCalledTimes(1);
    });

    await user.clear(field);
    await user.type(field, "M42");
    await user.click(screen.getByRole("button", { name: "Go" }));

    await waitFor(() => {
      expect(fetches.calls()).toBe(2);
    });
    await waitFor(() => {
      expect(onTargetChange).toHaveBeenCalledTimes(2);
    });
  });

  it("does nothing when the search box is empty", async () => {
    const user = userEvent.setup();
    const fetches = stubFetchCounting();
    const onTargetChange = vi.fn<(target: PlanningTarget | null) => void>();

    renderWithQueryClient(<TargetPicker target={null} onTargetChange={onTargetChange} />);

    await user.click(screen.getByRole("button", { name: "Go" }));

    expect(fetches.calls()).toBe(0);
    expect(onTargetChange).not.toHaveBeenCalled();
  });

  it("shows suggestions and selects one with a touch pointer", async () => {
    const user = userEvent.setup();
    stubFetchCounting([M31]);
    const onTargetChange = vi.fn<(target: PlanningTarget | null) => void>();

    renderWithQueryClient(<TargetPicker target={null} onTargetChange={onTargetChange} />);

    await user.type(screen.getByRole("combobox", { name: "Search targets" }), "Andr");
    const option = await screen.findByRole("option", { name: /Andromeda Galaxy/i });
    expect(option.className).toContain("min-h-[44px]");
    const listbox = screen.getByRole("listbox", { name: "Target suggestions" });
    expect(listbox.parentElement?.tagName).toBe("FORM");
    expect(listbox).toHaveClass("inset-x-0");
    expect(option.querySelectorAll(".break-words")).toHaveLength(2);
    expect(option.querySelector(".truncate")).toBeNull();
    await user.pointer({ target: option, keys: "[TouchA]" });

    await waitFor(() => {
      expect(onTargetChange).toHaveBeenCalledTimes(1);
    });
    expect(onTargetChange.mock.calls[0]?.[0]?.name).toBe("M31");
  });

  it("shows human-readable observing classes instead of catalogue type keys", async () => {
    const user = userEvent.setup();
    stubFetchCounting([SPIRAL_M31]);
    const target = { ...M31_TARGET, objectType: "reflection_nebula" };

    renderWithQueryClient(<TargetPicker target={target} onTargetChange={vi.fn()} />);

    expect(screen.getByText("Reflection nebula")).toBeInTheDocument();
    expect(screen.queryByText("reflection_nebula")).not.toBeInTheDocument();
    await user.type(screen.getByRole("combobox", { name: "Search targets" }), "Andr");
    const option = await screen.findByRole("option", { name: /Andromeda Galaxy/i });
    expect(option).toHaveTextContent("Galaxy");
    expect(option).not.toHaveTextContent("galaxy_spiral");
  });

  it("supports arrow-key selection", async () => {
    const user = userEvent.setup();
    stubFetchCounting([M31]);
    const onTargetChange = vi.fn<(target: PlanningTarget | null) => void>();

    renderWithQueryClient(<TargetPicker target={null} onTargetChange={onTargetChange} />);
    const field = screen.getByRole("combobox", { name: "Search targets" });
    await user.type(field, "Andr");
    await screen.findByRole("option", { name: /Andromeda Galaxy/i });
    await user.keyboard("{ArrowDown}{Enter}");

    await waitFor(() => {
      expect(onTargetChange).toHaveBeenCalledTimes(1);
    });
    expect(field).toHaveValue("M31");
    expect(field).toHaveAttribute("aria-expanded", "false");
  });

  it("announces a clear no-result state", async () => {
    const user = userEvent.setup();
    stubFetchCounting([]);
    renderWithQueryClient(<TargetPicker target={null} onTargetChange={vi.fn()} />);

    await user.type(screen.getByRole("combobox", { name: "Search targets" }), "xyzzy");
    expect(await screen.findByText("No target matches")).toBeInTheDocument();
  });

  it("selects a moving body and retains its identity", async () => {
    const user = userEvent.setup();
    stubFetchCounting([], [MARS]);
    const onTargetChange = vi.fn<(target: PlanningTarget | null) => void>();
    renderWithQueryClient(
      <TargetPicker
        target={null}
        onTargetChange={onTargetChange}
        instant={new Date("2026-09-13T20:00:00Z")}
      />,
    );

    await user.type(screen.getByRole("combobox", { name: "Search targets" }), "Mar");
    const option = await screen.findByRole("option", { name: /Mars.*Moving target/i });
    await user.click(option);

    expect(onTargetChange).toHaveBeenCalledWith(
      expect.objectContaining({ bodyId: "mars", name: "Mars", raH: 8.25, decDeg: 20.5 }),
    );
  });

  it("does not request or show comet suggestions", async () => {
    const user = userEvent.setup();
    const fetches = stubFetchCounting([], [], [HALLEY]);
    const onTargetChange = vi.fn<(target: PlanningTarget | null) => void>();
    renderWithQueryClient(
      <TargetPicker
        target={null}
        onTargetChange={onTargetChange}
        instant={new Date("2026-09-13T20:00:00Z")}
      />,
    );

    await user.type(screen.getByRole("combobox", { name: "Search targets" }), "Haly");
    expect(await screen.findByText("No target matches")).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Halley/i })).not.toBeInTheDocument();
    expect(fetches.paths().some((path) => path.endsWith("/comet-suggestions"))).toBe(false);
    expect(onTargetChange).not.toHaveBeenCalled();
  });
});
