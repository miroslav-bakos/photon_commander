// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Convenience aliases into the generated schema. Everything here is a type
 * *alias* — no field is redeclared — so there is nothing here to drift when
 * `npm run api:codegen` regenerates `schema.d.ts`.
 */
import type { components } from "./schema";

export type StateSnapshot = components["schemas"]["StateSnapshot"];
export type LinkState = components["schemas"]["LinkState"];
export type LinkStatus = components["schemas"]["LinkStatus"];
export type DeviceDescriptor = components["schemas"]["DeviceDescriptor"];
export type DiscoveryState = components["schemas"]["DiscoveryState"];
export type LocationState = components["schemas"]["LocationState"];
export type LocationSource = components["schemas"]["LocationSource"];
export type SettingsSummary = components["schemas"]["SettingsSummary"];
export type Notice = components["schemas"]["Notice"];

/** The WS frame union, straight off the endpoints that carry it — the same
 * discriminated union `EventFrame.event` uses server-side. */
export type StateEvent = components["schemas"]["EventFrame"]["event"];
export type ServerFrame =
  | components["schemas"]["SnapshotFrame"]
  | components["schemas"]["EventFrame"]
  | components["schemas"]["ErrorFrame"]
  | components["schemas"]["HeartbeatFrame"];
export type SnapshotReason = components["schemas"]["SnapshotFrame"]["reason"];

export type RememberedAddress = components["schemas"]["RememberedAddress"];
export type DeviceSettings = components["schemas"]["DeviceSettings"];
export type AppearanceSettings = components["schemas"]["AppearanceSettings"];

export type HorizonResponse = components["schemas"]["HorizonResponse"];
export type HorizonPointModel = components["schemas"]["HorizonPointModel"];
export type HorizonPointRequest = components["schemas"]["HorizonPointRequest"];
export type HorizonRequest = components["schemas"]["HorizonRequest"];

/** `StateSnapshot.devices` (`ssc.state.models.AlpacaDevicesState`, landed
 * `b504430`) — real generated aliases, replacing the hand-written stand-in
 * that predated codegen carrying this field. `polled_at` is nullable: no
 * poll has completed yet, not "just now" (architecture policy invariant 7). */
export type AlpacaDeviceStatus = components["schemas"]["AlpacaDeviceItem"];
export type AlpacaDeviceKind = AlpacaDeviceStatus["kind"];
export type DevicesState = components["schemas"]["AlpacaDevicesState"];
