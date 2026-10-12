// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useConnectionStore } from "../store/connectionStore";
import type { AlpacaDeviceKind, DevicesState } from "./types";

export function isAlpacaDeviceConnected(
  devices: DevicesState | null | undefined,
  kind: AlpacaDeviceKind,
): boolean {
  if (devices == null || devices.stale) return false;
  return (devices.items ?? []).some((item) => item.kind === kind && item.connected === true);
}

export function useAlpacaDeviceConnected(kind: AlpacaDeviceKind): boolean {
  return useConnectionStore((state) => isAlpacaDeviceConnected(state.snapshot?.devices, kind));
}
