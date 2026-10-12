// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * A held target needs its horizontal camera re-anchored only when time
 * actually advances. Same-instant display refreshes (for example a dragged
 * FOV updating its target coordinates) must preserve the continuous camera
 * established by the drag itself.
 */
export function heldTargetNeedsReanchor(
  previousInstant: Date | null,
  nextInstant: Date,
  holdOnTarget: boolean,
): boolean {
  return holdOnTarget
    && previousInstant !== null
    && previousInstant.getTime() !== nextInstant.getTime();
}
