/// <reference types="vitest/config" />
import { rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import type { Plugin } from 'vite'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
const SSC_PORT = process.env.SSC_DEV_PROXY_PORT ?? '7777'

// The iframe HTML can otherwise outlive a deployment in the browser cache,
// retaining an obsolete renderer even when the parent app has been updated.
function resolveSkyMapRevision(): string {
  try {
    const revision = execFileSync('git', ['rev-parse', '--verify', 'HEAD'], {
      cwd: fileURLToPath(new URL('.', import.meta.url)),
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim()
    if (/^[a-f0-9]{40}$/.test(revision)) return revision
  } catch { /* Source archives or development outside a Git checkout. */ }
  return 'development'
}

// Resolved once at build/config-eval time for AboutPanel's "source" link
// (ssc-8i3.3) — never hardcoded (architecture policy invariant 7: no fabricated value
// standing in for one that isn't actually known). This repo currently has no
// git remote configured at all, so `null` is the honest, expected answer in
// dev; AboutPanel renders no link rather than a broken or invented one. If a
// remote is ever added, an HTTPS-style URL is derived from either form so the
// rendered link is clickable instead of a raw `git@` SSH string.
function resolveSourceUrl(): string | null {
  try {
    const raw = execFileSync('git', ['remote', 'get-url', 'origin'], {
      cwd: fileURLToPath(new URL('.', import.meta.url)),
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim()
    if (raw === '') return null
    const sshMatch = /^git@([^:]+):(.+?)(?:\.git)?$/.exec(raw)
    if (sshMatch) return `https://${sshMatch[1]}/${sshMatch[2]}`
    return raw.replace(/\.git$/, '')
  } catch {
    return null // no remote configured, or git itself unavailable
  }
}

// Marketing screenshot permissions do not authorize application redistribution.
// Dev serves the standalone site, but every application build excludes it.
function excludeProductWebsite(): Plugin {
  let productOutput: string
  return {
    name: 'exclude-product-website',
    apply: 'build',
    configResolved(config) { productOutput = resolve(config.root, config.build.outDir, 'product') },
    async closeBundle() { await rm(productOutput, { recursive: true, force: true }) },
  }
}

export default defineConfig({
  define: {
    __SSC_SOURCE_URL__: JSON.stringify(resolveSourceUrl()),
    __SSC_SKYMAP_REV__: JSON.stringify(resolveSkyMapRevision()),
  },
  resolve: { alias: { "@ssc/locale": fileURLToPath(new URL("./src/i18n", import.meta.url)) } },
  plugins: [react({ jsxImportSource: "@ssc/locale" }), tailwindcss(), excludeProductWebsite()],
  build: {
    rollupOptions: {
      // A separate HTML entry, not folded into index.html's graph:
      // skymap-vector.html is its own Rollup entry point producing its own
      // emitted chunk. Originally an LGPL obligation for Aladin, then a
      // licence-isolation boundary for WWT (1.3 MiB) — both removed
      // (`THIRD_PARTY_NOTICES.md` §9) — it stays because it is also just
      // correct: the sky map has no business in the app chunk, and keeping
      // the boundary structural (architecture policy invariant 9) is what made
      // swapping the engine out a build change rather than a rewrite, twice.
      // The vector renderer has no engine to isolate a licence for, but
      // stays split the same way: the `postMessage`-only boundary every
      // `SkyMapBackend` uses, not a special case for the one that happens to
      // be pure canvas.
      input: {
        app: fileURLToPath(new URL('./index.html', import.meta.url)),
        'skymap-vector': fileURLToPath(new URL('./skymap-vector.html', import.meta.url)),
      },
    },
  },
  server: {
    // architecture policy: agents verify UI work against DEV (7778), never by clicking
    // around SIT (7777) -- which is the owner's own instance. That instruction
    // was unfollowable while this target was a literal, so it is an env var
    // with SIT's port as the default a bare `npm run dev` still gets.
    proxy: {
      '/api': `http://127.0.0.1:${SSC_PORT}`,
      '/ws': {
        target: `ws://127.0.0.1:${SSC_PORT}`,
        ws: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: false,
    css: true,
  },
})
