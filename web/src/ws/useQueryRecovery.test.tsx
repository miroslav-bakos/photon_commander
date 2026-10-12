// SPDX-License-Identifier: GPL-3.0-only
import { act, waitFor } from "@testing-library/react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { useConnectionStore } from "../store/connectionStore";
import { renderWithQueryClient } from "../test/queryClient";
import { useQueryRecovery } from "./useQueryRecovery";
afterEach(() => { useConnectionStore.getState().reset(); });
it("refreshes active reads after a fresh resync without repeating a mutation", async () => {
  useConnectionStore.getState().reset();
  const read = vi.fn().mockResolvedValue("cached"), inactive = vi.fn(), command = vi.fn();
  function View() {
    useQueryRecovery();
    useQuery({ queryKey: ["recovery-read"], queryFn: read, staleTime: Infinity });
    useQuery({ queryKey: ["disabled-read"], queryFn: inactive, enabled: false });
    useMutation({ mutationFn: command });
    return null;
  }
  renderWithQueryClient(<View />);
  await waitFor(() => { expect(read).toHaveBeenCalledOnce(); });
  act(() => { useConnectionStore.getState().setStatus("open"); });
  expect(read).toHaveBeenCalledOnce();
  act(() => { useConnectionStore.getState().setStatus("reconnecting"); });
  expect(read).toHaveBeenCalledOnce();
  act(() => { useConnectionStore.getState().setStatus("open"); });
  await waitFor(() => { expect(read).toHaveBeenCalledTimes(2); });
  expect(inactive).not.toHaveBeenCalled();
  expect(command).not.toHaveBeenCalled();
});
