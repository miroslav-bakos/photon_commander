// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pins the brief's own verification requirements:
 * - a capture in flight renders from `snapshot.capture.in_progress`, not
 *   local state — asserted by unmounting and remounting the panel against
 *   the same store, which is what a page reload does;
 * - `folder_configured: false` produces actionable guidance (a link to
 *   Settings), not a dead button;
 * - a 409's `detail` is shown verbatim.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CaptureState } from "../api/capture";
import { useConnectionStore } from "../store/connectionStore";
import type { StateSnapshot } from "../api/types";
import { testConnectedDevices, testSnapshot } from "../test/fixtures";
import { renderWithQueryClient } from "../test/queryClient";
import { CapturePanel } from "./CapturePanel";
import { useCaptureFormStore } from "./captureFormStore";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useConnectionStore.getState().reset();
  useCaptureFormStore.getState().reset();
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

function setCaptureSnapshot(capture: CaptureState | null) {
  const snapshot = { ...testSnapshot({ devices: testConnectedDevices("filterwheel") }), capture } as StateSnapshot & { capture: CaptureState | null };
  useConnectionStore.setState({ snapshot, seq: 1 });
}

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function stubCaptureApi(handlers: Record<string, () => Response> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const pathname = new URL(input instanceof Request ? input.url : input.toString(), "http://localhost").pathname;
      const handler = handlers[pathname];
      if (handler !== undefined) return Promise.resolve(handler());
      if (pathname === "/api/capture/frames") return Promise.resolve(json({ frames: [] }, 200));
      if (/^\/api\/capture\/frames\/\d+\/quality$/.test(pathname)) {
        return Promise.resolve(json({ star_count: 0, hfr_px: null, fwhm_px: null, hfr_arcsec: null, fwhm_arcsec: null, background: 0, noise: 0 }, 200));
      }
      if (pathname === "/api/capture/calibration/source-sets") return Promise.resolve(json({ sets: [] }, 200));
      return Promise.resolve(json({ detail: `unexpected request to ${pathname}` }, 500));
    }),
  );
}

function renderPanel() {
  return renderWithQueryClient(
    <MemoryRouter>
      <CapturePanel />
    </MemoryRouter>,
  );
}

