// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import { TrackingOffIcon, TrackingOnIcon } from "../../components/ActionIcons";
import { Button } from "../../components/Button";
import type { MountState, MountTrackingRate } from "../../api/mount";
import { useMountTrackingMutation } from "../../api/mount";
import { formatBool } from "./format";

const RATE_LABEL: Record<MountTrackingRate, string> = {
  sidereal: "Sidereal",
  lunar: "Lunar",
  solar: "Solar",
};

/** No King rate, no custom RA/Dec offsets on this device — DESIGN.md
 * §4.4b — so only the three rates the device itself reports are ever
 * offered, straight from `capabilities.tracking_rates`. */
export function TrackingCard({ mount }: { mount: MountState }) {
  const tracking = useMountTrackingMutation();
  const caps = mount.capabilities;
  const rates = caps?.tracking_rates ?? [];
  const [selectedRate, setSelectedRate] = useState<MountTrackingRate | null>(mount.tracking_rate ?? null);
  const rate = selectedRate ?? mount.tracking_rate ?? rates[0] ?? null;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm text-fg-muted">
          Tracking: <span className="text-fg">{formatBool(mount.tracking, "on", "off")}</span>
        </span>
        {caps?.can_set_tracking === true ? (
          <Button
            variant={mount.tracking === true ? "secondary" : "primary"}
            onClick={() => { tracking.mutate({ enabled: mount.tracking !== true, ...(rate !== null ? { rate } : {}) }); }
            }
          >
            {mount.tracking === true ? <TrackingOffIcon /> : <TrackingOnIcon />}
            {mount.tracking === true ? "Turn tracking off" : "Turn tracking on"}
          </Button>
        ) : (
          <span className="text-xs text-fg-disabled">This device does not accept a tracking command.</span>
        )}
      </div>
      {caps?.can_set_tracking === true && rates.length > 0 && (
        <div className="flex items-center gap-2">
          <label htmlFor="mount-tracking-rate" className="text-xs tracking-wide text-fg-muted uppercase">
            Rate
          </label>
          <select
            id="mount-tracking-rate"
            className="min-h-11 rounded-md border border-border-strong bg-surface-raised px-2 text-sm text-fg"
            value={rate ?? ""}
            onChange={(e) => { setSelectedRate(e.target.value as MountTrackingRate); }}
          >
            {rates.map((r) => (
              <option key={r} value={r}>
                {RATE_LABEL[r]}
              </option>
            ))}
          </select>
        </div>
      )}
      {tracking.isError && (
        <p role="alert" className="text-sm text-danger">
          {tracking.error instanceof Error ? tracking.error.message : "Could not change tracking."}
        </p>
      )}
    </div>
  );
}
