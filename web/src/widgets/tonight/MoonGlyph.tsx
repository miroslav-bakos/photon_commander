// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { moonPath, phaseName } from "./moonPhase";

/**
 * The moon phase, drawn — not just numbered (architecture policy: "Draw the limb from
 * the waxing/waning field, never from the fraction alone"). Every fill and
 * stroke is a theme token class (`fill-*`/`stroke-*` off `tailwind.config`'s
 * `--color-*` custom properties), so this is exactly the "moon glyph wants
 * to be white" case the red-light audit calls out, and it never is: the lit
 * limb is `fill-fg`, which is a saturated red in red-light mode, not white.
 */
export function MoonGlyph({
  illuminatedFraction,
  waxing,
  hemisphere,
  size = 22,
  className = "",
}: {
  illuminatedFraction: number;
  waxing: boolean;
  hemisphere: "north" | "south";
  size?: number;
  className?: string;
}) {
  const r = 10;
  const d = moonPath({ illuminatedFraction, waxing, hemisphere, radius: r, cx: r + 1, cy: r + 1 });
  const pct = Math.round(Math.min(1, Math.max(0, illuminatedFraction)) * 100).toString();
  const label = `${phaseName(illuminatedFraction, waxing)}, ${pct}% illuminated`;

  return (
    <svg
      viewBox="0 0 22 22"
      width={size}
      height={size}
      role="img"
      aria-label={label}
      className={className}
    >
      {/* The unlit disc — always drawn, including at new moon, so the glyph
          reads as "the moon, currently dark" rather than disappearing. */}
      <circle cx={r + 1} cy={r + 1} r={r} className="fill-surface-raised stroke-border-strong" strokeWidth={1} />
      {/* The lit limb — `moonPath` requires both `waxing` and `hemisphere`
          as real arguments (not optional ones), so this cannot regress to
          drawing the limb from `illuminatedFraction` alone. */}
      <path d={d} className="fill-fg" />
    </svg>
  );
}
