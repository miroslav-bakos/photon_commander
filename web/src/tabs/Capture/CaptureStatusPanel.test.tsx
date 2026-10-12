// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CaptureState } from "../../api/capture";
import { CaptureStatusPanel } from "./CaptureStatusPanel";

const capture = vi.hoisted(() => ({ value: null as CaptureState | null }));

vi.mock("../../api/capture", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../api/capture")>();
  return { ...original, useCaptureState: () => capture.value };
});

describe("CaptureStatusPanel", () => {
  it("shows master integration instead of the last raw frame stage", () => {
    capture.value = {
      in_progress: true,
      last_frame_id: 60,
      last_captured_at: "2026-09-24T04:24:39Z",
      last_error: null,
      folder_configured: true,
      stage: "saving",
      duration_s: 60,
      started_at: "2026-09-24T04:23:36Z",
      object_name: null,
      frame_type: "dark",
      gain: 200,
      loop_active: false,
      loop_completed_frames: 0,
      calibration: {
        status: "running",
        kind: "dark",
        phase: "integrating",
        requested_count: 60,
        filter_names: [],
        current_filter: null,
        completed_frames: 60,
        total_frames: 60,
        exposure_s: 60,
        gain: 200,
        probes: [],
        generate_master: true,
        keep_only_master: false,
        master_paths: [],
        removed_source_frames: 0,
        message: "Building calibration master",
        error: null,
      },
    };

    render(<CaptureStatusPanel />);

    expect(screen.getByRole("status", { name: "Calibration master activity" })).toHaveTextContent(
      "Building calibration master",
    );
    expect(screen.getByText("Dark frames: 60/60 saved")).toBeInTheDocument();
    expect(screen.queryByText("Saving")).not.toBeInTheDocument();
  });
});
