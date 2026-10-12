// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { acknowledgeCommand, pendingCommandKey, waitForOperation, type OperationState } from "./operations";

const accepted = { id: "test-operation", status: "running", message: "Working" } as OperationState;
const finished = () => Response.json({ ...accepted, status: "succeeded", result: { done: true } });

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); globalThis.sessionStorage.clear(); });

describe("accepted operation recovery", () => {
  it("retries only status reads after a network drop", async () => {
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError("offline")).mockResolvedValueOnce(finished());
    vi.stubGlobal("fetch", fetch);
    const result = waitForOperation(accepted);
    const assertion = expect(result).resolves.toEqual({ done: true });
    await vi.advanceTimersByTimeAsync(2_000);
    await assertion;
    expect(fetch).toHaveBeenCalledTimes(2);
    for (const [request] of fetch.mock.calls) {
      expect((request as Request).method).toBe("GET");
      expect(new URL((request as Request).url).pathname).toBe("/api/operations/test-operation");
    }
  });

  it("recovers a stalled read through the request deadline", async () => {
    const fetch = vi.fn().mockImplementationOnce(() => new Promise(() => undefined)).mockResolvedValueOnce(finished());
    vi.stubGlobal("fetch", fetch);
    const result = waitForOperation(accepted);
    const assertion = expect(result).resolves.toEqual({ done: true });
    await vi.advanceTimersByTimeAsync(32_000);
    await assertion;
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it.each([408, 429, 500, 502, 503, 504])("recovers transient HTTP %i", async (status) => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(null, { status })).mockResolvedValueOnce(finished());
    vi.stubGlobal("fetch", fetch);
    const result = waitForOperation(accepted);
    const assertion = expect(result).resolves.toEqual({ done: true });
    await vi.advanceTimersByTimeAsync(2_000);
    await assertion;
  });

  it.each([401, 404])("surfaces permanent HTTP %i without retry", async (status) => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status }));
    vi.stubGlobal("fetch", fetch);
    const assertion = expect(waitForOperation(accepted)).rejects.toThrow(`HTTP ${String(status)}`);
    await vi.advanceTimersByTimeAsync(1_000);
    await assertion;
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("preserves a terminal operation failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...accepted, status: "failed", error: "Solver failed" })));
    const assertion = expect(waitForOperation(accepted)).rejects.toThrow("Solver failed");
    await vi.advanceTimersByTimeAsync(1_000);
    await assertion;
  });

  it("bounds consecutive outage while keeping acceptance truthful", async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError("offline"));
    vi.stubGlobal("fetch", fetch);
    const assertion = expect(waitForOperation(accepted)).rejects.toThrow(/may still be running.*check its status/i);
    await vi.advanceTimersByTimeAsync(125_000);
    await assertion;
    expect(fetch.mock.calls.length).toBeLessThan(30);
  });
});

describe("pendingCommandKey", () => {
  it("uses crypto.randomUUID when the browser supports it", () => {
    const randomUUID = vi.fn(() => "11111111-2222-4333-8444-555555555555");
    vi.stubGlobal("crypto", { randomUUID, getRandomValues: vi.fn() });

    expect(pendingCommandKey("plate-solve")).toBe("11111111-2222-4333-8444-555555555555");
    expect(randomUUID).toHaveBeenCalledOnce();
  });

  it("builds an RFC 4122 v4 key with getRandomValues when randomUUID is unavailable", () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.forEach((_, index) => { bytes[index] = index; });
      return bytes;
    });
    vi.stubGlobal("crypto", { getRandomValues });

    expect(() => pendingCommandKey("plate-solve")).not.toThrow();
    expect(pendingCommandKey("plate-solve")).toBe("00010203-0405-4607-8809-0a0b0c0d0e0f");
    expect(getRandomValues).toHaveBeenCalledOnce();
  });

  it("reuses a pending key until the operation is acknowledged", () => {
    const randomUUID = vi.fn(() => "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee");
    vi.stubGlobal("crypto", { randomUUID, getRandomValues: vi.fn() });

    expect(pendingCommandKey("plate-solve")).toBe(pendingCommandKey("plate-solve"));
    expect(randomUUID).toHaveBeenCalledOnce();
    acknowledgeCommand("plate-solve");
    pendingCommandKey("plate-solve");
    expect(randomUUID).toHaveBeenCalledTimes(2);
  });
});
