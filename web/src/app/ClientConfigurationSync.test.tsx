// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { claimConfigurationReload } from "./clientConfigurationReloadGuard";

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear: () => { values.clear(); },
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key); },
    setItem: (key, value) => { values.set(key, value); },
  };
}

describe("client configuration reload guard", () => {
  it("allows only one reload for the same remote fingerprint", () => {
    const storage = memoryStorage();
    expect(claimConfigurationReload(storage, "remote-a")).toBe(true);
    expect(claimConfigurationReload(storage, "remote-a")).toBe(false);
  });

  it("allows one reload when the remote configuration genuinely changes", () => {
    const storage = memoryStorage();
    expect(claimConfigurationReload(storage, "remote-a")).toBe(true);
    expect(claimConfigurationReload(storage, "remote-b")).toBe(true);
    expect(claimConfigurationReload(storage, "remote-b")).toBe(false);
  });

  it("does not risk a loop when session storage is unavailable", () => {
    const storage = memoryStorage();
    storage.getItem = () => { throw new Error("blocked"); };
    expect(claimConfigurationReload(storage, "remote-a")).toBe(false);
  });
});

const configApi = vi.hoisted(() => ({ readClientConfiguration: vi.fn(), writeClientConfiguration: vi.fn() }));
vi.mock("../api/settings", () => configApi);
import { useState } from "react";
import * as reloadGuard from "./clientConfigurationReloadGuard";
import { act, render, cleanup, fireEvent, screen } from "@testing-library/react";
import { ClientConfigurationSync } from "./ClientConfigurationSync";
beforeEach(() => { vi.stubGlobal("localStorage", memoryStorage()); });
afterEach(() => { cleanup(); vi.useRealTimers(); configApi.readClientConfiguration.mockReset(); configApi.writeClientConfiguration.mockReset(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe("configuration synchronization recovery", () => {
  it("does not request a reload when a delayed initial read returns while editing", async () => {
    const reload = vi.spyOn(reloadGuard, "claimConfigurationReload");
    let finish!: (value: { entries: Record<string, string> }) => void;
    configApi.readClientConfiguration.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    function Editor() {
      const [draft, setDraft] = useState("original");
      return <><ClientConfigurationSync /><input aria-label="Plan draft" value={draft} onChange={(event) => { setDraft(event.target.value); }} /></>;
    }
    render(<Editor />);
    const field = screen.getByLabelText("Plan draft");
    fireEvent.change(field, { target: { value: "unsaved plan" } });
    await act(async () => { finish({ entries: { "ssc.collapsed.test": "1" } }); await Promise.resolve(); });
    expect(reload).not.toHaveBeenCalled();
    expect(field).toHaveValue("unsaved plan");
  });
  it("retries failed initialization while preserving preferences edited offline", async () => {
    vi.useFakeTimers();
    localStorage.setItem("ssc.collapsed.test", "en");
    configApi.readClientConfiguration.mockRejectedValueOnce(new TypeError("offline")).mockResolvedValue({ entries: { "ssc.collapsed.test": "sk" } });
    configApi.writeClientConfiguration.mockResolvedValue(undefined);
    render(<ClientConfigurationSync />);
    await act(async () => { await Promise.resolve(); });
    localStorage.setItem("ssc.collapsed.test", "cs");
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(configApi.readClientConfiguration).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem("ssc.collapsed.test")).toBe("cs");
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(configApi.writeClientConfiguration).toHaveBeenCalledWith({ "ssc.collapsed.test": "cs" });
  });
  it("does not overlap or resurrect a pending migration write after unmount", async () => {
    vi.useFakeTimers();
    localStorage.setItem("ssc.collapsed.test", "en");
    configApi.readClientConfiguration.mockResolvedValue({ entries: {} });
    let finish!: () => void;
    configApi.writeClientConfiguration.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
    const view = render(<ClientConfigurationSync />);
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(configApi.writeClientConfiguration).toHaveBeenCalledOnce();
    view.unmount();
    await act(async () => { finish(); await Promise.resolve(); await vi.advanceTimersByTimeAsync(10_000); });
    expect(configApi.writeClientConfiguration).toHaveBeenCalledOnce();
  });
});
