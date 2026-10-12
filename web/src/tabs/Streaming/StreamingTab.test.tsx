// SPDX-License-Identifier: GPL-3.0-only

import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { StreamingTab } from "./StreamingTab";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function pathOf(request: RequestInfo | URL): string {
  return new URL(request instanceof Request ? request.url : request.toString(), "http://localhost").pathname;
}

describe("StreamingTab", () => {
  it.each([false, true])("distinguishes a running relay from recent video progress (%s)", async (publishing) => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(
      pathOf(input).includes("/youtube")
        ? { active: true, publishing, configured: true, last_error: null }
        : { active: false, url: null, clients: 0 },
    ), { status: 200, headers: { "content-type": "application/json" } }))));
    renderWithQueryClient(<StreamingTab />);
    expect(await screen.findByRole("status")).toHaveTextContent(publishing
      ? "Video frames are being sent. Confirm the preview in YouTube Studio."
      : "Waiting for outgoing video frames. Check the source and YouTube connection.");
    expect(screen.getByRole("button", { name: "Stop YouTube stream" })).toBeEnabled();
  });
  it("previews the exact encoded frame and exposes the VLC URL", async () => {
    let youtubeActive = false;
    const fetchMock = vi.fn().mockImplementation((request: RequestInfo | URL, init?: RequestInit) => {
      const input = pathOf(request);
      const method = request instanceof Request ? request.method : init?.method;
      if (input.includes("/youtube")) {
        if (method === "POST" && input.endsWith("/start")) youtubeActive = true;
        return Promise.resolve(new Response(JSON.stringify({
          active: youtubeActive,
          configured: true,
          last_error: null,
        }), { status: 200, headers: { "content-type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({
        active: true,
        url: "rtsp://HOST:8554/live",
        clients: 2,
        encoded_version: "wide:7",
        last_error: null,
        encoder: "libx264-ultrafast",
        source: "wide_camera",
      }), { status: 200, headers: { "content-type": "application/json" } }));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithQueryClient(<StreamingTab />);
    const preview = await screen.findByRole("img", {
      name: "RTSP stream preview: Seestar S30 Wide Camera",
    });
    expect(preview).toHaveAttribute("src", "/api/capture/live-stack/rtsp/preview?v=wide%3A7");
    expect(screen.getByLabelText("Stream preview viewport")).toHaveClass("image-preview", "overflow-hidden");
    expect(preview).toHaveClass("h-full", "w-full", "object-contain");
    expect(screen.getByRole("link", { name: "rtsp://localhost:8554/live" })).toHaveAttribute(
      "href", "rtsp://localhost:8554/live",
    );
    expect(screen.getByText(/Seestar S30 Wide Camera · 2 RTSP client connection/)).toBeInTheDocument();
    expect(screen.getByText(/720p at 4 Mbps H\.264 at 30 fps/)).toBeInTheDocument();
    expect(screen.getByText(/No microphone or telescope sound is captured/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Restart streaming" }));
    expect(fetchMock.mock.calls.some(([request]) => request instanceof Request && pathOf(request) === "/api/capture/live-stack/rtsp/restart" && request instanceof Request && request.method === "POST")).toBe(true);
    await userEvent.click(await screen.findByRole("button", { name: "Start YouTube stream" }));
    expect(await screen.findByRole("button", { name: "Stop YouTube stream" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([request]) => request instanceof Request && pathOf(request) === "/api/capture/live-stack/youtube/start" && request instanceof Request && request.method === "POST")).toBe(true);
  });

  it("shows the configured URL without exposing manual RTSP controls", async () => {
    const fetchMock = vi.fn().mockImplementation((request: RequestInfo | URL) => {
      const input = pathOf(request);
      if (input === "/api/settings/streaming") {
        return Promise.resolve(new Response(JSON.stringify({
          rtsp_host: "0.0.0.0",
          rtsp_port: 8554,
          rtsp_path: "live",
          rtsp_width: 1280,
          rtsp_height: 720,
          rtsp_fps: 1,
          rtsp_bitrate_kbps: 2500,
          rtsp_update_interval_s: 5,
          rtsp_auto_crop: true,
          broadcast_when_unparked: false,
          url: "rtsp://HOST:8554/live",
        }), { status: 200, headers: { "content-type": "application/json" } }));
      }
      if (input.includes("/youtube")) {
        return Promise.resolve(new Response(JSON.stringify({ active: false, configured: true, last_error: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }));
      }
      return Promise.resolve(new Response(JSON.stringify({ active: false, url: null, clients: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWithQueryClient(<StreamingTab />);

    expect(await screen.findByRole("link", { name: "rtsp://localhost:8554/live" })).toBeInTheDocument();
    expect(screen.getByText(/Broadcasting from the wide camera will start automatically once the Seestar S30 Pro arm is open/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Wide Camera stream/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start YouTube stream" })).toBeDisabled();
  });
});
