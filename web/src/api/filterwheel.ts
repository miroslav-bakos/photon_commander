// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap, unwrapVoid } from "./client";
import { useAlpacaDeviceConnected } from "./deviceAvailability";
import { deviceNotesSayDisconnected } from "./deviceStatus";
import type { components } from "./schema";

export type FilterWheelState = components["schemas"]["FilterWheelStateResponse"];
export type FilterWheelPositionRequest = components["schemas"]["FilterWheelPositionRequest"];

/** Collapse the standard Alpaca "Connected = True" refusal into the device
 * state it actually communicates. Other notes remain available as useful
 * diagnostics (for example, a driver that does not implement Names). */
export function isFilterWheelDisconnected(state: FilterWheelState): boolean {
  return deviceNotesSayDisconnected(state.names_note, state.position_note);
}

const stateKey = ["filterwheel", "state"] as const;

export function useFilterWheelQuery() {
  const connected = useAlpacaDeviceConnected("filterwheel");
  return useQuery({
    queryKey: [...stateKey, connected] as const,
    queryFn: async () => unwrap(api.GET("/api/filterwheel")),
    enabled: connected,
    refetchInterval: connected ? 2000 : false,
  });
}

export function useSetFilterWheelPositionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: FilterWheelPositionRequest) =>
      unwrapVoid(api.PUT("/api/filterwheel/position", { body })),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: stateKey });
    },
  });
}
