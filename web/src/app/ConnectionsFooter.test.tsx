// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectionsFooter } from "./ConnectionsFooter";
import { useConnectionStore } from "../store/connectionStore";
import { testSnapshot } from "../test/fixtures";
import type { AlpacaDeviceStatus, DevicesState, StateSnapshot } from "../api/types";
import type { MountState } from "../api/mount";
import type { CaptureState } from "../api/capture";
import { useLiveStackQuery, useLiveStackRtspQuery } from "../api/capture";
import type { PolarAlignmentState } from "../api/polarAlignment";
import { useFilterWheelQuery } from "../api/filterwheel";
import { useAutofocusStateQuery, useFocuserPositionQuery } from "../api/focuser";
import { useHeaterQuery } from "../api/heater";
import { useMountCenterProgressQuery } from "../api/mount";
import { useLayoutStore } from "../layout/layoutStore";
import { useStreamPipStore } from "../store/streamPipStore";

const runningBuild = vi.hoisted((): { version: string | null | undefined } => ({ version: "0.0.3-test" }));
vi.mock("@tanstack/react-query", () => ({ useQuery: () => ({ data: { version: runningBuild.version } }) }));

vi.mock("../api/filterwheel", () => ({
  useFilterWheelQuery: vi.fn(),
  isFilterWheelDisconnected: (state: {
    names_note?: string | null;
    position_note?: string | null;
  }) => [state.names_note, state.position_note].some((note) => note?.includes("not connected") === true),
}));
vi.mock("../api/focuser", () => ({ useFocuserPositionQuery: vi.fn(), useAutofocusStateQuery: vi.fn() }));
vi.mock("../api/heater", () => ({ useHeaterQuery: vi.fn() }));
vi.mock("../api/capture", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api/capture")>(),
  useLiveStackQuery: vi.fn(),
  useLiveStackRtspQuery: vi.fn(),
}));
vi.mock("../api/mount", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api/mount")>(),
  useMountCenterProgressQuery: vi.fn(),
}));
vi.mock("./FullscreenToggle", () => ({
  FullscreenToggle: () => <button type="button" aria-label="Enter fullscreen">Fullscreen</button>,
}));

afterEach(cleanup);

beforeEach(() => {
  runningBuild.version = "0.0.3-test";
  useLayoutStore.setState({ unlocked: false, saved: { version: 1, tabs: {} } });
  useStreamPipStore.setState({ enabled: false });
  vi.mocked(useFilterWheelQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useFilterWheelQuery>);
  vi.mocked(useFocuserPositionQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useFocuserPositionQuery>);
  vi.mocked(useAutofocusStateQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useAutofocusStateQuery>);
  vi.mocked(useMountCenterProgressQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useMountCenterProgressQuery>);
  vi.mocked(useLiveStackQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useLiveStackQuery>);
  vi.mocked(useLiveStackRtspQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useLiveStackRtspQuery>);
  vi.mocked(useHeaterQuery).mockReturnValue({ data: undefined } as ReturnType<typeof useHeaterQuery>);
});

describe("ConnectionsFooter release changelog", () => {
  it("links a released version safely with a keyboard focus indicator", () => {
    runningBuild.version = "0.2.3";
    render(<ConnectionsFooter />);
    const link = screen.getByRole("link", { name: "Open release changelog" });
    expect(link).toHaveAttribute("href", "https://github.com/miroslav-bakos/photon_commander/releases/tag/v0.2.3");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("title", "Running version");
    expect(link).toHaveTextContent("0.2.3");
    link.focus();
    expect(link).toHaveFocus();
    expect(link).toHaveClass("focus-visible:outline-focus-ring");
  });

  it.each([undefined, null, "unknown", "dev", "0.2.3-dev", "0.2.3+local", "01.2.3"])(
    "keeps unavailable or unreleased version %s as plain text", (version) => {
      runningBuild.version = version;
      render(<ConnectionsFooter />);
      expect(screen.getByTitle("Running version")).toHaveTextContent(version ?? "—");
      expect(screen.queryByRole("link", { name: "Open release changelog" })).not.toBeInTheDocument();
    },
  );
});

function setDevices(devices: DevicesState | null): void {
  const snapshot: StateSnapshot = { ...testSnapshot(), devices };
  useConnectionStore.setState({ snapshot, status: "open" } as never);
}

