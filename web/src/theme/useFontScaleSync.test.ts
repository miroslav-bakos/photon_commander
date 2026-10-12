// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ServerFrame } from "../api/types";
import { useConnectionStore } from "../store/connectionStore";
import { testSnapshot } from "../test/fixtures";
import { useFontScaleSync } from "./useFontScaleSync";

function snapshotFrame(fontScale: string, seq = 1): ServerFrame {
  const snapshot = testSnapshot();
  if (snapshot.settings === undefined) throw new Error("test snapshot must include settings");
  return {
    type: "snapshot",
    epoch: "epoch-1",
    reason: "initial",
    seq,
    oldest_replayable_seq: 0,
    requested_seq: null,
    snapshot: {
      ...snapshot,
      settings: { ...snapshot.settings, font_scale: fontScale },
    },
  };
}

describe("useFontScaleSync", () => {
  beforeEach(() => {
    useConnectionStore.getState().reset();
    delete document.documentElement.dataset.fontScale;
  });

  afterEach(cleanup);

  it("writes nothing before the snapshot has a value", () => {
    renderHook(() => { useFontScaleSync(); });
    expect(document.documentElement.dataset.fontScale).toBeUndefined();
  });

  it("sets data-font-scale from the snapshot once it arrives", () => {
    const { rerender } = renderHook(() => { useFontScaleSync(); });
    useConnectionStore.getState().applyFrame(snapshotFrame("large"));
    rerender();
    expect(document.documentElement.dataset.fontScale).toBe("large");
  });

  it("updates data-font-scale when the snapshot's value changes", () => {
    const { rerender } = renderHook(() => { useFontScaleSync(); });
    useConnectionStore.getState().applyFrame(snapshotFrame("small"));
    rerender();
    expect(document.documentElement.dataset.fontScale).toBe("small");

    useConnectionStore.getState().applyFrame(snapshotFrame("large", 2));
    rerender();
    expect(document.documentElement.dataset.fontScale).toBe("large");
  });
});
