// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { useSyncExternalStore } from "react";
import messages from "./messages.json";

export type Language = "en" | "sk" | "cs";
export const LANGUAGES = { en: "English", sk: "Slovenčina", cs: "Čeština" } as const;
const caseKeys = new Map(Object.keys(messages).map((key) => [key.toLowerCase(), key]));
const templates = Object.entries(dictionaryTemplates());
function dictionaryTemplates(): Record<string, { pattern: RegExp; names: string[]; messages: readonly string[] }> {
  const result: Record<string, { pattern: RegExp; names: string[]; messages: readonly string[] }> = {};
  for (const [key, entry] of Object.entries(messages)) {
    const names: string[] = [];
    const parts = key.split(/(\{[a-zA-Z]+\})/);
    if (parts.length === 1) continue;
    const pattern = parts.map((part) => {
      if (/^\{[a-zA-Z]+\}$/.test(part)) { names.push(part.slice(1, -1)); return "(.+?)"; }
      return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }).join("");
    result[key] = { pattern: new RegExp(`^${pattern}$`), names, messages: entry };
  }
  return result;
}
const storageKey = "ssc.language";
const listeners = new Set<() => void>();
const dictionary: Readonly<Record<string, readonly string[]>> = messages;
function isLanguage(value: unknown): value is Language {
  return value === "en" || value === "sk" || value === "cs";
}
function initialLanguage(): Language {
  if (typeof window === "undefined") return "en";
  const query = new URLSearchParams(window.location.search).get("lang");
  if (isLanguage(query)) return query;
  try {
    const stored = window.localStorage.getItem(storageKey);
    if (isLanguage(stored)) return stored;
  } catch { /* Locked browser storage does not disable language switching. */ }
  return window.navigator.languages.map((tag) => tag.split("-")[0]).find(isLanguage) ?? "en";
}
let language = initialLanguage();
function notify(): void { listeners.forEach((listener) => { listener(); }); }
export function setLanguage(next: Language): void {
  if (next === language) return;
  language = next;
  if (typeof document !== "undefined") document.documentElement.lang = next;
  try { window.localStorage.setItem(storageKey, next); } catch { /* Session-only preference. */ }
  notify();
}
export function getLanguage(): Language { return language; }
function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function useLanguage(): Language {
  return useSyncExternalStore(subscribe, getLanguage, () => "en");
}
if (typeof window !== "undefined") {
  document.documentElement.lang = language;
  window.addEventListener("storage", (event) => {
    if (event.key !== storageKey) return;
    const next = isLanguage(event.newValue) ? event.newValue : "en";
    if (next !== language) {
      language = next;
      document.documentElement.lang = next;
      notify();
    }
  });
}
const translatableCache = new Map<string, boolean>();
/** Unknown technical evidence and numeric values need no locale subscription. */
export function isTranslatable(value: string): boolean {
  const cached = translatableCache.get(value);
  if (cached !== undefined) return cached;
  const trimmed = value.trim();
  const key = trimmed.replace(/\s+/g, " ");
  const result = dictionary[key] !== undefined || caseKeys.has(key.toLowerCase()) || templates.some(([, template]) => template.pattern.test(trimmed));
  if (translatableCache.size >= 512) translatableCache.clear();
  translatableCache.set(value, result);
  return result;
}
/** Exact message lookup preserves user names, paths, identifiers and unrecognised
 * backend evidence. Leading/trailing spaces remain React's own text boundaries. */
export function translate(value: string, locale: Language = language): string {
  if (locale === "en") return value;
  const trimmed = value.trim();
  const key = trimmed.replace(/\s+/g, " ");
  const entry = dictionary[key] ?? dictionary[caseKeys.get(key.toLowerCase()) ?? ""];
  let translated = entry?.[locale === "sk" ? 0 : 1];
  if (translated !== undefined) {
    if (key === key.toUpperCase() && key !== key.toLowerCase()) translated = translated.toLocaleUpperCase(locale);
    return value.replace(trimmed, translated);
  }
  for (const [, template] of templates) {
    const match = template.pattern.exec(trimmed);
    if (match === null) continue;
    const params = new Map(template.names.map((name, index) => [name, match[index + 1] ?? ""]));
    const text = template.messages[locale === "sk" ? 0 : 1];
    if (text !== undefined) return value.replace(trimmed, () => text.replace(/\{([a-zA-Z]+)\}/g, (_, name: string) => params.get(name) ?? `{${name}}`));
  }
  return value;
}
