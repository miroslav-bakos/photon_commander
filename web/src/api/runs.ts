// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Typed mutations for the scheduler executor. The live run itself is not
 * cached here: it belongs to `StateSnapshot.run` and arrives over the shared
 * WebSocket, so every client renders the same journal-derived state.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./client";
import type { components } from "./schema";

export type RunState = components["schemas"]["RunState"];
export type RunDetailResponse = components["schemas"]["RunDetailResponse"];
export type HistoricalFrameSummary = components["schemas"]["HistoricalFrameSummary"];

export function useHistoricalNightQuery(nightDate: string) {
  return useQuery({
    queryKey: ["runs", "night", nightDate] as const,
    retry: false,
    refetchInterval: 30000,
    queryFn: async () => unwrap(api.GET("/api/runs/nights/{night_date}", {
      params: { path: { night_date: nightDate } },
    })),
  });
}

export function useRunHistoryQuery(startDate: string, endDate: string) {
  return useQuery({
    queryKey: ["runs", "history", startDate, endDate] as const,
    retry: false,
    refetchInterval: 30000,
    queryFn: async () => unwrap(api.GET("/api/runs/history", {
      params: { query: { start_date: startDate, end_date: endDate } },
    })),
  });
}

export function useLatestRunQuery(enabled: boolean) {
  return useQuery({
    queryKey: ["runs", "latest"] as const,
    enabled,
    refetchInterval: 5000,
    retry: false,
    queryFn: async () => unwrap(api.GET("/api/runs/latest")),
  });
}

export function useRunDetailQuery(runId: string | null) {
  return useQuery({
    queryKey: ["runs", "detail", runId] as const,
    enabled: runId !== null,
    refetchInterval: 5000,
    retry: false,
    queryFn: async () => {
      if (runId === null) throw new Error("A run id is required.");
      return unwrap(api.GET("/api/runs/{run_id}", { params: { path: { run_id: runId } } }));
    },
  });
}

export function useStartRun() {
  return useMutation({
    mutationFn: async ({ planId, nightDate }: { planId: string; nightDate: string }) =>
      unwrap(api.POST("/api/runs", { body: { plan_id: planId, night_date: nightDate } })),
  });
}

export function usePauseRun() {
  return useMutation({
    mutationFn: async (runId: string) =>
      unwrap(
        api.POST("/api/runs/{run_id}/pause", {
          params: { path: { run_id: runId } },
          body: { reason: "Paused from Photon Commander" },
        }),
      ),
  });
}

export function useResumeRun() {
  return useMutation({
    mutationFn: async (runId: string) =>
      unwrap(
        api.POST("/api/runs/{run_id}/resume", {
          params: { path: { run_id: runId } },
          body: { reason: "Resumed from Photon Commander" },
        }),
      ),
  });
}

export function useStopRun() {
  return useMutation({
    mutationFn: async (runId: string) =>
      unwrap(
        api.POST("/api/runs/{run_id}/stop", {
          params: { path: { run_id: runId } },
          body: { reason: "Stopped from Photon Commander" },
        }),
      ),
  });
}

export function useResumeInterruptedRun() {
  return useMutation({
    mutationFn: async (runId: string) =>
      unwrap(
        api.POST("/api/runs/{run_id}/resume-interrupted", {
          params: { path: { run_id: runId } },
        }),
      ),
  });
}