describe("CapturePanel", () => {
  it("shows 'Exposing…' from snapshot.capture.in_progress, and it survives a remount", () => {
    stubCaptureApi();
    setCaptureSnapshot(captureState({ in_progress: true }));

    const { unmount } = renderPanel();
    expect(screen.getByText("Exposing…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /capture in progress/i })).toBeDisabled();

    // A remount is what a page reload does: nothing in the component itself
    // carries `in_progress`, only the store the snapshot lives in.
    unmount();
    renderPanel();
    expect(screen.getByText("Exposing…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /capture in progress/i })).toBeDisabled();
  });

  it("shows Idle when nothing is in progress", () => {
    stubCaptureApi();
    setCaptureSnapshot(captureState({ in_progress: false }));
    renderPanel();
    expect(screen.getByText("Idle")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled();
  });

  it("shows scheduled live-stack capture state and latest frame quality", async () => {
    stubCaptureApi({
      "/api/capture/frames/42/quality": () => json({
        star_count: 37,
        hfr_px: 1.234,
        fwhm_px: 2.345,
        hfr_arcsec: null,
        fwhm_arcsec: null,
        background: 101.0,
        noise: 4.2,
      }, 200),
    });
    setCaptureSnapshot(captureState({
      in_progress: true,
      stage: "dithering",
      frame_type: "light",
      object_name: "Sh2-131 2",
      duration_s: null,
      started_at: null,
      last_frame_id: 42,
      last_captured_at: "2026-09-18T23:40:00Z",
    }));

    renderPanel();

    expect(screen.getByText(/Dithering \(Light\)/)).toBeInTheDocument();
    expect(screen.getByText(/Last frame #42/)).toBeInTheDocument();
    expect(await screen.findByText(/Last HFR \/ FWHM: 1\.23 \/ 2\.35 · 37 stars/)).toBeInTheDocument();
  });

  it("preserves all capture form values when the Capture tab remounts", async () => {
    let sentBody: unknown = null;
    const capturePaths: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const pathname = new URL(input instanceof Request ? input.url : input.toString(), "http://localhost").pathname;
        if (pathname === "/api/capture/calibration/start") {
          capturePaths.push(pathname);
          sentBody = input instanceof Request
            ? await input.clone().json()
            : JSON.parse(init?.body as string) as unknown;
          return json(null, 202);
        }
        if (pathname === "/api/capture/exposure") capturePaths.push(pathname);
        if (pathname === "/api/filterwheel") {
          return json({ names: ["Dark", "IR", "LP"], position: 2, names_note: null, position_note: null }, 200);
        }
        if (pathname === "/api/capture/frames") return json({ frames: [] }, 200);
        return json({ detail: `unexpected request to ${pathname}` }, 500);
      }),
    );
    setCaptureSnapshot(captureState());

    const first = renderPanel();
    fireEvent.change(screen.getByLabelText("Duration (s)"), { target: { value: "12.5" } });
    fireEvent.change(screen.getByLabelText("Gain (optional)"), { target: { value: "321" } });
    fireEvent.change(screen.getByLabelText("Target name (optional)"), { target: { value: "M51" } });
    fireEvent.click(screen.getByRole("button", { name: "Flat" }));
    fireEvent.click(screen.getByRole("button", { name: "Twilight sky" }));
    fireEvent.change(screen.getByLabelText("Frames per filter"), { target: { value: "17" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Keep only master/ }));
    useCaptureFormStore.getState().update({ selectedFilters: [0, 1] });

    first.unmount();
    renderPanel();
    expect(useCaptureFormStore.getState().durationText).toBe("12.5");
    expect(screen.getByLabelText("Gain (optional)")).toHaveValue(321);
    expect(useCaptureFormStore.getState().objectText).toBe("M51");
    expect(screen.getByRole("button", { name: "Flat" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Twilight sky" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("radio", { name: "Tele" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByLabelText("Frames per filter")).toHaveValue(17);
    expect(screen.getByRole("checkbox", { name: /Keep only master/ })).toBeChecked();
    expect(screen.getByText(/deleted only after every requested master passes verification/)).toBeInTheDocument();
    expect(useCaptureFormStore.getState().selectedFilters).toEqual([0, 1]);
    await waitFor(() => {
      expect(screen.getByRole("checkbox", { name: "IR" })).toBeChecked();
    });
    expect(screen.queryByRole("checkbox", { name: "Dark" })).not.toBeInTheDocument();

    expect(screen.queryByLabelText("Duration (s)")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Target name (optional)")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Capture" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Loop" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start automatic flat set" }));
    await waitFor(() => {
      expect(sentBody).toEqual({
        kind: "flat",
        count: 17,
        gain: 321,
        filter_positions: [1],
        optic_id: "tele",
        flat_source: "sky",
        generate_master: true,
        keep_only_master: true,
      });
    });
    expect(capturePaths).toEqual(["/api/capture/calibration/start"]);
  });

  it("gives actionable guidance and a link to Settings when no capture folder is configured", () => {
    stubCaptureApi();
    setCaptureSnapshot(captureState({ folder_configured: false }));
    renderPanel();

    expect(screen.getByText(/no capture folder is configured/i)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Settings" });
    expect(link).toHaveAttribute("href", "/settings");
    // Not a dead button: the form is still usable (a 409 explains itself if
    // the user still tries, asserted below).
    expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled();
  });

  it("shows a 409's detail verbatim", async () => {
    stubCaptureApi({
      "/api/capture/exposure": () => json({ detail: "no capture folder configured" }, 409),
    });
    setCaptureSnapshot(captureState());
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "Capture" }));
    expect(await screen.findByText("no capture folder configured")).toBeInTheDocument();
  });

  it("shows a 501's detail verbatim", async () => {
    stubCaptureApi({
      "/api/capture/exposure": () => json({ detail: "this surface cannot expose" }, 501),
    });
    setCaptureSnapshot(captureState());
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "Capture" }));
    expect(await screen.findByText("this surface cannot expose")).toBeInTheDocument();
  });

  it("sends duration_s, gain, object and frame_type (defaulting to light) in the POST body", async () => {
    let sentBody: unknown = null;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(new URL(input.toString(), "http://localhost"), init);
        const pathname = new URL(request.url).pathname;
        const requestBody = await request.clone().text();
        if (pathname === "/api/capture/exposure" && requestBody !== "") {
          sentBody = JSON.parse(requestBody) as unknown;
          return Promise.resolve(json(null, 202));
        }
        if (pathname === "/api/capture/frames") return Promise.resolve(json({ frames: [] }, 200));
        return Promise.resolve(json({ detail: `unexpected request to ${pathname}` }, 500));
      }),
    );
    setCaptureSnapshot(captureState());
    renderPanel();

    fireEvent.change(screen.getByLabelText("Duration (s)"), { target: { value: "30" } });
    fireEvent.change(screen.getByLabelText("Target name (optional)"), { target: { value: "M31" } });
    fireEvent.click(screen.getByRole("button", { name: "Capture" }));

    await waitFor(() => {
      expect(sentBody).toMatchObject({ duration_s: 30, object: "M31", frame_type: "light" });
    });
  });

  it("starts one server-owned loop and stops it through the loop endpoint", async () => {
    const exposureBodies: unknown[] = [];
    const paths: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(new URL(input.toString(), "http://localhost"), init);
        const pathname = new URL(request.url).pathname;
        const requestBody = await request.clone().text();
        paths.push(pathname);
        if (pathname === "/api/capture/exposure") {
          exposureBodies.push(requestBody === "" ? null : JSON.parse(requestBody) as unknown);
          return Promise.resolve(json(null, 202));
        }
        if (pathname === "/api/capture/loop/stop") return Promise.resolve(json(null, 200));
        if (pathname === "/api/capture/frames") return Promise.resolve(json({ frames: [] }, 200));
        return Promise.resolve(json({ detail: `unexpected request to ${pathname}` }, 500));
      }),
    );
    setCaptureSnapshot(captureState());
    renderPanel();

    fireEvent.change(screen.getByLabelText("Duration (s)"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Loop frames (optional)"), { target: { value: "100" } });
    fireEvent.change(screen.getByLabelText("Target name (optional)"), { target: { value: "M42" } });
    fireEvent.click(screen.getByRole("button", { name: "Loop" }));

    await waitFor(() => {
      expect(exposureBodies).toHaveLength(1);
    });
    expect(exposureBodies[0]).toMatchObject({
      duration_s: 2,
      gain: 200,
      object: "M42",
      capture_mode: "loop",
      frame_limit: 100,
    });
    setCaptureSnapshot(captureState({ in_progress: true, frame_type: "light", loop_active: true }));
    expect(await screen.findByText(/Looping · Exposing/)).toBeInTheDocument();
    setCaptureSnapshot(captureState({
      in_progress: true,
      frame_type: "light",
      loop_active: true,
      loop_completed_frames: 1,
      last_frame_id: 1,
    }));
    await waitFor(() => {
      expect(screen.getByText(/Loop active · 1 completed/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "Stop loop" }));
    await waitFor(() => { expect(paths).toContain("/api/capture/loop/stop"); });
    expect(exposureBodies).toHaveLength(1);
  });

  it("recovers an active loop from server state after the whole UI remounts", () => {
    stubCaptureApi();
    setCaptureSnapshot(captureState({
      in_progress: true,
      frame_type: "light",
      loop_active: true,
      loop_completed_frames: 7,
    }));
    const first = renderPanel();
    first.unmount();
    renderPanel();
    expect(screen.getByRole("button", { name: "Stop loop" })).toBeEnabled();
    expect(screen.getByText(/Loop active · 7 completed/)).toBeInTheDocument();
  });

  it("stops the loop and shows the server error when a frame cannot start", async () => {
    stubCaptureApi({
      "/api/capture/exposure": () => json({ detail: "camera refused the exposure" }, 409),
    });
    setCaptureSnapshot(captureState());
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "Loop" }));

    expect(await screen.findByText("camera refused the exposure")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Loop" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Stop loop" })).not.toBeInTheDocument();
  });

  it("warns when an active loop has dithering disabled", async () => {
    stubCaptureApi({
      "/api/capture/exposure": () => json(null, 202),
      "/api/settings/capture-dither": () =>
        json({ enabled: false, every_n_frames: 6, min_pixels: 30, max_pixels: 200 }, 200),
    });
    setCaptureSnapshot(captureState({ loop_active: true }));
    renderPanel();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Dithering is disabled in Capture options; this loop will not dither.",
    );
  });

  it("offers flat and bias capture while excluding the Dark shutter from flats", async () => {
    stubCaptureApi({
      "/api/filterwheel": () => json({ names: ["Dark", "IR", "LP"], position: 2, names_note: null, position_note: null }, 200),
    });
    setCaptureSnapshot(captureState());
    renderPanel();

    const group = screen.getByRole("group", { name: "Frame type" });
    expect(within(group).getByRole("button", { name: "Light" })).toBeEnabled();
    expect(within(group).getByRole("button", { name: "Dark" })).toBeEnabled();
    expect(within(group).getByRole("button", { name: "Flat" })).toBeEnabled();
    expect(within(group).getByRole("button", { name: "Bias" })).toBeEnabled();
    expect(screen.getByText(/bias uses the camera's live minimum exposure/i)).toBeInTheDocument();
    fireEvent.click(within(group).getByRole("button", { name: "Flat" }));
    expect(screen.getByText("Flat wizard")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Tele" })).toHaveAttribute("aria-checked", "true");
    await waitFor(() => {
      expect(screen.getByRole("checkbox", { name: "IR" })).toBeChecked();
      expect(screen.getByRole("checkbox", { name: "LP" })).toBeChecked();
    });
    expect(screen.queryByRole("checkbox", { name: "Dark" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Duration (s)")).not.toBeInTheDocument();
    expect(screen.getByText(/starts at the camera's shortest supported duration.*increased automatically/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Capture" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Loop" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start automatic flat set" })).toBeInTheDocument();
  });

  it("keeps the frame type selector above settings that appear for calibration frames", () => {
    stubCaptureApi();
    setCaptureSnapshot(captureState());
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "Bias" }));
    const frameType = screen.getByRole("group", { name: "Frame type" });
    const calibrationSettings = screen.getByText("Automatic calibration set");

    expect(frameType.compareDocumentPosition(calibrationSettings)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("builds a master from a retained calibration set without recapturing", async () => {
    const calls: string[] = [];
    stubCaptureApi({
      "/api/filterwheel": () => json({ names: ["Dark", "IR", "LP"], position: 1, names_note: null, position_note: null }, 200),
      "/api/capture/calibration/source-sets": () => json({
        sets: [{
          frame_ids: Array.from({ length: 30 }, (_, index) => index + 1),
          frame_type: "flat",
          frame_count: 30,
          gain: 200,
          exposure_s: 0.062553,
          filter_name: "IR",
          width: 2160,
          height: 3840,
          started_at: "2026-09-14T07:40:49Z",
          ended_at: "2026-09-14T07:43:08Z",
          can_build: true,
          blocker: null,
          compatible_bias_frames: 10,
        }],
      }, 200),
      "/api/capture/calibration/masters/build": () => {
        calls.push("build");
        return json(captureState({ in_progress: true }), 202);
      },
    });
    setCaptureSnapshot(captureState());
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "Flat" }));
    expect(await screen.findByText(/30 IR frames · gain 200/)).toBeInTheDocument();
    expect(screen.getByText(/10 compatible biases/)).toBeInTheDocument();
    expect(screen.getByText(/before any flat is captured/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Build master from saved frames" }));
    await waitFor(() => { expect(calls).toEqual(["build"]); });
  });

  it("requires confirmation before sending the atomic flat-panel positioning command", async () => {
    const calls: string[] = [];
    let slewBody: unknown;
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal("confirm", confirm);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(new URL(input.toString(), "http://localhost"), init);
        const pathname = new URL(request.url).pathname;
        const requestBody = await request.clone().text();
        if (pathname === "/api/mount/slew-zenith") {
          calls.push(pathname);
          slewBody = requestBody === "" ? undefined : JSON.parse(requestBody) as unknown;
          return Promise.resolve(new Response(null, { status: 202 }));
        }
        if (pathname === "/api/capture/frames") return Promise.resolve(json({ frames: [] }, 200));
        return Promise.resolve(json({ detail: `unexpected request to ${pathname}` }, 500));
      }),
    );
    const snapshot = {
      ...testSnapshot(),
      capture: captureState(),
      mount: { at_park: true },
    } as unknown as StateSnapshot;
    useConnectionStore.setState({ snapshot, seq: 1 });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Flat" }));
    const position = screen.getByRole("button", { name: "Position for flat panel…" });

    fireEvent.click(position);
    expect(calls).toEqual([]);
    fireEvent.click(position);
    await waitFor(() => {
      expect(calls).toEqual(["/api/mount/slew-zenith"]);
    });
    expect(slewBody).toEqual({ unpark_if_needed: true });
    expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/remove any loose phone/i));
  });

  it("sends the bias sentinel with no target and disables manual duration", async () => {
    let sentBody: unknown = null;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(new URL(input.toString(), "http://localhost"), init);
        const pathname = new URL(request.url).pathname;
        const requestBody = await request.clone().text();
        if (pathname === "/api/capture/exposure" && requestBody !== "") {
          sentBody = JSON.parse(requestBody) as unknown;
          return Promise.resolve(json(null, 202));
        }
        if (pathname === "/api/capture/frames") return Promise.resolve(json({ frames: [] }, 200));
        return Promise.resolve(json({ detail: `unexpected request to ${pathname}` }, 500));
      }),
    );
    setCaptureSnapshot(captureState());
    renderPanel();

    fireEvent.change(screen.getByLabelText("Target name (optional)"), { target: { value: "M31" } });
    fireEvent.click(screen.getByRole("button", { name: "Bias" }));
    expect(screen.getByLabelText("Duration (s)")).toBeDisabled();
    expect(screen.getByLabelText("Target name (optional)")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Capture" }));

    await waitFor(() => {
      expect(sentBody).toMatchObject({ duration_s: 0, object: null, frame_type: "bias" });
    });
  });

  it("sends the selected frame_type (dark) in the POST body", async () => {
    let sentBody: unknown = null;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(new URL(input.toString(), "http://localhost"), init);
        const pathname = new URL(request.url).pathname;
        const requestBody = await request.clone().text();
        if (pathname === "/api/capture/exposure" && requestBody !== "") {
          sentBody = JSON.parse(requestBody) as unknown;
          return Promise.resolve(json(null, 202));
        }
        if (pathname === "/api/capture/frames") return Promise.resolve(json({ frames: [] }, 200));
        return Promise.resolve(json({ detail: `unexpected request to ${pathname}` }, 500));
      }),
    );
    setCaptureSnapshot(captureState());
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "Dark" }));
    fireEvent.click(screen.getByRole("button", { name: "Capture" }));

    await waitFor(() => {
      expect(sentBody).toMatchObject({ frame_type: "dark" });
    });
  });

  it("does not require a target for a dark, and does not silently send a stale target typed while light was selected", async () => {
    let sentBody: unknown = null;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(new URL(input.toString(), "http://localhost"), init);
        const pathname = new URL(request.url).pathname;
        const requestBody = await request.clone().text();
        if (pathname === "/api/capture/exposure" && requestBody !== "") {
          sentBody = JSON.parse(requestBody) as unknown;
          return Promise.resolve(json(null, 202));
        }
        if (pathname === "/api/capture/frames") return Promise.resolve(json({ frames: [] }, 200));
        return Promise.resolve(json({ detail: `unexpected request to ${pathname}` }, 500));
      }),
    );
    setCaptureSnapshot(captureState());
    renderPanel();

    // Type a target while `light` is selected, then switch to `dark` without
    // clearing it — the leftover text must never reach the request.
    fireEvent.change(screen.getByLabelText("Target name (optional)"), { target: { value: "M31" } });
    fireEvent.click(screen.getByRole("button", { name: "Dark" }));
    expect(screen.getByLabelText("Target name (optional)")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Capture" }));

    await waitFor(() => {
      expect(sentBody).toMatchObject({ frame_type: "dark", object: null });
    });
  });

  it("shows the in-progress frame type from snapshot.capture.frame_type", () => {
    stubCaptureApi();
    setCaptureSnapshot(captureState({ in_progress: true, frame_type: "dark" }));
    renderPanel();
    expect(screen.getByText("Exposing (Dark)…")).toBeInTheDocument();
  });

  it.each(["0", "-2", "1.5"])("rejects invalid loop count %s without blocking one-shot capture", (value) => {
    stubCaptureApi();
    setCaptureSnapshot(captureState());
    renderPanel();
    fireEvent.change(screen.getByLabelText("Loop frames (optional)"), { target: { value } });
    expect(screen.getByRole("button", { name: "Loop" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled();
  });

});

describe("Capture camera selection", () => {
  function prepare(wide: boolean, dither = false) {
    const bodies: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(input instanceof Request ? input.url : input.toString(), "http://localhost").pathname;
      if (path === "/api/astro/profiles") return Promise.resolve(json({ profiles: [{ id: "test-scope", capabilities: { wide_camera: wide } }] }, 200));
      if (path === "/api/settings/capture-dither") return Promise.resolve(json({ enabled: dither }, 200));
      if (path === "/api/capture/exposure") {
        const body = input instanceof Request ? await input.clone().text() : init?.body;
        if (typeof body !== "string") throw new Error("Expected a JSON request body");
        bodies.push(JSON.parse(body) as unknown);
        return Promise.resolve(json(captureState({ in_progress: true }), 202));
      }
      if (path === "/api/capture/frames") return Promise.resolve(json({ frames: [] }, 200));
      return Promise.resolve(json({ detail: "not available in fixture" }, 503));
    }));
    const snapshot = { ...testSnapshot(), capture: captureState() } as StateSnapshot & { capture: CaptureState };
    if (snapshot.settings === undefined) throw new Error("Missing fixture settings");
    snapshot.settings.hardware_profile_id = "test-scope";
    useConnectionStore.setState({ snapshot, seq: 1 });
    renderPanel();
    return bodies;
  }

  it.each(["Capture", "Loop"])("sends Wide to %s and preserves choice across remounts", async (action) => {
    const bodies = prepare(true);
    const wide = screen.getByRole("radio", { name: "Wide" });
    await waitFor(() => { expect(wide).toBeEnabled(); });
    fireEvent.click(wide);
    expect(useCaptureFormStore.getState().opticId).toBe("wide");
    await waitFor(() => { expect(screen.getByRole("button", { name: action })).toBeEnabled(); });
    fireEvent.click(screen.getByRole("button", { name: action }));
    await waitFor(() => { expect(bodies).toHaveLength(1); });
    expect(bodies[0]).toMatchObject({ optic_id: "wide", frame_type: "light", ...(action === "Loop" ? { capture_mode: "loop" } : {}) });
    cleanup();
    renderPanel();
    expect(screen.getByRole("radio", { name: "Wide" })).toHaveAttribute("aria-checked", "true");
  });

  it("locks camera selection between loop frames", async () => {
    prepare(true);
    await waitFor(() => { expect(screen.getByRole("radio", { name: "Wide" })).toBeEnabled(); });
    setCaptureSnapshot(captureState({ loop_active: true, in_progress: false }));
    await waitFor(() => {
      expect(screen.getByRole("radio", { name: "Wide" })).toBeDisabled();
      expect(screen.getByRole("radio", { name: "Tele" })).toBeDisabled();
    });
  });

  it("explains Wide dithering and calibration restrictions", async () => {
    prepare(true, true);
    await waitFor(() => { expect(screen.getByRole("radio", { name: "Wide" })).toBeEnabled(); });
    fireEvent.click(screen.getByRole("radio", { name: "Wide" }));
    expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Loop" })).toBeDisabled();
    expect(screen.getByText(/Disable dithering in Capture options/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dark" }));
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Capture dark set" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /Wide lens is fully covered/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: /Wide lens is fully covered/ }));
    expect(screen.getByRole("button", { name: "Capture dark set" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: "Both" }));
    expect(screen.getByRole("checkbox", { name: /Wide lens is fully covered/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Capture dark set" })).toBeDisabled();
  });

  it("does not offer unsupported Wide hardware", async () => {
    prepare(false);
    await waitFor(() => { expect(screen.getByText(/Wide camera requires a confirmed/)).toBeInTheDocument(); });
    expect(screen.getByRole("radio", { name: "Wide" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Tele" })).toHaveAttribute("aria-checked", "true");
  });
});
