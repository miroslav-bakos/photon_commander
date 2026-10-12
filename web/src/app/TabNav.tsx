// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { translate, useLanguage } from "../i18n/locale";
import { LocalizedText } from "../i18n/jsx-runtime";
import { useLayoutEffect, useRef } from "react";
import { Gauge, Settings } from "lucide-react";
import { Link, NavLink, useLocation } from "react-router-dom";

import type { ReactNode } from "react";
import { useMediaQuery } from "../tabs/SchedulingTab/useMediaQuery";

export const BELOW_DESKTOP_NAV_QUERY = "(max-width: 767px)";

/**
 * Stroke-drawn from `currentColor` at 24 units, so each icon inherits the
 * link's own active/muted colour token and needs no per-theme handling of
 * its own (a filled icon would have needed a `--ssc-*` fill and a red-light
 * variant). Hand-written rather than pulled from an icon package: this small
 * navigation set does not justify a dependency.
 */
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className="size-6 shrink-0 md:size-5"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  );
}

/** A folded observing plan/map: Planning is where the night's target and
 * framing route are laid out before scheduling and capture. */
function PlanningIcon() {
  return (
    <Icon>
      <path d="m4 5 5-2 6 2 5-2v16l-5 2-6-2-5 2V5Z" />
      <path d="M9 3v16M15 5v16" />
      <path d="m6.5 10 2 2 4-4" />
    </Icon>
  );
}

function DashboardIcon() {
  return <Gauge aria-hidden="true" className="size-6 shrink-0 md:size-5" strokeWidth={1.5} />;
}

/** A calendar: Scheduling is the plan laid out across the night. */
function SchedulingIcon() {
  return (
    <Icon>
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <path d="M3 10h18M8 3v4M16 3v4" />
    </Icon>
  );
}

/** Month grid: the saved-plan overview adjacent to the nightly scheduler. */
function CalendarOverviewIcon() {
  return (
    <Icon>
      <rect x="3" y="4" width="18" height="17" rx="2" />
      <path d="M3 9h18M8 2v4M16 2v4M7 13h2M11 13h2M15 13h2M7 17h2M11 17h2M15 17h2" />
    </Icon>
  );
}

/** A telescope tripod: Mount is the stable physical base and its controls. */
function MountIcon() {
  return (
    <Icon>
      <circle cx="12" cy="7" r="2.5" />
      <g opacity="0">
      <path d="M12 3l-1.8 1.8M12 3l1.8 1.8M12 21l-1.8-1.8M12 21l1.8-1.8M3 12l1.8-1.8M3 12l1.8 1.8M21 12l-1.8-1.8M21 12l-1.8 1.8" />
      </g>
      <path d="M12 9.5 5 21M12 9.5 19 21M12 9.5 12 21M4 21h4M16 21h4" />
    </Icon>
  );
}

/** Polaris above an equatorial axis and circular sweep: a direct visual cue
 * for aligning the mount to the celestial pole. */
function PolarAlignIcon() {
  return (
    <Icon>
      <g opacity="0">
      <path d="M5 19 19 5" />
      <path d="M6 16a7.5 7.5 0 0 0 10.5 1.5" />
      <path d="m16 2 .9 2.1L19 5l-2.1.9L16 8l-.9-2.1L13 5l2.1-.9L16 2Z" fill="currentColor" stroke="none" />
      </g>
      <circle cx="12" cy="14" r="7" />
      <path d="M12 14 17 5M17 5l-1.8 1M17 5l.2 2" />
      <path d="m17 2 .9 2.1L20 5l-2.1.9L17 8l-.9-2.1L14 5l2.1-.9L17 2Z" fill="currentColor" stroke="none" />
    </Icon>
  );
}

/** An aperture mid-exposure: Capture is what the scope is doing right now —
 * this tab was `LiveTab` and this is the same glyph, kept as the screen grew
 * into the all-around capture control centre (route stays `/live`; only the
 * label changed). */
function CaptureIcon() {
  return (
    <Icon>
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="3" fill="currentColor" stroke="none" />
    </Icon>
  );
}

/** Three clean image layers: incoming frames accumulating in a live stack.
 * The previous three intersecting rectangles produced a dense knot of
 * strokes at the 24px mobile size and looked like a broken glyph. */
function LiveStackingIcon() {
  return (
    <Icon>
      <path d="m4 7 8-4 8 4-8 4-8-4Z" />
      <path d="m4 12 8 4 8-4" />
      <path d="m4 17 8 4 8-4" />
    </Icon>
  );
}

