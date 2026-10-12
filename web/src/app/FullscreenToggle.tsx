// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect, useState } from "react";
import { useMediaQuery } from "../tabs/SchedulingTab/useMediaQuery";

const STORAGE_KEY = "ssc.fullscreen-preferred";

/** Same guarded-access pattern as `skymap/layerPrefs.ts`: a locked-down
 * browser (private mode, a policy) throws on `localStorage`, and this is a
 * presentation preference, not anything the app depends on to function. */
function rememberPreference(preferred: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, preferred ? "true" : "false");
  } catch {
    // Not remembered this time; the toggle still works for this session.
  }
}

/**
 * A stopgap for the TLS work landing separately: the manifest's `display`
 * only ever applies to an *installed* PWA, and installing one needs a secure
 * context this app does not have on a bare LAN IP. The Fullscreen API works
 * today, in a plain tab, over plain HTTP — this button is the only way into
 * it, since the API requires an explicit user gesture and forbids requesting
 * it on load.
 *
 * Note this is deliberately *more* aggressive than the installed app: the
 * manifest asks for `standalone`, which keeps the system status bar so the
 * clock and battery stay readable in the field (owner's explicit call), while
 * the Fullscreen API has no such middle setting — it takes the status bar
 * too. That is the right trade only while there is no installed app: in a
 * plain tab the alternative is a permanent URL bar.
 *
 * Renders nothing when it would be meaningless: unsupported (feature-
 * detected via `document.fullscreenEnabled`, which is `false` on iPhone
 * Safari — no UA sniffing needed), or already running as an installed PWA,
 * where `standalone` has already removed the browser chrome at the OS level
 * and the status bar is being kept on purpose.
 *
 * "Remember the user's preference" (frontend-react.md-adjacent ask): the
 * Fullscreen API resets on every navigation/reload and cannot be
 * re-entered without a fresh gesture, so there is nothing this value can
 * safely *drive* automatically — it is persisted so a later feature has it
 * to read, and so the choice a user made is not simply thrown away the
 * moment the tab reloads.
 */
export function FullscreenToggle() {
  const supported = typeof document !== "undefined" && document.fullscreenEnabled;
  const isStandaloneDisplay = useMediaQuery("(display-mode: standalone)");
  const isFullscreenDisplay = useMediaQuery("(display-mode: fullscreen)");
  const [isFullscreen, setIsFullscreen] = useState(
    () => typeof document !== "undefined" && document.fullscreenElement !== null,
  );

  useEffect(() => {
    function onChange() {
      setIsFullscreen(document.fullscreenElement !== null);
    }
    document.addEventListener("fullscreenchange", onChange);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
    };
  }, []);

  if (!supported || isStandaloneDisplay || isFullscreenDisplay) return null;

  function toggle(): void {
    if (document.fullscreenElement === null) {
      void document.documentElement.requestFullscreen().then(
        () => {
          rememberPreference(true);
        },
        () => {
          // Refused (permissions policy, not inside a gesture anymore,
          // etc.) — leave everything exactly as it was rather than claim
          // the toggle is now on.
        },
      );
    } else {
      void document.exitFullscreen().then(
        () => {
          rememberPreference(false);
        },
        () => undefined,
      );
    }
  }

  return (
    <button
      type="button"
      onClick={toggle}
      aria-pressed={isFullscreen}
      aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
      title={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
      className="inline-flex size-6 shrink-0 items-center justify-center rounded text-fg-muted hover:text-fg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
    >
      <FullscreenIcon expanded={!isFullscreen} />
    </button>
  );
}

/** Corner brackets pointing outward (enter) or inward (exit) — the same
 * hand-drawn, `currentColor`-stroked style as `TabNav.tsx`'s icons, so this
 * needs no per-theme colour handling of its own. */
function FullscreenIcon({ expanded }: { expanded: boolean }) {
  const d = expanded
    ? "M9 4H5a1 1 0 0 0-1 1v4M15 4h4a1 1 0 0 1 1 1v4M9 20H5a1 1 0 0 1-1-1v-4M15 20h4a1 1 0 0 0 1-1v-4"
    : "M4 9V5a1 1 0 0 1 1-1h4M20 9V5a1 1 0 0 0-1-1h-4M4 15v4a1 1 0 0 0 1 1h4M20 15v4a1 1 0 0 1-1 1h-4";
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className="size-4 shrink-0"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={d} />
    </svg>
  );
}
