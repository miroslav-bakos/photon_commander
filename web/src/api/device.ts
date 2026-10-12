// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap, unwrapVoid } from "./client";
import { acknowledgeCommand, pendingCommandKey, waitForOperation } from "./operations";
import type { components } from "./schema";

interface ProbeResponse {
  device: components["schemas"]["DeviceDescriptor"];
  tested_at: string;
  remembered: boolean;
}

const addressesKey = ["device", "addresses"] as const;

/** Start a bounded LAN scan. Results and progress arrive as
 * `discovery.changed` WS events into the live snapshot, not in this
 * response — the mutation only hands back the `scan_id`. */
export function useStartDiscoveryMutation() {
  return useMutation({
    mutationFn: async (timeout_s?: number) =>
      unwrap(api.POST("/api/device/discover", { body: { timeout_s } })),
  });
}

export function useCancelDiscoveryMutation() {
  return useMutation({
    mutationFn: async () => unwrapVoid(api.DELETE("/api/device/discover")),
  });
}

/** A real Test-connection: reachable, firmware, capabilities — never a bare
 * green tick (frontend-react.md). Also remembers the address server-side if
 * it answered, which is why it invalidates the address book. */
export function useProbeMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: {
      host: string;
      control_port?: number | undefined;
      timeout_s?: number | undefined;
    }) => waitForOperation<ProbeResponse>(await unwrap(api.POST(
      "/api/device/probe-job",
      { body, headers: { "Idempotency-Key": pendingCommandKey("device_probe") } },
    ))),
    onSuccess: async () => {
      acknowledgeCommand("device_probe");
      await queryClient.invalidateQueries({ queryKey: addressesKey });
    },
  });
}

export function useConnectMutation() {
  return useMutation({
    mutationFn: async (body: { host?: string | undefined; control_port?: number | undefined }) =>
      unwrap(api.POST("/api/device/connect-job", {
        body,
        headers: { "Idempotency-Key": pendingCommandKey("device_connect") },
      })),
    onSuccess: () => { acknowledgeCommand("device_connect"); },
  });
}

export function useDisconnectMutation() {
  return useMutation({
    mutationFn: async () => unwrapVoid(api.POST("/api/device/disconnect")),
  });
}

export function useHardwareProfileMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (profile_id: string) =>
      unwrap(api.PUT("/api/device/hardware-profile", { body: { profile_id } })),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: addressesKey });
    },
  });
}

export function useEthernetAddressMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { host: string | null; control_port?: number | null }) =>
      unwrap(api.PUT("/api/device/ethernet-address", { body })),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: addressesKey });
    },
  });
}

export function useWifiAddressMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { host: string | null; control_port?: number | null }) =>
      unwrap(api.PUT("/api/device/wifi-address", { body })),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: addressesKey });
    },
  });
}

export function useAddressesQuery() {
  return useQuery({
    queryKey: addressesKey,
    queryFn: async () => unwrap(api.GET("/api/device/addresses")),
  });
}

export function useForgetAddressMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ host, controlPort }: { host: string; controlPort: number }) =>
      unwrapVoid(
        api.DELETE("/api/device/addresses/{host}/{control_port}", {
          params: { path: { host, control_port: controlPort } },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: addressesKey });
    },
  });
}
