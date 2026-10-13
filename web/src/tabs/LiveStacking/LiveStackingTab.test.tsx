// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { LiveStackingTab } from "./LiveStackingTab";

vi.mock("../Observe/ObservingModeSwitch", () => ({ ObservingModeSwitch: () => <div>Capture / Live stack</div> }));
vi.mock("../Observe/ObservingSessionPanel", () => ({ ObservingSessionPanel: () => <div>Observing now</div> }));
vi.mock("../Capture/CaptureStatusPanel", () => ({ CaptureStatusPanel: () => <div>Capture status</div> }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

describe("LiveStackingTab", () => {
  it("changes the preview when a new target reuses the same stack sequence and count", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    const key = ["capture", "live-stack", "status"];
    const stack = { sequence: 1, output_path: "/NGC7023/live_001.fit", parameters_path: "",
      frame_count: 3, dark_master: null, bias_master: null, flat_master: null,
      active: true, error: null };
    client.setQueryData(key, stack);
    const view = renderWithQueryClient(<QueryClientProvider client={client}><LiveStackingTab /></QueryClientProvider>);
    try {
      const oldImage = await screen.findByRole("img", { name: "Live stack 1" });
      const oldUrl = oldImage.getAttribute("src");
      await act(async () => { client.setQueryData(key, { ...stack, output_path: "/M31/live_001.fit" }); });
      await waitFor(() => {
        const image = screen.getByRole("img", { name: "Live stack 1" });
        expect(image.getAttribute("src")).not.toBe(oldUrl);
        expect(image).not.toBe(oldImage);
      });
    } finally {
      view.unmount();
      client.clear();
    }
  });
  it.each([[1800 * 1024 ** 2, "2"], [3800 * 1024 ** 2, "1"]])(
    "uses host RAM for a new stack default (%s bytes)", async (total, expected) => {
      vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string | Request) => {
        const pathname = new URL(typeof input === "string" ? input : input.url, "http://localhost").pathname;
        if (pathname === "/api/system/metrics") {
          return Promise.resolve(json({
            hostname: "test-pi", collected_at: "2026-10-08T07:00:00Z",
            memory: { total_bytes: total, percent_used: 10 },
            cpu: { percent: 0, temperature_c: 30 }, process: { cpu_percent: 0 },
            network: { bytes_received: 0, bytes_sent: 0 },
          }));
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      }));
      renderWithQueryClient(<LiveStackingTab />);
      await waitFor(() => expect(screen.getByLabelText("Stack resolution")).toHaveValue(expected));
      fireEvent.change(screen.getByLabelText("Stack resolution"), { target: { value: expected === "2" ? "1" : "2" } });
      expect(screen.getByLabelText("Stack resolution")).toHaveValue(expected === "2" ? "1" : "2");
    },
  );
  it("lets the Live stack preview fill the center panel", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    renderWithQueryClient(<LiveStackingTab />);
    const card = screen.getByRole("button", { name: "Live stack" }).closest(".rounded-lg");
    expect(card).not.toHaveClass("lg:self-start");
  });

  it("reconnects to the server-owned run and shows its masters and stop control", async () => {
    let resetBody: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: string | Request, init?: RequestInit) => {
      const pathname = new URL(typeof input === "string" ? input : input.url, "http://localhost").pathname;
      if (pathname.endsWith("/masters")) {
        return Promise.resolve(json({
          masters: [
            { name: "dark.fit", path: "/masters/dark.fit", frame_type: "dark" },
            { name: "bias.fit", path: "/masters/bias.fit", frame_type: "bias" },
            { name: "flat.fit", path: "/masters/flat.fit", frame_type: "flat" },
          ],
        }));
      }
      if (pathname === "/api/capture/live-stack" || pathname === "/api/capture/live-stack/reset") {
        if (pathname.endsWith("/reset")) {
          resetBody = input instanceof Request ? await input.json() as Record<string, unknown> : JSON.parse(init?.body as string) as Record<string, unknown>;
        }
        return Promise.resolve(json({
          sequence: 7,
          binning: 2,
          output_path: "/captures/live_007.fit",
          parameters_path: "/captures/live_007.json",
          frame_count: 3,
          dark_master: "/masters/dark.fit",
          bias_master: "/masters/bias.fit",
          flat_master: "/masters/flat.fit",
          active: true,
          error: null,
          auto_select_masters: true,
          master_selection_message: "Calibration for light.fit: dark dark.fit; flat flat.fit.",
          watch_folder: "/watch/session",
          watch_recursive: true,
          processing_file: "/watch/new-light.fit",
          pending_files: 2,
          last_shift_x_px: 3,
          last_shift_y_px: -2,
          registration_peak_ratio: 8.25,
          object_name: "M31",
          filter_name: "LP",
          exposure_s: 30,
          gain: 80,
          ccd_temp_c: 10.2,
          hfr_px: 2.34,
          fwhm_px: 3.45,
          star_count: 126,
          snr_estimate: 18.72,
          rejected_count: 1,
          total_integration_s: 90,
          target_header: "M31  ·  next: M45 (starting 22:00)",
        }));
      }
      if (pathname === "/api/capture/live-stack/rtsp") {
        return Promise.resolve(json({
          active: true,
          url: "rtsp://HOST:8554/live",
          clients: 1,
          encoded_version: "7:3",
          last_error: null,
          encoder: "libx264-ultrafast",
          width: 3840,
          height: 2160,
          fps: 1,
          bitrate_kbps: 12000,
        }));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }));

    renderWithQueryClient(<LiveStackingTab />);

    expect(await screen.findByRole("img", { name: "Live stack 7" })).toHaveAttribute(
      "src",
      "/api/capture/live-stack/image?stack=%2Fcaptures%2Flive_007.fit%3A7&v=3&auto_crop=true",
    );
    expect(screen.getByText(/3 frame\(s\).*live_007/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop stacking" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save stack" })).toBeEnabled();
    expect(screen.getByLabelText("Stack resolution")).toHaveValue("2");
    expect(screen.getByText(/Calibration for light\.fit/)).toBeInTheDocument();
    expect(screen.getByLabelText("Watch folder (optional)")).toHaveValue("/watch/session");
    expect(screen.getByLabelText("Include subfolders")).toBeChecked();
    expect(screen.getByLabelText("Dark master")).toHaveValue("");
    expect(screen.getByLabelText("Bias master")).toHaveValue("");
    expect(screen.getByLabelText("Flat master")).toHaveValue("");
    expect(screen.getByLabelText("Dark master")).toBeDisabled();
    const activeIndicator = screen.getByRole("status", { name: "Live stacking active" });
    expect(activeIndicator).toHaveClass("bg-dot-bad");
    expect(activeIndicator).toHaveStyle({ animationDuration: "650ms" });
    expect(activeIndicator.parentElement).toHaveTextContent("Live stack");
    expect(screen.getByText("Calibrating, registering, and integrating new-light.fit…")).toBeInTheDocument();
    expect(screen.getByText("Queue: 2 file(s)")).toBeInTheDocument();
    expect(screen.queryByText(/Registration:|confidence 8.25/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("RTSP port")).not.toBeInTheDocument();
    expect(screen.getByText(/M31.*LP.*integration 1m 30s/)).toBeInTheDocument();
    expect(screen.getByText(/accepted 3.*received 4.*rejected 1.*exposure 30s/)).toBeInTheDocument();
    expect(screen.getByText(/HFR 2.34px.*FWHM 3.45px.*stars 126.*SNR est\. 18.7/)).toBeInTheDocument();
    expect(screen.getByText("M31 · next: M45 (starting 22:00)")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Automatically select compatible masters"));
    expect(screen.getByLabelText("Automatically select compatible masters")).not.toBeChecked();
    expect(screen.getByLabelText("Dark master")).toBeEnabled();
    expect(screen.getByLabelText("Dark master")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "Reset stack" }));
    await waitFor(() => {
      expect(resetBody).toMatchObject({
        watch_folder: "/watch/session",
        watch_recursive: true,
        binning: 2,
        dark_master_path: null,
        bias_master_path: null,
        flat_master_path: null,
        auto_select_masters: false,
      });
    });
  });

  it("restores disabled automatic master selection from the running stack", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string | Request) => {
      const pathname = new URL(typeof input === "string" ? input : input.url, "http://localhost").pathname;
      if (pathname.endsWith("/masters")) return Promise.resolve(json({ masters: [] }));
      if (pathname === "/api/capture/live-stack") {
        return Promise.resolve(json({
          sequence: 8,
          output_path: "/captures/live_008.fit",
          parameters_path: "/captures/live_008.json",
          frame_count: 0,
          dark_master: null,
          bias_master: null,
          flat_master: null,
          active: true,
          error: null,
          auto_select_masters: false,
          master_selection_message: "Automatic master selection is disabled.",
        }));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }));

    renderWithQueryClient(<LiveStackingTab />);

    expect(await screen.findByText("Automatic master selection is disabled.")).toBeInTheDocument();
    expect(screen.getByLabelText("Automatically select compatible masters")).not.toBeChecked();
    expect(screen.getByLabelText("Dark master")).toBeEnabled();
    expect(screen.getByLabelText("Dark master")).toHaveValue("");
  });

  it("renders a dark-temperature mismatch as a prominent warning", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const rawUrl = input instanceof Request ? input.url : input.toString();
      const pathname = new URL(rawUrl, "http://localhost").pathname;
      if (pathname.endsWith("/masters")) return json({ masters: [] });
      if (pathname.endsWith("/live-stack")) return json({
        sequence: 1, output_path: "", parameters_path: "", frame_count: 1,
        dark_master: "/masters/dark.fit", bias_master: null, flat_master: null,
        active: true, error: null, auto_select_masters: true,
        master_selection_message: "Calibration for light.fit: dark dark.fit. WARNING: dark temperature mismatch is 7.0 C (light +20.0 C, master +13.0 C).",
        watch_folder: "/captures", watch_recursive: false, pending_files: 0, binning: 2,
      });
      return json({});
    }));

    renderWithQueryClient(<LiveStackingTab />);

    expect(await screen.findByRole("alert")).toHaveTextContent("dark temperature mismatch is 7.0 C");
  });

  it("fits on opening and returning, while allowing intentional native zoom and cropping", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string | Request) => {
      const pathname = new URL(typeof input === "string" ? input : input.url, "http://localhost").pathname;
      if (pathname === "/api/capture/live-stack") {
        return Promise.resolve(json({
          sequence: 2,
          output_path: "/captures/live_002.fit",
          parameters_path: "/captures/live_002.json",
          frame_count: 2,
          dark_master: null,
          bias_master: null,
          flat_master: null,
          active: true,
          error: null,
          object_name: "M42",
        }));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }));
    const user = userEvent.setup();
    const { unmount } = renderWithQueryClient(<LiveStackingTab />);
    const image = await screen.findByRole("img", { name: "Live stack 2" });
    Object.defineProperties(image, {
      naturalWidth: { configurable: true, value: 2160 },
      naturalHeight: { configurable: true, value: 3840 },
      clientWidth: { configurable: true, value: 480 }, clientHeight: { configurable: true, value: 480 },
    });
    fireEvent.load(image);
    expect(screen.getByText(/2160×3840 px/)).toBeInTheDocument();
    expect(screen.getByText("M42")).toBeInTheDocument();
    expect(screen.queryByText(/next:/)).not.toBeInTheDocument();

    const viewer = screen.getByLabelText("Live-stack image viewer");
    expect(image).toHaveStyle({ transform: "translate(0px, 0px) scale(1)" });
    await user.dblClick(viewer);
    expect(image).toHaveStyle({ transform: "translate(0px, 0px) scale(8)" });
    fireEvent.pointerDown(viewer, { pointerId: 1, clientX: 20, clientY: 20 });
    fireEvent.pointerMove(viewer, { pointerId: 1, clientX: 35, clientY: 30 });
    fireEvent.pointerUp(viewer, { pointerId: 1 });
    expect(image.getAttribute("style")).toContain("translate(15px, 10px)");
    await user.dblClick(viewer);
    expect(image).toHaveStyle({ transform: "translate(0px, 0px) scale(1)" });
    expect(screen.queryByRole("button", { name: "Reset zoom" })).not.toBeInTheDocument();

    const crop = screen.getByRole("checkbox", { name: /Auto-crop dithering edges/ });
    expect(crop).toBeChecked();
    await user.click(crop);
    expect(image).toHaveAttribute("src", "/api/capture/live-stack/image?stack=%2Fcaptures%2Flive_002.fit%3A2&v=2&auto_crop=false");
    await user.dblClick(viewer);
    expect(viewer).toHaveAttribute("aria-pressed", "true");
    unmount();
    renderWithQueryClient(<LiveStackingTab />);
    const returnedImage = await screen.findByRole("img", { name: "Live stack 2" });
    fireEvent.load(returnedImage);
    expect(screen.getByLabelText("Live-stack image viewer")).toHaveAttribute("aria-pressed", "false");
    expect(returnedImage).toHaveStyle({ transform: "translate(0px, 0px) scale(1)" });
  });

  it("shows a grey live-stack indicator when no run exists", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    renderWithQueryClient(<LiveStackingTab />);
    const indicator = await screen.findByRole("status", { name: "Live stacking inactive" });
    expect(indicator).toHaveClass("bg-dot-unknown");
    expect(indicator).not.toHaveClass("animate-pulse");
    expect(screen.getByText("No live-stack watcher is active.")).toBeInTheDocument();
  });

  it("browses and selects a server-side watch folder", async () => {
    const fetchMock = vi.fn().mockImplementation((input: string | Request) => {
      const url = new URL(typeof input === "string" ? input : input.url, "http://localhost");
      if (url.pathname === "/api/capture/live-stack/directories") {
        const path = url.searchParams.get("path");
        if (path === "/captures/Pictures") {
          return Promise.resolve(json({ path, parent: "/captures", directories: ["/captures/Pictures/live_test"] }));
        }
        if (path === "/captures/Pictures/live_test") {
          return Promise.resolve(json({ path, parent: "/captures/Pictures", directories: [] }));
        }
        return Promise.resolve(json({ path: "/captures", parent: null, directories: ["/captures/Pictures"] }));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderWithQueryClient(<LiveStackingTab />);

    await user.type(screen.getByLabelText("Watch folder (optional)"), "/old/path");
    await user.click(screen.getByRole("button", { name: "Browse" }));
    expect(fetchMock.mock.calls.some(([input]) => input instanceof Request && new URL(input.url).pathname === "/api/capture/live-stack/directories")).toBe(true);
    await user.click(await screen.findByRole("button", { name: "Pictures" }));
    await user.click(await screen.findByRole("button", { name: "live_test" }));
    await user.click(screen.getByRole("button", { name: "Select this folder" }));

    expect(screen.getByLabelText("Watch folder (optional)")).toHaveValue("/captures/Pictures/live_test");
  });

  it("does not show broadcast configuration on the live-stacking screen", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    renderWithQueryClient(<LiveStackingTab />);
    await screen.findByRole("status", { name: "Live stacking inactive" });
    expect(screen.queryByText("Broadcast stream")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("RTSP port")).not.toBeInTheDocument();
  });
});
