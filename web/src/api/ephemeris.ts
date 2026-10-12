// SPDX-License-Identifier: GPL-3.0-only
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./client";
import type { components } from "./schema";

export type EphemerisSetup = components["schemas"]["EphemerisSetupResponse"];

export function useEphemerisSetupQuery() {
  return useQuery({
    queryKey: ["settings", "ephemeris"],
    queryFn: () => unwrap(api.GET("/api/settings/ephemeris")),
  });
}
