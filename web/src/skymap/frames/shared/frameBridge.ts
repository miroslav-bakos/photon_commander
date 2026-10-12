// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The iframe-side half of the `postMessage` bridge. The frame document
 * (`frames/vector/main.ts`; `frames/aladin/main.ts` and `frames/wwt/main.ts`
 * did the same before each was removed in turn — `THIRD_PARTY_NOTICES.md` §9)
 * imports only this file and `skymap/protocol.ts` — never anything under
 * `skymap/` that touches an engine's own library type. This is what kept
 * every frame document symmetrical regardless of what, if anything, it
 * wrapped underneath.
 */
import { type FrameToHostMessage, type HostToFrameMessage, isHostToFrameMessage } from "../../protocol";

export function listenForHostMessages(handler: (message: HostToFrameMessage) => void): void {
  window.addEventListener("message", (event: MessageEvent) => {
    if (!isHostToFrameMessage(event.data)) return;
    handler(event.data);
  });
}

/** `targetOrigin` is `"*"` because the frame does not know at build time what
 * origin the host app is served from (LAN IP, `localhost`, or a phone's
 * address, per architecture policy invariant 11 — no hardcoded address). The message
 * payload carries no secret (view state, overlay geometry, survey credit),
 * so a broadcast `postMessage` here is not a confidentiality problem the way
 * it would be for the WS/REST traffic invariant 8 governs. */
export function postToHost(message: FrameToHostMessage): void {
  window.parent.postMessage(message, "*");
}
