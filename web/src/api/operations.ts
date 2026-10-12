// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { timedApiFetch } from "../performance/diagnostics";
import { useConnectionStore } from "../store/connectionStore";
import type { components } from "./schema";

export type OperationState = components["schemas"]["OperationState"];

export function useLatestOperation(kind: string): OperationState | undefined {
  return useConnectionStore((state) => {
    const operations = state.snapshot?.operations ?? [];
    return [...operations].reverse().find((operation) => operation.kind === kind);
  });
}

function createCommandKey(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi.randomUUID === "function") return cryptoApi.randomUUID();

  const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** Keep a key until the server acknowledges acceptance. A response lost after
 * acceptance therefore makes the next click a safe lookup, not a duplicate. */
export function pendingCommandKey(kind: string): string {
  const storageKey = `photon.pending-command.${kind}`;
  const existing = globalThis.sessionStorage.getItem(storageKey);
  if (existing != null) return existing;
  const created = createCommandKey();
  globalThis.sessionStorage.setItem(storageKey, created);
  return created;
}

export function acknowledgeCommand(kind: string): void {
  globalThis.sessionStorage.removeItem(`photon.pending-command.${kind}`);
}

export function isOperationState(value: unknown): value is OperationState {
  return typeof value === "object" && value !== null && "id" in value && "status" in value;
}

export async function waitForOperation<T>(accepted: OperationState | T | undefined): Promise<T> {
  if (!isOperationState(accepted)) return accepted as T;
  let current = accepted;
  let outageStartedAt: number | null = null;
  let failures = 0;
  // Bound inability to observe an accepted command, not the command's runtime.
  // Long transfers and captures can legitimately run for hours.
  const outageLimitMs = 120_000;
  for (;;) {
    if (current.status === "succeeded") return current.result as T;
    if (current.status === "failed" || current.status === "cancelled") {
      throw new Error(current.error ?? current.message);
    }
    const delay = failures === 0 ? 250 : Math.min(1000 * 2 ** (failures - 1), 5000);
    await new Promise((resolve) => setTimeout(resolve, delay));
    if (outageStartedAt !== null && Date.now() - outageStartedAt >= outageLimitMs) {
      throw new Error(`Operation ${current.id} may still be running. Reconnect and check its status before trying again.`);
    }
    let response: Response;
    try {
      response = await timedApiFetch(new Request(new URL(`/api/operations/${encodeURIComponent(current.id)}`, window.location.origin), {
        credentials: "same-origin",
      }));
    } catch {
      outageStartedAt ??= Date.now();
      failures += 1;
      continue;
    }
    if ([408, 429, 500, 502, 503, 504].includes(response.status)) {
      outageStartedAt ??= Date.now();
      failures += 1;
      continue;
    }
    if (!response.ok) throw new Error(`Could not read operation ${current.id} (HTTP ${String(response.status)}).`);
    current = await response.json() as OperationState;
    outageStartedAt = null;
    failures = 0;
  }
}
