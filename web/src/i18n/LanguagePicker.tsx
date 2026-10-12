// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { LANGUAGES, setLanguage, useLanguage, type Language } from "./locale";
export function LanguagePicker() {
  const language = useLanguage();
  return <label className="flex items-center gap-2 text-sm text-fg-muted">
    Language
    <select aria-label="Language" value={language} onChange={(event) => { setLanguage(event.target.value as Language); }} className="min-h-11 rounded border border-border bg-surface px-3 text-fg">
      {Object.entries(LANGUAGES).map(([code, name]) => <option key={code} value={code} translate="no">{name}</option>)}
    </select>
  </label>;
}
