// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/** A controllable stand-in for the browser `WebSocket`, for `client.test.ts`.
 * Real network behaviour (open/message/close/error) is triggered by the test,
 * not by an actual socket. */
export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  closed = false;
  private readonly listeners = new Map<string, Set<(event: unknown) => void>>();

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: (event: unknown) => void): void {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closed = true;
    this.dispatch("close", {});
  }

  dispatch(type: string, event: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }

  triggerOpen(): void {
    this.dispatch("open", {});
  }

  triggerMessage(data: unknown): void {
    this.dispatch("message", { data: JSON.stringify(data) });
  }

  triggerServerClose(): void {
    this.closed = true;
    this.dispatch("close", {});
  }

  static reset(): void {
    FakeWebSocket.instances = [];
  }

  static latest(): FakeWebSocket {
    const instance = FakeWebSocket.instances.at(-1);
    if (instance === undefined) throw new Error("no FakeWebSocket has been constructed yet");
    return instance;
  }
}
