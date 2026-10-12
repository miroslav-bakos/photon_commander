// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect } from "react";
import { readClientConfiguration, writeClientConfiguration } from "../api/settings";
import { collectClientConfiguration, restoreClientConfiguration } from "../tabs/SettingsTab/configurationFile";

const SYNC_INTERVAL_MS = 1000;

function serialise(entries: Record<string, string>): string {
  return JSON.stringify(Object.entries(entries).sort(([left], [right]) => left.localeCompare(right)));
}

/** Backend is authoritative; localStorage is only a synchronous cache for
 * existing Zustand stores and hooks. This bridge migrates old installs once
 * and continuously persists same-page changes that do not emit storage events. */
export function ClientConfigurationSync() {
  useEffect(() => {
    const lifecycle = { disposed: false };
    const isDisposed = (): boolean => lifecycle.disposed;
    let last = "";
    let timer: ReturnType<typeof setTimeout> | undefined;
    let bootstrapped = false;
    let recovering = false;
    let busy = false;
    const initialLocal = collectClientConfiguration(window.localStorage);

    const sync = async () => {
      if (isDisposed() || busy) return;
      busy = true;
      try {
        if (!bootstrapped) {
          const remote = await readClientConfiguration();
          if (isDisposed()) return;
          const local = collectClientConfiguration(window.localStorage);
          if (Object.keys(remote.entries).length === 0 && Object.keys(local).length > 0) {
            await writeClientConfiguration(local);
            if (isDisposed()) return;
            last = serialise(local);
          } else {
            const remoteSerialised = serialise(remote.entries);
            if (remoteSerialised !== serialise(local)) {
              const restored = { ...remote.entries };
              // Keep browser preference edits made while bootstrap was offline.
              for (const key of new Set([...Object.keys(initialLocal), ...Object.keys(local)])) {
                if (local[key] === initialLocal[key]) continue;
                if (local[key] === undefined) Reflect.deleteProperty(restored, key);
                else restored[key] = local[key];
              }
              restoreClientConfiguration(window.localStorage, restored);
              // Editors are already mounted. Even a successful but delayed
              // bootstrap must not reload and destroy their unsaved work.
              // Restored cache applies fully at the next normal launch.
            }
            last = remoteSerialised;
          }
          bootstrapped = true;
        } else {
          const current = collectClientConfiguration(window.localStorage);
          const next = serialise(current);
          if (next !== last) {
            await writeClientConfiguration(current);
            if (isDisposed()) return;
            last = next;
          }
        }
      } catch {
        recovering = true;
      } finally {
        busy = false;
        if (!isDisposed()) timer = setTimeout(() => { void sync(); }, recovering && !bootstrapped ? 5000 : SYNC_INTERVAL_MS);
      }
    };
    void sync();

    return () => {
      lifecycle.disposed = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, []);
  return null;
}
