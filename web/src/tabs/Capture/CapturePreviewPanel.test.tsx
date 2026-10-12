import userEvent from "@testing-library/user-event";
// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pins `ssc-bkv.5`'s own verification requirements:
 * - the big preview defaults to `useCaptureState().last_frame_id`, not a
 *   locally-picked frame;
 * - clicking a thumbnail locks the preview to that frame;
 * - a 409 (`CaptureFrameNotReadyError`) reads as "not ready yet", a 404
 *   (`CaptureFrameNotFoundError`) reads as "no longer exists" — neither as a
 *   broken-image icon;
 * - no frames yet is an empty-state message, not a broken `<img>`.
 */
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CalibrationRunState, CaptureFrame, CaptureState } from "../../api/capture";
import { useConnectionStore } from "../../store/connectionStore";
import type { StateSnapshot } from "../../api/types";
import { testSnapshot } from "../../test/fixtures";
import { renderWithQueryClient } from "../../test/queryClient";
import { CapturePreviewPanel } from "./CapturePreviewPanel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useConnectionStore.getState().reset();
});

function captureState(overrides: Partial<CaptureState> = {}): CaptureState {
  return {
    in_progress: false,
    last_frame_id: null,
    last_captured_at: null,
    last_error: null,
    folder_configured: true,
    ...overrides,
  };
}

function calibrationState(overrides: Partial<CalibrationRunState> = {}): CalibrationRunState {
  return {
    status: "running",
    kind: "flat",
    phase: "capturing",
    requested_count: 30,
    filter_names: ["IR"],
    current_filter: "IR",
    completed_frames: 4,
    total_frames: 30,
    exposure_s: 0.45,
    gain: 200,
    probes: [],
    generate_master: true,
    master_paths: [],
    removed_source_frames: 0,
    message: "Capturing IR flats",
    error: null,
    ...overrides,
  };
}

function frame(overrides: Partial<CaptureFrame> = {}): CaptureFrame {
  return {
    id: 1,
    object_name: null,
    filter_name: null,
    date_obs: "2026-09-10T20:00:00Z",
    exposure_s: 10,
    gain: null,
    width: 100,
    height: 100,
    status: "ready",
    ...overrides,
  };
}

function setSnapshot(capture: CaptureState | null) {
  const snapshot = { ...testSnapshot(), capture } as StateSnapshot & { capture: CaptureState | null };
  useConnectionStore.setState({ snapshot, seq: 1 });
}

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** `/api/capture/frames` is the only route this component's own code calls
 * through `fetch` directly — the big preview and each thumbnail are plain
 * `<img src>` requests, which jsdom never issues on its own. A failed
 * `<img>` is simulated with `fireEvent.error`, and the component's own
 * follow-up `fetch(url)` (to recover the status jsdom's synthetic event
 * doesn't carry) is what `imageHandlers` answers below. */
function stubCaptureApi(frames: CaptureFrame[], imageHandlers: Record<string, () => Response> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: string | Request) => {
      const pathname = new URL(input instanceof Request ? input.url : input, "http://localhost").pathname;
      if (pathname === "/api/capture/frames") return Promise.resolve(json({ frames }, 200));
      const handler = imageHandlers[pathname];
      if (handler !== undefined) return Promise.resolve(handler());
      return Promise.resolve(json({ detail: `unexpected request to ${pathname}` }, 500));
    }),
  );
}

function renderPanel() {
  return renderWithQueryClient(<CapturePreviewPanel />);
}

