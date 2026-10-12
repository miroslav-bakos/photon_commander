// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The vector backend's own run of the shared adapter conformance suite
 * (`skyMapBackend.conformance.ts`'s own doc comment). WWT had this same
 * coverage (`WwtSkyMapBackend.test.ts`, deleted with the rest of WWT,
 * `THIRD_PARTY_NOTICES.md` §9); the vector backend — the only `SkyMapBackend`
 * left — had none of its own until now, since `IframeSkyMapBackend` (what
 * this suite actually exercises) was already covered through WWT's call
 * into it. Moving the call rather than dropping it keeps the one remaining
 * backend from losing a check it always should have had.
 */
import { createVectorSkyMapBackend } from "./VectorSkyMapBackend";
import { describeSkyMapBackendConformance } from "./conformance/skyMapBackend.conformance";
import { describe, expect, it } from "vitest";
import { readySignal } from "../test/fakeSkyMapFrame";
import { FakeSkyMapFrame } from "../test/fakeSkyMapFrame";

describeSkyMapBackendConformance("vector", () => {
  const fake = new FakeSkyMapFrame();
  const backend = createVectorSkyMapBackend(fake.frame, { messageSource: fake.messageSource, targetOrigin: "*" });
  return { fake, backend };
});


describe("reattached frame handshake", () => {
  it("requests readiness after missing the document's initial ready and flushes current layers", () => {
    const fake = new FakeSkyMapFrame();
    fake.simulateFrameMessage(readySignal());
    const backend = createVectorSkyMapBackend(fake.frame, {
      messageSource: fake.messageSource, targetOrigin: "*", requestReady: true,
    });
    backend.setSurveys([]);
    backend.setLayers({ background: null, backgroundOpacity: 1, overlays: [] });
    expect(fake.sentToFrame.map((message) => message.type)).toEqual(["requestReady"]);
    fake.simulateFrameMessage(readySignal());
    expect(fake.sentToFrame.map((message) => message.type)).toEqual(["requestReady", "setSurveys", "setLayers"]);
    backend.dispose();
  });
});
