// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { fireEvent, render, screen, cleanup, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useConnectionStore } from "../store/connectionStore";
import { ImagePreview } from "./ImagePreview";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); useConnectionStore.getState().reset(); });
describe("mobile preview gestures", () => {
  it("retries a transient image failure without another frame or network event", async () => {
    vi.useFakeTimers();
    useConnectionStore.getState().reset();
    useConnectionStore.getState().setStatus("open");
    render(<ImagePreview src="/api/capture/frames/2/image" alt="Transient image" label="Transient viewer" />);
    const image = screen.getByAltText("Transient image");
    const before = image.getAttribute("src");
    fireEvent.error(image);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(image.getAttribute("src")).not.toBe(before);
    fireEvent.load(image);
    const successful = image.getAttribute("src");
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(image.getAttribute("src")).toBe(successful);
  });
  it("reloads a failed image after fresh connection while retaining viewer zoom", () => {
    useConnectionStore.getState().reset();
    render(<ImagePreview src="/api/capture/frames/1/image?thumbnail=true" alt="Recovering stars" label="Recovering viewer" />);
    const image = screen.getByAltText("Recovering stars");
    const viewer = screen.getByLabelText("Recovering viewer");
    fireEvent.keyDown(viewer, { key: "Enter" });
    fireEvent.error(image);
    const before = image.getAttribute("src");
    act(() => { useConnectionStore.getState().setStatus("open"); });
    expect(image.getAttribute("src")).not.toBe(before);
    expect(image.getAttribute("src")).toContain("thumbnail=true");
    expect(viewer).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByAltText("Recovering stars")).toBe(image);
    useConnectionStore.getState().reset();
  });
  it("returns a panned touch preview to fit with a second double tap", () => {
    render(<ImagePreview src="/test.png" alt="Stars" label="Preview viewer" />);
    const viewer = screen.getByLabelText("Preview viewer");
    const image = screen.getByAltText("Stars");
    Object.defineProperties(image, {
      naturalWidth: { value: 3840 }, naturalHeight: { value: 2160 },
      clientWidth: { value: 480 }, clientHeight: { value: 400 },
    });
    fireEvent.load(image);
    const tap = () => {
      fireEvent.pointerDown(viewer, { pointerId: 1, pointerType: "touch", isPrimary: true, clientX: 100, clientY: 100 });
      fireEvent.pointerUp(viewer, { pointerId: 1, pointerType: "touch", isPrimary: true, clientX: 100, clientY: 100 });
    };
    tap(); tap();
    expect(image.style.transform).toContain("scale(8)");
    fireEvent.pointerDown(viewer, { pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.pointerMove(viewer, { pointerId: 1, clientX: 150, clientY: 140 });
    fireEvent.pointerUp(viewer, { pointerId: 1, clientX: 150, clientY: 140 });
    expect(image.style.transform).toContain("translate(50px, 40px)");
    tap(); tap();
    expect(image.style.transform).toBe("translate(0px, 0px) scale(1)");
    expect(viewer.style.touchAction).toBe("pan-y");
  });
  it("opens a portrait stack at native scale and measures each new image", () => {
    render(<ImagePreview defaultNative src="/stack.png" alt="Stack" label="Stack viewer" />);
    const image = screen.getByAltText("Stack");
    Object.defineProperties(image, {
      naturalWidth: { configurable: true, value: 2160 }, naturalHeight: { configurable: true, value: 3840 },
      clientWidth: { value: 600 }, clientHeight: { value: 480 },
    });
    fireEvent.load(image);
    expect(image.style.transform).toContain("scale(8)");
    expect(screen.getByLabelText("Stack viewer")).toHaveAttribute("aria-pressed", "true");
    Object.defineProperties(image, { naturalWidth: { value: 960 }, naturalHeight: { value: 540 } });
    fireEvent.load(image);
    expect(image.style.transform).toContain("scale(1.6)");
    fireEvent.keyDown(screen.getByLabelText("Stack viewer"), { key: "Escape" });
    expect(image.style.transform).toContain("scale(1)");
  });
  it("keeps native pixels after the viewport changes", () => {
    let resize: () => void = () => { return undefined; };
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: () => void) { resize = callback; }
      observe() { return undefined; }
      disconnect() { return undefined; }
    });
    render(<ImagePreview defaultNative src="/stack.png" alt="Stack" label="Stack viewer" />);
    const image = screen.getByAltText("Stack");
    Object.defineProperties(image, {
      naturalWidth: { value: 3840 }, naturalHeight: { value: 2160 },
      clientWidth: { configurable: true, value: 960 }, clientHeight: { configurable: true, value: 540 },
    });
    fireEvent.load(image);
    expect(image.style.transform).toContain("scale(4)");
    Object.defineProperties(image, { clientWidth: { value: 320 }, clientHeight: { value: 400 } });
    act(() => { resize(); });
    expect(image.style.transform).toContain("scale(12)");
  });
  it("does not mistake cancelled scrolling or multitouch for a double tap", () => {
    render(<ImagePreview src="/test.png" alt="Stars" label="Preview viewer" />);
    const viewer = screen.getByLabelText("Preview viewer");
    fireEvent.pointerDown(viewer, { pointerId: 1, pointerType: "touch", isPrimary: true });
    fireEvent.pointerCancel(viewer, { pointerId: 1 });
    fireEvent.pointerDown(viewer, { pointerId: 2, pointerType: "touch", isPrimary: false });
    fireEvent.pointerUp(viewer, { pointerId: 2 });
    expect(screen.getByAltText("Stars").style.transform).toContain("scale(1)");
  });
});
