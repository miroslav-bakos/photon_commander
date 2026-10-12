// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { create } from "zustand";
import { useSimulatePolarAlignmentMutation, type PolarAlignmentPointState, type PolarAlignmentState } from "../../api/polarAlignment";
import { usePolarDebugStore } from "../../store/polarDebugStore";

const pause = (milliseconds: number) => new Promise<void>((resolve) => { window.setTimeout(resolve, milliseconds); });
let generation = 0;

interface SimulationSession {
  state: PolarAlignmentState | null;
  siteLatitude: number;
  busy: boolean;
}

export const usePolarSimulationSession = create<SimulationSession>(() => ({
  state: null, siteLatitude: 48.15, busy: false,
}));

function pendingPoint(index: number): PolarAlignmentPointState {
  return {
    index, status: "pending", target_ra_j2000_h: 0, target_dec_j2000_deg: 0,
    planned_altitude_deg: null, actual_altitude_deg: null, actual_azimuth_deg: null,
    mount_slewing: null, mount_tracking: null, solved_ra_j2000_h: null,
    solved_dec_j2000_deg: null, attempt: 0, error: null,
  };
}

export function usePolarSimulationWorkflow() {
  const altitudeArcsec = usePolarDebugStore((debug) => debug.altitudeAdjustmentArcsec);
  const azimuthArcsec = usePolarDebugStore((debug) => debug.azimuthAdjustmentArcsec);
  const simulate = useSimulatePolarAlignmentMutation();
  const state = usePolarSimulationSession((session) => session.state);
  const siteLatitude = usePolarSimulationSession((session) => session.siteLatitude);
  const busy = usePolarSimulationSession((session) => session.busy);
  const setSession = usePolarSimulationSession.setState;

  async function run(pointCount: number, recheck: boolean): Promise<void> {
    if (busy) return;
    const token = ++generation;
    const startedAt = new Date().toISOString();
    const kind = recheck && state?.result != null ? "recheck" : "measure";
    const history = state?.history ?? [];
    const points = Array.from({ length: pointCount }, (_, index) => pendingPoint(index));
    setSession({ busy: true, state: {
      status: "running", run_kind: kind, started_at: startedAt, ended_at: null,
      exposure_started_at: null, exposure_duration_s: null, current_point_index: 0,
      preflight_reason: "Artificial sky · no device commands", points, result: null,
      history, assessment: null, start: null, error: null,
    } });
    try {
      const response = await simulate.mutateAsync({
        altitude_adjustment_deg: altitudeArcsec / 3600,
        azimuth_adjustment_deg: azimuthArcsec / 3600,
        point_count: pointCount,
      });
      if (generation !== token) return;
      setSession({ siteLatitude: response.site_latitude_deg });
      for (const point of response.points) {
        for (const status of ["slewing", "capturing", "solving", "solved"] as const) {
          if (generation !== token) return;
          setSession((session) => ({ state: session.state == null ? null : {
            ...session.state,
            current_point_index: point.index,
            points: session.state.points?.map((entry) => entry.index === point.index ? {
              ...entry, status, target_ra_j2000_h: point.ra_j2000_hours,
              target_dec_j2000_deg: point.dec_j2000_deg,
              solved_ra_j2000_h: status === "solved" ? point.ra_j2000_hours : null,
              solved_dec_j2000_deg: status === "solved" ? point.dec_j2000_deg : null,
              attempt: status === "solved" ? 2 : 1,
            } : entry),
          } }));
          await pause(160);
        }
      }
      if (generation !== token) return;
      const completedAt = new Date().toISOString();
      setSession((session) => ({ state: session.state == null ? null : {
        ...session.state, status: "completed", ended_at: completedAt, current_point_index: null,
        result: response.result, history: [...history, {
          run_kind: kind, completed_at: completedAt,
          altitude_error_deg: response.result.altitude_error_deg,
          azimuth_error_deg: response.result.azimuth_error_deg,
          total_error_deg: response.result.total_error_deg,
        }],
      } }));
    } catch (error) {
      if (generation !== token) return;
      setSession((session) => ({ state: session.state == null ? null : {
        ...session.state, status: "failed", ended_at: new Date().toISOString(),
        current_point_index: null, error: error instanceof Error ? error.message : "Simulation failed",
      } }));
    } finally {
      if (generation === token) setSession({ busy: false });
    }
  }

  function cancel(): void {
    generation += 1;
    setSession((session) => ({ busy: false, state: session.state == null ? null : {
      ...session.state, status: "cancelled", ended_at: new Date().toISOString(), current_point_index: null,
    } }));
  }

  function reset(): void {
    generation += 1;
    setSession({ busy: false, state: null });
  }

  return { state, siteLatitude, busy, run, cancel, reset };
}
