// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect } from "react";
import { useConnectionStore } from "../store/connectionStore";
import { useAutoThemeResolution } from "./useAutoThemeResolution";

/**
 * Theme is server state, not client state (`settings.theme` in the live
 * snapshot) — DESIGN.md §5's provenance discipline applies to more than just
 * location: if a phone changes the theme, every open client should agree.
 * This effect is the only thing that writes `data-theme`, and it writes
 * nothing until the snapshot says to; `:root`'s defaults are the dark theme,
 * so the unthemed paint before the first frame arrives is already correct.
 *
 * `"auto"` is the one value that is not itself a `data-theme` — it is a
 * *choice* (dark by day, red by night), resolved by `useAutoThemeResolution`
 * from the site's own sunset/sunrise, independently on every open client
 * rather than pushed from the server: sunset/sunrise are absolute UTC
 * instants, so every client resolving the same stored `"auto"` against the
 * same site's events agrees without needing to (reported live:
 * "automatically switches between dark and red ... based on sunrise /
 * sunset times"). The store never learns which of the two is momentarily in
 * effect — only `"auto"` itself is ever written back as the user's choice.
 */
export function useThemeSync(): void {
  const theme = useConnectionStore((s) => s.snapshot?.settings?.theme ?? null);
  const resolution = useAutoThemeResolution(theme === "auto");

  useEffect(() => {
    if (theme === null) return;
    if (theme !== "auto") {
      document.documentElement.dataset.theme = theme;
      return;
    }
    // Nothing resolved yet (still loading, or no location configured so the
    // request never fires) — keep whatever is already painted rather than
    // flash to a guessed value; `:root`'s own dark default already covers
    // the very first paint (this hook's own doc comment above).
    if (resolution === null) return;
    document.documentElement.dataset.theme = resolution.effective;
  }, [theme, resolution]);
}
