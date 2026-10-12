// SPDX-License-Identifier: GPL-3.0-only
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "./client";
import type { components } from "./schema";

export type ToolPaths = components["schemas"]["ExternalToolsSettings"];

export function useToolPathsQuery() {
  return useQuery({ queryKey: ["settings", "tool-paths"], queryFn: () => unwrap(api.GET("/api/settings/external-tools/paths")) });
}

export function useSaveToolPaths() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ToolPaths) => unwrap(api.PUT("/api/settings/external-tools/paths", { body })),
    onSuccess: () => client.invalidateQueries({ queryKey: ["settings"] }),
  });
}

export function useHostPathsQuery(open: boolean, path: string | undefined) {
  return useQuery({ queryKey: ["settings", "host-paths", path], enabled: open,
    queryFn: () => unwrap(api.GET("/api/settings/host-paths", { params: { query: { path } } })), retry: false });
}
