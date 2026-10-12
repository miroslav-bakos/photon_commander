// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import {
  isFilterWheelDisconnected,
  useFilterWheelQuery,
  useSetFilterWheelPositionMutation,
} from "../../api/filterwheel";
import { SegmentedControl } from "../../components/SegmentedControl";
import { Card, CardTitle } from "../../components/Card";
import { useAlpacaDeviceConnected } from "../../api/deviceAvailability";

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

/** Manual slot selection backed only by standard ASCOM FilterWheel members. */
export function FilterWheelPanel() {
  const connected = useAlpacaDeviceConnected("filterwheel");
  const wheel = useFilterWheelQuery();
  const setPosition = useSetFilterWheelPositionMutation();

  const names = wheel.data?.names;
  const position = wheel.data?.position;
  const disconnected = !connected || (wheel.data !== undefined && isFilterWheelDisconnected(wheel.data));
  const currentName =
    names !== null && names !== undefined && position !== null && position !== undefined && position >= 0
      ? names[position]
      : undefined;

  return (
    <Card>
      <CardTitle>Filter wheel</CardTitle>

      {wheel.isPending && connected && <p className="text-sm text-fg-muted">Waiting for filter slots…</p>}
      {!connected && <p className="text-sm text-fg-muted">Disconnected</p>}
      {wheel.isError && (
        <p role="alert" className="text-sm text-danger">
          {errorMessage(wheel.error, "Could not read the filter wheel.")}
        </p>
      )}
      {connected && wheel.isSuccess && disconnected && <p className="text-sm text-fg-muted">Disconnected</p>}
      {wheel.isSuccess && !disconnected && (
        <p className="text-sm text-fg-muted">
          Current: <span className="text-fg">{position === -1 ? "moving…" : (currentName ?? "unknown")}</span>
        </p>
      )}

      {!disconnected && names !== null && names !== undefined && names.length > 0 ? (
        <SegmentedControl className="mt-3" label="Filter slot" semantics="pressed" value={String(position)}
          disabled={setPosition.isPending}
          options={names.map((name, index) => ({ value: String(index), label: name.trim() === "" ? `Slot ${String(index + 1)}` : name }))}
          onChange={(slot) => { setPosition.mutate({ position: Number(slot) }); }} />
      ) : (
        wheel.isSuccess && !disconnected && (
          <p className="mt-2 text-xs text-fg-muted">
            {wheel.data.names_note ?? "No filter slots were reported by the device."}
          </p>
        )
      )}

      {wheel.isSuccess && !disconnected && position === null && wheel.data.position_note !== null && (
        <p className="mt-2 text-xs text-fg-muted">{wheel.data.position_note}</p>
      )}
      {setPosition.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {errorMessage(setPosition.error, "Could not change filter position.")}
        </p>
      )}
    </Card>
  );
}
