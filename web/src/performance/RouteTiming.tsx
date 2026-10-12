// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import { finishNavigation } from "./diagnostics";

/** Two animation frames let the route's first frame reach the browser. */
export function RouteTiming() {
  const { pathname } = useLocation();
  useEffect(() => {
    const timing = finishNavigation(pathname);
    let secondFrame = 0;
    const firstFrame = requestAnimationFrame(() => {
      secondFrame = requestAnimationFrame(timing.finish);
    });
    return () => {
      cancelAnimationFrame(firstFrame);
      cancelAnimationFrame(secondFrame);
      timing.cancel();
    };
  }, [pathname]);
  return null;
}
