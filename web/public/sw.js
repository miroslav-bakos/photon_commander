/*
 * SSC's app-shell service worker.
 *
 * Hand-rolled, not generated: every asset this app ships from `dist/` is
 * content-hashed by Vite (`app-<hash>.js`), so a "precache this exact file
 * list" step would need to be regenerated from the build output on every
 * build. Instead this worker discovers the current shell's asset URLs by
 * fetching `/` itself at install time and reading the `<script src>` /
 * `<link href>` tags out of it — self-updating with zero build step, and
 * the only build-time artefact (`dist/index.html`) it depends on already
 * has to exist for the app to run at all.
 *
 * Update strategy — chosen over a "new version available, reload?" prompt:
 * `skipWaiting`/`clients.claim` below make a new *service worker script*
 * take over immediately once the browser notices its bytes changed. But the
 * actual fix for the staleness bug this was written for (a stale service
 * worker serving an old bundle) is simpler and does not depend on that at
 * all: navigation requests below are network-first, so the next reload —
 * with or without a service worker update — always fetches the current
 * `index.html`, which always references the current build's hashed
 * asset URLs. A hashed asset is immutable by construction (its name changes
 * iff its content does), so caching it forever is safe; the *document* that
 * points to it is the only thing that must never be served stale while a
 * network is reachable, which is exactly what network-first gives it.
 *
 * Never intercepts `/api/*` or `/ws`: this app has exactly one authoritative
 * server state (architecture policy invariant 4) and a cached REST/WS response
 * presented as live would be exactly the silent staleness invariant 7
 * forbids. Those requests are left alone entirely — not even a pass-through
 * `fetch()` — so they behave identically to there being no service worker.
 */

const SHELL_CACHE = "ssc-shell-v2";
const RUNTIME_CACHE = "ssc-runtime-v2";
const CURRENT_CACHES = new Set([SHELL_CACHE, RUNTIME_CACHE]);

// A browser can retain a dead LAN connection after a laptop/phone sleeps or
// the tab sits unused. Letting a navigation fetch inherit the browser's TCP
// timeout kept the page blank for 10-20 seconds even though a complete shell
// was already cached. This deadline applies only when that cached fallback
// exists; a first-ever load still waits for the real server because there is
// nothing honest to render offline.
const NAVIGATION_NETWORK_TIMEOUT_MS = 2_000;

// Stable (non-hashed) filenames worth warming proactively alongside the
// shell document itself.
const KNOWN_SHELL_URLS = [
  "/manifest.webmanifest",
  "/icons/icon-192.png?v=3",
  "/icons/icon-512.png?v=3",
  "/icons/favicon-32.png?v=3",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      // Best-effort: an install that happens while offline (a first-ever
      // install with no network yet) must not fail outright — the fetch
      // handler below fills the cache in as things are actually requested
      // once a network does show up.
      try {
        const shellResponse = await fetch("/", { cache: "no-store" });
        const shellHtml = await shellResponse.clone().text();
        const assetUrls = [...shellHtml.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map((m) => m[1]);
        await cache.put("/", shellResponse);
        await cache.addAll([...new Set(assetUrls)]);
      } catch {
        // No network at install time — nothing to precache yet.
      }
      try {
        await cache.addAll(KNOWN_SHELL_URLS);
      } catch {
        // Same as above: best-effort, not fatal.
      }
    })(),
  );
  void self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((name) => !CURRENT_CACHES.has(name)).map((name) => caches.delete(name)));
      await self.clients.claim();
    })(),
  );
});

/** Try the network first, falling back to whatever this exact request last
 * cached to. Used for navigations (the shell document, and the sky map
 * iframes' own HTML documents) — the one class of request that must never
 * be served stale while a network is actually reachable. */
async function networkFirst(request) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  if (!cached) {
    const response = await fetch(request);
    if (response.ok) {
      await cache.put(request, response.clone());
    }
    return response;
  }

  const controller = new AbortController();
  let timeoutId;
  const network = fetch(request, { signal: controller.signal })
    .then(async (response) => {
      if (response.ok) {
        await cache.put(request, response.clone());
      }
      return response;
    })
    .catch(() => cached);
  const deadline = new Promise((resolve) => {
    timeoutId = setTimeout(() => {
      controller.abort();
      resolve(cached);
    }, NAVIGATION_NETWORK_TIMEOUT_MS);
  });

  try {
    return await Promise.race([network, deadline]);
  } finally {
    clearTimeout(timeoutId);
  }
}

/** Cache-first for everything else same-origin: Vite's hashed asset
 * filenames are immutable by construction, and the handful of non-hashed
 * static files (icons, WWT's own text assets) change rarely enough that
 * "was already fetched once" is a fine proxy for "still current". */
async function cacheFirst(request) {
  const shellCache = await caches.open(SHELL_CACHE);
  const shellHit = await shellCache.match(request);
  if (shellHit) return shellHit;

  const runtimeCache = await caches.open(RUNTIME_CACHE);
  const runtimeHit = await runtimeCache.match(request);
  if (runtimeHit) return runtimeHit;

  const response = await fetch(request);
  if (response.ok) {
    await runtimeCache.put(request, response.clone());
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // No protocol/API knowledge belongs in a component (architecture policy), and no
  // caching belongs here either — see the file header.
  if (url.pathname.startsWith("/api/") || url.pathname === "/ws") return;

  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request));
    return;
  }

  event.respondWith(cacheFirst(request));
});