function StreamingIcon() {
  return (
    <Icon>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="m10 9 5 3-5 3V9Z" />
      <path d="M7 22h10" />
    </Icon>
  );
}

/** A storage box for captures and completed observing sessions. */
function ArchiveIcon() {
  return (
    <Icon>
      <path d="M4 7h16v13H4zM3 3h18v4H3z" />
      <path d="M9 11h6" />
    </Icon>
  );
}

/** A small host monitor: system load, temperature, memory and storage. */
function SystemIcon() {
  return (
    <Icon>
      <rect x="3" y="4" width="18" height="14" rx="2" />
      <path d="m6 13 3-3 3 2 4-5 2 2M9 21h6M12 18v3" />
    </Icon>
  );
}

/** The conventional gear used for application configuration. */
function SettingsIcon() {
  return (
    <Settings
      aria-hidden="true"
      className="size-6 shrink-0 md:size-5"
      strokeWidth={1.5}
    />
  );
}

/** A circled question mark: the conventional entry point for guidance. */
function HelpIcon() {
  return (
    <Icon>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.7 9a2.5 2.5 0 1 1 3.7 2.2c-.9.5-1.4 1-1.4 2.1M12 17h.01" />
    </Icon>
  );
}

const TABS = [
  { to: "/dashboard", label: "Observe", Glyph: DashboardIcon },
  { to: "/planning", label: "Plan", Glyph: PlanningIcon },
  { to: "/scheduling", label: "Execute", Glyph: SchedulingIcon },
  { to: "/calendar", label: "Calendar", Glyph: CalendarOverviewIcon },
  { to: "/mount", label: "Control", Glyph: MountIcon },
  { to: "/polar-align", label: "Polar align", Glyph: PolarAlignIcon },
  { to: "/live", label: "Capture", Glyph: CaptureIcon },
  { to: "/live-stacking", label: "Live stack", Glyph: LiveStackingIcon },
  { to: "/streaming", label: "Stream", Glyph: StreamingIcon },
  { to: "/archive", label: "Library", Glyph: ArchiveIcon },
  { to: "/system", label: "System", Glyph: SystemIcon },
  { to: "/settings", label: "Settings", Glyph: SettingsIcon },
  { to: "/help", label: "Help", Glyph: HelpIcon },
] as const;

type Tab = (typeof TABS)[number];

const PRIMARY_TABS = [TABS[0], TABS[1], TABS[4], TABS[9], TABS[10]] as const;

const WORKFLOW_GROUPS = [
  { paths: ["/dashboard"], tabs: [TABS[0]] },
  { paths: ["/planning", "/scheduling", "/calendar"], tabs: [TABS[1], TABS[2], TABS[3]] },
  { paths: ["/mount", "/polar-align", "/live", "/live-stacking", "/streaming"], tabs: [TABS[4], TABS[5], TABS[6], TABS[7], TABS[8]] },
  { paths: ["/archive"], tabs: [TABS[9]] },
  { paths: ["/system", "/settings", "/help"], tabs: [TABS[10], TABS[11], TABS[12]] },
] as const;

function TabLink({
  tab,
  label,
  mobile = false,
  iconOnly = false,
  activePaths,
}: {
  tab: Tab;
  label?: string;
  mobile?: boolean;
  iconOnly?: boolean;
  activePaths?: readonly string[];
}) {
  const { pathname } = useLocation();
  const workflowActive = activePaths?.includes(pathname) ?? false;
  const active = pathname === tab.to || workflowActive;
  const displayLabel = label ?? tab.label;
  const language = useLanguage();

  return (
    <Link
      to={tab.to}
      title={mobile ? undefined : translate(displayLabel, language)}
      aria-current={active ? "page" : undefined}
      className={`ssc-nav-link flex items-center justify-center gap-2 text-sm font-medium ${
        mobile ? "ssc-mobile-tab-item min-h-11 min-w-0 flex-col gap-0.5 px-1 py-1.5 text-[11px] leading-tight" : "min-h-11 rounded-md px-2 py-2 xl:px-3"
      } ${active ? "bg-accent-muted text-accent" : "text-fg-muted hover:text-fg"}`}
    >
      <tab.Glyph />
      <span className={mobile ? "max-w-full truncate" : iconOnly ? "sr-only" : undefined}>{displayLabel}</span>
    </Link>
  );
}

