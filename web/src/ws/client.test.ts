// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeWebSocket } from "../test/fakeWebSocket";
import { createWsClient, type WsConnectionStatus } from "./client";

/** The reconnect path is the one that matters (frontend-react.md). */
describe("createWsClient", () => {
  beforeEach(() => {
    FakeWebSocket.reset();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function impl() {
    return FakeWebSocket as unknown as typeof WebSocket;
  }

  it("connects with no ?seq on the first attempt", () => {
    createWsClient({
      url: (seq) => (seq === null ? "ws://x/ws" : `ws://x/ws?seq=${String(seq)}`),
      onFrame: vi.fn(),
      onStatusChange: vi.fn(),
      getResumeSeq: () => null,
      WebSocketImpl: impl(),
    });

    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.latest().url).toBe("ws://x/ws");
  });

  it("reports 'connecting' then 'open', and delivers a parsed frame", () => {
    const onStatusChange = vi.fn<(status: WsConnectionStatus) => void>();
    const onFrame = vi.fn<(frame: import("../api/types").ServerFrame) => void>();

    createWsClient({
      url: () => "ws://x/ws",
      onFrame,
      onStatusChange,
      getResumeSeq: () => null,
      WebSocketImpl: impl(),
    });

    expect(onStatusChange).toHaveBeenCalledWith("connecting");

    FakeWebSocket.latest().triggerOpen();
    expect(onStatusChange).not.toHaveBeenCalledWith("open");

    const frame = { epoch: "test", type: "snapshot", reason: "initial", seq: 1, oldest_replayable_seq: 0, snapshot: {} };
    FakeWebSocket.latest().triggerMessage(frame);
    expect(onFrame).toHaveBeenCalledWith(frame);
    expect(onStatusChange).toHaveBeenLastCalledWith("open");
  });

  it("reconnects after the socket closes, resuming from the last applied seq", () => {
    const onStatusChange = vi.fn<(status: WsConnectionStatus) => void>();
    let resumeSeq: number | null = null;

    createWsClient({
      url: (seq) => (seq === null ? "ws://x/ws" : `ws://x/ws?seq=${String(seq)}`),
      onFrame: vi.fn(),
      onStatusChange,
      getResumeSeq: () => resumeSeq,
      WebSocketImpl: impl(),
      backoffMs: () => 1000,
    });

    FakeWebSocket.latest().triggerOpen();
    resumeSeq = 42; // the store applied a frame with seq 42 before the drop

    FakeWebSocket.latest().triggerServerClose();
    expect(onStatusChange).toHaveBeenCalledWith("reconnecting");
    expect(FakeWebSocket.instances).toHaveLength(1); // not yet — waiting on backoff

    vi.advanceTimersByTime(1000);

    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(FakeWebSocket.latest().url).toBe("ws://x/ws?seq=42");
  });

  it("keeps reconnecting across repeated drops, with an increasing attempt count", () => {
    const backoffMs = vi.fn((_attempt: number) => 100);

    createWsClient({
      url: () => "ws://x/ws",
      onFrame: vi.fn(),
      onStatusChange: vi.fn(),
      getResumeSeq: () => null,
      WebSocketImpl: impl(),
      backoffMs,
    });

    // Never opens — three drops in a row without a successful "open".
    FakeWebSocket.latest().triggerServerClose();
    vi.advanceTimersByTime(100);
    FakeWebSocket.latest().triggerServerClose();
    vi.advanceTimersByTime(100);
    FakeWebSocket.latest().triggerServerClose();
    vi.advanceTimersByTime(100);

    expect(FakeWebSocket.instances).toHaveLength(4);
    expect(backoffMs.mock.calls.map((call) => call[0])).toEqual([0, 1, 2]);
  });

  it("resets the attempt count after a successful open", () => {
    const backoffMs = vi.fn((_attempt: number) => 100);

    createWsClient({
      url: () => "ws://x/ws",
      onFrame: vi.fn(),
      onStatusChange: vi.fn(),
      getResumeSeq: () => null,
      WebSocketImpl: impl(),
      backoffMs,
    });

    FakeWebSocket.latest().triggerServerClose(); // attempt 0 recorded, now attempt=1
    vi.advanceTimersByTime(100);
    FakeWebSocket.latest().triggerOpen();
    FakeWebSocket.latest().triggerMessage({ epoch: "test", type: "snapshot", seq: 1, snapshot: {} }); // authoritative success
    FakeWebSocket.latest().triggerServerClose();
    vi.advanceTimersByTime(100);

    expect(backoffMs.mock.calls.map((call) => call[0])).toEqual([0, 0]);
  });

  it("stops reconnecting once closed by the caller", () => {
    const onStatusChange = vi.fn<(status: WsConnectionStatus) => void>();

    const client = createWsClient({
      url: () => "ws://x/ws",
      onFrame: vi.fn(),
      onStatusChange,
      getResumeSeq: () => null,
      WebSocketImpl: impl(),
      backoffMs: () => 1000,
    });

    FakeWebSocket.latest().triggerServerClose();
    expect(FakeWebSocket.instances).toHaveLength(1);

    client.close();
    expect(onStatusChange).toHaveBeenLastCalledWith("closed");

    vi.advanceTimersByTime(5000);
    expect(FakeWebSocket.instances).toHaveLength(1); // the pending reconnect never fired
  });

  it("ignores a stale socket's close event once a newer attempt exists", () => {
    // Guards against a slow-to-fire close on an abandoned socket reconnecting twice.
    const onStatusChange = vi.fn<(status: WsConnectionStatus) => void>();

    createWsClient({
      url: () => "ws://x/ws",
      onFrame: vi.fn(),
      onStatusChange,
      getResumeSeq: () => null,
      WebSocketImpl: impl(),
      backoffMs: () => 100,
    });

    const first = FakeWebSocket.latest();
    first.triggerServerClose();
    vi.advanceTimersByTime(100);
    expect(FakeWebSocket.instances).toHaveLength(2);

    onStatusChange.mockClear();
    first.dispatch("close", {}); // the old socket fires close again, late
    expect(onStatusChange).not.toHaveBeenCalled();
  });
  it("recovers a stalled handshake even when close never arrives", () => {
    const client = createWsClient({
      url: () => "ws://x/ws", onFrame: vi.fn(), onStatusChange: vi.fn(),
      getResumeSeq: () => null, WebSocketImpl: impl(), backoffMs: () => 100,
    });
    const first = FakeWebSocket.latest();
    vi.advanceTimersByTime(15_100);
    expect(first.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(2);
    client.close();
  });

  it("reconnects a half-open socket and ignores all late frames from it", () => {
    const onFrame = vi.fn<(frame: import("../api/types").ServerFrame) => void>();
    const onStatusChange = vi.fn();
    const client = createWsClient({
      url: () => "ws://x/ws", onFrame, onStatusChange,
      getResumeSeq: () => null, WebSocketImpl: impl(), backoffMs: () => 100,
    });
    const first = FakeWebSocket.latest();
    first.triggerOpen();
    first.triggerMessage({ epoch: "test", type: "snapshot", seq: 1, snapshot: {} });
    vi.advanceTimersByTime(30_100);
    expect(first.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(2);
    onFrame.mockClear();
    onStatusChange.mockClear();
    first.triggerOpen();
    first.triggerMessage({ epoch: "test", type: "snapshot", seq: 2, snapshot: {} });
    expect(onFrame).not.toHaveBeenCalled();
    expect(onStatusChange).not.toHaveBeenCalled();
    client.close();
  });

  it("heartbeat keeps an idle synchronized connection live without a new snapshot", () => {
    const onFrame = vi.fn<(frame: import("../api/types").ServerFrame) => void>();
    const client = createWsClient({
      url: () => "ws://x/ws", onFrame, onStatusChange: vi.fn(),
      getResumeSeq: () => null, WebSocketImpl: impl(), backoffMs: () => 100,
    });
    const socket = FakeWebSocket.latest();
    socket.triggerMessage({ epoch: "test", type: "snapshot", seq: 1, snapshot: {} });
    for (let i = 0; i < 10; i++) {
      vi.advanceTimersByTime(10_000);
      socket.triggerMessage({ epoch: "test", type: "heartbeat", seq: 1 });
    }
    expect(socket.closed).toBe(false);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(onFrame.mock.calls.filter(([frame]) => frame.type === "snapshot")).toHaveLength(1);
    client.close();
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("recovers error without close and keeps controls disabled before resnapshot", () => {
    const onStatusChange = vi.fn();
    const client = createWsClient({
      url: () => "ws://x/ws", onFrame: vi.fn(), onStatusChange,
      getResumeSeq: () => 42, WebSocketImpl: impl(), backoffMs: () => 100,
    });
    const first = FakeWebSocket.latest();
    first.triggerOpen();
    first.triggerMessage({ epoch: "test", type: "heartbeat", seq: 42 });
    expect(onStatusChange).not.toHaveBeenCalledWith("open");
    first.dispatch("error", {});
    vi.advanceTimersByTime(100);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(first.closed).toBe(true);
    client.close();
  });

  it("requests resync rather than treating a sequence gap as live", () => {
    const onFrame = vi.fn();
    const client = createWsClient({
      url: () => "ws://x/ws", onFrame, onStatusChange: vi.fn(),
      getResumeSeq: () => null, WebSocketImpl: impl(), backoffMs: () => 100,
    });
    const socket = FakeWebSocket.latest();
    socket.triggerMessage({ epoch: "test", type: "snapshot", seq: 1, snapshot: {} });
    onFrame.mockClear();
    socket.triggerMessage({ epoch: "test", type: "event", seq: 3, event: {} });
    expect(onFrame).not.toHaveBeenCalled();
    expect(socket.closed).toBe(true);
    vi.advanceTimersByTime(100);
    expect(FakeWebSocket.instances).toHaveLength(2);
    client.close();
  });

  it("conditional recovery keeps valid idle heartbeats and resyncs after suspended liveness", () => {
    const onFrame = vi.fn();
    const onStatusChange = vi.fn();
    const client = createWsClient({ url: () => "ws://x/ws", onFrame, onStatusChange,
      getResumeSeq: () => null, WebSocketImpl: impl(), backoffMs: () => 15000 });
    const old = FakeWebSocket.latest();
    Object.defineProperty(old, "readyState", { value: 1, configurable: true });
    old.triggerMessage({ epoch: "old", type: "snapshot", seq: 1, snapshot: {} });
    vi.setSystemTime(Date.now() + 29000);
    old.triggerMessage({ epoch: "old", type: "heartbeat", seq: 1 });
    vi.setSystemTime(Date.now() + 29000);
    client.recoverIfNeeded();
    expect(old.closed).toBe(false);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(onStatusChange).toHaveBeenLastCalledWith("open");
    vi.setSystemTime(Date.now() + 1001); // Timers did not run while asleep.
    client.recoverIfNeeded();
    expect(old.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(onStatusChange).toHaveBeenLastCalledWith("reconnecting");
    const fresh = FakeWebSocket.latest();
    fresh.triggerOpen();
    fresh.triggerMessage({ epoch: "new", type: "heartbeat", seq: 0 });
    expect(onStatusChange).toHaveBeenLastCalledWith("reconnecting");
    fresh.triggerMessage({ epoch: "new", type: "snapshot", seq: 0, snapshot: {} });
    expect(onStatusChange).toHaveBeenLastCalledWith("open");
    client.close();
  });

  it("immediately replaces a stale socket on return and waits for fresh state", () => {
    const onFrame = vi.fn();
    const onStatusChange = vi.fn();
    const client = createWsClient({ url: () => "ws://x/ws", onFrame, onStatusChange,
      getResumeSeq: () => null, WebSocketImpl: impl(), backoffMs: () => 15_000 });
    const old = FakeWebSocket.latest();
    old.triggerMessage({ epoch: "old", type: "snapshot", seq: 1, snapshot: {} });
    onFrame.mockClear();
    client.reconnect();
    expect(old.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(onStatusChange).toHaveBeenLastCalledWith("reconnecting");
    old.triggerMessage({ epoch: "old", type: "snapshot", seq: 2, snapshot: {} });
    expect(onFrame).not.toHaveBeenCalled();
    FakeWebSocket.latest().triggerOpen();
    expect(onStatusChange).toHaveBeenLastCalledWith("reconnecting");
    FakeWebSocket.latest().triggerMessage({ epoch: "new", type: "snapshot", seq: 0, snapshot: {} });
    expect(onStatusChange).toHaveBeenLastCalledWith("open");
    client.close();
    client.reconnect();
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });
});
