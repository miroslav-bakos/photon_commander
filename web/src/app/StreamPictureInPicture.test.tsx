// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { renderWithQueryClient } from "../test/queryClient";
import { useStreamPipStore } from "../store/streamPipStore";
import { StreamPictureInPicture } from "./StreamPictureInPicture";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useStreamPipStore.setState({ enabled: false });
});

function stubActiveStream(): void {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    active: true,
    encoded_version: "wide:12",
    source: "wide_camera",
  }), { status: 200, headers: { "content-type": "application/json" } })));
}

describe("StreamPictureInPicture", () => {
  it("shows the existing encoded stream across another application route and can close it", async () => {
    stubActiveStream();
    useStreamPipStore.setState({ enabled: true });
    renderWithQueryClient(
      <MemoryRouter initialEntries={["/mount"]}>
        <StreamPictureInPicture />
      </MemoryRouter>,
    );

    const preview = await screen.findByRole("img", {
      name: "RTSP stream: Seestar S30 Wide Camera",
    });
    expect(preview).toHaveAttribute("src", "/api/capture/live-stack/rtsp/preview?v=wide%3A12");
    expect(screen.getByRole("complementary", {
      name: "Stream picture in picture: Seestar S30 Wide Camera",
    })).toHaveClass("fixed", "z-40");

    await userEvent.click(screen.getByRole("button", { name: "Close stream picture in picture" }));
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(useStreamPipStore.getState().enabled).toBe(false);
  });

  it("does not duplicate the full preview on the Stream screen", () => {
    stubActiveStream();
    useStreamPipStore.setState({ enabled: true });
    renderWithQueryClient(
      <MemoryRouter initialEntries={["/streaming"]}>
        <StreamPictureInPicture />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("resizes from touch-style pointer drags while preserving the portrait crop", async () => {
    stubActiveStream();
    useStreamPipStore.setState({ enabled: true });
    renderWithQueryClient(
      <MemoryRouter initialEntries={["/mount"]}>
        <StreamPictureInPicture />
      </MemoryRouter>,
    );

    const popup = await screen.findByRole("complementary");
    const handle = screen.getByRole("button", { name: "Resize stream picture in picture" });
    Object.defineProperty(popup, "getBoundingClientRect", {
      value: () => ({ width: 352 }),
    });

    fireEvent.pointerDown(handle, { pointerId: 4, pointerType: "touch", clientX: 100, clientY: 100 });
    fireEvent.pointerMove(handle, { pointerId: 4, pointerType: "touch", clientX: 36, clientY: 100 });
    fireEvent.pointerUp(handle, { pointerId: 4, pointerType: "touch", clientX: 36, clientY: 100 });
    expect(popup).toHaveStyle({ width: "416px" });
    expect(screen.getByLabelText("Portrait stream viewport")).toHaveClass("aspect-[9/16]");
    expect(screen.getByRole("img")).toHaveClass("object-cover", "object-center");

    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(popup).toHaveStyle({ width: "384px" });
    fireEvent.keyDown(handle, { key: "Home" });
    expect(popup).toHaveStyle({ width: "352px" });
  });

  it("stays hidden when the stream has no encoded frame", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      active: true,
      encoded_version: null,
      source: "wide_camera",
    }), { status: 200, headers: { "content-type": "application/json" } })));
    useStreamPipStore.setState({ enabled: true });
    renderWithQueryClient(
      <MemoryRouter initialEntries={["/mount"]}>
        <StreamPictureInPicture />
      </MemoryRouter>,
    );

    await waitFor(() => { expect(globalThis.fetch).toHaveBeenCalled(); });
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  });
});
