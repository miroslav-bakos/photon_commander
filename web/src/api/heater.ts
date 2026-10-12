// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap, unwrapVoid } from "./client";
import { useAlpacaDeviceConnected } from "./deviceAvailability";
import type { components } from "./schema";

export type HeaterState = components["schemas"]["HeaterStateResponse"];
export type HeaterStateRequest = components["schemas"]["HeaterStateRequest"];
export type HeaterAutomation = components["schemas"]["HeaterAutomationResponse"];
export type HeaterAutomationRequest = components["schemas"]["HeaterAutomationRequest"];

const heaterKey = ["heater", "state"] as const;
const heaterAutomationKey = ["heater", "automation"] as const;

export function useHeaterQuery() {
  const connected = useAlpacaDeviceConnected("switch");
  return useQuery({
    queryKey: [...heaterKey, connected] as const,
    queryFn: async () => unwrap(api.GET("/api/heater")),
    enabled: connected,
    refetchInterval: connected ? 2000 : false,
  });
}

export function useSetHeaterMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: HeaterStateRequest) => unwrapVoid(api.PUT("/api/heater", { body })),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: heaterKey });
    },
  });
}

export function useHeaterAutomationQuery() {
  return useQuery({
    queryKey: heaterAutomationKey,
    queryFn: async () => unwrap(api.GET("/api/heater/automation")),
    refetchInterval: 5000,
  });
}

export function useSetHeaterAutomationMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: HeaterAutomationRequest) =>
      unwrap(api.PUT("/api/heater/automation", { body })),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: heaterAutomationKey });
      await queryClient.invalidateQueries({ queryKey: heaterKey });
    },
  });
}