/** `mount`/`capture` are hand-written types not yet in the generated
 * `StateSnapshot` (`api/mount.ts`/`api/capture.ts`'s own file headers), the
 * same reason `useMountState`/`useCaptureState` read them via a cast rather
 * than the generated field — so this helper builds the snapshot the same
 * defensive way rather than fighting the generated type here. */
function setMountAndCapture(mount: MountState | null, capture: CaptureState | null): void {
  const snapshot = { ...testSnapshot(), mount, capture };
  useConnectionStore.setState({ snapshot, status: "open" } as never);
}

function runningRefinement(stage: "capturing" | "solving"): PolarAlignmentState {
  return {
    status: "completed",
    run_kind: "measure",
    started_at: "2026-09-11T20:00:00Z",
    ended_at: "2026-09-11T20:01:00Z",
    current_point_index: null,
    preflight_reason: null,
    points: [],
    result: null,
    start: null,
    assessment: null,
    error: null,
    exposure_started_at: null,
    exposure_duration_s: null,
    refinement: {
      status: "running",
      started_at: "2026-09-11T20:01:00Z",
      ended_at: null,
      stage,
      exposure_started_at: stage === "capturing" ? "2026-09-11T20:01:01Z" : null,
      exposure_duration_s: stage === "capturing" ? 1 : null,
      iteration: 0,
      degenerate_count: 0,
      last_degenerate_reason: null,
      error: null,
      result: null,
    },
  };
}

function mountState(overrides: Partial<MountState> = {}): MountState {
  return {
    polled_at: "2026-09-11T20:00:00Z",
    stale: false,
    ra_hours: 5,
    dec_deg: 45,
    alt_deg: 60,
    az_deg: 120,
    sidereal_time_hours: 5,
    tracking: null,
    tracking_rate: null,
    at_park: null,
    at_home: null,
    slewing: null,
    equatorial_system: "topocentric",
    capabilities: null,
    ...overrides,
  };
}

function device(overrides: Partial<AlpacaDeviceStatus> = {}): AlpacaDeviceStatus {
  return {
    id: "telescope/0",
    kind: "telescope",
    number: 0,
    label: "Mount",
    connected: true,
    note: null,
    temperature_c: null,
    temperature_note: null,
    ...overrides,
  };
}

