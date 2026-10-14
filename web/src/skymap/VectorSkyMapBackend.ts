// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * M11's own `SkyMapBackend` — the third implementation, and the only one
 * left: no third-party vector-map library (d3-celestial and friends are one
 * more licence to audit for no real gain, `docs/IMPLEMENTATION.md`'s M11
 * section), just `projection.ts`'s existing gnomonic math driving a
 * `<canvas>` (`frames/vector/main.ts`, loaded as this iframe's own document,
 * `web/skymap-vector.html`).
 *
 * The default since 2026-09-09 (architecture policy invariant 9 amended the same day),
 * and the sole backend since WWT was removed once this frame drew HiPS too
 * (`THIRD_PARTY_NOTICES.md` §9: WWT had no remaining capability). Draws survey
 * imagery of its own (`frames/vector/imageryLayer.ts`): Mercator-scheme and
 * HiPS surveys, addressed through this frame's own tile mesh math
 * (`frames/vector/mercatorTiles.ts`, `frames/vector/hipsTiles.ts`) rather
 * than a third-party engine's tiling — a TOAST-scheme survey (none in
 * today's registry) is refused with a stated reason, not silently missing.
 *
 * `IframeSkyMapBackend` needs no changes to serve this backend: both sides
 * of the `postMessage` boundary (`skymap/protocol.ts`) are shared, and what
 * differs is entirely inside `frames/vector/main.ts` — exactly as
 * `IframeSkyMapBackend`'s own doc comment promises any backend would work,
 * proven twice now (WWT, then this one).
 */
import { IframeSkyMapBackend, type IframeSkyMapBackendOptions, type SkyMapFrameHandle } from "./IframeSkyMapBackend";
import type { SkyMapBackend } from "./types";

export const VECTOR_FRAME_SRC = `/skymap-vector.html?build=${encodeURIComponent(__SSC_SKYMAP_REV__)}`;

export function createVectorSkyMapBackend(
  frame: SkyMapFrameHandle,
  options?: Omit<IframeSkyMapBackendOptions, "frame">,
): SkyMapBackend {
  return new IframeSkyMapBackend({ frame, ...options });
}
