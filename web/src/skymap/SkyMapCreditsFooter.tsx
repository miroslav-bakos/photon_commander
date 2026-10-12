// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The footer counterpart of `SkyMap.tsx`'s own credit computation: reads
 * `skyMapCreditsStore` (written by whichever `SkyMap` is currently mounted)
 * and renders exactly what the map's own in-viewport bar used to — moved
 * here, at the end of main's scrollable content, so it stops sitting over the
 * imagery on a small screen (reported live: "the survey license text
 * overlay is blocking view").
 *
 * Renders nothing at all when no `SkyMap` is mounted (`mounted === false`):
 * never an empty bar taking up chrome space for a tab with no map. Shows
 * the same "Loading survey attribution…" honesty (architecture policy invariant 7)
 * a mounted map with no resolved credits yet used to show in place.
 *
 * Planning keeps its `SkyMap` mounted offscreen after the user leaves the
 * tab, so the mounted flag alone is not enough: credits belong only to the
 * visible map screen, not to Capture/Live Stack/etc.
 */
import { useLocation } from "react-router-dom";
import { useSkyMapCreditsStore } from "./skyMapCreditsStore";
import type { SurveyCredit } from "./types";

export function SkyMapCreditsFooter() {
  const visibleMapScreen = useLocation().pathname === "/planning";
  const mounted = useSkyMapCreditsStore((s) => s.mounted);
  const credits = useSkyMapCreditsStore((s) => s.credits);
  if (!visibleMapScreen || !mounted) return null;
  return (
    <div className="flex shrink-0 items-center border-t border-border px-0 py-2 text-[11px] leading-snug text-fg-muted">
      <div>{credits === null ? "Loading survey attribution…" : <CreditLine credits={credits} />}</div>
    </div>
  );
}

/** Every visible layer's credit, in draw order, each linked when it has a
 * URL — the same rendering `SkyMap.tsx`'s own in-map bar used before this
 * moved out to the footer. */
function CreditLine({ credits }: { credits: SurveyCredit[] }) {
  if (credits.length === 0) return <>No imagery layer selected</>;
  return (
    <>
      {credits.map((credit, i) => (
        <span key={`${credit.label}-${String(i)}`}>
          {i > 0 && " · "}
          {credit.url === undefined ? (
            credit.label
          ) : (
            <a href={credit.url} className="underline" target="_blank" rel="noreferrer">
              {credit.label}
            </a>
          )}
        </span>
      ))}
    </>
  );
}
