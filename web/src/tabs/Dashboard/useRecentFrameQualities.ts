import { useState } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import { timedApiFetch } from "../../performance/diagnostics";
import type { CaptureFrameQuality } from "../../api/capture";

async function fetchQuality(frameId: number, signal: AbortSignal): Promise<CaptureFrameQuality> {
  const response = await timedApiFetch(new Request(new URL(`/api/capture/frames/${String(frameId)}/quality`, window.location.origin), { credentials: "same-origin", signal }));
  if (!response.ok) throw new Error(`Quality request failed (${String(response.status)})`);
  return response.json() as Promise<CaptureFrameQuality>;
}

/** One cold frame at a time; cache observers advance the queue after success or
 * failure. Query cancellation aborts the active fetch when the dashboard leaves. */
export function useRecentFrameQualities(frameIds: number[], ready = true) {
  const client = useQueryClient();
  const [errorsBeforeVisit] = useState(() => new Map(client.getQueryCache().getAll().map((query) => [query.queryHash, query.state.errorUpdateCount])));
  const ids = [...new Set(frameIds)];
  const nextId = ids.find((id) => client.getQueryState(["capture", "frame-quality", id])?.fetchStatus === "fetching") ?? ids.find((id) => {
    const state = client.getQueryState(["capture", "frame-quality", id]);
    return state?.status !== "success" && (state?.status !== "error" || state.errorUpdateCount <= (errorsBeforeVisit.get(client.getQueryCache().find({ queryKey: ["capture", "frame-quality", id] })?.queryHash ?? "") ?? 0));
  });
  return useQueries({
    queries: ids.map((id) => ({
      queryKey: ["capture", "frame-quality", id] as const,
      queryFn: ({ signal }: { signal: AbortSignal }) => fetchQuality(id, signal),
      enabled: ready && id === nextId,
      notifyOnChangeProps: "all" as const,
      staleTime: Number.POSITIVE_INFINITY,
      retry: false,
    })),
  });
}

