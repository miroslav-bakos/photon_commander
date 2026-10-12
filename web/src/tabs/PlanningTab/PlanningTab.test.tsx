// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * A render smoke test, not a behaviour suite: the goal is catching "the
 * component tree throws" (a bad hook order, a `noUncheckedIndexedAccess`
 * miss `tsc` didn't catch because a mock hid it, a prop mismatch across the
 * `PlanningTab`/`SessionControls`/`TimeScrubber`/etc. boundary) before a
 * real browser does. `SkyMap` is mocked out — its own `<iframe>`/postMessage
 * machinery is exercised by `VectorSkyMapBackend.test.ts` and the
 * conformance suite, not this file.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StateSnapshot } from "../../api/types";
import { testSnapshot } from "../../test/fixtures";
import { PlanningTab } from "./PlanningTab";
import { usePlanningUiStore } from "./planningUiStore";
import { renderWithQueryClient } from "../../test/queryClient";

const { mockSkyMapGoto, mockSkyMapSetOverlay } = vi.hoisted(() => ({
  mockSkyMapGoto: vi.fn(() => Promise.resolve()),
  mockSkyMapSetOverlay: vi.fn(),
}));

vi.mock("../../skymap/SkyMap", () => ({
  SkyMap: ({
    instant,
    onBackendReady,
  }: {
    instant?: Date | null;
    onBackendReady?: (backend: {
      onViewChange: (callback: (view: { fovDeg: number }) => void) => void;
      onFovMoved: (callback: (raDeg: number, decDeg: number) => void) => void;
      goto: (raDeg: number, decDeg: number, fovDeg: number) => Promise<void>;
      setOverlay: (shapes: unknown[]) => void;
    }) => void;
  }) => (
    <div data-testid="skymap-stub" data-instant={instant?.toISOString() ?? "none"}>
      <button
        type="button"
        onClick={() => {
          let moved: ((raDeg: number, decDeg: number) => void) | undefined;
          onBackendReady?.({
            onViewChange: (callback) => { callback({ fovDeg: 4.25 }); },
            onFovMoved: (callback) => {
              moved = callback;
            },
            goto: mockSkyMapGoto,
            setOverlay: mockSkyMapSetOverlay,
          });
          moved?.(150, -12.3);
        }}
      >
        Move blue frame
      </button>
    </div>
  ),
}));

vi.mock("../../skymap/MountSlewSyncControls", () => ({
  MountSlewSyncControls: ({ target, toolbar }: { target: { label: string; raHoursJ2000: number; decDegJ2000: number }; toolbar?: boolean }) => (
    <output
      data-testid="slew-center-target"
      data-label={target.label}
      data-ra-hours={target.raHoursJ2000}
      data-dec-deg={target.decDegJ2000}
      data-toolbar={String(toolbar === true)}
    />
  ),
}));

