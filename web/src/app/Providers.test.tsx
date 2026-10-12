// SPDX-License-Identifier: GPL-3.0-only
import { act, renderHook, waitFor } from "@testing-library/react";
import { onlineManager, useMutation, useQueryClient } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { Providers } from "./Providers";
afterEach(() => { onlineManager.setOnline(true); });
it("does not queue an offline command for automatic execution on reconnect", async () => {
  onlineManager.setOnline(false);
  const command = vi.fn().mockRejectedValue(new TypeError("Network unavailable"));
  const { result, unmount } = renderHook(() => ({ mutation: useMutation({ mutationFn: command }), client: useQueryClient() }), { wrapper: Providers });
  try {
    act(() => { result.current.mutation.mutate(); });
    await waitFor(() => { expect(command).toHaveBeenCalledOnce(); });
    await waitFor(() => { expect(result.current.mutation.isError).toBe(true); });
    act(() => { onlineManager.setOnline(true); });
    await act(async () => { await Promise.resolve(); });
    expect(command).toHaveBeenCalledOnce();
  } finally { unmount(); result.current.client.clear(); }
});
