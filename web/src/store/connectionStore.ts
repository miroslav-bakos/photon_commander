// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The one place the live `StateSnapshot` lives. Nothing under `src/tabs` or
 * `src/components` may hold a copy of it in `useState` — they read this store
 * (frontend-react.md: "server state is never duplicated into component
 * state"). WS frames are the only way this store's `snapshot`/`seq` change;
 * REST mutations change the server and then wait for the WS event that
 * confirms it, rather than optimistically writing here.
 */
import { create } from "zustand";
import type { ServerFrame, SnapshotReason, StateSnapshot } from "../api/types";
import { reduce } from "./reduce";
import type { WsConnectionStatus } from "../ws/client";

export interface Resync {
  reason: Exclude<SnapshotReason, "initial">;
  at: number;
}

interface ConnectionState {
  /** WS transport status. Independent of whether a snapshot has arrived yet. */
  status: WsConnectionStatus;
  /** Changes only when a replacement socket has received authoritative state. */
  generation: number;
  snapshot: StateSnapshot | null;
  seq: number | null;
  oldestReplayableSeq: number | null;
  /** Set whenever the server resnapshots us instead of replaying — the UI
   * surfaces this rather than pretending nothing happened. */
  lastResync: Resync | null;
  applyFrame: (frame: ServerFrame) => void;
  setStatus: (status: WsConnectionStatus) => void;
  reset: () => void;
}

const initial = {
  status: "connecting" as WsConnectionStatus,
  generation: 0,
  snapshot: null,
  seq: null,
  oldestReplayableSeq: null,
  lastResync: null,
};

export const useConnectionStore = create<ConnectionState>((set, get) => ({
  ...initial,

  setStatus: (status) => {
    set((state) => ({ status, generation: status === "open" && state.status !== "open"
      ? state.generation + 1 : state.generation }));
  },

  applyFrame: (frame) => {
    switch (frame.type) {
      case "snapshot": {
        set({
          snapshot: frame.snapshot,
          seq: frame.seq,
          oldestReplayableSeq: frame.oldest_replayable_seq,
          lastResync:
            frame.reason === "initial" ? null : { reason: frame.reason, at: Date.now() },
        });
        return;
      }
      case "event": {
        const { snapshot, seq } = get();
        if (snapshot === null || (seq !== null && frame.seq <= seq)) return;
        set({ snapshot: reduce(snapshot, frame.event), seq: frame.seq });
        return;
      }
      case "heartbeat":
        return; // Liveness never replaces state or changes its sequence.
      case "error": {
        // Surfaced by the caller (useWsConnection logs + the connection
        // badge shows `status`); the socket closes right after this arrives.
        return;
      }
    }
  },

  reset: () => {
    set(initial);
  },
}));

/** What the WS client resumes from: the last `seq` this store applied, or
 * `null` on a first connection so the server sends a fresh "initial" snapshot. */
export function currentResumeSeq(): number | null {
  return useConnectionStore.getState().seq;
}
