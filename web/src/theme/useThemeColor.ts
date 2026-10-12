// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useMemo } from "react";
import { useConnectionStore } from "../store/connectionStore";

/**
 * The live value of a `--ssc-*` token, resolved from the DOM rather than
 * duplicated as a literal — so a component that needs an actual colour string
 * (an SVG library prop, a `<canvas>` call) still never writes one down. See
 * `eslint-rules/no-literal-color.js`: this is the sanctioned way around it.
 *
 * Reads a detached probe element carrying `data-theme={theme}` rather than
 * `document.documentElement`, so the result is correct for `theme` even if
 * `useThemeSync`'s effect (which writes the real attribute) has not run yet
 * in this commit — no ordering dependency between the two. Computed during
 * render, not in an effect: there is nothing to subscribe to.
 */
export function useThemeColor(variable: `--ssc-${string}`): string {
  const theme = useConnectionStore((s) => s.snapshot?.settings?.theme ?? "dark");

  return useMemo(() => {
    const probe = document.createElement("div");
    probe.dataset.theme = theme;
    probe.style.display = "none";
    document.body.appendChild(probe);
    const value = getComputedStyle(probe).getPropertyValue(variable).trim();
    probe.remove();
    return value;
  }, [variable, theme]);
}
