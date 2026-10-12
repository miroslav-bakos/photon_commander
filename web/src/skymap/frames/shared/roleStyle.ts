// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `ShapeRole` -> an actual drawable colour, for the frame document that hands
 * shapes to a real drawing API (`frames/vector/main.ts`'s `<canvas>`
 * `fillStyle`/`strokeStyle`; Aladin's `A.polygon`/`A.circle` options and
 * WWT's `Poly`/`Circle` annotation setters wanted the same thing before each
 * was removed in turn, `THIRD_PARTY_NOTICES.md` §9) — a CSS colour string, not a
 * custom-property reference.
 *
 * The colour always comes from `getComputedStyle`, never a literal — the
 * same escape hatch `useThemeColor.ts` documents host-side
 * (`eslint-rules/no-literal-color.js`). `applyHostTheme.ts` is what actually
 * set these properties on `:root` from the iframe's own URL; this file only
 * reads them back out, once per `setOverlay` call (see each frame's
 * `main.ts`), not once per shape.
 */
import type { ShapeRole, ShapeStyle } from "../../types";

export const ROLE_TOKEN: Record<ShapeRole, `--ssc-${string}`> = {
  primary: "--ssc-accent",
  ghost: "--ssc-fg-muted",
  // Its own token, not --ssc-accent. Sharing the accent with `primary` made
  // the rotation indicator the same colour as the FOV rectangle it surrounds,
  // so a fan of swept frames read as a broken duplicate of the frame rather
  // than as rotation (owner, 2026-09-10).
  stack: "--ssc-stack",
  // Its own token, not --ssc-fg-muted. It shared that with `ghost`, so the
  // swept envelope and the session-end frame drew as a dashed grey outline
  // inside another dashed grey outline (found by roleStyle.test.ts while
  // fixing `stack`, 2026-09-10).
  union: "--ssc-envelope",
  keep: "--ssc-success",
  // A mosaic panel outline competes with a dense star field, not with a
  // surface behind a card. --ssc-border-strong is a UI chrome token: at 1px
  // over Pan-STARRS imagery the panels were drawn correctly and could not be
  // seen, which read as "mosaic panels are not displayed".
  grid: "--ssc-warning",
  warning: "--ssc-warning",
  danger: "--ssc-danger",
  // Catalogue outlines, the skyline (equator/meridian/horizon), the
  // coordinate grid and constellation boundaries all share `--ssc-sky-ink`
  // now, not the UI chrome tokens (`--ssc-border`/`--ssc-border-strong`/
  // `--ssc-fg-muted`) they used to point at individually. Those are tuned
  // for contrast against `--ssc-surface`, a mid-grey card, and drawn over
  // `--ssc-bg` (a near-black sky) `--ssc-border` in particular was all but
  // invisible even at full opacity (owner, 2026-09-10: "all the lines and
  // the titles and stuff on skymap should start in white color so opacity
  // can slide it to grey when needed, but even full opacity now is too
  // dark"). See `--ssc-sky-ink`'s own comment in `tokens.css` for why these
  // four share one bright token instead of being hue-separated the way the
  // FOV roles above are: `skyLinesOpacity`/`catalogueOpacity` still tell
  // catalogue outlines apart from the grid/skyline/constellation-boundary
  // group by how much each is dimmed.
  catalog: "--ssc-sky-ink",
  skyline: "--ssc-sky-ink",
  // Deeper than --ssc-danger on purpose: a permanent orientation mark, not a
  // status colour. Bright red here would pull the eye off the target every
  // time the view moved.
  cardinal: "--ssc-cardinal",
  coordGrid: "--ssc-sky-ink",
  constellation: "--ssc-sky-ink",
  // Point markers, not a line or a fill — its own token so a star dot never
  // borrows the catalogue-outline or constellation-boundary colour.
  star: "--ssc-star",
  // Not a colour so much as an absence of one: the never-rises cap washes
  // toward the page background, so it darkens correctly under both the dark
  // and the red-light palettes without needing a token of its own.
  unobservable: "--ssc-bg",
  // The Sun, Moon and planets (solarSystem.ts) each get their own token: a
  // shared colour would make the Sun and Moon indistinguishable at a glance,
  // which defeats the point of drawing them as separate, independently
  // toggled shapes.
  sun: "--ssc-sun",
  moon: "--ssc-moon",
  planet: "--ssc-planet",
  // The believed-pointing indicator (`mountPointing.ts`): its own token,
  // never `danger` or `cardinal` even though both are already red in the
  // dark theme — a status warning, an orientation letter, and "where the
  // scope is right now" are three different questions and must stay three
  // different colours at a glance.
  pointing: "--ssc-pointing",
};

