// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The thin WS client (frontend-react.md): connect, resume from `seq`, and
 * reconnect with jittered backoff when the phone leaves Wi-Fi or the laptop
 * sleeps. It knows nothing about the state shape beyond "a frame arrived" —
 * `useWsConnection` is what feeds frames into the store.
 *
 * `WebSocketImpl` is injectable so `client.test.ts` can drive it with a fake
 * socket instead of a real network connection.
 */
import type { ServerFrame } from "../api/types";

export type WsConnectionStatus = "connecting" | "open" | "reconnecting" | "closed";

export interface WsClientOptions {
  /** Resolves the `ws(s)://.../ws` URL, optionally with `?seq=`. */
  url: (seq: number | null) => string;
  onFrame: (frame: ServerFrame) => void;
  onStatusChange: (status: WsConnectionStatus) => void;
  /** The `seq` to resume from. `null` asks for a fresh "initial" snapshot. */
  getResumeSeq: () => number | null;
  WebSocketImpl?: typeof WebSocket;
  /** Backoff schedule, exposed for tests. Real callers keep the default. */
  backoffMs?: (attempt: number) => number;
}

export interface WsClient {
  close: () => void;
  reconnect: () => void;
  /** Preserve healthy transport on focus; recover stale or interrupted state. */
  recoverIfNeeded: () => void;
}

const MAX_BACKOFF_MS = 15_000;
const FRAME_TIMEOUT_MS = 30_000;

/** Full jitter exponential backoff: attempt 0 → 0–500ms, capped at 15s. */
export function defaultBackoffMs(attempt: number): number {
  const cap = Math.min(MAX_BACKOFF_MS, 500 * 2 ** attempt);
  return Math.random() * cap;
}

export function createWsClient(options: WsClientOptions): WsClient {
  const WS = options.WebSocketImpl ?? WebSocket;
  const backoff = options.backoffMs ?? defaultBackoffMs;

  let socket: WebSocket | null = null;
  let ready = false;
  let lastValidFrameAt: number | null = null;
  let attempt = 0;
  let closedByCaller = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  let watchdog: ReturnType<typeof setTimeout> | undefined;

  function clearWatchdog(): void {
    if (watchdog !== undefined) clearTimeout(watchdog);
    watchdog = undefined;
  }

  function scheduleReconnect(): void {
    if (closedByCaller) return;
    options.onStatusChange("reconnecting");
    const delay = backoff(attempt++);
    reconnectTimer = setTimeout(connect, delay);
  }

  function abandon(instance: WebSocket): void {
    if (socket !== instance || closedByCaller) return;
    socket = null; // Ignore late open/message/close from this attempt.
    ready = false;
    lastValidFrameAt = null;
    clearWatchdog();
    instance.close();
    scheduleReconnect();
  }

  function armWatchdog(instance: WebSocket, delay: number): void {
    clearWatchdog();
    watchdog = setTimeout(() => { abandon(instance); }, delay);
  }

  function connect(): void {
    reconnectTimer = undefined;
    if (closedByCaller) return;
    options.onStatusChange(attempt === 0 ? "connecting" : "reconnecting");
    const seq = options.getResumeSeq();
    let socketInstance: WebSocket;
    try {
      socketInstance = new WS(options.url(seq));
    } catch {
      scheduleReconnect();
      return;
    }
    socket = socketInstance;
    ready = false;
    lastValidFrameAt = null;
    let epoch: string | null = null;
    let lastSeq: number | null = null;
    // Includes waiting for the authoritative snapshot, not just TCP open.
    armWatchdog(socketInstance, 15_000);

    socketInstance.addEventListener("message", (event: MessageEvent<string>) => {
      if (closedByCaller || socket !== socketInstance) return;
      let frame: ServerFrame;
      try {
        const parsed: unknown = JSON.parse(event.data);
        if (typeof parsed !== "object" || parsed === null ||
            !("epoch" in parsed) || typeof parsed.epoch !== "string" ||
            !("type" in parsed) || typeof parsed.type !== "string" ||
            !["snapshot", "event", "heartbeat", "error"].includes(parsed.type)) {
          throw new Error("Invalid state frame");
        }
        frame = parsed as ServerFrame;
        if (frame.type === "error") {
          options.onFrame(frame);
          abandon(socketInstance);
          return;
        }
        // The production URL requests a fresh snapshot on every reconnect.
        // Never enable controls against a retained snapshot before resync.
        if (!ready && frame.type !== "snapshot") return;
        if (!Number.isSafeInteger(frame.seq) || frame.seq < 0) {
          throw new Error("Invalid state sequence");
        }
        if (frame.type === "snapshot") {
          epoch = frame.epoch;
          lastSeq = frame.seq;
        } else {
          if (frame.epoch !== epoch ||
              (frame.type === "heartbeat" ? frame.seq !== lastSeq : frame.seq !== (lastSeq ?? 0) + 1)) {
            throw new Error("State continuation gap");
          }
          lastSeq = frame.seq;
        }
        options.onFrame(frame);
      } catch {
        abandon(socketInstance);
        return;
      }
      lastValidFrameAt = Date.now();
      armWatchdog(socketInstance, FRAME_TIMEOUT_MS);
      if (!ready) {
        ready = true;
        attempt = 0;
        options.onStatusChange("open");
      }
    });

    socketInstance.addEventListener("close", () => {
      if (socket !== socketInstance || closedByCaller) return;
      socket = null;
      ready = false;
      lastValidFrameAt = null;
      clearWatchdog();
      scheduleReconnect();
    });

    socketInstance.addEventListener("error", () => { abandon(socketInstance); });
  }

  connect();

  function reconnect(): void {
    if (closedByCaller) return;
    if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
    clearWatchdog();
    const previous = socket;
    socket = null;
    ready = false;
    lastValidFrameAt = null;
    previous?.close();
    attempt = 1;
    connect();
  }

  return {
    reconnect,
    recoverIfNeeded(): void {
      // OPEN (1) plus a recent accepted frame proves this transport is usable.
      // Rejected frames and TCP open alone never count as authoritative state.
      if (socket?.readyState === 1 && ready && lastValidFrameAt !== null &&
          Date.now() - lastValidFrameAt < FRAME_TIMEOUT_MS) return;
      reconnect();
    },
    close(): void {
      closedByCaller = true;
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      clearWatchdog();
      options.onStatusChange("closed");
      socket?.close();
      socket = null;
    },
  };
}
