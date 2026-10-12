// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { CaptureState, LiveStackResponse } from "../api/capture";
import type { AutofocusState } from "../api/focuser";
import type { TargetCenteringProgress } from "../api/mount";
import { testSnapshot } from "../test/fixtures";
import { activityProgress, formatActivityCount, schedulerOperationLabel, selectFooterActivities } from "./footerActivity";

const NOW = Date.parse("2026-09-14T20:00:05Z");

function select(overrides: Partial<Parameters<typeof selectFooterActivities>[0]> = {}) {
  return selectFooterActivities({
    snapshot: testSnapshot(),
    capture: null,
    polarAlignment: null,
    autofocus: undefined,
    centering: undefined,
    liveStack: undefined,
    mount: null,
    filterWheel: undefined,
    focuser: undefined,
    now: NOW,
    ...overrides,
  });
}

function capture(overrides: Partial<CaptureState>): CaptureState {
  return {
    in_progress: false,
    last_frame_id: null,
    last_captured_at: null,
    last_error: null,
    folder_configured: true,
    ...overrides,
  };
}

function firstActivity(activities: ReturnType<typeof select>): ReturnType<typeof select>[number] {
  const activity = activities[0];
  if (activity === undefined) throw new Error("expected one active footer operation");
  return activity;
}

