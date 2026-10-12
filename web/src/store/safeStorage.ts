// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { StateStorage } from "zustand/middleware";

const fallback = new Map<string, string>();

const memoryStorage: StateStorage = {
  getItem: (name) => fallback.get(name) ?? null,
  setItem: (name, value) => { fallback.set(name, value); },
  removeItem: (name) => { fallback.delete(name); },
};

/** localStorage can be absent or throw in tests, private browsing and locked
 * down iframes. Persistence is optional; the application itself is not. */
export function safeLocalStorage(): StateStorage {
  try {
    const storage = Reflect.get(window, "localStorage") as StateStorage | undefined;
    return storage ?? memoryStorage;
  } catch {
    return memoryStorage;
  }
}
