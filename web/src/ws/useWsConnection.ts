// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect } from "react";
import { currentResumeSeq, useConnectionStore } from "../store/connectionStore";
import { createWsClient } from "./client";

function wsUrl(seq: number | null): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const base = `${proto}//${window.location.host}/ws`;
  return seq === null ? base : `${base}?seq=${String(seq)}`;
}

/**
 * Owns the single WS connection for the whole app. Mounted once at the root
 * (`App.tsx`), gated on `enabled` so an unpaired session never opens a socket
 * the server would immediately close with 4401.
 */
export function useWsConnection(enabled: boolean): void {
  const applyFrame = useConnectionStore((s) => s.applyFrame);
  const setStatus = useConnectionStore((s) => s.setStatus);
  const reset = useConnectionStore((s) => s.reset);

  useEffect(() => {
    if (!enabled) return;
    const client = createWsClient({
      url: wsUrl,
      onFrame: applyFrame,
      onStatusChange: setStatus,
      getResumeSeq: currentResumeSeq,
    });
    // Mobile browsers can freeze timers and sockets while asleep. A return
    // to the page/network must obtain fresh state, not trust the old snapshot
    // or wait through the remaining offline backoff.
    const reconnect = () => { client.reconnect(); };
    const recoverIfNeeded = () => { client.recoverIfNeeded(); };
    const visibilityChanged = () => {
      if (document.visibilityState === "visible") recoverIfNeeded();
    };
    window.addEventListener("online", reconnect);
    window.addEventListener("offline", reconnect);
    window.addEventListener("pageshow", recoverIfNeeded);
    window.addEventListener("focus", recoverIfNeeded);
    const network: unknown = Reflect.get(navigator, "connection");
    if (network instanceof EventTarget) network.addEventListener("change", reconnect);
    document.addEventListener("visibilitychange", visibilityChanged);
    return () => {
      window.removeEventListener("online", reconnect);
      window.removeEventListener("offline", reconnect);
      window.removeEventListener("pageshow", recoverIfNeeded);
      window.removeEventListener("focus", recoverIfNeeded);
      if (network instanceof EventTarget) network.removeEventListener("change", reconnect);
      document.removeEventListener("visibilitychange", visibilityChanged);
      client.close();
      reset();
    };
  }, [enabled, applyFrame, setStatus, reset]);
}