function MobileTabNav({ docked = false }: { docked?: boolean }) {
  return (
    <nav
      aria-label="Main"
      data-docked={docked ? "true" : undefined}
      className={`ssc-mobile-tabbar ${docked ? "relative mx-2 my-2 shrink-0" : "fixed inset-x-2 bottom-[env(safe-area-inset-bottom)]"} z-10 grid h-[3.5rem] grid-cols-5 items-stretch`}
    >
      {PRIMARY_TABS.map((tab) => (
        <TabLink
          key={tab.to}
          tab={tab}
          label={tab.to === "/mount" ? "Telescope" : undefined}
          mobile
          activePaths={WORKFLOW_GROUPS.find((group) => group.tabs[0] === tab)?.paths}
        />
      ))}
    </nav>
  );
}

/**
 * Desktop keeps the complete observing workflow in one concise row. Below
 * 768px, five equal-width destinations stay visible with 44px touch targets.
 * Long translated labels truncate within their destination.
 */
export function TabNav({ docked = false }: { docked?: boolean }) {
  const isMobile = useMediaQuery(BELOW_DESKTOP_NAV_QUERY);

  if (isMobile) return <MobileTabNav docked={docked} />;

  return (
    <nav aria-label="Main" className="flex flex-nowrap">
      {PRIMARY_TABS.map((tab) => (
        <TabLink
          key={tab.to}
          tab={tab}
          label={tab.to === "/mount" ? "Telescope" : undefined}
          activePaths={WORKFLOW_GROUPS.find((group) => group.tabs[0] === tab)?.paths}
        />
      ))}
    </nav>
  );
}

/** Contextual destinations keep the primary navigation to five stable mental
 * buckets while every existing screen remains one tap away. */
export function WorkflowSubnav({ compact = false, docked = false }: { compact?: boolean; docked?: boolean }) {
  const { pathname } = useLocation();
  const scrollRef = useRef<HTMLElement>(null);
  const group = WORKFLOW_GROUPS.find((candidate) => candidate.paths.some((path) => path === pathname));
  useLayoutEffect(() => {
    if (!compact || scrollRef.current === null) return;
    const viewport = scrollRef.current;
    const revealActive = () => {
      const active = viewport.querySelector<HTMLElement>('[aria-current="page"]');
      if (active === null) return;
      if (active.offsetLeft < viewport.scrollLeft) viewport.scrollLeft = active.offsetLeft;
      else if (active.offsetLeft + active.offsetWidth > viewport.scrollLeft + viewport.clientWidth) {
        viewport.scrollLeft = active.offsetLeft + active.offsetWidth - viewport.clientWidth;
      }
    };
    revealActive();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(revealActive);
    observer?.observe(viewport);
    viewport.querySelectorAll("a").forEach((link) => observer?.observe(link));
    window.addEventListener("resize", revealActive);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", revealActive);
    };
  }, [compact, pathname]);
  if (group === undefined || group.tabs.length < 2) return null;

  const nav = (
    <nav
      ref={scrollRef}
      aria-label="Current workflow"
      className={compact
        ? `${docked ? "relative shrink-0" : "fixed inset-x-0 bottom-[calc(5.5rem+env(safe-area-inset-bottom))]"} z-10 flex h-[2.75rem] min-w-0 items-center gap-1 overflow-x-auto overscroll-x-contain border-t border-border bg-surface/90 px-2 backdrop-blur-md [scrollbar-width:none] [&::-webkit-scrollbar]:hidden`
        : "flex min-h-11 items-center gap-1 overflow-x-auto border-b border-border bg-surface/55 px-2 sm:px-4"}
    >
      {group.tabs.map((tab) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          className={({ isActive }) => `shrink-0 rounded-md ${compact ? "px-2" : "px-3"} py-2 text-sm font-medium ${isActive ? "bg-accent-muted text-accent" : "text-fg-muted hover:text-fg"}`}
        >
          <LocalizedText value={tab.label} />
        </NavLink>
      ))}
    </nav>
  );

  if (!compact || docked) return nav;

  return (
    <>
      {nav}
      <div aria-hidden="true" data-mobile-workflow-spacer className="h-[2.75rem] shrink-0" />
    </>
  );
}

/** Settings and Help are application chrome on desktop, not observing
 * workflow steps. Shell places these compact actions beside Forecast and
 * Fullscreen; mobile keeps them inside the System workflow. */
export function DesktopUtilityNav() {
  return (
    <nav aria-label="Settings and help" className="hidden items-center lg:flex">
      {TABS.slice(-2).map((tab) => <TabLink key={tab.to} tab={tab} iconOnly />)}
    </nav>
  );
}
