// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useCallback, useEffect, useRef, useState, type DragEvent, type PointerEvent, type ReactNode } from "react";
import { LayoutPanelProvider, useLayoutRoute } from "../layout/LayoutContext";
import { useLayoutStore, type LayoutPanel } from "../layout/layoutStore";

/**
 * The one shape every tab's content is laid out in: desktop three-pane →
 * tablet two-pane → phone single column (frontend-react.md — "do not fork a
 * component per breakpoint"). `detail` collapses out at `sm` and reappears
 * inline in document order below `secondary` at `sm`–`lg`, and stacks last on
 * phone rather than disappearing, so nothing here is desktop-only content.
 *
 * `fill` makes the panes claim the full height below the header — for a tab
 * whose centre pane is a map rather than a document. It applies only from `sm`
 * up, where the panes sit side by side: on phone they stack into one scrolling
 * column, and forcing viewport height there would squeeze every pane below the
 * map off-screen instead of letting the column scroll.
 *
 * Under `fill` each pane scrolls on its own. Without that, a sidebar taller
 * than the window stretched the grid row, the map grew to match it, and the
 * map's centre — the target, the crosshair — ended up below the fold. The
 * shell bounds itself to the viewport from `sm` up (`Shell.tsx`) so that a
 * `min-h-0` cell here really is clipped rather than merely allowed to be.
 *
 * Between `sm` and `lg` the layout is two columns, so `detail` wraps onto a
 * second row and would otherwise take half the height from the map. The
 * explicit `1fr auto` rows give the free space to the first row and let
 * `detail` size to its content beneath it.
 */
