// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useQuery } from "@tanstack/react-query";
import type { components } from "./schema";
import { api, unwrap } from "./client";

export type SystemMetrics = components["schemas"]["SystemMetricsResponse"];

export interface SystemHistoryPoint {
  at: number;
  cpu: number;
  memory: number;
  temperature: number | null;
  processCpu: number;
  receiveMiBs: number;
  sendMiBs: number;
  receivedBytes: number;
  sentBytes: number;
}

const MAX_HISTORY_POINTS = 180;
let historyHost: string | null = null;
let history: SystemHistoryPoint[] = [];

function record(metrics: SystemMetrics): SystemHistoryPoint[] {
  const at = Date.parse(metrics.collected_at);
  if (historyHost !== metrics.hostname) {
    historyHost = metrics.hostname;
    history = [];
  }
  if (history.at(-1)?.at === at) return history;
  const prior = history.at(-1);
  const countersReset = prior !== undefined && (
    metrics.network.bytes_received < prior.receivedBytes || metrics.network.bytes_sent < prior.sentBytes
  );
  const elapsed = prior === undefined ? 0 : Math.max(0.001, (at - prior.at) / 1000);
  const mib = 1024 * 1024;
  const point: SystemHistoryPoint = {
    at,
    cpu: metrics.cpu.percent,
    memory: metrics.memory.percent_used,
    temperature: metrics.cpu.temperature_c,
    processCpu: metrics.process.cpu_percent,
    receiveMiBs: prior === undefined || countersReset ? 0 : Math.max(0, metrics.network.bytes_received - prior.receivedBytes) / elapsed / mib,
    sendMiBs: prior === undefined || countersReset ? 0 : Math.max(0, metrics.network.bytes_sent - prior.sentBytes) / elapsed / mib,
    receivedBytes: metrics.network.bytes_received,
    sentBytes: metrics.network.bytes_sent,
  };
  history = [...history, point].slice(-MAX_HISTORY_POINTS);
  return history;
}

export function useSystemMetricsQuery() {
  return useQuery({
    queryKey: ["system", "metrics"],
    queryFn: async () => {
      const metrics = await unwrap(api.GET("/api/system/metrics"));
      return { metrics, history: record(metrics) };
    },
    refetchInterval: 2_000,
  });
}
