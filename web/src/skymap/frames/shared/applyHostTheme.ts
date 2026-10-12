// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Applies the host app's resolved theme tokens (passed through the iframe's
 * own URL, since a separate document does not inherit `tokens.css`'s CSS
 * custom properties) as `--ssc-*` variables on this document's `:root`, and
 * sets the body background directly.
 *
 * Why this exists at all: an engine inside the frame can paint things a
 * stylesheet cannot reach. WWT drew its own crosshair and constellation
 * figures in its own colours — the crosshair was white — so red-light mode
 * had no way to recolour them from the host; both were switched off
 * outright instead (`applyDisplayDefaults` — the crosshair duplicated the
 * draggable "primary" FOV rectangle once the map was goto'd to a target).
 * WWT is gone (architecture policy invariant 9, removed 2026-09-09: no remaining
 * capability once the vector frame drew HiPS); the vector frame still reads
 * the variables this sets, for the FOV overlay's own colours (`roleStyle.ts`).
 *
 * It also existed for Aladin, whose chrome carried a hardcoded light-grey
 * background (`aladin-box`, computed `rgb(236, 236, 236)`) regardless of the
 * iframe body's colour. Aladin is gone too (`THIRD_PARTY_NOTICES.md` §9); the
 * trap both illustrated — an engine's own chrome ignoring the host's theme —
 * is not.
 *
 * `URL_THEME_KEYS` grew in the M4 overlay pass: `roleStyle.ts` needs a real
 * colour per `ShapeRole` to hand the engine (`fillColor`/`color` options take
 * a CSS colour string, not a custom-property reference), and the only way to
 * get one without a literal colour in this file — or in `roleStyle.ts` —
 * is `getComputedStyle` reading a variable that was actually set. Every
 * token `roleStyle.ts` resolves must be listed here or it silently reads an
 * empty string forever.
 */
/** Exported for `applyHostTheme.test.ts`: it is the authoritative list this
 * doc comment says `roleStyle.ts`'s `ROLE_TOKEN` must match, so a test can
 * actually check that rather than relying on someone rereading the comment
 * next time a role is added. */
export const URL_THEME_KEYS = [
  "bg",
  "fg",
  "fgMuted",
  "surface",
  "surfaceRaised",
  "border",
  "borderStrong",
  "accent",
  "accentMuted",
  "success",
  "warning",
  "danger",
  "cardinal",
  "skyInk",
  "star",
  "sun",
  "moon",
  "planet",
  "stack",
  "envelope",
  "pointing",
] as const;

export function applyHostTheme(): void {
  const params = new URLSearchParams(window.location.search);
  const root = document.documentElement.style;
  for (const key of URL_THEME_KEYS) {
    const value = params.get(key);
    if (value !== null) root.setProperty(`--ssc-${camelToKebab(key)}`, value);
  }
  const bg = params.get("bg");
  if (bg !== null) document.body.style.backgroundColor = bg;
}

function camelToKebab(key: string): string {
  return key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
}
