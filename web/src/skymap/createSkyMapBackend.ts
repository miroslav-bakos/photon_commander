// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The one place `SkyMap.tsx` constructs a `SkyMapBackend`. M11's own vector
 * renderer (`VectorSkyMapBackend.ts`) is the only implementation today —
 * Aladin, then WWT, each had a licence problem the other didn't share
 * (`THIRD_PARTY_NOTICES.md` §9), and WWT was removed outright once the vector
 * frame drew every survey the registry carries (`b6955f4`), leaving it no
 * remaining capability.
 *
 * This indirection stays anyway, one backend behind it or not: it is what
 * made dropping Aladin, and then WWT, a config/import change rather than a
 * rewrite, and it is exactly the seam any future backend would arrive
 * through and be required to pass the same conformance suite
 * (`VectorSkyMapBackend.test.ts`) on. `FRAME_SRC` is the iframe's `src`;
 * `createSkyMapBackend` is the adapter constructor — `SkyMap.tsx` calls both
 * without knowing which backend answers.
 */
import type { IframeSkyMapBackendOptions, SkyMapFrameHandle } from "./IframeSkyMapBackend";
import type { SkyMapBackend } from "./types";
import { createVectorSkyMapBackend, VECTOR_FRAME_SRC } from "./VectorSkyMapBackend";

export const FRAME_SRC = VECTOR_FRAME_SRC;

export function createSkyMapBackend(
  frame: SkyMapFrameHandle,
  options?: Omit<IframeSkyMapBackendOptions, "frame">,
): SkyMapBackend {
  return createVectorSkyMapBackend(frame, options);
}