/** Per-role defaults for how much of the shape's own fill opacity actually
 * gets used, layered under whatever `ShapeStyle.fillOpacity` a specific
 * shape instance asks for. Kept here rather than in `overlay.ts` because
 * "how translucent does a role look" is a drawing decision, not a geometry
 * one — `overlay.ts` stays library-agnostic. */
const ROLE_LINE_WIDTH: Record<ShapeRole, number> = {
  primary: 2,
  ghost: 1.5,
  stack: 1,
  union: 1.5,
  keep: 1.5,
  grid: 3,
  warning: 2,
  danger: 2,
  catalog: 1,
  skyline: 1,
  // No stroke: the cardinals are their letters, nothing else. The marker
  // dot was removed on 2026-09-09 ("ok remove the circle then") — the shape
  // itself stays only because a label needs something to anchor to.
  cardinal: 0,
  coordGrid: 1,
  constellation: 1,
  star: 1,
  // No visible outline: the cap is a wash, and a stroked edge would read as
  // a reference line the sky does not actually have.
  unobservable: 0,
  // A thin, ordinary outline — the disc itself (fillOpacity 1) carries the
  // shape; a heavy stroke would just be a ring around a filled circle.
  sun: 1,
  moon: 1,
  planet: 1,
  // A visible stroke, heavier than a planet marker's: this is a status
  // indicator, not background — it must read even when stale/dashed.
  pointing: 2,
};

export interface ResolvedShapeStyle {
  color: string;
  fillColor: string;
  /** 0 (invisible) - 1 (opaque); the shape's own stroke is always fully
   * opaque (`opacity` here governs the fill only, matched to what
   * `applyOverlay` passes as each drawable's `opacity`/fillColor alpha via
   * the caller — see each frame's own use of this field). */
  fillOpacity: number;
  lineWidth: number;
  dashed: boolean;
}

/** Reads every `--ssc-*` token `ROLE_TOKEN` needs, once, from the given
 * element (defaults to `document.documentElement`, where `applyHostTheme`
 * wrote them) — call once per `setOverlay`, not once per shape, so a
 * hundred-shape frame stack does one `getComputedStyle` pass rather than a
 * hundred. */
export function makeRoleColorResolver(root: Element = document.documentElement): (role: ShapeRole) => string {
  const computed = getComputedStyle(root);
  const cache = new Map<ShapeRole, string>();
  return (role: ShapeRole) => {
    const cached = cache.get(role);
    if (cached !== undefined) return cached;
    const value = computed.getPropertyValue(ROLE_TOKEN[role]).trim();
    cache.set(role, value);
    return value;
  };
}

/** A floor on a role's fill opacity, for roles that must stay legible against
 * survey imagery rather than against a UI surface.
 *
 * Mosaic panel outlines were reported as "almost invisible" twice. The stroke
 * colour was already a bright theme token at full opacity, so the problem was
 * weight and fill: thin lines with a 0.05 fill vanish over a dense star field,
 * whatever their hue. A floor here rather than in `overlay.ts` keeps the
 * decision where the other drawing decisions live — `overlay.ts` stays
 * library- and appearance-agnostic. */
const ROLE_MIN_FILL_OPACITY: Partial<Record<ShapeRole, number>> = {
  grid: 0.22,
  // Dark enough that the region reads as 'not your sky' at a glance, light
  // enough that a target sitting just inside it is still findable rather
  // than erased — this hides nothing, it only de-emphasises.
  unobservable: 0.72,
};

export function resolveShapeStyle(style: ShapeStyle, colorOf: (role: ShapeRole) => string): ResolvedShapeStyle {
  const color = colorOf(style.role);
  const floor = ROLE_MIN_FILL_OPACITY[style.role] ?? 0;
  return {
    color,
    fillColor: color,
    fillOpacity: Math.max(style.fillOpacity ?? 0, floor),
    lineWidth: ROLE_LINE_WIDTH[style.role],
    dashed: style.dashed ?? false,
  };
}
