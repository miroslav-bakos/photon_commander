// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The three honest states `ObjectInfoPanel.tsx` renders for one
 * `GET /api/astro/objects/{designation}/info` response (`docs/IMPLEMENTATION.md`
 * M11's own gate: real content when available, an honest "no source
 * configured"/"nothing found" state otherwise — never a blank panel and
 * never a spinner as an end state).
 *
 * Pure and React-free, the same reason `cutoutLayers.ts`'s `resolveCutoutRows`
 * is: the mapping from `configured`/`found` to what actually renders is
 * worth testing without mounting anything.
 */
import type { ObjectInfoResponse } from "../api/astro";

export type ObjectInfoPanelState =
  /** `configured: false` — the feature itself is off. `message` names the
   * fix (THIRD_PARTY_NOTICES.md's shipped-disabled default); the panel points at
   * the Settings toggle rather than dead-ending there. */
  | { kind: "not-configured"; message: string }
  /** `configured: true, found: false` — a real, honest "no article found",
   * not an error. */
  | { kind: "not-found"; message: string }
  /** `configured: true, found: true` — real content to render verbatim,
   * including whatever `attributions`/`image` the response carries. */
  | { kind: "found"; response: ObjectInfoResponse };

const NOT_CONFIGURED_FALLBACK = "no catalogue source configured";

function notFoundFallback(designation: string): string {
  return `no article found for ${designation}`;
}

export function resolveObjectInfoPanelState(response: ObjectInfoResponse): ObjectInfoPanelState {
  if (!response.configured) {
    return { kind: "not-configured", message: response.message ?? NOT_CONFIGURED_FALLBACK };
  }
  if (!response.found) {
    return { kind: "not-found", message: response.message ?? notFoundFallback(response.designation) };
  }
  return { kind: "found", response };
}
