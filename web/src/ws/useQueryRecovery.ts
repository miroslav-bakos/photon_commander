// SPDX-License-Identifier: GPL-3.0-only
import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useConnectionStore } from "../store/connectionStore";

/** A fresh socket snapshot proves the host is reachable again. Refresh active
 * read queries even when Wi-Fi roaming never changed navigator.onLine. */
export function useQueryRecovery(): void {
  const client = useQueryClient();
  const generation = useConnectionStore((state) => state.generation);
  const previous = useRef(generation);
  useEffect(() => {
    const before = previous.current;
    previous.current = generation;
    if (before === 0 || generation <= before) return;
    // Cancel an old stalled read first; retain its cached data. No mutation is
    // resumed or retried here. Query functions are reads or pure calculations.
    void client.cancelQueries({ type: "active" }).then(() =>
      client.invalidateQueries({ refetchType: "active" }));
  }, [client, generation]);
}
