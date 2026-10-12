// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect, useState } from "react";

/**
 * A wall clock that ticks on its own. Staleness here cannot be judged only
 * from when the last WS frame arrived — this device is polled server-side
 * (DESIGN.md §4.4b), and nothing pushes a new frame just because a second
 * has passed with no change. Without this, a `polled_at` that was fresh
 * when the frame arrived would keep rendering as fresh forever, which is
 * exactly the "stale data rendered as live" failure the tab's brief warns
 * against.
 *
 * `prefers-reduced-motion` is irrelevant here — this drives a text label,
 * not an animation — so it is not consulted.
 */
export function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, intervalMs);
    return () => {
      clearInterval(timer);
    };
  }, [intervalMs]);
  return now;
}