const mockUseConnectionStore = vi.fn<(selector: (s: { snapshot: StateSnapshot | null }) => unknown) => unknown>();
vi.mock("../../store/connectionStore", () => ({
  useConnectionStore: (selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
    mockUseConnectionStore(selector),
}));

const renderWithClient = renderWithQueryClient;

const withLocationSnapshot = testSnapshot({
  location: {
    latitude_deg: 51.5,
    longitude_deg: -0.1,
    elevation_m: 20,
    timezone: "Europe/London",
    source: "manual",
    accuracy_m: null,
    acquired_at: "2026-08-28T00:00:00Z",
  },
});

describe("PlanningTab", () => {
  // `planningUiStore` is a module-level singleton (deliberately — that is
  // the whole fix), so state set by one test's interactions must not leak
  // into the next. Real navigation away from Planning never resets it
  // either, which is exactly the point.
  afterEach(() => {
    usePlanningUiStore.setState(usePlanningUiStore.getInitialState());
    mockSkyMapGoto.mockClear();
    mockSkyMapSetOverlay.mockClear();
  });

  it("starts a fresh Planning session with 20 percent mosaic overlap", () => {
    expect(usePlanningUiStore.getInitialState().mosaicSpec).toEqual({ raNum: 1, decNum: 1, overlapPct: 20 });
  });

  it("opens a morning reload on today's upcoming evening", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 8, 18, 8, 30));
    try {
      mockUseConnectionStore.mockImplementation((selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
        selector({ snapshot: withLocationSnapshot }),
      );
      renderWithClient(<MemoryRouter><PlanningTab /></MemoryRouter>);
      expect(screen.getByLabelText("Night of")).toHaveValue("2026-09-18");
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a location banner instead of the planner when no location is set", () => {
    mockUseConnectionStore.mockImplementation((selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
      selector({ snapshot: testSnapshot({ location: null }) }),
    );

    renderWithClient(
      <MemoryRouter>
        <PlanningTab />
      </MemoryRouter>,
    );

    expect(screen.getByText(/no observing location is set/i)).toBeInTheDocument();
    expect(screen.queryByTestId("skymap-stub")).not.toBeInTheDocument();
  });

  it("opens mosaic configuration from the sky-map toolbar", () => {
    mockUseConnectionStore.mockImplementation((selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
      selector({
        snapshot: testSnapshot({
          location: {
            latitude_deg: 51.5,
            longitude_deg: -0.1,
            elevation_m: 20,
            timezone: "Europe/London",
            source: "manual",
            accuracy_m: null,
            acquired_at: "2026-08-28T00:00:00Z",
          },
        }),
      }),
    );
    // Absolute mode explicitly: a fresh session now defaults to
    // event-relative on astronomical dusk (planningUiStore.ts), which cannot
    // resolve to an instant until `/api/astro/night` answers — and this file
    // mocks no fetch, so the scrubber would never appear. The scrubber's own
    // prop boundary is what this smoke test exists to exercise, so it pins
    // the mode rather than dropping the assertion. The default is covered by
    // its own test below.
    usePlanningUiStore.setState({ startMode: "absolute", mount: "altaz" });

    const { container } = renderWithClient(
      <MemoryRouter>
        <PlanningTab />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("skymap-stub")).toBeInTheDocument();
    // The map is a direct flex item of the center pane. An extra full-height
    // wrapper would put a saved/portaled timeline below the map again.
    expect(screen.getByTestId("planning-skymap").parentElement).toHaveClass("contents");
    expect(screen.getByTestId("planning-skymap")).toHaveClass("order-last");
    expect(screen.getByText("Target")).toBeInTheDocument();
    const toolbar = screen.getByRole("toolbar", { name: /planning controls/i });
    expect(screen.getByTestId("planning-skymap")).toContainElement(toolbar);
    expect(toolbar).toHaveClass("bg-surface/80");
    expect(toolbar).toHaveClass("ssc-map-toolbar", "rounded-full");
    expect(screen.queryByLabelText(/desired deviation/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /planning equipment settings/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /choose skymap fov profile/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /planning time settings/i })).not.toBeInTheDocument();
    const mosaicButton = within(toolbar).getByRole("button", { name: /configure mosaic/i });
    const mountMode = within(toolbar).getByRole("radiogroup", { name: /mount mode/i });
    expect(within(mountMode).getByRole("radio", { name: /alt\/az/i })).toBeChecked();
    expect(within(mountMode).getByRole("radio", { name: /^eq$/i })).not.toBeChecked();
    const scheduling = screen.getByTestId("planning-scheduling");
    expect(within(scheduling).getByRole("button", { name: "Schedule" })).toBeDisabled();
    expect(within(scheduling).getByText(/resolve a target first/i)).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /mosaic configuration/i })).not.toBeInTheDocument();
    fireEvent.click(mosaicButton);
    const mosaicDialog = screen.getByRole("dialog", { name: /mosaic configuration/i });
    expect(within(mosaicDialog).getByLabelText(/ra panels/i)).toHaveValue(1);
    expect(within(mosaicDialog).getByLabelText(/dec panels/i)).toHaveValue(1);
    expect(within(mosaicDialog).getByLabelText(/overlap %/i)).toHaveValue(20);
    // No target chosen yet: the detail pane says so rather than showing a
    // half-populated stats panel.
    expect(screen.getByText(/a resolved target/i)).toBeInTheDocument();
    // DESIGN.md §4.1: session start is a TimeSpec (absolute or
    // event-relative), and the night summary is visible without a target.
    expect(screen.getByRole("radio", { name: /absolute/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /event-relative/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/session length/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/instant/i)).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /planning equipment/i })).not.toBeInTheDocument();

    fireEvent.change(within(mosaicDialog).getByLabelText(/ra panels/i), { target: { value: "2" } });
    expect(usePlanningUiStore.getState().mosaicSpec).toEqual({ raNum: 2, decNum: 1, overlapPct: 20 });
    expect(within(mosaicDialog).getByLabelText(/ra panels/i)).toHaveValue(2);
    fireEvent.click(within(mosaicDialog).getByRole("button", { name: /close mosaic configuration/i }));
    expect(screen.queryByRole("dialog", { name: /mosaic configuration/i })).not.toBeInTheDocument();
    expect(screen.getByText("Night summary")).toBeInTheDocument();
    const detailContents = container.querySelector('[data-layout-panel-target="detail"] > .contents');
    expect(detailContents).not.toBeNull();
    expect(detailContents?.querySelector(':scope > .flex.flex-col.gap-4')).not.toBeInTheDocument();
  });

  // Reported live, twice: "make the astronomical dusk the default option
  // selected", then "Session start does not start by default from
  // astronomical dusk" after only `startEventName` had been defaulted. Both
  // halves are asserted here, because setting either one alone leaves the
  // session starting at whenever the tab was opened.
  it("starts a fresh session on astronomical dusk, event-relative", () => {
    mockUseConnectionStore.mockImplementation((selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
      selector({ snapshot: withLocationSnapshot }),
    );

    renderWithClient(
      <MemoryRouter>
        <PlanningTab />
      </MemoryRouter>,
    );

    expect(screen.getByRole("radio", { name: /event-relative/i })).toBeChecked();
    expect(screen.getByRole("combobox", { name: /event/i })).toHaveValue("astronomical_dusk");
    // No fetch mock here, so the night's events never arrive: the honest
    // "still working it out" state, not the "fix your event pick" message
    // that would be pointing at something which is not wrong.
    expect(screen.getByText(/working out tonight's twilight times/i)).toBeInTheDocument();
    expect(screen.queryByText(/does not resolve to a time/i)).not.toBeInTheDocument();
  });

  it("keeps a resolved target and the rest of the session setup across an unmount and remount (a tab switch, DESIGN.md's own single-authoritative-state rule applied to client-only UI state)", async () => {
    const user = userEvent.setup();
    mockUseConnectionStore.mockImplementation((selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
      selector({ snapshot: withLocationSnapshot }),
    );

    const first = renderWithClient(
      <MemoryRouter>
        <PlanningTab />
      </MemoryRouter>,
    );

    // Resolve a target via the manual RA/Dec fallback — no network mock
    // needed, `ManualCoordinates` hands the target straight to `PlanningTab`.
    await user.click(screen.getByText(/or enter ra\/dec directly/i));
    await user.type(screen.getByLabelText(/ra \(hours\)/i), "5.5");
    await user.type(screen.getByLabelText(/dec \(deg\)/i), "-5.4");
    await user.click(screen.getByRole("button", { name: /use coordinates/i }));

    // And retain a second, independent equipment default configured in Settings.
    usePlanningUiStore.setState({ altitudeLimitDeg: 35 });

    expect(screen.getByText(/ra 5\.500h \/ dec -5\.400/i)).toBeInTheDocument();
    expect(usePlanningUiStore.getState().altitudeLimitDeg).toBe(35);

    // Simulate `<Routes>` unmounting the tab on navigation away, then back.
    first.unmount();

    const second = renderWithClient(
      <MemoryRouter>
        <PlanningTab />
      </MemoryRouter>,
    );

    expect(second.getByText(/ra 5\.500h \/ dec -5\.400/i)).toBeInTheDocument();
    expect(usePlanningUiStore.getState().altitudeLimitDeg).toBe(35);
  });

  it("reopens the sky map at the current time without resetting the rest of the planning setup", async () => {
    mockUseConnectionStore.mockImplementation((selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
      selector({ snapshot: withLocationSnapshot }),
    );
    const stale = new Date("2025-01-01T20:00:00.000Z");
    const openedAt = new Date();
    usePlanningUiStore.setState({
      startMode: "absolute",
      startAbsolute: stale,
      startAnchorDate: stale,
      instantSeconds: 7200,
      altitudeLimitDeg: 35,
    });

    renderWithClient(
      <MemoryRouter>
        <PlanningTab />
      </MemoryRouter>,
    );

    await waitFor(() => {
      const shown = new Date(screen.getByTestId("skymap-stub").dataset.instant ?? 0);
      expect(Math.abs(shown.getTime() - openedAt.getTime())).toBeLessThan(1000);
    });
    expect(usePlanningUiStore.getState().altitudeLimitDeg).toBe(35);

    const deliberate = "2026-09-12T23:30";
    fireEvent.change(screen.getByLabelText(/absolute time/i), { target: { value: deliberate } });
    await waitFor(() => {
      expect(screen.getByTestId("skymap-stub").dataset.instant).toBe(new Date(2026, 8, 12, 23, 30).toISOString());
    });
  });

  it("accepts the renderer-recentered frame drag without issuing a projection-resetting goto", async () => {
    mockUseConnectionStore.mockImplementation((selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
      selector({ snapshot: withLocationSnapshot }),
    );
    usePlanningUiStore.setState({
      startMode: "absolute",
      mount: "altaz",
      target: {
        name: "M45",
        raH: 3.791,
        decDeg: 24.105,
        magnitude: 1.6,
        sizeMajorArcmin: 110,
        positionAngleDeg: null,
        objectType: "open cluster",
      },
    });

    renderWithClient(
      <MemoryRouter>
        <PlanningTab />
      </MemoryRouter>,
    );

    mockSkyMapGoto.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Move blue frame" }));

    await waitFor(() => {
      const target = screen.getByTestId("slew-center-target");
      expect(target).toHaveAttribute("data-ra-hours", "10");
      expect(target).toHaveAttribute("data-dec-deg", "-12.3");
      expect(target).toHaveAttribute("data-label", "M45");
    });
    expect(screen.getByText("M45")).toBeInTheDocument();
    expect(screen.getByTestId("slew-center-target")).toHaveAttribute("data-toolbar", "true");
    expect(usePlanningUiStore.getState().target).toMatchObject({
      name: "M45",
      raH: 10,
      decDeg: -12.3,
      magnitude: 1.6,
    });
    expect(mockSkyMapGoto).not.toHaveBeenCalled();
    expect(usePlanningUiStore.getState().mount).toBe("altaz");
    expect(mockSkyMapSetOverlay).not.toHaveBeenCalledWith([]);
  });

  it("clears the EQ overlay immediately when switching to Alt/Az", () => {
    mockUseConnectionStore.mockImplementation((selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
      selector({ snapshot: withLocationSnapshot }),
    );
    usePlanningUiStore.setState({
      startMode: "absolute",
      mount: "eq",
      target: {
        name: "M31",
        raH: 0.712,
        decDeg: 41.269,
        magnitude: 3.4,
        sizeMajorArcmin: 190,
        positionAngleDeg: 35,
        objectType: "galaxy",
      },
    });
    renderWithClient(<MemoryRouter><PlanningTab /></MemoryRouter>);

    // Installs the mocked map backend. Its synthetic frame move is unrelated
    // to this assertion; clear those calls before changing coordinate mode.
    fireEvent.click(screen.getByRole("button", { name: "Move blue frame" }));
    mockSkyMapSetOverlay.mockClear();
    fireEvent.click(screen.getByRole("radio", { name: /alt\/az/i }));

    expect(mockSkyMapSetOverlay).toHaveBeenCalledWith([]);
    expect(usePlanningUiStore.getState().mount).toBe("altaz");
  });

  it("resets a cleared target's mosaic to one frame while retaining overlap", async () => {
    mockUseConnectionStore.mockImplementation((selector: (s: { snapshot: StateSnapshot | null }) => unknown) =>
      selector({ snapshot: withLocationSnapshot }),
    );
    usePlanningUiStore.setState({
      startMode: "absolute",
      mosaicSpec: { raNum: 3, decNum: 2, overlapPct: 35 },
      target: {
        name: "M45",
        raH: 3.791,
        decDeg: 24.105,
        magnitude: 1.6,
        sizeMajorArcmin: 110,
        positionAngleDeg: null,
        objectType: "open cluster",
      },
    });

    renderWithClient(
      <MemoryRouter>
        <PlanningTab />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Clear M45" }));
    await waitFor(() => {
      expect(usePlanningUiStore.getState().target).toBeNull();
      expect(usePlanningUiStore.getState().mosaicSpec).toEqual({ raNum: 1, decNum: 1, overlapPct: 35 });
    });
  });
});
