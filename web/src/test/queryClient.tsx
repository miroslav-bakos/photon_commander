// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";

/**
 * Render inside a fresh `QueryClient` that is actually torn down afterwards.
 *
 * Three test files each built their own client inline and none disposed of it.
 * TanStack's `notifyManager` batches notifications onto a `setTimeout(0)`, so a
 * query settling near the end of a file fires its notification *after* vitest
 * has torn down that file's jsdom environment — React then reaches for `window`
 * and throws `ReferenceError: window is not defined` as an unhandled error.
 *
 * It reproduced roughly one run in seven, always attributed to whichever file
 * happened to be running, which is exactly the shape of flake that gets
 * dismissed as unrelated. Unmounting the client cancels those pending
 * notifications, so there is nothing left to fire.
 */
const liveClients: QueryClient[] = [];

export function renderWithQueryClient(ui: ReactElement): RenderResult {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  liveClients.push(client);
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

/** Called from the global `afterEach` in `setup.ts` — components first, so no
 * observer outlives the client it belongs to. */
export function disposeQueryClients(): void {
  cleanup();
  for (const client of liveClients) {
    client.unmount();
    client.clear();
  }
  liveClients.length = 0;
}
