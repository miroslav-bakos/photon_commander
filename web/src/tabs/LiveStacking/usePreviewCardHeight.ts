// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useLayoutEffect, useState } from "react";

/** The shell already subtracts its toolbar, footer and mobile navigation.
 * Size the whole card to main's content box, not the image to the viewport. */
export function usePreviewCardHeight(): number | undefined {
  const [height, setHeight] = useState<number>();
  useLayoutEffect(() => {
    const main = document.getElementById("main");
    if (main === null) return;
    const measure = () => {
      const style = getComputedStyle(main);
      const available = main.clientHeight - parseFloat(style.paddingTop || "0") - parseFloat(style.paddingBottom || "0");
      if (available > 0) setHeight(available);
    };
    measure();
    const observer = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(measure);
    observer?.observe(main);
    window.addEventListener("resize", measure);
    return () => { observer?.disconnect(); window.removeEventListener("resize", measure); };
  }, []);
  return height;
}