describe("footer activity selection", () => {
  it("shows the retained-master message and its authoritative count", () => {
    const activities = select({
      capture: capture({
        in_progress: true,
        calibration: {
          status: "running",
          kind: "bias",
          phase: "integrating",
          requested_count: 100,
          filter_names: [],
          current_filter: null,
          completed_frames: 100,
          total_frames: 100,
          exposure_s: null,
          gain: 200,
          probes: [],
          generate_master: true,
          master_paths: [],
          removed_source_frames: 0,
          message: "Building master from 100 retained frames",
          error: null,
        },
      }),
    });
    expect(activities[0]).toMatchObject({
      label: "Building master from 100 retained frames",
      current: 100,
      total: 100,
    });
    expect(formatActivityCount(firstActivity(activities))).toBe("100/100");
    expect(activityProgress(firstActivity(activities))).toBe(100);
  });

  it("uses elapsed exposure time without inventing transfer progress", () => {
    const exposing = firstActivity(select({
      capture: capture({
        in_progress: true,
        stage: "exposing",
        frame_type: "light",
        started_at: "2026-09-14T20:00:00Z",
        duration_s: 30,
      }),
    }));
    expect(exposing).toMatchObject({ id: "exposure", current: 5, total: 30 });
    expect(formatActivityCount(exposing)).toBe("5.0/30.0s");

    const transfer = firstActivity(select({ capture: capture({ in_progress: true, stage: "transferring" }) }));
    expect(transfer.label).toBe("Transferring exposure");
    expect(activityProgress(transfer)).toBeNull();
  });

  it("surfaces calibration capture, PA, autofocus, centering, and schedule progress", () => {
    const point = (index: number, status: "solved" | "solving") => ({
      index, status, target_ra_j2000_h: index + 1, target_dec_j2000_deg: index + 1,
      planned_altitude_deg: null, actual_altitude_deg: null, actual_azimuth_deg: null,
      mount_slewing: null, mount_tracking: null, solved_ra_j2000_h: null,
      solved_dec_j2000_deg: null, attempt: 1, error: null,
    });
    const calibration = select({
      capture: capture({
        calibration: {
          status: "running", kind: "flat", phase: "capturing", requested_count: 100,
          filter_names: ["IR"], current_filter: "IR", completed_frames: 48, total_frames: 100,
          exposure_s: 0.5, gain: 200, probes: [], generate_master: true, master_paths: [],
          removed_source_frames: 0, message: null, error: null,
        },
      }),
      polarAlignment: {
        status: "running", run_kind: "measure", started_at: null, ended_at: null,
        exposure_started_at: null, exposure_duration_s: null, current_point_index: 1,
        preflight_reason: null,
        points: [point(0, "solved"), point(1, "solving")], result: null, assessment: null, start: null, error: null,
      },
      autofocus: { status: "running", stage: "fine", samples: [{ position: 100, hfr_px: 2, star_count: 10, stage: "fine" }], curve: [], candidate_position: null, final_position: null, started_at: null, completed_at: null, error: null } satisfies AutofocusState,
      centering: {
        status: "running", stage: "correcting", attempt: 2, max_attempts: 4,
        target_ra_j2000_h: 1, target_dec_j2000_deg: 2, tolerance_arcmin: 1,
        attempts: [], message: "Correcting target", error: null, updated_at: null,
      } satisfies TargetCenteringProgress,
      snapshot: testSnapshot({
        run: {
          run_id: "run", plan_id: "plan", plan_name: "Night", plan_revision: 1,
          night_date: "2026-09-14", status: "running", started_at: "2026-09-14T19:00:00Z",
          ended_at: null, ended_reason: null, current_item_id: "b", gate_held: false,
          gate_reason: null, last_decision: null, last_decision_at: null, journal_length: 1,
          available_actions: [], items: [
            { item_id: "a", kind: "slew", enabled: true, status: "done", attempt: 1, panel_index: null, held: false, reason: "", start_utc: null, end_utc: null },
            { item_id: "b", kind: "capture", enabled: true, status: "running", attempt: 1, panel_index: null, held: false, reason: "Capturing M31", start_utc: null, end_utc: null },
          ],
        },
      }),
    });
    expect(calibration.map((item) => item.id)).toEqual([
      "calibration-capture", "polar-alignment", "autofocus", "centering", "schedule",
    ]);
    expect(calibration[0]).toMatchObject({ current: 48, total: 100 });
    expect(calibration[1]).toMatchObject({ current: 1, total: 2 });
    expect(calibration[3]).toMatchObject({ current: 2, total: 4 });
    expect(calibration[4]).toMatchObject({ label: "Capture", current: null, total: null });
    expect(calibration[4]?.detail).toContain("1/2 items");
  });

  it("turns executor decisions into the live scheduler operation", () => {
    const run = testSnapshot({
      run: {
        run_id: "run", plan_id: "plan", plan_name: "Night", plan_revision: 1,
        night_date: "2026-09-15", status: "running", started_at: "2026-09-15T20:00:00Z",
        ended_at: null, ended_reason: null, current_item_id: "panel", gate_held: false,
        gate_reason: null, last_decision: "issuing dither (attempt 25)",
        last_decision_at: "2026-09-15T20:15:00Z", journal_length: 50,
        available_actions: [], items: [
          { item_id: "panel", kind: "goto_and_stack", enabled: true, status: "running", attempt: 25, panel_index: null, held: false, reason: "issuing dither (attempt 25)", start_utc: null, end_utc: null },
        ],
      },
    }).run;
    if (run === null) throw new Error("expected run fixture");
    expect(schedulerOperationLabel(run)).toBe("Dithering and settling");
  });

  it("reports live-stack work and each background job shape", () => {
    const liveStack = {
      sequence: 1, output_path: "stack.fits", parameters_path: "stack.json", frame_count: 12,
      dark_master: null, bias_master: null, flat_master: null, active: true, error: null,
      processing_file: "/data/light_0012.fits", pending_files: 3,
    } satisfies LiveStackResponse;
    expect(select({ liveStack })[0]).toMatchObject({ id: "live-stack", current: null, total: null });

    const snapshot = testSnapshot({
      discovery: { running: true, scan_id: "scan", started_at: null, finished_at: null, completed: 3, total: 8, found: [], cancelled: false, error: null },
    });
    expect(select({ snapshot })[0]).toMatchObject({ id: "discovery", current: 3, total: 8 });

    const maps = testSnapshot({
      skymap: {
        surveys: [], cache_bytes: 0, cache_max_bytes: 1, upstream_reachable: true,
        cutouts: [], cutouts_bytes: 0, cutouts_max_bytes: 1, bulk_cutout_jobs: [],
        jobs: [{
          id: "job", survey: "DSS2", mode: "sky", max_order: 4, ra_deg: null, dec_deg: null,
          radius_deg: null, status: "running", done: 25, total: 100, missing: 0, bytes: 1,
          error: null, created_at: "2026-09-14T20:00:00Z", updated_at: "2026-09-14T20:00:00Z", finished_at: null,
        }],
      },
    });
    expect(select({ snapshot: maps })[0]).toMatchObject({ id: "skymap-pack", current: 25, total: 100 });
  });

  it("keeps deterministic priority and returns every concurrent activity", () => {
    const activities = select({
      capture: capture({ in_progress: true, stage: "saving" }),
      liveStack: {
        sequence: 1, output_path: "stack.fits", parameters_path: "stack.json", frame_count: 1,
        dark_master: null, bias_master: null, flat_master: null, active: true, error: null,
        pending_files: 2,
      },
      snapshot: testSnapshot({
        discovery: { running: true, scan_id: "scan", started_at: null, finished_at: null, completed: 1, total: 2, found: [], cancelled: false, error: null },
      }),
      mount: {
        polled_at: null, stale: false, ra_hours: null, dec_deg: null, alt_deg: null, az_deg: null,
        sidereal_time_hours: null, tracking: null, tracking_rate: null, at_park: null, at_home: null,
        slewing: true, equatorial_system: null, capabilities: null,
      },
      filterWheel: { names: null, position: -1 },
      focuser: { position: 120, is_moving: true },
    });
    expect(activities.map((item) => item.id)).toEqual([
      "capture", "mount-slew", "filter-wheel", "focuser-move", "live-stack-queue", "discovery",
    ]);
  });
});
