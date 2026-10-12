// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import type { Plan } from "../../api/plans";

/** Compare saved content, never server bookkeeping or JSON property order. */
export function samePlanContent(left: Plan, right: Plan): boolean {
  const content = (plan: Plan) => {
    const { updated_at: _updatedAt, ...rest } = plan;
    return JSON.stringify(rest, (_key, value: unknown) => {
      if (value !== null && typeof value === "object" && !Array.isArray(value)) {
        return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)));
      }
      return value;
    });
  };
  return content(left) === content(right);
}
