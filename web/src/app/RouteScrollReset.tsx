// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useLayoutEffect } from "react";
import { useLocation } from "react-router-dom";

/** Phones scroll the document; wider layouts scroll main and fill panes. */
export function RouteScrollReset() {
  const { pathname, hash } = useLocation();

  useLayoutEffect(() => {
    const previous = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";
    return () => { window.history.scrollRestoration = previous; };
  }, []);

  useLayoutEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
    const main = document.getElementById("main");
    if (main === null) return;
    if (hash === "#night-report" || hash === "#bug-report") {
      document.getElementById(hash.slice(1))?.scrollIntoView({ block: "start" });
      return;
    }
    main.scrollTop = 0;
    main.scrollLeft = 0;
    for (const pane of Array.from(main.querySelectorAll<HTMLElement>("[data-panes] > div"))) {
      pane.scrollTop = 0;
      pane.scrollLeft = 0;
    }
  }, [pathname, hash]);

  return null;
}
