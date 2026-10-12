// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Rate limits are real here (architecture policy: firmware 8.46 drops its control
      // port when it is unhappy) — no query refetches itself into that.
      retry: 1,
      refetchOnWindowFocus: false,
    },
    // An offline click must fail visibly, never become a queued telescope
    // command that executes later when the owner reconnects elsewhere.
    mutations: { networkMode: "always", retry: false },
  },
});

export function Providers({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
