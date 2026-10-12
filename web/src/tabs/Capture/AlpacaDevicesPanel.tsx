// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Card, CardTitle } from "../../components/Card";
import { useConnectionStore } from "../../store/connectionStore";
import type { AlpacaDeviceKind, AlpacaDeviceStatus, DevicesState } from "../../api/types";

/** Same abbreviations `ConnectionsFooter.tsx` uses for its compact strip —
 * kept in sync rather than imported, since that file's constant is private
 * to a footer-shaped layout and this panel has room to always show the full
 * label instead of falling back to a letter. */
const KIND_LETTER: Record<AlpacaDeviceKind, string> = {
  telescope: "T",
  camera: "C",
  focuser: "F",
  filterwheel: "W",
  switch: "S",
};

/** Brightness tiers, not hue — the same rule `ConnectionsFooter.tsx` and
 * `TabNav.tsx` document at length: the red-light theme is red-only, and in
 * it `--ssc-success` is *darker* than `--ssc-fg-muted`, so a green-for-
 * connected scheme would render backwards. `connected` is also genuinely
 * tri-state (`ssc/state/models.py`'s `AlpacaDeviceItem` docstring) and is
 * *not* a health signal on its own — it only means "the device reports
 * Connected", nothing about whether it actually answers. */
const CONNECTED_TIER_CLASS: Record<"true" | "false" | "null", string> = {
  true: "bg-dot-ok",
  null: "bg-dot-unknown",
  false: "bg-dot-bad",
};

function connectedWord(connected: boolean | null): string {
  if (connected === true) return "connected";
  if (connected === false) return "not connected";
  return "connection unknown";
}

function Dot({ className }: { className: string }) {
  return <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${className}`} />;
}

function DeviceRow({ item, stale }: { item: AlpacaDeviceStatus; stale: boolean }) {
  const tierKey = item.connected === true ? "true" : item.connected === false ? "false" : "null";
  return (
    <li className="flex items-center gap-2 py-1 text-sm">
      <Dot className={stale ? "bg-dot-unknown" : CONNECTED_TIER_CLASS[tierKey]} />
      <span className="text-fg-muted" aria-hidden="true">
        {KIND_LETTER[item.kind]}
        {item.number}
      </span>
      <span className="flex-1 truncate text-fg">{item.label}</span>
      <span className="text-xs text-fg-muted">
        {connectedWord(item.connected)}
        {stale && " (stale)"}
      </span>
    </li>
  );
}

/**
 * Side panel for the Capture screen: every Alpaca device the backend has
 * inventoried, not just the mount — the user's brief for this screen names
 * "all devices available via alpaca" as its own panel, distinct from the
 * mount controls beside it. No new backend work: `StateSnapshot.devices`
 * (`AlpacaDevicesState`) already carries this, and the only prior consumer
 * was `ConnectionsFooter.tsx`'s compact strip — this reuses its exact
 * tri-state rendering, just laid out as a full list rather than a row of
 * dots.
 *
 * Three honest states, mirroring `DevicesEntries` there: no snapshot yet
 * (`null`) is distinct from a snapshot that has never been *polled*
 * (`stale` with no `polled_at`), which is distinct from a poll that came
 * back with a real but empty list. None of the three is collapsed into
 * "no devices" (invariant 7 — no silent degradation).
 */
export function AlpacaDevicesPanel() {
  const devices = useConnectionStore((s): DevicesState | null => s.snapshot?.devices ?? null);

  return (
    <Card>
      <CardTitle>Devices</CardTitle>
      {devices === null && <p className="text-sm text-fg-muted">Devices: unpolled.</p>}
      {devices !== null && (
        <>
          {devices.stale && (
            <p className="mb-2 text-xs text-warning">
              Device list is stale
              {devices.polled_at !== null ? ` — last polled ${new Date(devices.polled_at).toLocaleString()}` : " — never successfully polled"}.
            </p>
          )}
          {(devices.items ?? []).length === 0 ? (
            <p className="text-sm text-fg-muted">No devices configured.</p>
          ) : (
            <ul className="divide-y divide-border">
              {(devices.items ?? []).map((item) => (
                <DeviceRow key={item.id} item={item} stale={devices.stale} />
              ))}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}
