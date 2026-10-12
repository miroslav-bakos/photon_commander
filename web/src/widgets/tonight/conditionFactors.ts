// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { components } from "../../api/schema";

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

export function factorImpact(factor: ConditionFactor): string {
  if (factor.points_deducted === null) return "Not scored: data unavailable";
  if (factor.points_deducted < 0.05) return "No score reduction";
  return `−${factor.points_deducted.toFixed(1)} points`;
}
