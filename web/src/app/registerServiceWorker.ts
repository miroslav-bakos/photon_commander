// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Registers `public/sw.js` (see that file for the caching strategy). Called
 * once from `main.tsx`, production builds only: Vite's dev server serves
 * modules unbundled and un-hashed, and a caching service worker sitting in
 * front of that fights HMR far more than it helps — this app's install/
 * offline story is about the built app `ssc-core` actually serves.
 */
export function registerServiceWorker(): void {
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((err: unknown) => {
      // Registration can fail (insecure context, disabled in browser
      // settings, etc.) — the app itself works identically without it, so
      // this is worth a console note for diagnosis, never a user-facing
      // error.
      console.error("Service worker registration failed", err);
    });
  });
}