describe("ConnectionsFooter — backend without duplicate Alpaca roster", () => {
  beforeEach(() => {
    useConnectionStore.setState({ snapshot: null, status: "connecting", lastResync: null } as never);
  });

  it("shows the backend link's own state in words, not colour alone", () => {
    useConnectionStore.setState({ status: "open" } as never);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Connection to ssc-core: Live")).toBeInTheDocument();
    expect(screen.getByLabelText("Connections")).toHaveClass("bg-surface/75", "backdrop-blur-md");
    expect(screen.getByLabelText("Connections")).toHaveClass("ssc-connections-footer");
    expect(screen.getByLabelText("Connections")).not.toHaveClass("pb-[env(safe-area-inset-bottom)]");
    expect(screen.getByLabelText("Connections")).not.toHaveClass("border-t");
  });

  it("does not duplicate Alpaca device connection entries", () => {
    setDevices({
      polled_at: "2026-09-10T14:00:00Z",
      stale: true,
      items: [
        device({ id: "telescope/0", label: "Mount" }),
        device({ id: "camera/0", kind: "camera", label: "Camera (tele)" }),
        device({ id: "focuser/0", kind: "focuser", label: "Focuser (tele)" }),
        device({ id: "filterwheel/0", kind: "filterwheel", label: "Filter wheel" }),
        device({ id: "switch/0", kind: "switch", label: "Switch" }),
      ],
    });
    render(<ConnectionsFooter />);

    expect(screen.queryByTitle("Mount: connected — device list is stale")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Camera (tele): connected — device list is stale")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Focuser (tele): connected — device list is stale")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Filter wheel: connected — device list is stale")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Switch: connected — device list is stale")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Device list is stale")).not.toBeInTheDocument();
    expect(screen.getByTitle("Connection to ssc-core: Live")).toBeInTheDocument();
  });

  it("shows the primary Seestar sensor temperature with an explicit unit", () => {
    setDevices({
      polled_at: "2026-09-10T14:00:00Z",
      stale: false,
      items: [device({ kind: "camera", number: 0, label: "Camera (tele)", temperature_c: 24.85 })],
    });
    render(<ConnectionsFooter />);
    const temperature = screen.getByTitle("Seestar sensor temperature: 24.9°C");
    expect(temperature).toHaveTextContent("24.9°C");
    expect(temperature.className).toContain("text-fg-muted");
    expect(temperature.previousElementSibling).toHaveAttribute("title", "Dew heater state has not been read yet");
  });

  it("shows an honest unavailable sensor-temperature reading", () => {
    setDevices(null);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Seestar sensor temperature unavailable")).toHaveTextContent("—°C");
  });

  it("puts authoritative long-running progress immediately after sensor temperature", () => {
    const snapshot = testSnapshot({
      devices: {
        polled_at: "2026-09-10T14:00:00Z",
        stale: false,
        items: [device({ kind: "camera", number: 0, temperature_c: 25 })],
      },
    });
    useConnectionStore.setState({
      snapshot: {
        ...snapshot,
        capture: {
          in_progress: true,
          last_frame_id: null,
          last_captured_at: null,
          last_error: null,
          folder_configured: true,
          calibration: {
            status: "running", kind: "bias", phase: "integrating", requested_count: 100,
            filter_names: [], current_filter: null, completed_frames: 100, total_frames: 100,
            exposure_s: null, gain: 200, probes: [], generate_master: true, master_paths: [],
            removed_source_frames: 0, message: "Building master from 100 retained frames", error: null,
          },
        },
      },
      status: "open",
    } as never);

    render(<ConnectionsFooter />);
    const temperature = screen.getByTitle("Seestar sensor temperature: 25.0°C");
    const activity = temperature.nextElementSibling;
    expect(activity).toHaveTextContent("Building master from 100 retained frames · 100/100");
    expect(activity?.querySelector('[role="progressbar"]')).toHaveAttribute("aria-valuenow", "100");
  });

  it("locks layout editing by default and toggles it from the footer", () => {
    render(<ConnectionsFooter />);
    const button = screen.getByRole("button", { name: "Unlock card layout" });
    expect(button).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(button);
    expect(screen.getByRole("button", { name: "Lock card layout" })).toHaveAttribute("aria-pressed", "true");
  });

  it("toggles the application-wide stream preview from a compact footer icon", () => {
    vi.mocked(useLiveStackRtspQuery).mockReturnValue({
      data: { active: true, encoded_version: "wide:9", source: "wide_camera" },
    } as ReturnType<typeof useLiveStackRtspQuery>);
    render(<ConnectionsFooter />);

    const show = screen.getByRole("button", { name: "Show stream picture in picture" });
    expect(show).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(show);
    expect(screen.getByRole("button", { name: "Hide stream picture in picture" })).toHaveAttribute("aria-pressed", "true");
    expect(useStreamPipStore.getState().enabled).toBe(true);
  });

  it("disables the footer PiP trigger until an encoded stream frame exists", () => {
    render(<ConnectionsFooter />);
    expect(screen.getByRole("button", { name: "Stream picture in picture unavailable" })).toBeDisabled();
  });

  it("places fullscreen at the far-right end of the footer", () => {
    render(<ConnectionsFooter />);
    const fullscreen = screen.getByRole("button", { name: "Enter fullscreen" });
    expect(fullscreen.parentElement?.lastElementChild).toBe(fullscreen);
  });

});

