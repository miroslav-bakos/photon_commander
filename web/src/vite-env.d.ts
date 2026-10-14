// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/// <reference types="vite/client" />

/** This repo's own git remote, resolved once at build/config-eval time
 * (`vite.config.ts`'s `resolveSourceUrl`) — `null` when none is configured.
 * Read only by `AboutPanel`, which renders no source link rather than one
 * that's hardcoded or guessed (architecture policy invariant 7). */
declare const __SSC_SOURCE_URL__: string | null;

/** Exact source commit bound to the iframe HTML URL at build time;
 * "development" when Git provenance is unavailable. */
declare const __SSC_SKYMAP_REV__: string;