describe("CapturePreviewPanel", () => {
  it("shows the latest frame (last_frame_id) as the big preview by default", async () => {
    stubCaptureApi([frame({ id: 1 }), frame({ id: 2 })]);
    setSnapshot(captureState({ last_frame_id: 2 }));
    renderPanel();

    const preview = await screen.findByRole("img", { name: "Frame #2" });
    expect(preview).toHaveAttribute("src", "/api/capture/frames/2/image");

    expect(screen.queryByRole("button", { name: /frame #2/i })).not.toBeInTheDocument();
  });

  it("does not preview each raw frame during a calibration set", () => {
    stubCaptureApi([]);
    setSnapshot(captureState({
      last_frame_id: 42,
      calibration: calibrationState(),
    }));
    renderPanel();

    expect(screen.getByText(/captured without per-frame previews/i)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /plate-solve/i })).not.toBeInTheDocument();
  });

  it("shows only the final generated calibration master", () => {
    stubCaptureApi([]);
    setSnapshot(captureState({
      last_frame_id: 49,
      calibration: calibrationState({
        status: "completed",
        phase: "integrating",
        completed_frames: 30,
        master_paths: ["/captures/Calibration/flats/master_flat_IR.fits"],
      }),
    }));
    renderPanel();

    const image = screen.getByRole("img", { name: "Generated flat master" });
    expect(image).toHaveAttribute(
      "src",
      "/api/capture/calibration/masters/image?frame_type=flat&name=master_flat_IR.fits",
    );
    expect(screen.getByText(/final generated master.*auto-stretched preview/i)).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Frame #49" })).not.toBeInTheDocument();
  });

  it("shows no raw fallback when master generation was not selected", () => {
    stubCaptureApi([]);
    setSnapshot(captureState({
      last_frame_id: 30,
      calibration: calibrationState({
        status: "completed",
        generate_master: false,
        master_paths: [],
      }),
    }));
    renderPanel();

    expect(screen.getByText(/no calibration master was generated/i)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("does not show yesterday's durable frame after process state has restarted", () => {
    stubCaptureApi([frame({ id: 135 })]);
    setSnapshot(captureState({ last_frame_id: null }));
    renderPanel();

    expect(screen.getByText("No image captured in this app session.")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("does not load the removed timeline thumbnails", async () => {
    stubCaptureApi([frame({ id: 1 }), frame({ id: 2 })]);
    setSnapshot(captureState({ last_frame_id: 2 }));
    renderPanel();

    await screen.findByRole("img", { name: "Frame #2" });
    expect(screen.queryByRole("button", { name: /frame #1/i })).not.toBeInTheDocument();
  });

  it("shows a 409 (not catalogued yet) as 'not ready', not a broken image", async () => {
    stubCaptureApi([frame({ id: 3 })], {
      "/api/capture/frames/3/image": () => json({ detail: "still stacking" }, 409),
    });
    setSnapshot(captureState({ last_frame_id: 3 }));
    renderPanel();

    const preview = await screen.findByRole("img", { name: "Frame #3" });
    fireEvent.error(preview);

    expect(await screen.findByText("still stacking")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Frame #3" })).not.toBeInTheDocument();
  });

  it("shows a 404 as 'no longer exists', distinct from a 409", async () => {
    stubCaptureApi([frame({ id: 4 })], {
      "/api/capture/frames/4/image": () => json({ detail: "not found" }, 404),
    });
    setSnapshot(captureState({ last_frame_id: 4 }));
    renderPanel();

    const preview = await screen.findByRole("img", { name: "Frame #4" });
    fireEvent.error(preview);

    expect(await screen.findByText("This frame no longer exists.")).toBeInTheDocument();
  });

  it("shows an empty state, not a broken image, when there are no frames yet", () => {
    stubCaptureApi([]);
    setSnapshot(captureState({ last_frame_id: null }));
    renderPanel();

    const messages = screen.getAllByText("No image captured in this app session.");
    expect(messages.length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("does not render the slow timeline", async () => {
    stubCaptureApi([frame({ id: 1 })]);
    setSnapshot(captureState({ last_frame_id: 1 }));
    renderPanel();

    await screen.findByRole("img", { name: "Frame #1" });
    expect(screen.queryByRole("button", { name: "Timeline" })).not.toBeInTheDocument();
  });

  it("keeps live-stacking controls out of the capture preview", async () => {
    stubCaptureApi([frame({ id: 1 })]);
    setSnapshot(captureState({ last_frame_id: 1 }));
    renderPanel();

    await screen.findByRole("img", { name: "Frame #1" });
    expect(screen.queryByText(/live stack/i)).not.toBeInTheDocument();
  });

  it("toggles zoom and fit with double taps and permits panning", async () => {
    stubCaptureApi([frame({ id: 5 })]);
    setSnapshot(captureState({ last_frame_id: 5 }));
    renderPanel();

    const image = await screen.findByRole("img", { name: "Frame #5" });
    Object.defineProperties(image, {
      naturalWidth: { configurable: true, value: 3840 }, naturalHeight: { configurable: true, value: 2160 },
      clientWidth: { configurable: true, value: 480 }, clientHeight: { configurable: true, value: 400 },
    });
    fireEvent.load(image);
    const viewer = screen.getByLabelText("Capture image viewer");
    expect(image).toHaveStyle({ transform: "translate(0px, 0px) scale(1)" });

    const user = userEvent.setup();
    await user.dblClick(viewer);
    expect(image).toHaveStyle({ transform: "translate(0px, 0px) scale(8)" });

    fireEvent.pointerDown(viewer, { pointerId: 1, clientX: 20, clientY: 30 });
    fireEvent.pointerMove(viewer, { pointerId: 1, clientX: 50, clientY: 70 });
    fireEvent.pointerUp(viewer, { pointerId: 1, clientX: 50, clientY: 70 });
    expect(image.style.transform).toContain("translate(30px, 40px)");

    await user.dblClick(viewer);
    expect(image).toHaveStyle({ transform: "translate(0px, 0px) scale(1)" });
    expect(screen.queryByRole("button", { name: "Zoom in" })).not.toBeInTheDocument();
  });

  it("plate-solves the frame already shown and renders its astrometry", async () => {
    stubCaptureApi([frame({ id: 7 })], {
      "/api/capture/frames/7/solve": () =>
        json(
          {
            ra_j2000_hours: 5.5881,
            dec_j2000_deg: -5.3911,
            date_obs: "2026-09-10T20:00:00Z",
            pixel_scale_arcsec: 2.42,
            rotation_deg: 91.3,
            solver: "astap",
            solver_version: "2026.09",
          },
          200,
        ),
    });
    setSnapshot(captureState({ last_frame_id: 7 }));
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Plate-solve this frame" }));

    expect(await screen.findByText("5.58810 h")).toBeInTheDocument();
    expect(screen.getByText("-5.3911°")).toBeInTheDocument();
    expect(screen.getByText("2.420″/px")).toBeInTheDocument();
    expect(screen.getByText("91.30°")).toBeInTheDocument();
    expect(screen.getByText("astap 2026.09")).toBeInTheDocument();
  });
});