describe("ConnectionsFooter — mount status word (bottom-left, owner brief 2026-09-11)", () => {
  beforeEach(() => {
    useConnectionStore.setState({ snapshot: null, status: "connecting", lastResync: null } as never);
  });

  it("renders nothing for mount while no snapshot has arrived yet", () => {
    render(<ConnectionsFooter />);
    expect(screen.queryByText(/Mount/)).not.toBeInTheDocument();
  });

  it("renders an honest placeholder when a snapshot exists but has no mount reading", () => {
    setMountAndCapture(null, null);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("No mount reading yet")).toBeInTheDocument();
  });

  it("prioritises slewing over every other true field", () => {
    setMountAndCapture(mountState({ slewing: true, at_park: true, at_home: true, tracking: true }), null);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Mount: Slewing").querySelector('[data-status-icon="slewing"]')).toBeInTheDocument();
  });

  it("prioritises parked over home and tracking once not slewing", () => {
    setMountAndCapture(mountState({ slewing: false, at_park: true, at_home: true, tracking: true }), null);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Mount: Parked")).toBeInTheDocument();
  });

  it("prioritises home over tracking once not slewing or parked", () => {
    setMountAndCapture(mountState({ slewing: false, at_park: false, at_home: true, tracking: true }), null);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Mount: Home")).toHaveClass("text-dot-ok");
  });

  it("reports tracking / not tracking from the boolean once nothing else applies", () => {
    setMountAndCapture(mountState({ slewing: false, at_park: false, at_home: false, tracking: true }), null);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Mount: Tracking").querySelector('[data-status-icon="tracking"]')).toBeInTheDocument();

    cleanup();
    setMountAndCapture(mountState({ slewing: false, at_park: false, at_home: false, tracking: false }), null);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Mount: Not tracking").querySelector('[data-status-icon="not_tracking"]')).toBeInTheDocument();
  });

  it("reports unknown, never a fabricated 'settling', when every field is null", () => {
    setMountAndCapture(mountState(), null);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Mount: Unknown")).toBeInTheDocument();
    expect(screen.queryByText(/settl/i)).not.toBeInTheDocument();
  });

  it("marks a stale mount reading distinctly, in the title and the dot colour", () => {
    setMountAndCapture(mountState({ tracking: true, stale: true }), null);
    render(<ConnectionsFooter />);
    const entry = screen.getByTitle("Mount: Tracking (stale)");
    expect(entry).toBeInTheDocument();
    expect(entry.className).toContain("text-dot-unknown");
  });
});

describe("ConnectionsFooter — capturing + progress + pipeline stage (owner brief 2026-09-11)", () => {
  beforeEach(() => {
    useConnectionStore.setState({ snapshot: null, status: "connecting", lastResync: null } as never);
  });

  function captureState(overrides: Partial<CaptureState> = {}): CaptureState {
    return {
      in_progress: true,
      last_frame_id: null,
      last_captured_at: null,
      last_error: null,
      folder_configured: true,
      ...overrides,
    };
  }

  it("does not duplicate an idle camera connection icon", () => {
    setMountAndCapture(null, captureState({ in_progress: false }));
    render(<ConnectionsFooter />);
    expect(screen.queryByTitle("Camera idle")).not.toBeInTheDocument();
  });

  it("does not invent an idle camera icon when capture state is absent", () => {
    setMountAndCapture(null, null);
    render(<ConnectionsFooter />);
    expect(screen.queryByTitle("Camera idle")).not.toBeInTheDocument();
  });

  it("shows a bare 'Capturing' entry when in progress but no timing is known yet", () => {
    setMountAndCapture(null, captureState({ started_at: null, duration_s: null }));
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Camera capturing")).toBeInTheDocument();
  });

  it("shows elapsed/total once started_at and duration_s are both known", () => {
    setMountAndCapture(
      null,
      captureState({ started_at: new Date(Date.now() - 30_000).toISOString(), duration_s: 120 }),
    );
    render(<ConnectionsFooter />);
    const progress = screen.getByRole("progressbar", { name: "Exposure progress" });
    expect(progress).toHaveAttribute("aria-valuemax", "120");
    expect(screen.getByTitle(/Camera capturing — .*s of 120s/)).toBeInTheDocument();
  });

  it("shows the pipeline stage next to the capturing entry, using the real CaptureStage label", () => {
    setMountAndCapture(null, captureState({ stage: "transferring" }));
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Pipeline stage: Transferring picture").querySelector('[data-status-icon="transferring"]')).toBeInTheDocument();
  });

  it("uses a dedicated motion glyph for dithering", () => {
    setMountAndCapture(null, captureState({ stage: "dithering" }));
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Pipeline stage: Dithering mount").querySelector('[data-status-icon="dithering"]')).toBeInTheDocument();
  });

  it("shows no stage entry when the stage is not known", () => {
    setMountAndCapture(null, captureState({ stage: null }));
    render(<ConnectionsFooter />);
    expect(screen.queryByTitle(/Pipeline stage/)).not.toBeInTheDocument();
  });
});

describe("ConnectionsFooter — live PA refinement", () => {
  it("shows the refinement camera exposure", () => {
    useConnectionStore.setState({
      snapshot: testSnapshot({ polar_alignment: runningRefinement("capturing") }),
      status: "open",
    } as never);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle(/Camera capturing PA/)).toBeInTheDocument();
  });

  it("shows plate solving separately from camera exposure", () => {
    useConnectionStore.setState({
      snapshot: testSnapshot({ polar_alignment: runningRefinement("solving") }),
      status: "open",
    } as never);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Camera platesolving PA").querySelector('[data-status-icon="solving"]')).toBeInTheDocument();
  });
});

