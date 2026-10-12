// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { CSSProperties, ReactNode } from "react";
import { Bug } from "lucide-react";
import { Link, useLocation } from "react-router-dom";
import appIcon from "../assets/brand/app-icon.svg";
import { SkyMapCreditsFooter } from "../skymap/SkyMapCreditsFooter";
import { ConnectionsFooter } from "./ConnectionsFooter";
import { BELOW_DESKTOP_NAV_QUERY, DesktopUtilityNav, TabNav, WorkflowSubnav } from "./TabNav";
import { GlobalOperationBanner } from "./GlobalOperationBanner";
import { WeatherWidget } from "../widgets/weather/WeatherWidget";
import { useConnectionStore } from "../store/connectionStore";
import { beginNavigation } from "../performance/diagnostics";
import { useMediaQuery } from "../tabs/SchedulingTab/useMediaQuery";

/** One shell owns the viewport. Only main scrolls; mobile navigation and
 * connection status occupy flex rows at the bottom, above the safe area.
 * Normal flow avoids Safari's offset painting of viewport-fixed controls.
 */
export function Shell({ children }: { children: ReactNode }) {
  const planning = useLocation().pathname === "/planning";
  const mobileHeader = useMediaQuery(BELOW_DESKTOP_NAV_QUERY);
  const appearance = useConnectionStore((state) => state.snapshot?.settings ?? null);
  const wallpaperEnabled = appearance?.background_enabled === true && appearance.theme !== "red";
  const wallpaperStyle = {
    "--ssc-wallpaper-opacity": String((appearance?.background_opacity_percent ?? 15) / 100),
  } as CSSProperties;

  return (
    <div
      className="app-shell flex min-h-0 flex-col overflow-hidden text-fg"
      data-wallpaper={wallpaperEnabled ? "on" : "off"}
      style={wallpaperStyle}
      onClickCapture={(event) => {
        const link = (event.target as Element).closest("a[href]");
        const hash = link?.getAttribute("href")?.match(/^#(\/[^?#]*)/);
        if (hash?.[1]) beginNavigation(hash[1]);
      }}
    >
      {/* No skip-to-content link: removed on the owner's request, 2026-09-08
          ("remove that skip to content button please") — it was showing up in
          real use rather than staying the invisible-until-focused affordance
          it is meant to be. The nav it would skip past is four links, so the
          keyboard cost of losing it is small. */}
      {/* `pt` adds `env(safe-area-inset-top)` on top of the normal `py-3`
          padding (rather than replacing it) so the header clears a notch/
          camera cutout when the browser chrome is gone (the footer Fullscreen
          API toggle; the installed app runs `standalone`, which keeps
          the status bar) without adding any extra gap on a device with no
          cutout, where the env() value is just 0. */}
      {/* Mobile navigation lives in the bottom dock; desktop navigation stays
          beside the brand. Keep the mobile header light and compact. */}
      <header className="ssc-toolbar relative z-50 flex flex-nowrap items-center justify-between gap-1 overflow-visible border-b border-border bg-surface/35 px-2 pt-[calc(var(--spacing)*3+env(safe-area-inset-top))] pb-3 sm:px-4 md:gap-3 md:backdrop-blur-sm">
        <div className="flex min-w-0 items-center md:gap-4">
          <h1 className="shrink-0">
            <button type="button" aria-label="Reload Photon Commander" title="Reload Photon Commander" className="flex size-11 items-center justify-center rounded-lg focus-visible:outline-2 focus-visible:outline-accent" onClick={() => { window.location.reload(); }}>
            <img
              src={appIcon}
              alt="Photon Commander"
              title="Photon Commander"
              className="size-10 rounded-lg"
            />
            </button>
          </h1>
          {!mobileHeader && <TabNav />}
        </div>
        <div className="flex min-w-0 shrink-0 items-center gap-0 lg:gap-2">
          <WeatherWidget />
          <Link to="/help#bug-report" className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md px-2 text-fg-muted hover:text-fg" aria-label="Report a bug" title="Report a bug"><Bug aria-hidden="true" className="size-5" /></Link>
          <DesktopUtilityNav />
        </div>
      </header>
      {mobileHeader ? <WorkflowSubnav compact docked /> : <WorkflowSubnav />}
      <GlobalOperationBanner />
      <div className="flex min-h-0 flex-1 flex-col">
        <main id="main" className="relative flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto overscroll-y-contain px-4 py-4 sm:pb-6">
          {planning ? <div className="flex min-h-full shrink-0 flex-col md:h-full md:min-h-0">{children}</div> : children}
          <SkyMapCreditsFooter />
        </main>
        <ConnectionsFooter docked={mobileHeader} />
        {mobileHeader && <TabNav docked />}
      </div>
    </div>
  );
}
