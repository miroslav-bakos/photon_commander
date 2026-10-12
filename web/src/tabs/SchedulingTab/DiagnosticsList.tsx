// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Plan diagnostics with a severity badge each — from `/api/plans/validate`
 * (structural, while editing) or from a compile (per night). Nothing is
 * filtered out by severity here: an `info` is still something the server
 * chose to say (architecture policy invariant 7).
 */
import { Badge } from "../../components/Badge";
import type { Diagnostic } from "../../api/plans";
import { SEVERITY_TONE } from "./severity";

export function DiagnosticsList({
  diagnostics,
  itemLabel,
  onSelectItem,
}: {
  diagnostics: readonly Diagnostic[];
  /** How to name a diagnostic's item in the list, when it has one. */
  itemLabel?: (itemId: string) => string;
  onSelectItem?: (itemId: string) => void;
}) {
  if (diagnostics.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1.5" aria-label="Diagnostics">
      {diagnostics.map((d, index) => (
        <li key={`${d.code}-${d.item_id ?? "plan"}-${String(index)}`} className="flex flex-col items-start gap-1 text-sm sm:flex-row sm:gap-2">
          <Badge tone={SEVERITY_TONE[d.severity]} className="shrink-0">
            {d.severity}
          </Badge>
          <span className="min-w-0 text-fg">
            {d.item_id !== null && itemLabel !== undefined && (
              onSelectItem !== undefined ? (
                <button
                  type="button"
                  className="mr-1 inline-flex min-h-[44px] items-center text-link underline-offset-2 hover:underline lg:min-h-8"
                  onClick={() => {
                    onSelectItem(d.item_id ?? "");
                  }}
                >
                  {itemLabel(d.item_id)}:
                </button>
              ) : (
                <span className="mr-1 text-fg-muted">{itemLabel(d.item_id)}:</span>
              )
            )}
            {d.message}
          </span>
        </li>
      ))}
    </ul>
  );
}
