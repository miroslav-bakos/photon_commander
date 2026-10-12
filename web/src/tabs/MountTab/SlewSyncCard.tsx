// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { LoaderCircle } from "lucide-react";
import { Button } from "../../components/Button";
import { SlewHereIcon, SyncHereIcon } from "../../components/ActionIcons";
import { TargetSearchField, type TargetSearchSelection } from "../../components/TargetSearchField";
import type { MountState } from "../../api/mount";
import { useMountSlewMutation, useMountSyncMutation } from "../../api/mount";
import { MountSlewSyncControls } from "../../skymap/MountSlewSyncControls";
import { MountMotionStopButton, type ActiveMountMotion } from "./MountMotionStopButton";

/**
 * RA/Dec only — `CanSlewAltAz`/`CanSyncAltAz` are both false on this device
 * (DESIGN.md §4.4b), so there is no alt/az target field to offer. Values
 * entered here are J2000, matching the catalogue and the mount API. The
 * server converts them into the connected mount's declared frame immediately
 * before `SlewToCoordinatesAsync`/`SyncToCoordinates`.
 */
export function SlewSyncCard({
  mount,
  activeMotion,
  setActiveMotion,
}: {
  mount: MountState;
  activeMotion: ActiveMountMotion | null;
  setActiveMotion: Dispatch<SetStateAction<ActiveMountMotion | null>>;
}) {
  const [selectedTarget, setSelectedTarget] = useState<TargetSearchSelection | null>(null);
  const [searchInstant] = useState(() => new Date());
  const [raText, setRaText] = useState("");
  const [decText, setDecText] = useState("");
  const slew = useMountSlewMutation();
  const sync = useMountSyncMutation();
  const caps = mount.capabilities;
  const slewActive = activeMotion === "slew";

  function clearSlew(): void {
    setActiveMotion((current) => current === "slew" ? null : current);
  }
  const selectedCoordinates = selectedTarget === null ? null : selectedTarget.kind === "body"
    ? {
        label: selectedTarget.body.name,
        detail: "Solar-system body at the current time",
        raHoursJ2000: selectedTarget.body.ra_j2000_h,
        decDegJ2000: selectedTarget.body.dec_j2000_deg,
      }
    : {
        label: selectedTarget.result.name,
        detail: selectedTarget.result.common_name,
        raHoursJ2000: selectedTarget.result.ra_j2000_h,
        decDegJ2000: selectedTarget.result.dec_j2000_deg,
      };

  const raHours = Number(raText);
  const decDeg = Number(decText);
  const valid =
    raText.trim() !== "" &&
    decText.trim() !== "" &&
    Number.isFinite(raHours) &&
    Number.isFinite(decDeg) &&
    raHours >= 0 &&
    raHours < 24 &&
    decDeg >= -90 &&
    decDeg <= 90;

  if (caps?.can_slew_async !== true && caps?.can_sync !== true) {
    return <p className="text-sm text-fg-muted">This mount does not accept a coordinate target.</p>;
  }

  return (
    <div className="space-y-3">
      <TargetSearchField label="Search target" instant={searchInstant} onSelect={setSelectedTarget} />

      {selectedCoordinates !== null && (
        <div className="space-y-2 rounded-md border border-border bg-surface-raised p-3 text-sm">
          <div>
            <p className="font-medium text-fg">{selectedCoordinates.label}</p>
            {selectedCoordinates.detail !== null && (
              <p className="text-xs text-fg-muted">{selectedCoordinates.detail}</p>
            )}
            <p className="mt-1 text-xs text-fg-muted tabular-nums">
              J2000 {selectedCoordinates.raHoursJ2000.toFixed(4)}h / {selectedCoordinates.decDegJ2000.toFixed(3)}°
            </p>
          </div>
          <MountSlewSyncControls
            target={{
              label: selectedCoordinates.label,
              raHoursJ2000: selectedCoordinates.raHoursJ2000,
              decDegJ2000: selectedCoordinates.decDegJ2000,
            }}
          />
        </div>
      )}

      <p className="text-xs tracking-wide text-fg-muted uppercase">Or enter J2000 coordinates</p>
      <div className="flex flex-wrap gap-3">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs tracking-wide text-fg-muted uppercase">J2000 RA (hours, 0–24)</span>
          <input
            type="number"
            inputMode="decimal"
            step="any"
            min={0}
            max={23.9999}
            value={raText}
            onChange={(e) => { setRaText(e.target.value); }}
            className="min-h-11 w-32 rounded-md border border-border-strong bg-surface-raised px-2 text-fg"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs tracking-wide text-fg-muted uppercase">J2000 Dec (degrees, -90–90)</span>
          <input
            type="number"
            inputMode="decimal"
            step="any"
            min={-90}
            max={90}
            value={decText}
            onChange={(e) => { setDecText(e.target.value); }}
            className="min-h-11 w-32 rounded-md border border-border-strong bg-surface-raised px-2 text-fg"
          />
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        {caps.can_slew_async && (
          <Button
            variant="primary"
            aria-busy={slewActive}
            disabled={!valid || activeMotion !== null || slew.isPending}
            onClick={() => {
              setActiveMotion("slew");
              slew.mutate(
                { ra_hours: raHours, dec_deg: decDeg },
                { onError: clearSlew },
              );
            }}
          >
            {slewActive
              ? <LoaderCircle aria-hidden="true" className="size-5 shrink-0 animate-spin" strokeWidth={1.8} />
              : <SlewHereIcon />}
            {slewActive ? "Slewing…" : "Slew here"}
          </Button>
        )}
        {slewActive && (
          <MountMotionStopButton
            disabled={slew.isPending}
            onStopped={clearSlew}
          />
        )}
        {caps.can_sync && (
          <Button
            aria-busy={sync.isPending}
            disabled={!valid || activeMotion !== null || sync.isPending}
            onClick={() => { sync.mutate({ ra_hours: raHours, dec_deg: decDeg }); }}
          >
            {sync.isPending
              ? <LoaderCircle aria-hidden="true" className="size-5 shrink-0 animate-spin" strokeWidth={1.8} />
              : <SyncHereIcon />}
            {sync.isPending ? "Syncing…" : "Sync to here"}
          </Button>
        )}
      </div>
      {slew.isError && (
        <p role="alert" className="text-sm text-danger">
          {slew.error instanceof Error ? slew.error.message : "Slew failed."}
        </p>
      )}
      {sync.isError && (
        <p role="alert" className="text-sm text-danger">
          {sync.error instanceof Error ? sync.error.message : "Sync failed."}
        </p>
      )}
    </div>
  );
}
