// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ServerFrame } from "../api/types";
import { useConnectionStore } from "../store/connectionStore";
import { testSnapshot } from "../test/fixtures";
import { useAccentSync } from "./useAccentSync";

function snapshotFrame(accent: string, seq = 1): ServerFrame {
  const snapshot = testSnapshot();
  if (snapshot.settings === undefined) throw new Error("test snapshot must include settings");
  return {
    type: "snapshot",
    epoch: "epoch-1",
    reason: "initial",
    seq,
    oldest_replayable_seq: 0,
    requested_seq: null,
    snapshot: { ...snapshot, settings: { ...snapshot.settings, accent } },
  };
}

describe("useAccentSync", () => {
  beforeEach(() => {
    useConnectionStore.getState().reset();
    delete document.documentElement.dataset.accent;
  });

  afterEach(cleanup);

  it("applies and updates the server-owned accent", () => {
    const { rerender } = renderHook(() => { useAccentSync(); });
    expect(document.documentElement.dataset.accent).toBeUndefined();

    useConnectionStore.getState().applyFrame(snapshotFrame("violet"));
    rerender();
    expect(document.documentElement.dataset.accent).toBe("violet");

    useConnectionStore.getState().applyFrame(snapshotFrame("amber", 2));
    rerender();
    expect(document.documentElement.dataset.accent).toBe("amber");
  });
});
