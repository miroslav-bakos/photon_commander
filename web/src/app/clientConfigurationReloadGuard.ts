// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

const RELOAD_FINGERPRINT_KEY = "ssc.client-configuration.reload-fingerprint.v1";

/** Claim the single reload allowed for one exact remote configuration.
 * Zustand stores hydrate before the sync effect fetches the backend copy.
 * Some stores then normalise restored JSON, and another open browser can also
 * update it between loads. Without a session guard, each small difference
 * restores and reloads forever. If session storage is unavailable, failing
 * closed keeps the current page usable instead of risking a loop. */
export function claimConfigurationReload(storage: Storage, fingerprint: string): boolean {
  try {
    if (storage.getItem(RELOAD_FINGERPRINT_KEY) === fingerprint) return false;
    storage.setItem(RELOAD_FINGERPRINT_KEY, fingerprint);
    return true;
  } catch {
    return false;
  }
}
