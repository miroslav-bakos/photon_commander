// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * A remembered collapsed/expanded flag, for the card headers and the
 * side-pane toggles (`Card`, `PaneLayout`).
 *
 * Persisted rather than session state: collapsing a panel is a statement
 * about how someone wants their screen laid out, and having to redo it on
 * every reload would make the feature not worth using. This is per-browser
 * UI preference, not app state — it never goes near `ssc-core`, which is why
 * it lives in `localStorage` and not in the authoritative snapshot
 * (architecture policy invariant 4: clients render state, they do not own it — but
 * "is this card folded up on this screen" is not state the telescope has an
 * opinion about).
 *
 * Every access is wrapped: `localStorage` throws outright in some contexts
 * (a private window with site data blocked, an iframe with third-party
 * storage disabled), and a panel that cannot remember its fold is a much
 * smaller problem than a panel that throws while rendering.
 */
import { useCallback, useState } from "react";

const PREFIX = "ssc.collapsed.";

function read(key: string, fallback: boolean): boolean {
  try {
    const raw = window.localStorage.getItem(PREFIX + key);
    return raw === null ? fallback : raw === "1";
  } catch {
    return fallback;
  }
}

function write(key: string, collapsed: boolean): void {
  try {
    window.localStorage.setItem(PREFIX + key, collapsed ? "1" : "0");
  } catch {
    // No persistence available; the in-memory state below still works for
    // this session, which is the useful half.
  }
}

export function useCollapsed(key: string, defaultCollapsed = false): [boolean, () => void] {
  // Read once on mount rather than on every render: the stored value cannot
  // change under us except through this hook's own setter.
  const [collapsed, setCollapsed] = useState(() => read(key, defaultCollapsed));
  const toggle = useCallback(() => {
    setCollapsed((previous) => {
      write(key, !previous);
      return !previous;
    });
  }, [key]);
  return [collapsed, toggle];
}
