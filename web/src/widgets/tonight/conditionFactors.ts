// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { components } from "../../api/schema";
import { translate, type Language } from "../../i18n/locale";

export type ConditionFactor = components["schemas"]["CaptureConditionFactorModel"];
export type ConditionFactorName = components["schemas"]["ConditionFactorName"];

export const CONDITION_FACTOR_LABEL: Record<ConditionFactorName, string> = {
  clouds: "Clouds",
  moon: "Moon",
  dew_point: "Dew-point margin",
  wind: "Wind",
  precipitation: "Precipitation",
  humidity: "Humidity",
};

/** Cloud measurements are three server-owned phrases joined by commas;
 * localize their labels without treating the remaining values as one capture. */
export function localizedFactorMeasurement(factor: ConditionFactor, locale: Language): string {
  return factor.name === "clouds"
    ? factor.measurement.split(", ").map((part) => translate(part, locale)).join(", ")
    : translate(factor.measurement, locale);
}

export function factorImpact(factor: ConditionFactor): string {
  if (factor.points_deducted === null) return "Not scored: data unavailable";
  if (factor.points_deducted < 0.05) return "No score reduction";
  return `−${factor.points_deducted.toFixed(1)} points`;
}
