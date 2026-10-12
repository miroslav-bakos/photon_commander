// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { Severity } from "../../api/plans";

/** Diagnostic severity → badge tone. In red-light mode the three tones are
 * one hue at three lightnesses (`tokens.css`), so the word is always shown
 * alongside the colour. */
export const SEVERITY_TONE: Record<Severity, "neutral" | "warning" | "danger"> = {
  info: "neutral",
  warning: "warning",
  error: "danger",
};
