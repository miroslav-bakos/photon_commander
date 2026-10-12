// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §3.3: "Drag a grid over the map: `ra_num x dec_num` with
 * `overlap_pct`. Show total panels x per-panel time = session estimate. In
 * alt-az mode, warn that each panel rotates too."
 *
 * Panel count and the tiling outline both come straight off
 * `MosaicResponse` — nothing here computes panel spacing or overlap, that is
 * `ssc.astro.mosaic`'s job. The "per-panel time" half of the estimate is a
 * placeholder the user fills in (this app does not yet know a per-panel
 * exposure plan — that is the Scheduling tab, DESIGN.md §4), multiplied by
 * the server-reported `panel_count`.
 */
import { useState } from "react";
import { Card } from "../../components/Card";
import { DraftNumberField } from "../../components/DraftNumberField";
import type { MosaicResponse, MountMode } from "../../api/astro";

export function MosaicPanel({
  raNum,
  decNum,
  overlapPct,
  mount,
  response,
  isFetching,
  onChange,
  embedded = false,
}: {
  raNum: number;
  decNum: number;
  overlapPct: number;
  mount: MountMode;
  response: MosaicResponse | undefined;
  isFetching: boolean;
  onChange: (next: { raNum: number; decNum: number; overlapPct: number }) => void;
  /** A Planning modal already supplies the titled surface. */
  embedded?: boolean;
}) {
  const [perPanelMinutes, setPerPanelMinutes] = useState(30);
  const isMosaic = raNum > 1 || decNum > 1;
  const panelCount = response?.panel_count ?? raNum * decNum;
  const estimateHours = (panelCount * perPanelMinutes) / 60;

  const content = (
    <>
      <div className="flex flex-wrap items-end gap-2">
        <DraftNumberField
          label="RA panels"
          min={1}
          max={12}
          value={raNum}
          onValueChange={(value) => { if (value !== null) onChange({ raNum: clamp(Math.round(value), 1, 12), decNum, overlapPct }); }}
          className="w-24"
        />
        <DraftNumberField
          label="Dec panels"
          min={1}
          max={12}
          value={decNum}
          onValueChange={(value) => { if (value !== null) onChange({ raNum, decNum: clamp(Math.round(value), 1, 12), overlapPct }); }}
          className="w-24"
        />
        <DraftNumberField
          label="Overlap %"
          min={0}
          max={90}
          value={overlapPct}
          onValueChange={(value) => { if (value !== null) onChange({ raNum, decNum, overlapPct: clamp(value, 0, 90) }); }}
          className="w-24"
        />
      </div>

      {isMosaic && (
        <>
          <DraftNumberField
            label="Estimated minutes per panel"
            min={1}
            value={perPanelMinutes}
            onValueChange={(value) => { if (value !== null && value > 0) setPerPanelMinutes(Math.round(value)); }}
            className="w-40"
          />

          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-fg-muted">Panels</dt>
            <dd className="text-fg tabular-nums">
              {panelCount} {isFetching && "(updating…)"}
            </dd>
            <dt className="text-fg-muted">Estimated session time</dt>
            <dd className="text-fg tabular-nums">{estimateHours.toFixed(1)}h</dd>
          </dl>

          {mount === "altaz" && (
            <p className="rounded-md border border-warning bg-surface-raised px-2 py-1.5 text-xs text-warning">
              Alt-az mode: each panel rotates independently over the session — field rotation and mosaic tiling compound.
            </p>
          )}
          {response !== undefined && response.worst_rotation_severity !== "none" && (
            <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-xs text-danger">
              Worst panel rotation severity: {response.worst_rotation_severity}.
            </p>
          )}
          {response?.warnings.map((warning) => (
            <p key={warning} className="rounded-md border border-warning bg-surface-raised px-2 py-1.5 text-xs text-warning">
              {warning}
            </p>
          ))}
        </>
      )}

    </>
  );

  if (embedded) return <div className="space-y-3">{content}</div>;
  return <Card title="Mosaic grid" collapseKey="planning.mosaic" className="space-y-3">{content}</Card>;
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}
