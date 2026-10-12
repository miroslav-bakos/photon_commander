// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Resolve Planning's scope without copying server state into the UI store.
 * An explicit choice belongs to this planning session and wins; otherwise a
 * uniquely detected connected profile wins over the bundled-list default.
 */
export function selectPlanningProfile<T extends { id: string }>(
  profiles: T[],
  chosenProfileId: string | null,
  detectedProfileId: string | null,
): T | null {
  return (
    profiles.find((profile) => profile.id === chosenProfileId) ??
    profiles.find((profile) => profile.id === detectedProfileId) ??
    profiles[0] ??
    null
  );
}
