// SPDX-License-Identifier: GPL-3.0-only

import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { StreamingSettingsPanel } from "./StreamingSettingsPanel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("StreamingSettingsPanel", () => {
  it("shows the usable VLC URL and persists the idle wide-camera policy", async () => {
    const broadcast = {
      broadcast_when_unparked: false,
      wide_camera_picture_in_picture: false,
      rtsp_host: "0.0.0.0",
      rtsp_port: 8554,
      rtsp_path: "live",
      rtsp_width: 1280,
      rtsp_height: 720,
      rtsp_fps: 1,
      rtsp_bitrate_kbps: 2500,
      rtsp_update_interval_s: 5,
      rtsp_auto_crop: true,
      youtube_resolution: "720p",
    };
    let youtubeKeySet = false;
    const fetchMock = vi.fn().mockImplementation(async (input: string | Request, init?: RequestInit) => {
      const path = typeof input === "string" ? input : new URL(input.url).pathname;
      const method = init?.method ?? (typeof input === "string" ? "GET" : input.method);
      const requestBody = typeof init?.body === "string"
        ? init.body
        : typeof input === "string"
          ? ""
          : await input.clone().text();
      if (path.endsWith("/api/settings/secrets") && method === "PUT") {
        const body = JSON.parse(requestBody) as { name: string; value: string };
        youtubeKeySet = body.name === "youtube_stream_key" && body.value !== "";
        return Promise.resolve(new Response(JSON.stringify({ names: [body.name] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }));
      }
      if (path.endsWith("/api/settings/streaming") && method === "PUT") {
        Object.assign(broadcast, JSON.parse(requestBody));
      }
      return Promise.resolve(new Response(JSON.stringify({
        ...broadcast,
        youtube_stream_key_set: youtubeKeySet,
        active: broadcast.broadcast_when_unparked,
        url: "rtsp://HOST:8554/live",
        source: broadcast.broadcast_when_unparked ? "wide_camera" : null,
        last_error: null,
      }), { status: 200, headers: { "content-type": "application/json" } }));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithQueryClient(<StreamingSettingsPanel />);
    expect(await screen.findByRole("link", { name: "rtsp://localhost:8554/live" })).toHaveAttribute(
      "href", "rtsp://localhost:8554/live",
    );
    await userEvent.click(screen.getByRole("checkbox", { name: /Automatically broadcast/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /picture-in-picture/ }));
    await userEvent.selectOptions(screen.getByLabelText("Resolution"), "1920x1080");
    await userEvent.selectOptions(screen.getByLabelText(/YouTube quality/), "1080p");
    await userEvent.click(screen.getByRole("button", { name: "Save broadcast settings" }));
    expect(await screen.findByText("Status: streaming Seestar S30 Wide Camera")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([request]) => request instanceof Request && request.method === "PUT" && new URL(request.url).pathname === "/api/settings/streaming")).toBe(true);
    expect(await screen.findByRole("checkbox", { name: /picture-in-picture/ })).toBeChecked();
    expect(broadcast.rtsp_width).toBe(1920);
    expect(broadcast.rtsp_bitrate_kbps).toBe(6000);
    expect(broadcast.youtube_resolution).toBe("1080p");
    await userEvent.type(screen.getByLabelText("YouTube stream key"), "youtube-secret");
    await userEvent.click(screen.getByRole("button", { name: "Save key" }));
    expect(await screen.findByPlaceholderText("Stored — enter a new key to replace")).toBeInTheDocument();
  });
});

it("keeps broadcast edits made while a previous save is pending", async () => {
  const data = { broadcast_when_unparked: false, wide_camera_picture_in_picture: false, rtsp_host: "0.0.0.0", rtsp_port: 8554, rtsp_path: "live", rtsp_width: 1280, rtsp_height: 720, rtsp_fps: 1, rtsp_bitrate_kbps: 2500, rtsp_update_interval_s: 5, rtsp_auto_crop: true, youtube_resolution: "720p", youtube_stream_key_set: false, active: false, url: "rtsp://HOST:8554/live", source: null, last_error: null };
  const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn((request: Request) => request.method === "PUT"
    ? new Promise<Response>((resolve) => { finish = resolve; }) : Promise.resolve(json(data))));
  renderWithQueryClient(<StreamingSettingsPanel />);
  const path = await screen.findByLabelText("Stream path");
  fireEvent.change(path, { target: { value: "first" } });
  fireEvent.click(screen.getByRole("button", { name: "Save broadcast settings" }));
  await waitFor(() => { expect(finish).toBeDefined(); });
  fireEvent.change(path, { target: { value: "newer" } });
  await act(async () => { finish(json({ ...data, rtsp_path: "first" })); await Promise.resolve(); });
  await waitFor(() => { expect(screen.getByRole("button", { name: "Save broadcast settings" })).toBeEnabled(); });
  expect(path).toHaveValue("newer");
});
