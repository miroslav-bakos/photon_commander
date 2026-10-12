// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect } from "react";
import { useConnectionStore } from "../store/connectionStore";

/** Apply the server-owned accent preference to the theme-token selector. */
export function useAccentSync(): void {
  const accent = useConnectionStore((s) => s.snapshot?.settings?.accent ?? null);

  useEffect(() => {
    if (accent === null) return;
    document.documentElement.dataset.accent = accent;
  }, [accent]);
}
