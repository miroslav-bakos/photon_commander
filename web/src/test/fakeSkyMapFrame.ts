// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/** A controllable stand-in for the `<iframe>` + `window` message channel
 * `IframeSkyMapBackend` talks over, for the sky-map conformance suite. No
 * real iframe, no real WebGL — this exercises the host-side postMessage
 * contract the same way for every backend, which is the entire point of the
 * shared conformance suite. */
import type { FrameToHostMessage, HostToFrameMessage } from "../skymap/protocol";
import { SKYMAP_CHANNEL } from "../skymap/protocol";
import type { SkyMapFrameHandle } from "../skymap/IframeSkyMapBackend";

export class FakeSkyMapFrame {
  readonly sentToFrame: HostToFrameMessage[] = [];
  private readonly listeners = new Set<(event: MessageEvent) => void>();

  readonly frame: SkyMapFrameHandle = {
    contentWindow: {
      postMessage: (message: unknown) => {
        this.sentToFrame.push(message as HostToFrameMessage);
      },
    },
  };

  readonly messageSource = {
    addEventListener: (type: string, listener: EventListenerOrEventListenerObject) => {
      if (type !== "message") return;
      this.listeners.add(listener as (event: MessageEvent) => void);
    },
    removeEventListener: (type: string, listener: EventListenerOrEventListenerObject) => {
      if (type !== "message") return;
      this.listeners.delete(listener as (event: MessageEvent) => void);
    },
  };

  /** Simulate the frame document posting a message back to the host. */
  simulateFrameMessage(message: FrameToHostMessage): void {
    const event = { data: message } as MessageEvent;
    for (const listener of this.listeners) listener(event);
  }

  listenerCount(): number {
    return this.listeners.size;
  }
}

export function readySignal(overrides: Partial<Extract<FrameToHostMessage, { type: "ready" }>> = {}): FrameToHostMessage {
  return {
    channel: SKYMAP_CHANNEL,
    type: "ready",
    view: { ra: 83.633, dec: 22.0145, fovDeg: 2 },
    credits: [{ label: "Legacy Surveys DR11 (grz) — Legacy Surveys / D. Lang (Perimeter Institute)" }],
    viewportPx: { w: 800, h: 600 },
    ...overrides,
  };
}
