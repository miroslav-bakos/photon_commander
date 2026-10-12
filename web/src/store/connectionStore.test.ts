// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { beforeEach, describe, expect, it } from "vitest";
import type { ServerFrame } from "../api/types";
import { testSnapshot } from "../test/fixtures";
import { currentResumeSeq, useConnectionStore } from "./connectionStore";

describe("useConnectionStore", () => {
  beforeEach(() => {
    useConnectionStore.getState().reset();
  });

  it("starts with no snapshot and a null resume seq", () => {
    expect(useConnectionStore.getState().snapshot).toBeNull();
    expect(currentResumeSeq()).toBeNull();
  });

  it("applies an initial snapshot frame without flagging a resync", () => {
    const frame: ServerFrame = {
      type: "snapshot",
      epoch: "epoch-1",
      reason: "initial",
      seq: 5,
      oldest_replayable_seq: 0,
      requested_seq: null,
      snapshot: testSnapshot(),
    };
    useConnectionStore.getState().applyFrame(frame);

    const state = useConnectionStore.getState();
    expect(state.seq).toBe(5);
    expect(state.lastResync).toBeNull();
    expect(currentResumeSeq()).toBe(5);
  });

  it("flags a gap/overflow/server_restarted snapshot as a resync", () => {
    const frame: ServerFrame = {
      type: "snapshot",
      epoch: "epoch-1",
      reason: "gap",
      seq: 10,
      oldest_replayable_seq: 3,
      requested_seq: 2,
      snapshot: testSnapshot(),
    };
    useConnectionStore.getState().applyFrame(frame);
    expect(useConnectionStore.getState().lastResync?.reason).toBe("gap");
  });

  it("applies an event frame by reducing onto the current snapshot", () => {
    useConnectionStore.getState().applyFrame({
      type: "snapshot",
      epoch: "epoch-1",
      reason: "initial",
      seq: 1,
      oldest_replayable_seq: 0,
      requested_seq: null,
      snapshot: testSnapshot(),
    });
    useConnectionStore.getState().applyFrame({
      type: "event",
      epoch: "epoch-1",
      seq: 2,
      event: { type: "location.changed", location: null },
    });

    const state = useConnectionStore.getState();
    expect(state.seq).toBe(2);
    expect(state.snapshot?.location).toBeNull();
  });

  it("ignores an event at or below the current seq (a replayed duplicate)", () => {
    useConnectionStore.getState().applyFrame({
      type: "snapshot",
      epoch: "epoch-1",
      reason: "initial",
      seq: 5,
      oldest_replayable_seq: 0,
      requested_seq: null,
      snapshot: testSnapshot({ device_events_seen: 0 }),
    });
    useConnectionStore.getState().applyFrame({
      type: "event",
      epoch: "epoch-1",
      seq: 5,
      event: {
        type: "device.event",
        event: { name: "x", received_at: "2026-08-28T00:00:00Z", device_uptime_s: null, payload: {} },
      },
    });

    expect(useConnectionStore.getState().seq).toBe(5);
    expect(useConnectionStore.getState().snapshot?.device_events_seen).toBe(0);
  });

  it("ignores an event frame that arrives before any snapshot", () => {
    useConnectionStore.getState().applyFrame({
      type: "event",
      epoch: "epoch-1",
      seq: 1,
      event: { type: "location.changed", location: null },
    });
    expect(useConnectionStore.getState().snapshot).toBeNull();
    expect(useConnectionStore.getState().seq).toBeNull();
  });
});
