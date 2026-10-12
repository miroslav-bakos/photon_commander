// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./client";
import type { components } from "./schema";

export type WeatherForecast = components["schemas"]["WeatherForecastResponse"];
export type ForecastPoint = components["schemas"]["ForecastPointModel"];

export function useForecastQuery(enabled: boolean) {
  return useQuery({
    queryKey: ["weather", "forecast"] as const,
    queryFn: async () => unwrap(api.GET("/api/weather/forecast")),
    enabled,
    refetchInterval: 5 * 60_000,
    staleTime: 60_000,
    retry: false,
  });
}

export function nearestForecastPoint(points: ForecastPoint[], now = Date.now()): ForecastPoint | null {
  if (points.length === 0) return null;
  return points.reduce((best, point) =>
    Math.abs(new Date(point.t).getTime() - now) < Math.abs(new Date(best.t).getTime() - now) ? point : best,
  );
}
