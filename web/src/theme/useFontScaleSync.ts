// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect } from "react";
import { useConnectionStore } from "../store/connectionStore";

/**
 * Font scale is server state, not client state (`settings.font_scale` in the
 * live snapshot) — the exact same reasoning as `useThemeSync`: if a phone
 * changes the text size, every open client should agree. This effect is the
 * only thing that writes `data-font-scale`, and it writes nothing until the
 * snapshot says to; `:root`'s own base size is already "normal", so the
 * unscaled paint before the first frame arrives is already correct.
 */
export function useFontScaleSync(): void {
  const fontScale = useConnectionStore((s) => s.snapshot?.settings?.font_scale ?? null);

  useEffect(() => {
    if (fontScale === null) return;
    document.documentElement.dataset.fontScale = fontScale;
  }, [fontScale]);
}
