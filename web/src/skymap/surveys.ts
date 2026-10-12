// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/** DSS2 is the requested default, consent-gated before any personal-use imagery.
 * Existing user selections and their opacity remain stored independently. */
export const DEFAULT_BACKGROUND_SURVEY_ID = "dss2-color-hips-poc";

export const DEFAULT_CUTOUT_SOURCE_ID = "unwise";

/** Whether an imageset NAME looks like restricted-use imagery.
 *
 * DSS and the Guide Star Catalogs share STScI's non-commercial restriction
 * (DESIGN.md §3.1.2). A second, independent check on the name about to be
 * shown, so a rename or a registry change cannot quietly put DSS on screen
 * in a commercial product (invariant 10).
 */
export function isRestrictedImagesetName(name: string): boolean {
  return /\b(dss\d?|digitized sky survey|guide star catalog|gsc)\b/i.test(name);
}
