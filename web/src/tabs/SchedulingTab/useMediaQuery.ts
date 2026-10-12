// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `window.matchMedia` as React state — for the one component that changes
 * its *axis*, not just its layout, at a breakpoint (the timeline: DESIGN.md
 * §4.4 "On mobile the same timeline goes vertical"). Tailwind's responsive
 * classes cannot express "render this SVG transposed", so the orientation is
 * derived here and passed down as a prop, keeping one component rather than
 * two (frontend-react.md: "do not fork a component per breakpoint").
 *
 * The query string is the `--breakpoint-sm` in `index.css`, minus one pixel:
 * below `sm` is phone.
 */
import { useSyncExternalStore } from "react";

export const BELOW_SM_QUERY = "(max-width: 639px)";

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window.matchMedia !== "function") return () => undefined;
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => {
        list.removeEventListener("change", onChange);
      };
    },
    () => (typeof window.matchMedia === "function" ? window.matchMedia(query).matches : false),
    () => false,
  );
}
