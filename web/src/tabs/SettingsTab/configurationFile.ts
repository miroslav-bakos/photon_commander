// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { ServerConfigurationBundle } from "../../api/settings";
import { LAYOUT_STORAGE_KEY, validateSavedLayouts } from "../../layout/layoutStore";

export const CONFIGURATION_FORMAT = "photon-commander-configuration";
export const MAX_CONFIGURATION_FILE_BYTES = 2 * 1024 * 1024;
const CLIENT_KEYS = [LAYOUT_STORAGE_KEY, "ssc.capture-form.v1", "ssc.planning-ui.v1", "ssc.scheduling-ui.v1", "ssc.fullscreen-preferred", "ssc.setup.v1"];
const CLIENT_PREFIXES = ["ssc.collapsed.", "ssc.skymap."];

export interface ConfigurationFile {
  format: typeof CONFIGURATION_FORMAT;
  schema_version: 1;
  exported_at: string;
  server: ServerConfigurationBundle;
  client: { local_storage: Record<string, string> };
}

function allowedClientKey(key: string): boolean {
  return CLIENT_KEYS.includes(key) || CLIENT_PREFIXES.some((prefix) => key.startsWith(prefix));
}

export function collectClientConfiguration(storage: Storage): Record<string, string> {
  const result: Record<string, string> = {};
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key !== null && allowedClientKey(key)) {
      const value = storage.getItem(key);
      if (value !== null) result[key] = value;
    }
  }
  return result;
}

export function parseConfigurationFile(text: string): ConfigurationFile {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("This is not a valid JSON configuration file.");
  }
  if (typeof value !== "object" || value === null) throw new Error("Configuration must be an object.");
  const root = value as Record<string, unknown>;
  if (root.format !== CONFIGURATION_FORMAT || root.schema_version !== 1) throw new Error("Unsupported Photon Commander configuration format.");
  if (typeof root.exported_at !== "string" || !Number.isFinite(Date.parse(root.exported_at))) throw new Error("Configuration export time is invalid.");
  if (typeof root.server !== "object" || root.server === null) throw new Error("Server settings are missing or invalid.");
  const server = root.server as Record<string, unknown>;
  if (server.schema_version !== 1 || typeof server.exported_at !== "string" || typeof server.settings !== "object" || server.settings === null) throw new Error("Server settings are missing or invalid.");
  if (typeof root.client !== "object" || root.client === null) throw new Error("Browser settings are missing or invalid.");
  const entries = (root.client as Record<string, unknown>).local_storage;
  if (typeof entries !== "object" || entries === null || Array.isArray(entries)) throw new Error("Browser settings are missing or invalid.");
  for (const [key, entry] of Object.entries(entries)) {
    if (!allowedClientKey(key) || typeof entry !== "string" || entry.length > MAX_CONFIGURATION_FILE_BYTES) throw new Error(`Unsupported browser setting: ${key}`);
  }
  const rawLayout = (entries as Record<string, string>)[LAYOUT_STORAGE_KEY];
  if (rawLayout !== undefined) {
    try {
      const persisted = JSON.parse(rawLayout) as { state?: { saved?: unknown } };
      validateSavedLayouts(persisted.state?.saved);
    } catch (error) {
      throw new Error(error instanceof Error ? `Invalid saved layout: ${error.message}` : "Invalid saved layout.");
    }
  }
  return value as ConfigurationFile;
}

export function restoreClientConfiguration(storage: Storage, entries: Record<string, string>): void {
  const existing = collectClientConfiguration(storage);
  for (const key of Object.keys(existing)) storage.removeItem(key);
  for (const [key, entry] of Object.entries(entries)) storage.setItem(key, entry);
}
