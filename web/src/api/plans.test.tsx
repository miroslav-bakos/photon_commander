// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { expect, it, vi } from "vitest";
import { useCreatePlan, useUpdatePlan, type Plan, type PlanRecord } from "./plans";
import { ApiError } from "./client";

const put = vi.hoisted(() => vi.fn());
vi.mock("./client", () => ({
  api: { PUT: put, POST: put },
  unwrap: async (response: Promise<{ data: unknown }>) => (await response).data,
  ApiError: class extends Error {
    constructor(readonly status: number) { super(`HTTP ${String(status)}`); }
  },
}));

it("finishes a saved timeline edit without waiting for compilation or refetching the authoritative document", async () => {
  const client = new QueryClient();
  const document = { id: "plan-1", items: [] } as unknown as Plan;
  const record = { document, revision: 2 } as PlanRecord;
  put.mockResolvedValue({ data: record });
  const invalidate = vi.spyOn(client, "invalidateQueries").mockImplementation(() => new Promise(() => { /* Simulate an unfinished compile refetch. */ }));
  const { result, unmount } = renderHook(useUpdatePlan, {
    wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  });
  act(() => { result.current.mutate({ document, revision: 1 }); });
  await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
  expect(result.current.isPending).toBe(false);
  expect(client.getQueryData(["plans", "plan-1"])).toEqual(record);
  expect(invalidate).not.toHaveBeenCalled();
  unmount();
  client.clear();
});

it("releases timeline editing after a revision conflict without waiting for refetch", async () => {
  const client = new QueryClient();
  const document = { id: "plan-1", items: [] } as unknown as Plan;
  const error = new ApiError(409, "Changed in another client");
  put.mockRejectedValueOnce(error);
  const invalidate = vi.spyOn(client, "invalidateQueries").mockImplementation(() => new Promise(() => { /* Refetch remains unfinished. */ }));
  const { result, unmount } = renderHook(useUpdatePlan, {
    wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  });
  act(() => { result.current.mutate({ document, revision: 1 }); });
  await waitFor(() => { expect(result.current.isError).toBe(true); });
  expect(result.current.isPending).toBe(false);
  expect(result.current.error).toBe(error);
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["plans", "plan-1"], exact: true });
  unmount();
  client.clear();
});

it("finishes creating a plan after its acknowledgement while the plan-list refresh hangs", async () => {
  const client = new QueryClient();
  const document = { id: "new-plan", items: [] } as unknown as Plan;
  const record = { document, revision: 1 } as PlanRecord;
  put.mockResolvedValue({ data: record });
  vi.spyOn(client, "invalidateQueries").mockImplementation(() => new Promise(() => { /* Simulate an unfinished refresh. */ }));
  const { result, unmount } = renderHook(useCreatePlan, {
    wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  });
  act(() => { result.current.mutate(document); });
  await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
  expect(client.getQueryData(["plans", "new-plan"])).toEqual(record);
  unmount();
  client.clear();
});
