// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { collectClientConfiguration, parseConfigurationFile, restoreClientConfiguration } from "./configurationFile";

function storage(initial: Record<string, string>): Storage {
  const values = new Map(Object.entries(initial));
  return {
    get length() { return values.size; },
    clear: () => { values.clear(); },
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key); },
    setItem: (key, value) => { values.set(key, value); },
  };
}

function file(client: Record<string, string> = {}) {
  return JSON.stringify({
    format: "photon-commander-configuration",
    schema_version: 1,
    exported_at: "2026-09-14T10:00:00Z",
    server: { schema_version: 1, exported_at: "2026-09-14T10:00:00Z", settings: { appearance: { theme: "red" } } },
    client: { local_storage: client },
  });
}

describe("configuration files", () => {
  it("preserves guided setup progress through server preferences and configuration backup", () => {
    const progress = JSON.stringify({ step: 4, reviewed: [0, 1, 2] });
    const entries = collectClientConfiguration(storage({ "ssc.setup.v1": progress }));
    const parsed = parseConfigurationFile(file(entries));
    const restored = storage({});
    restoreClientConfiguration(restored, parsed.client.local_storage);
    expect(restored.getItem("ssc.setup.v1")).toBe(progress);
  });
  it("collects and restores only reviewed Photon Commander preferences", () => {
    const source = storage({
      "ssc.capture-form.v1": "capture",
      "ssc.collapsed.planning.target": "1",
      "unrelated-site-token": "private",
    });
    expect(collectClientConfiguration(source)).toEqual({
      "ssc.capture-form.v1": "capture",
      "ssc.collapsed.planning.target": "1",
    });

    restoreClientConfiguration(source, { "ssc.planning-ui.v1": "planning" });
    expect(source.getItem("ssc.capture-form.v1")).toBeNull();
    expect(source.getItem("ssc.planning-ui.v1")).toBe("planning");
    expect(source.getItem("unrelated-site-token")).toBe("private");
  });

  it("validates version, browser-key allowlist, and layout structure before import", () => {
    expect(parseConfigurationFile(file()).schema_version).toBe(1);
    expect(() => parseConfigurationFile(file({ "session-token": "no" }))).toThrow("Unsupported browser setting");
    expect(() => parseConfigurationFile(file({
      "ssc.layout.v1": JSON.stringify({ state: { saved: { version: 99, tabs: {} } }, version: 1 }),
    }))).toThrow("Invalid saved layout");
  });
});
