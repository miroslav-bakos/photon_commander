// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/** True only for notes that explicitly describe a disconnected Alpaca
 * device. Callers may collapse these protocol details to "Disconnected";
 * all other notes remain useful diagnostics and must still be shown. */
export function deviceNotesSayDisconnected(...notes: (string | null | undefined)[]): boolean {
  return notes.some((note) => {
    if (note == null) return false;
    const normalized = note.toLowerCase();
    return (
      normalized.includes("not connected") ||
      (normalized.includes("0x407") && normalized.includes("connected = true"))
    );
  });
}
