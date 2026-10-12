// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { ForecastPoint } from "../../api/weather";
import type { AxisRange } from "./timelineGeometry";

export type WeatherQuality = "good" | "marginal" | "poor" | "unknown";
export type WeatherIcon = "clear" | "partly-cloudy" | "cloudy" | "rain" | "wind" | "fog";

export interface WeatherAssessment {
  quality: WeatherQuality;
  icon: WeatherIcon;
  label: string;
  dewMarginC: number | null;
}

export interface WeatherSlot {
  point: ForecastPoint;
  startMs: number;
  endMs: number;
  assessment: WeatherAssessment;
}

/** The same 30/60% cloud boundaries used by the scheduler's pause gate. */
export function assessWeather(point: ForecastPoint): WeatherAssessment {
  const cloud = point.cloud_total;
  const rain = point.precip_prob;
  const wind = point.wind_ms;
  const dewMarginC = point.temp_c === null || point.dew_point_c === null ? null : point.temp_c - point.dew_point_c;
  const lowVisibility = point.visibility_m !== null && point.visibility_m < 3_000;

  const poor = (cloud !== null && cloud >= 60)
    || (rain !== null && rain >= 50)
    || (wind !== null && wind >= 10)
    || (dewMarginC !== null && dewMarginC <= 1)
    || lowVisibility;
  const marginal = (cloud !== null && cloud >= 30)
    || (rain !== null && rain >= 20)
    || (wind !== null && wind >= 7)
    || (dewMarginC !== null && dewMarginC <= 2.5)
    || (point.visibility_m !== null && point.visibility_m < 8_000);
  const quality: WeatherQuality = poor ? "poor" : marginal ? "marginal" : cloud === null ? "unknown" : "good";

  const icon: WeatherIcon = rain !== null && rain >= 30
    ? "rain"
    : lowVisibility || (dewMarginC !== null && dewMarginC <= 1)
      ? "fog"
      : wind !== null && wind >= 10
        ? "wind"
        : cloud !== null && cloud >= 60
          ? "cloudy"
          : cloud !== null && cloud >= 15
            ? "partly-cloudy"
            : "clear";
  const label = icon === "rain" ? "Rain risk"
    : icon === "fog" ? "Fog or dew risk"
      : icon === "wind" ? "Strong wind"
        : icon === "cloudy" ? "Cloudy"
          : icon === "partly-cloudy" ? "Partly cloudy"
            : cloud === null ? "Conditions unknown" : "Clear";
  return { quality, icon, label, dewMarginC };
}

/** Centre each forecast point in its validity slot, clipped to the timeline. */
export function weatherSlots(points: readonly ForecastPoint[], range: AxisRange): WeatherSlot[] {
  const sorted = points
    .filter((point) => Number.isFinite(Date.parse(point.t)))
    .toSorted((a, b) => Date.parse(a.t) - Date.parse(b.t));
  return sorted.flatMap((point, index) => {
    const at = Date.parse(point.t);
    const previous = sorted[index - 1];
    const next = sorted[index + 1];
    const previousAt = previous === undefined ? at - 3_600_000 : Date.parse(previous.t);
    const nextAt = next === undefined ? at + 3_600_000 : Date.parse(next.t);
    const startMs = Math.max(range.startMs, (previousAt + at) / 2);
    const endMs = Math.min(range.endMs, (at + nextAt) / 2);
    return endMs <= startMs ? [] : [{ point, startMs, endMs, assessment: assessWeather(point) }];
  });
}

export function weatherDetail(slot: WeatherSlot): string {
  const { point, assessment } = slot;
  const parts = [assessment.label];
  if (point.cloud_total !== null) parts.push(`${String(Math.round(point.cloud_total))}% cloud`);
  if (point.precip_prob !== null) parts.push(`${String(Math.round(point.precip_prob))}% rain`);
  if (point.temp_c !== null) parts.push(`${point.temp_c.toFixed(1)}°C`);
  if (assessment.dewMarginC !== null) parts.push(`${assessment.dewMarginC.toFixed(1)}°C above dew point`);
  if (point.wind_ms !== null) parts.push(`${String(Math.round(point.wind_ms * 3.6))} km/h wind`);
  return parts.join(" · ");
}