export function PaneLayout({
  primary,
  secondary,
  detail,
  fill = false,
  compactSecondary = false,
}: {
  /** The narrow left pane. Optional: a tab whose content is one working
   * surface plus a settings rail (Mount) has no third thing to put on the
   * left, and rendering an empty one would spend 272px and a collapse
   * toggle on nothing. Omitting it drops the column from the template
   * rather than leaving it blank. */
  primary?: ReactNode;
  secondary?: ReactNode;
  detail?: ReactNode;
  fill?: boolean;
  compactSecondary?: boolean;
}) {
  const route = useLayoutRoute();
  const unlocked = useLayoutStore((state) => state.unlocked);
  const saved = useLayoutStore((state) => state.saved.tabs[route.tab]);
  const placeCards = useLayoutStore((state) => state.placeCards);
  const setPanelWidth = useLayoutStore((state) => state.setPanelWidth);
  const setPanelHeight = useLayoutStore((state) => state.setPanelHeight);
  const setPanelCollapsed = useLayoutStore((state) => state.setPanelCollapsed);
  const paneRootRef = useRef<HTMLDivElement>(null);
  const primaryCellRef = useRef<HTMLDivElement>(null);
  const secondaryCellRef = useRef<HTMLDivElement>(null);
  const detailCellRef = useRef<HTMLDivElement>(null);
  const [toggleCenters, setToggleCenters] = useState<{ left: number | null; right: number | null }>({ left: null, right: null });
  const [panels, setPanels] = useState<Partial<Record<LayoutPanel, HTMLDivElement>>>({});
  const primaryRef = useCallback((node: HTMLDivElement | null) => {
    setPanels((current) => current.primary === node ? current : { ...current, primary: node ?? undefined });
  }, []);
  const secondaryRef = useCallback((node: HTMLDivElement | null) => {
    setPanels((current) => current.secondary === node ? current : { ...current, secondary: node ?? undefined });
  }, []);
  const detailRef = useCallback((node: HTMLDivElement | null) => {
    setPanels((current) => current.detail === node ? current : { ...current, detail: node ?? undefined });
  }, []);
  useEffect(() => {
    const container = panels.secondary;
    if (!compactSecondary || unlocked || container === undefined) return;
    const cards = new Set<HTMLElement>();
    const measure = (card: HTMLElement) => {
      // Theme spacing changes in compact mode; measure the actual gap.
      const style = getComputedStyle(container);
      const gap = Number.parseFloat(style.rowGap) || 0;
      const track = Number.parseFloat(style.gridAutoRows) || 8;
      const rows = Math.max(1, Math.ceil((card.getBoundingClientRect().height + gap) / (track + gap)));
      card.style.setProperty("--ssc-equipment-rows", String(rows));
    };
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) measure(entry.target as HTMLElement);
    });
    const discover = () => {
      for (const card of Array.from(container.querySelectorAll<HTMLElement>(":scope > [data-layout-card-id]"))) {
        if (!cards.has(card)) { cards.add(card); observer.observe(card); }
        measure(card);
      }
    };
    const mutations = new MutationObserver(discover);
    mutations.observe(container, { childList: true });
    discover();
    return () => {
      mutations.disconnect(); observer.disconnect();
      for (const card of cards) card.style.removeProperty("--ssc-equipment-rows");
    };
  }, [compactSecondary, unlocked, panels.secondary]);
  const primaryCollapsed = !unlocked && (saved?.primaryCollapsed ?? false);
  const detailCollapsed = !unlocked && (saved?.detailCollapsed ?? false);
  const togglePrimary = () => { setPanelCollapsed(route.tab, "primary", !primaryCollapsed); };
  const toggleDetail = () => { setPanelCollapsed(route.tab, "detail", !detailCollapsed); };
  const grid = fill
    ? "sm:min-h-0 sm:flex-1 sm:grid-rows-[minmax(0,1fr)_auto] lg:grid-rows-[minmax(0,1fr)]"
    : "";
  const cell = fill ? "min-w-0 sm:min-h-0 sm:overflow-y-auto" : "min-w-0";
  // A collapsed side pane keeps a narrow strip rather than vanishing: the
  // reopen control has to live somewhere, and a column that disappears
  // entirely leaves nothing to click. The template only changes from `sm` up,
  // where the panes are actually side by side — on phone everything is one
  // scrolling column and a "column" is just a stacked section, so collapsing
  // there would hide content with no width saved.
  const hasPrimary = primary !== undefined || unlocked || Object.values(saved?.cards ?? {}).some((card) => card.panel === "primary");
  const hasDetail = detail !== undefined || unlocked || Object.values(saved?.cards ?? {}).some((card) => card.panel === "detail");
  const primaryWidth = saved?.primaryWidth ?? 272;
  const detailWidth = saved?.detailWidth ?? 354;
  const sideCols = hasPrimary
    ? `${primaryCollapsed ? "minmax(0,0px)" : `minmax(0,${String(primaryWidth)}px)`} 1fr`
    : "1fr";
  const wideCols = [
    ...(hasPrimary ? [primaryCollapsed ? "minmax(0,0px)" : `minmax(0,${String(primaryWidth)}px)`] : []),
    "1fr",
    ...(hasDetail ? [detailCollapsed ? "minmax(0,0px)" : `minmax(0,${String(detailWidth)}px)`] : []),
  ].join(" ");

  useEffect(() => {
    function updateToggleCenters(): void {
      const root = paneRootRef.current;
      const secondaryCell = secondaryCellRef.current;
      if (root === null || secondaryCell === null) return;
      const rootRect = root.getBoundingClientRect();
      const secondaryRect = secondaryCell.getBoundingClientRect();
      const primaryRect = primaryCellRef.current?.getBoundingClientRect();
      const detailRect = detailCellRef.current?.getBoundingClientRect();
      const left = primaryRect === undefined ? null : (primaryRect.right + secondaryRect.left) / 2;
      const detailSharesRow = detailRect !== undefined
        && Math.min(secondaryRect.bottom, detailRect.bottom) > Math.max(secondaryRect.top, detailRect.top);
      const right = detailRect === undefined
        ? null
        : detailSharesRow
          ? (secondaryRect.right + detailRect.left) / 2
          : (rootRect.right + globalThis.innerWidth) / 2;
      setToggleCenters((current) => current.left === left && current.right === right ? current : { left, right });
    }

    updateToggleCenters();
    globalThis.addEventListener("resize", updateToggleCenters);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(updateToggleCenters);
    for (const element of [paneRootRef.current, primaryCellRef.current, secondaryCellRef.current, detailCellRef.current]) {
      if (element !== null) observer?.observe(element);
    }
    return () => {
      globalThis.removeEventListener("resize", updateToggleCenters);
      observer?.disconnect();
    };
  }, [detailCollapsed, detailWidth, primaryCollapsed, primaryWidth]);

  function dropOnPanel(panel: LayoutPanel, event: DragEvent<HTMLDivElement>): void {
    if (!unlocked) return;
    event.preventDefault();
    const dragged = event.dataTransfer.getData("application/x-ssc-layout-card");
    if (dragged === "") return;
    const cards = Array.from(event.currentTarget.querySelectorAll<HTMLElement>("[data-layout-card-id]"))
      .sort((a, b) => Number(a.style.order || 0) - Number(b.style.order || 0))
      .map((element) => element.dataset.layoutCardId)
      .filter((id): id is string => id !== undefined && id !== dragged);
    cards.push(dragged);
    placeCards(route.tab, panel, cards);
  }

  function resize(panel: "primary" | "detail", event: PointerEvent<HTMLElement>): void {
    event.currentTarget.setPointerCapture(event.pointerId);
    const gridElement = event.currentTarget.closest<HTMLElement>("[data-panes]");
    if (gridElement === null) return;
    const bounds = gridElement.getBoundingClientRect();
    const move = (moveEvent: globalThis.PointerEvent) => {
      const raw = panel === "primary" ? moveEvent.clientX - bounds.left : bounds.right - moveEvent.clientX;
      setPanelWidth(route.tab, panel, Math.max(panel === "primary" ? 220 : 260, Math.min(panel === "primary" ? 480 : 560, raw)));
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  }

  function resizeHeight(panel: LayoutPanel, event: PointerEvent<HTMLElement>): void {
    const target = panels[panel];
    if (target === undefined) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const startY = event.clientY;
    const startHeight = target.getBoundingClientRect().height;
    const move = (moveEvent: globalThis.PointerEvent) => {
      setPanelHeight(route.tab, panel, Math.max(240, Math.min(1200, startHeight + moveEvent.clientY - startY)));
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  }

  function panelContents(panel: LayoutPanel, children: ReactNode, ref: (node: HTMLDivElement | null) => void, panelCollapsed = false) {
    const panelHeight = saved?.panelHeights?.[panel];
    return (
      <div
        ref={ref}
        className={`${compactSecondary && panel === "secondary" && !unlocked ? "grid grid-cols-[repeat(auto-fit,minmax(min(100%,17rem),1fr))] items-start" : "flex flex-col"} min-w-0 gap-3 ${panelCollapsed && !unlocked ? "sm:pointer-events-none sm:invisible" : ""} ${unlocked ? "min-h-24 border border-dashed border-accent/40 p-2" : ""} ${panelHeight === undefined ? (fill ? "h-full min-h-0" : "") : "sm:h-[var(--ssc-panel-height)]"}`}
        style={panelHeight === undefined ? undefined : { ["--ssc-panel-height" as string]: `${String(panelHeight)}px` }}
        data-compact-secondary={compactSecondary && panel === "secondary" ? true : undefined}
        data-layout-panel-target={panel}
        onDragOver={unlocked ? (event) => { event.preventDefault(); } : undefined}
        onDrop={unlocked ? (event) => { dropOnPanel(panel, event); } : undefined}
      >
        <LayoutPanelProvider panel={panel} panels={panels}>
          <div className="contents [&>div:empty]:hidden">{children}</div>
        </LayoutPanelProvider>
      </div>
    );
  }

  function heightHandle(panel: LayoutPanel) {
    if (!unlocked) return null;
    const height = saved?.panelHeights?.[panel];
    return <button type="button" aria-label={`Resize ${panel} panel height${height === undefined ? "" : `, currently ${String(height)} pixels`}`} className="absolute inset-x-0 -bottom-2 z-10 hidden h-3 cursor-row-resize sm:block" onPointerDown={(event) => { resizeHeight(panel, event); }} onKeyDown={(event) => { if (event.key === "ArrowUp" || event.key === "ArrowDown") setPanelHeight(route.tab, panel, Math.max(240, Math.min(1200, (height ?? panels[panel]?.getBoundingClientRect().height ?? 400) + (event.key === "ArrowDown" ? 10 : -10)))); }} />;
  }
  return (
    <div
      ref={paneRootRef}
      className={`relative grid grid-cols-1 gap-4 sm:grid-cols-[var(--ssc-pane-cols-sm)] lg:grid-cols-[var(--ssc-pane-cols-lg)] ${grid}`}
      style={{
        // Inline rather than Tailwind classes: the widths are computed from
        // two booleans, and Tailwind cannot generate a class for a value it
        // never sees in the source.
        ["--ssc-pane-cols-sm" as string]: sideCols,
        ["--ssc-pane-cols-lg" as string]: wideCols,
        ["--ssc-primary-track" as string]: primaryCollapsed ? "0px" : `${String(primaryWidth)}px`,
        ["--ssc-detail-track" as string]: detailCollapsed ? "0px" : `${String(detailWidth)}px`,
      }}
      data-panes="1"
    >
      {hasPrimary && (
        <div ref={primaryCellRef} data-pane-cell="primary" className={`relative ${cell} ${primaryCollapsed ? "sm:overflow-hidden" : ""}`}>
          {panelContents("primary", primary, primaryRef, primaryCollapsed)}
          {!primaryCollapsed && heightHandle("primary")}
          {unlocked && !primaryCollapsed && <button type="button" aria-label={`Resize left panel, currently ${String(primaryWidth)} pixels`} className="absolute inset-y-0 -right-2 z-10 hidden w-3 cursor-col-resize lg:block" onPointerDown={(event) => { resize("primary", event); }} onKeyDown={(event) => { if (event.key === "ArrowLeft" || event.key === "ArrowRight") setPanelWidth(route.tab, "primary", Math.max(220, Math.min(480, primaryWidth + (event.key === "ArrowRight" ? 10 : -10)))); }} />}
        </div>
      )}
      {secondary !== undefined && <div ref={secondaryCellRef} data-pane-cell="secondary" className={`relative ${cell}`}>{panelContents("secondary", secondary, secondaryRef)}{heightHandle("secondary")}</div>}
      {hasDetail && (
        <div
          ref={detailCellRef}
          data-pane-cell="detail"
          className={`relative ${cell} ${detailCollapsed ? "sm:overflow-hidden" : ""} ${
            hasPrimary ? "lg:col-start-3" : "lg:col-start-2"
          }`}
        >
          {panelContents("detail", detail, detailRef, detailCollapsed)}
          {!detailCollapsed && heightHandle("detail")}
          {unlocked && !detailCollapsed && <button type="button" aria-label={`Resize right panel, currently ${String(detailWidth)} pixels`} className="absolute inset-y-0 -left-2 z-10 hidden w-3 cursor-col-resize lg:block" onPointerDown={(event) => { resize("detail", event); }} onKeyDown={(event) => { if (event.key === "ArrowLeft" || event.key === "ArrowRight") setPanelWidth(route.tab, "detail", Math.max(260, Math.min(560, detailWidth + (event.key === "ArrowLeft" ? 10 : -10)))); }} />}
        </div>
      )}
      {hasPrimary && (
        <PaneToggle
          side="left"
          collapsed={primaryCollapsed}
          centerX={toggleCenters.left}
          onToggle={togglePrimary}
          label={primaryCollapsed ? "Show the left panel" : "Hide the left panel"}
        />
      )}
      {hasDetail && (
        <PaneToggle
          side="right"
          collapsed={detailCollapsed}
          centerX={toggleCenters.right}
          onToggle={toggleDetail}
          label={detailCollapsed ? "Show the right panel" : "Hide the right panel"}
        />
      )}
    </div>
  );
}

/** The one control that survives a collapsed pane. Hidden below `sm`, where
 * the layout is a single scrolling column and there is no width to reclaim. */
function PaneToggle({
  side,
  collapsed,
  centerX,
  onToggle,
  label,
}: {
  side: "left" | "right";
  collapsed: boolean;
  centerX: number | null;
  onToggle: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={!collapsed}
      aria-label={label}
      title={label}
      data-pane-toggle={side}
      style={centerX === null ? undefined : { left: `${String(centerX)}px` }}
      className={`fixed top-1/2 z-20 hidden size-8 -translate-x-1/2 -translate-y-1/2 items-center justify-center bg-transparent text-lg text-fg-muted hover:text-accent ${centerX === null ? "invisible" : ""} ${
        side === "left"
          ? "sm:flex"
          : collapsed ? "sm:flex" : "lg:flex"
      }`}
    >
      <span
        aria-hidden
        className={side === "left" ? (collapsed ? "" : "rotate-180") : collapsed ? "rotate-180" : ""}
      >
        ▸
      </span>
    </button>
  );
}
