// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { ConditionsCategory, TonightResponse } from "../tonight/api";
import { CONDITION_FACTOR_LABEL, factorImpact } from "../tonight/conditionFactors";

export const QUALITY_LABEL: Record<ConditionsCategory, string> = {
  excellent: "Excellent",
  good: "Good",
  fair: "Fair",
  poor: "Poor",
  very_poor: "Very poor",
};

export const QUALITY_TONE: Record<ConditionsCategory, "success" | "warning" | "danger"> = {
  excellent: "success",
  good: "success",
  fair: "warning",
  poor: "warning",
  very_poor: "danger",
};

export function observingDate(now: Date, timezone: string): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const value = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? "00";
  return `${value("year")}-${value("month")}-${value("day")}`;
}

function localDateParts(instant: Date, timezone: string): { iso: string; hour: number } {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
  }).formatToParts(instant);
  const value = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? "00";
  return {
    iso: `${value("year")}-${value("month")}-${value("day")}`,
    hour: Number(value("hour")),
  };
}

function shiftDate(iso: string, days: number): string {
  const date = new Date(`${iso}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

/** Forecast timestamps before local noon belong to the preceding observing night. */
export function forecastNightDates(points: { t: string }[], timezone: string, today: string): Set<string> {
  const nights = new Set([today]);
  for (const point of points) {
    const local = localDateParts(new Date(point.t), timezone);
    const nightDate = local.hour < 12 ? shiftDate(local.iso, -1) : local.iso;
    if (nightDate >= today) nights.add(nightDate);
  }
  return nights;
}

export function describeNightQuality(
  prediction: TonightResponse | undefined,
  pending: boolean,
  error: boolean,
) {
  if (pending) return { label: "Checking", tone: "neutral" as const, detail: "Calculating this night's prediction." };
  if (error || prediction === undefined) return { label: "Unavailable", tone: "neutral" as const, detail: "The night prediction could not be loaded." };
  if (prediction.conditions === null) {
    return {
      label: "No forecast",
      tone: "neutral" as const,
      detail: prediction.conditions_unavailable_reason ?? "No forecast covers this night yet.",
    };
  }
  const staleSince = prediction.weather?.stale_since;
  const factorDetail = prediction.conditions.factors
    .map((factor) => `${CONDITION_FACTOR_LABEL[factor.name]}: ${factor.measurement}; ${factorImpact(factor)}`)
    .join(". ");
  return {
    label: `${QUALITY_LABEL[prediction.conditions.category]} ${Math.round(prediction.conditions.score).toString()}${staleSince === null || staleSince === undefined ? "" : " · stale"}`,
    tone: QUALITY_TONE[prediction.conditions.category],
    detail: `${QUALITY_LABEL[prediction.conditions.category]} — ${Math.round(prediction.conditions.score).toString()}/100. ${prediction.conditions.reasons.join(". ")} Score breakdown: ${factorDetail}.${staleSince === null || staleSince === undefined ? "" : ` Forecast is stale since ${new Date(staleSince).toLocaleString()}.`}`,
  };
}
