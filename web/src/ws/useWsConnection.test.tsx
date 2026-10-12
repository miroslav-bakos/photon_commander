// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FakeWebSocket } from "../test/fakeWebSocket";
import { useConnectionStore } from "../store/connectionStore";
import { useWsConnection } from "./useWsConnection";

beforeEach(() => {
  vi.useFakeTimers();
  FakeWebSocket.reset();
  useConnectionStore.getState().reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers(); });

it.each(["focus", "pageshow", "visibilitychange"])("keeps a healthy fresh socket and query generation on %s", (event) => {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const { unmount } = renderHook(() => { useWsConnection(true); });
  const socket = FakeWebSocket.latest();
  Object.defineProperty(socket, "readyState", { value: 1, configurable: true });
  act(() => { socket.triggerMessage({ type: "snapshot", epoch: "healthy", seq: 10, snapshot: {} }); });
  const generation = useConnectionStore.getState().generation;
  act(() => { (event === "visibilitychange" ? document : window).dispatchEvent(new Event(event)); });
  expect(socket.closed).toBe(false);
  expect(FakeWebSocket.instances).toHaveLength(1);
  expect(useConnectionStore.getState().status).toBe("open");
  expect(useConnectionStore.getState().seq).toBe(10);
  expect(useConnectionStore.getState().generation).toBe(generation);
  unmount();
});

it.each(["online", "pageshow", "focus", "visibilitychange"])("resynchronizes after %s and releases listeners on unmount", (event) => {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const { unmount } = renderHook(() => { useWsConnection(true); });
  const old = FakeWebSocket.latest();
  act(() => { old.triggerMessage({ type: "snapshot", epoch: "old", seq: 10, snapshot: {} }); });
  expect(useConnectionStore.getState().status).toBe("open");
  if (event !== "online") vi.setSystemTime(Date.now() + 31000); // Browser sleep can suspend watchdog callbacks.
  act(() => { (event === "visibilitychange" ? document : window).dispatchEvent(new Event(event)); });
  expect(old.closed).toBe(true);
  expect(useConnectionStore.getState().status).toBe("reconnecting");
  act(() => { FakeWebSocket.latest().triggerMessage({ type: "snapshot", epoch: "new", seq: 0, snapshot: {} }); });
  expect(useConnectionStore.getState().status).toBe("open");
  expect(useConnectionStore.getState().seq).toBe(0);
  unmount();
  act(() => { (event === "visibilitychange" ? document : window).dispatchEvent(new Event(event)); });
  expect(FakeWebSocket.instances).toHaveLength(2);
});

it("recovers Wi-Fi roaming even while the browser remains online", () => {
  const network = new EventTarget();
  Object.defineProperty(navigator, "connection", { configurable: true, value: network });
  try {
    const { unmount } = renderHook(() => { useWsConnection(true); });
    const old = FakeWebSocket.latest();
    act(() => { network.dispatchEvent(new Event("change")); });
    expect(old.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(2);
    unmount();
    act(() => { network.dispatchEvent(new Event("change")); });
    expect(FakeWebSocket.instances).toHaveLength(2);
  } finally { Reflect.deleteProperty(navigator, "connection"); }
});