describe("ConnectionsFooter — consolidated equipment status", () => {
  it("does not duplicate idle filter-wheel and focuser connection icons", () => {
    vi.mocked(useFilterWheelQuery).mockReturnValue({
      data: { names: ["Dark", "IR", "LP"], position: 1, names_note: null, position_note: null },
    } as ReturnType<typeof useFilterWheelQuery>);
    vi.mocked(useFocuserPositionQuery).mockReturnValue({
      data: {
        position: 1210,
        note: null,
        is_moving: false,
        is_moving_note: null,
        step_size: null,
        step_size_note: null,
      },
    } as ReturnType<typeof useFocuserPositionQuery>);
    render(<ConnectionsFooter />);
    expect(screen.queryByTitle("Selected filter: IR")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Focuser idle · 1210")).not.toBeInTheDocument();
  });

  it("shows filter-wheel and focuser movement", () => {
    vi.mocked(useFilterWheelQuery).mockReturnValue({
      data: { names: ["Dark", "IR", "LP"], position: -1, names_note: null, position_note: null },
    } as ReturnType<typeof useFilterWheelQuery>);
    vi.mocked(useFocuserPositionQuery).mockReturnValue({
      data: {
        position: 1215,
        note: null,
        is_moving: true,
        is_moving_note: null,
        step_size: null,
        step_size_note: null,
      },
    } as ReturnType<typeof useFocuserPositionQuery>);
    render(<ConnectionsFooter />);
    expect(screen.getByRole("status")).toHaveAttribute(
      "title",
      expect.stringContaining("Moving filter wheel"),
    );
    expect(screen.getByRole("status")).toHaveAttribute(
      "title",
      expect.stringContaining("Moving focuser · Position 1215"),
    );
  });

  it("shows the live dew-heater state", () => {
    vi.mocked(useHeaterQuery).mockReturnValue({
      data: { enabled: true, note: null },
    } as ReturnType<typeof useHeaterQuery>);
    render(<ConnectionsFooter />);
    expect(screen.getByTitle("Dew heater on")).toBeInTheDocument();
  });

  it("keeps only the important disconnected heater warning", () => {
    const note = "[0x407] member requires Connected = True (this device is not connected)";
    vi.mocked(useFilterWheelQuery).mockReturnValue({
      data: { names: null, position: null, names_note: note, position_note: note },
    } as ReturnType<typeof useFilterWheelQuery>);
    vi.mocked(useFocuserPositionQuery).mockReturnValue({
      data: {
        position: null,
        note,
        is_moving: null,
        is_moving_note: note,
        step_size: null,
        step_size_note: note,
      },
    } as ReturnType<typeof useFocuserPositionQuery>);
    vi.mocked(useHeaterQuery).mockReturnValue({
      data: { enabled: null, note },
    } as ReturnType<typeof useHeaterQuery>);

    render(<ConnectionsFooter />);
    expect(screen.queryByTitle("Filter wheel disconnected")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Focuser disconnected")).not.toBeInTheDocument();
    expect(screen.getByTitle("Dew heater disconnected")).toBeInTheDocument();
    expect(screen.queryByTitle(note)).not.toBeInTheDocument();
  });
});

describe("footer battery power", () => {
  it.each([
    [true, "Charging", "Charging", "text-success", false],
    [true, "Full", "Fully charged — external power connected", "text-success", false],
    [false, "Discharging", "Discharging", "text-danger", false],
    [null, null, "Power state unknown", "text-fg-muted", false],
    [true, "Full", "Power state unknown", "text-fg-muted", true],
  ])("reports power %s / %s, stale %s", (external_power, charging_status, label, colour, stale) => {
    const snapshot = { ...testSnapshot(), device: { battery: {
      percent: 80, external_power, charging_status, note: null,
      measured_at: new Date(Date.now() - (stale ? 121_000 : 0)).toISOString(),
    } } };
    useConnectionStore.setState({ snapshot } as never);
    render(<ConnectionsFooter />);
    const entry = screen.getByLabelText(`Battery 80 percent, ${label}${stale ? ", stale" : ""}`);
    expect(entry.querySelector("svg")).toHaveClass(colour);
    expect(entry.title).toContain(label);
  });
});


it("shows the running build metadata version", () => {
  render(<ConnectionsFooter />);
  expect(screen.getByTitle("Running version")).toHaveTextContent("0.0.3-test");
});
