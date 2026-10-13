// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useLayoutEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { LayoutRouteProvider } from "../layout/LayoutContext";
import { PlanningTab } from "../tabs/PlanningTab/PlanningTab";

export function PersistentPlanningTab() {
  const isPlanning = useLocation().pathname === "/planning";
  const [visited, setVisited] = useState(isPlanning);
  const containerRef = useRef<HTMLDivElement>(null);
  const [visibleSize, setVisibleSize] = useState<{ width: number; height: number } | null>(null);
  if (isPlanning && !visited) setVisited(true);

  useLayoutEffect(() => {
    if (!isPlanning || containerRef.current === null) return;
    const container = containerRef.current;
    const updateSize = () => {
      const { width, height } = container.getBoundingClientRect();
      if (width > 0 && height > 0) {
        setVisibleSize((current) => current?.width === width && current.height === height ? current : { width, height });
      }
    };
    updateSize();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(updateSize);
    observer?.observe(container);
    window.addEventListener("resize", updateSize);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", updateSize);
    };
  }, [isPlanning, visited]);

  if (!visited) return null;
  return (
    // Keep the warmed map mounted at its measured size, but contain its
    // overflow so an inactive Planning page cannot extend another route.
    <div className={isPlanning ? "contents" : "pointer-events-none invisible absolute top-0 left-0 h-0 w-0 overflow-hidden"} aria-hidden={!isPlanning}>
      <div
        ref={containerRef}
        className={isPlanning
          ? "flex min-h-0 flex-1 flex-col"
          : "flex flex-col"}
        style={!isPlanning && visibleSize !== null ? visibleSize : undefined}
        aria-hidden={!isPlanning}
      >
        <LayoutRouteProvider tab="/planning">
          <PlanningTab active={isPlanning} />
        </LayoutRouteProvider>
      </div>
    </div>
  );
}
