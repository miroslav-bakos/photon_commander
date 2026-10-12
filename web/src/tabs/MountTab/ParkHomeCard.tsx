// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Button } from "../../components/Button";
import { CircleParking, CircleParkingOff, LoaderCircle, MapPinHouse } from "lucide-react";
import type { Dispatch, SetStateAction } from "react";
import type { MountState } from "../../api/mount";
import { useMountFindHomeMutation, useMountParkMutation, useMountUnparkMutation } from "../../api/mount";
import { formatBool } from "./format";
import { MountMotionStopButton, type ActiveMountMotion } from "./MountMotionStopButton";

/**
 * `CanSetPark` is false on this device — DESIGN.md §4.4b — so there is no
 * "set park position" control here, only park/unpark against the device's
 * own fixed position. `Unpark` permits motion; it does not itself raise the
 * arm (correct ASCOM semantics), so its button is never worded as "open
 * arm" — that would claim success while leaving the scope shut.
 */
export function ParkHomeCard({
  mount,
  activeMotion,
  setActiveMotion,
}: {
  mount: MountState;
  activeMotion: ActiveMountMotion | null;
  setActiveMotion: Dispatch<SetStateAction<ActiveMountMotion | null>>;
}) {
  const park = useMountParkMutation();
  const unpark = useMountUnparkMutation();
  const findHome = useMountFindHomeMutation();
  const caps = mount.capabilities;
  const parkActive = activeMotion === "park";
  const findHomeActive = activeMotion === "find-home";

  function clearMotion(expected: ActiveMountMotion): void {
    setActiveMotion((current) => current === expected ? null : current);
  }

  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-fg-muted">Parked</dt>
        <dd className="text-fg">{formatBool(mount.at_park, "yes", "no")}</dd>
        <dt className="text-fg-muted">At home</dt>
        <dd className="text-fg">{formatBool(mount.at_home, "yes", "no")}</dd>
      </dl>
      <div className="flex flex-wrap gap-2">
        {caps?.can_park === true && (
          <Button
            aria-busy={parkActive}
            onClick={() => {
              setActiveMotion("park");
              park.mutate(undefined, { onError: () => { clearMotion("park"); } });
            }}
            disabled={activeMotion !== null || park.isPending}
          >
            {parkActive
              ? <LoaderCircle aria-hidden="true" className="size-5 shrink-0 animate-spin" strokeWidth={1.8} />
              : <CircleParking aria-hidden="true" className="size-5 shrink-0" strokeWidth={1.8} />}
            {parkActive ? "Parking…" : "Park"}
          </Button>
        )}
        {parkActive && (
          <MountMotionStopButton
            disabled={park.isPending}
            onStopped={() => { clearMotion("park"); }}
          />
        )}
        {caps?.can_unpark === true && (
          <Button
            aria-busy={unpark.isPending}
            onClick={() => { unpark.mutate(); }}
            disabled={activeMotion !== null || unpark.isPending}
          >
            {unpark.isPending
              ? <LoaderCircle aria-hidden="true" className="size-5 shrink-0 animate-spin" strokeWidth={1.8} />
              : <CircleParkingOff aria-hidden="true" className="size-5 shrink-0" strokeWidth={1.8} />}
            {unpark.isPending ? "Unparking…" : "Unpark (permit motion)"}
          </Button>
        )}
        {caps?.can_find_home === true && (
          <Button
            aria-busy={findHomeActive}
            onClick={() => {
              setActiveMotion("find-home");
              findHome.mutate(undefined, { onError: () => { clearMotion("find-home"); } });
            }}
            disabled={activeMotion !== null || findHome.isPending}
          >
            {findHomeActive
              ? <LoaderCircle aria-hidden="true" className="size-5 shrink-0 animate-spin" strokeWidth={1.8} />
              : <MapPinHouse aria-hidden="true" className="size-5 shrink-0" strokeWidth={1.8} />}
            {findHomeActive ? "Finding home…" : "Find home"}
          </Button>
        )}
        {findHomeActive && (
          <MountMotionStopButton
            disabled={findHome.isPending}
            onStopped={() => { clearMotion("find-home"); }}
          />
        )}
      </div>
      {/* Shown verbatim — the important one is a 409 below the horizon
          (`device_code: 1279`), and swallowing it would leave someone
          thinking the scope parked when it did not (the tab's brief). */}
      {park.isError && (
        <p role="alert" className="text-sm text-danger">
          {park.error instanceof Error ? park.error.message : "Park failed."}
        </p>
      )}
      {unpark.isError && (
        <p role="alert" className="text-sm text-danger">
          {unpark.error instanceof Error ? unpark.error.message : "Unpark failed."}
        </p>
      )}
      {findHome.isError && (
        <p role="alert" className="text-sm text-danger">
          {findHome.error instanceof Error ? findHome.error.message : "Find home failed."}
        </p>
      )}
    </div>
  );
}
