// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Children, createContext, isValidElement, useContext, useId, useRef, type DragEvent, type HTMLAttributes, type PointerEvent, type ReactElement, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useLayoutBoard, useLayoutRoute } from "../layout/LayoutContext";
import { useLayoutStore, type LayoutPanel } from "../layout/layoutStore";
import { useCollapsed } from "./useCollapsed";

const CardCollapseContext = createContext<{ label: string; collapse: () => void } | null>(null);

/** A visual application card. Inside Shell every titled card automatically
 * participates in the persistent layout editor; outside Shell it retains the
 * former opt-in folding behaviour used by isolated screens and tests. */
export function Card({
  children,
  className = "",
  padded = true,
  title,
  collapseKey,
  layoutId,
  layoutLabel,
  defaultAfter,
  resizableHeight = false,
  defaultCollapsed = false,
  ...rest
}: Omit<HTMLAttributes<HTMLDivElement>, "title"> & {
  children: ReactNode;
  padded?: boolean;
  title?: ReactNode;
  collapseKey?: string;
  layoutId?: string;
  /** Name used by layout-editor controls when the card has no visible title
   * (for example the Planning skymap). */
  layoutLabel?: string;
  /** Follow this card's saved placement until this card is explicitly moved. */
  defaultAfter?: string;
  /** Show a per-card height handle while the layout editor is unlocked. */
  resizableHeight?: boolean;
  defaultCollapsed?: boolean;
}) {
  const instanceId = useId();
  const route = useLayoutRoute();
  const board = useLayoutBoard();
  const unlocked = useLayoutStore((state) => state.unlocked);
  const derivedTitle = title ?? findCardTitle(children);
  const displayTitle = derivedTitle;
  const propTitle = title;
  const label = layoutLabel ?? nodeText(derivedTitle);
  const cardId = layoutId ?? collapseKey ?? rest.id ?? (slug(label) || `card-${instanceId}`);
  const customizable = route.enabled && cardId !== "";
  const placement = useLayoutStore((state) => state.saved.tabs[route.tab]?.cards[cardId]);
  const anchorPlacement = useLayoutStore((state) =>
    defaultAfter !== undefined && placement?.panel === undefined && placement?.order === undefined
      ? state.saved.tabs[route.tab]?.cards[defaultAfter] : undefined);
  // CSS order only accepts integers; reserve odd slots for newly anchored cards.
  const defaultOrder = anchorPlacement?.order === undefined ? undefined : anchorPlacement.order * 2 + 1;
  const tabCustomized = useLayoutStore((state) => Object.keys(state.saved.tabs[route.tab]?.cards ?? {}).length > 0);
  const setCollapsed = useLayoutStore((state) => state.setCollapsed);
  const placeCards = useLayoutStore((state) => state.placeCards);
  const setCardHeight = useLayoutStore((state) => state.setCardHeight);
  const collapsed = placement?.collapsed ?? readLegacyCollapsed(collapseKey, defaultCollapsed);
  const destination = placement?.panel ?? anchorPlacement?.panel ?? board.defaultPanel;
  const target = destination === undefined ? undefined : board.panels?.[destination];
  const foldable = title !== undefined && collapseKey !== undefined;
  const pointerDrag = useRef<{ pointerId: number; startX: number; startY: number } | null>(null);
  const height = resizableHeight && !collapsed ? placement?.height : undefined;
  const measuredHeight = (element: HTMLElement | null): number => {
    const measured = element?.closest<HTMLElement>("[data-layout-card-id]")?.getBoundingClientRect().height ?? 0;
    return measured > 0 ? measured : 300;
  };

  function beginHeightResize(event: PointerEvent<HTMLButtonElement>): void {
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = measuredHeight(event.currentTarget);
    const move = (moveEvent: globalThis.PointerEvent) => {
      setCardHeight(route.tab, cardId, Math.max(180, Math.min(1200, Math.round(startHeight + moveEvent.clientY - startY))));
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
    window.addEventListener("pointercancel", stop, { once: true });
  }

  function orderedCards(container: HTMLElement, panel: LayoutPanel | undefined, exclude = cardId): string[] {
    return Array.from(container.querySelectorAll<HTMLElement>("[data-layout-card-id]"))
      .filter((element) => element.dataset.layoutPanel === panel)
      .sort((a, b) => Number(a.style.order || 0) - Number(b.style.order || 0))
      .map((element) => element.dataset.layoutCardId)
      .filter((id): id is string => id !== undefined && id !== exclude);
  }

  function moveRelative(direction: -1 | 1, element: HTMLElement, crossPanelBoundary: boolean): void {
    const container = element.closest<HTMLElement>("[data-layout-panel-target]");
    if (container === null) return;
    const cards = orderedCards(container, destination, "");
    const current = cards.indexOf(cardId);
    const next = current + direction;
    if (current >= 0 && next >= 0 && next < cards.length) {
      const currentCard = cards[current];
      const nextCard = cards[next];
      if (currentCard === undefined || nextCard === undefined) return;
      cards[current] = nextCard;
      cards[next] = currentCard;
      placeCards(route.tab, destination, cards);
      return;
    }
    if (!crossPanelBoundary || destination === undefined || board.panels === undefined) return;
    const available = (["primary", "secondary", "detail"] as const).filter((panel) => board.panels?.[panel] !== undefined);
    const panelIndex = available.indexOf(destination);
    const nextPanel = available[panelIndex + direction];
    if (nextPanel === undefined) return;
    const nextContainer = board.panels[nextPanel];
    if (nextContainer === undefined) return;
    const targetCards = orderedCards(nextContainer, nextPanel, cardId);
    if (direction > 0) targetCards.unshift(cardId);
    else targetCards.push(cardId);
    placeCards(route.tab, nextPanel, targetCards);
  }

  function moveToPanel(panel: LayoutPanel): void {
    const container = board.panels?.[panel];
    const cards = container === undefined ? [] : orderedCards(container, panel);
    cards.push(cardId);
    placeCards(route.tab, panel, cards);
  }

  function dropBefore(event: DragEvent<HTMLDivElement>): void {
    if (!unlocked) return;
    event.preventDefault();
    event.stopPropagation();
    const dragged = event.dataTransfer.getData("application/x-ssc-layout-card");
    if (dragged === "" || dragged === cardId) return;
    const container = event.currentTarget.closest<HTMLElement>("[data-layout-panel-target]");
    if (container === null) return;
    const cards = orderedCards(container, destination, dragged);
    const before = cards.indexOf(cardId);
    cards.splice(before < 0 ? cards.length : before, 0, dragged);
    placeCards(route.tab, destination, cards);
  }

  function beginPointerDrag(event: PointerEvent<HTMLSpanElement>): void {
    if (event.button !== 0) return;
    event.preventDefault();
    pointerDrag.current = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY };
    if (typeof event.currentTarget.setPointerCapture === "function") event.currentTarget.setPointerCapture(event.pointerId);
  }

  function finishPointerDrag(event: PointerEvent<HTMLSpanElement>): void {
    const drag = pointerDrag.current;
    if (drag?.pointerId !== event.pointerId) return;
    pointerDrag.current = null;
    if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 4) return;
    const hit = document.elementFromPoint(event.clientX, event.clientY) as HTMLElement | null;
    const panelElement = hit?.closest<HTMLElement>("[data-layout-panel-target]") ?? null;
    const panel = panelElement?.dataset.layoutPanelTarget;
    if (panelElement === null || (panel !== "primary" && panel !== "secondary" && panel !== "detail")) return;
    const cards = orderedCards(panelElement, panel, cardId);
    const targetCard = hit?.closest<HTMLElement>("[data-layout-card-id]") ?? null;
    const before = targetCard === null ? -1 : cards.indexOf(targetCard.dataset.layoutCardId ?? "");
    cards.splice(before < 0 ? cards.length : before, 0, cardId);
    placeCards(route.tab, panel, cards);
  }

  const card = (
    <div
      {...rest}
      className={`ssc-card relative rounded-lg border border-border bg-surface shadow-[0_1px_0_var(--color-shadow)] ${padded ? "p-4" : ""} ${unlocked && customizable ? "ring-1 ring-accent/60" : ""} ${collapsed ? "h-auto min-h-0 self-start" : ""} ${height === undefined ? "" : "overflow-y-auto"} ${className}`}
      data-layout-card-id={customizable ? cardId : undefined}
      data-layout-panel={destination}
      style={{ ...rest.style, ...(height === undefined ? {} : { height, flex: "none" }), ...(customizable && (placement?.order !== undefined || defaultOrder !== undefined || tabCustomized) ? { order: placement?.order === undefined ? defaultOrder ?? 2000 : placement.order * 2 } : {}) }}
      onDragOver={unlocked && customizable ? (event) => { event.preventDefault(); } : rest.onDragOver}
      onDrop={unlocked && customizable ? dropBefore : rest.onDrop}
    >
      {unlocked && customizable && (
        <div className="mb-2 flex flex-wrap min-h-8 items-center justify-between gap-2 rounded-md border border-accent/50 bg-surface-raised px-2 text-xs text-fg-muted">
          <span
            className="touch-none cursor-grab select-none font-medium text-fg active:cursor-grabbing"
            title="Drag this card"
            onPointerDown={beginPointerDrag}
            onPointerMove={(event) => { if (pointerDrag.current?.pointerId === event.pointerId) event.preventDefault(); }}
            onPointerUp={finishPointerDrag}
            onPointerCancel={() => { pointerDrag.current = null; }}
          >
            ⠿ {label || cardId}
          </span>
          <span className="flex items-center gap-1">
            {derivedTitle !== undefined && <button type="button" className="min-h-11 min-w-11 rounded border border-border" aria-label={`Collapse ${label}`} onClick={() => { setCollapsed(route.tab, cardId, true); }}>−</button>}
            <button type="button" className="min-h-11 min-w-11 rounded border border-border" aria-label={`Move ${label || cardId} up`} onClick={(event) => { const owner = event.currentTarget.closest<HTMLElement>("[data-layout-card-id]"); if (owner !== null) moveRelative(-1, owner, isSingleColumnLayout()); }}>↑</button>
            <button type="button" className="min-h-11 min-w-11 rounded border border-border" aria-label={`Move ${label || cardId} down`} onClick={(event) => { const owner = event.currentTarget.closest<HTMLElement>("[data-layout-card-id]"); if (owner !== null) moveRelative(1, owner, isSingleColumnLayout()); }}>↓</button>
            {board.panels !== undefined && (
              <span className="flex flex-wrap items-center gap-1" aria-label="Move to panel">
                {(["primary", "secondary", "detail"] as const).map((panel) => board.panels?.[panel] === undefined ? null : (
                  <button key={panel} type="button" className={`min-h-11 min-w-11 rounded border ${destination === panel ? "border-accent text-accent" : "border-border"}`} aria-label={`Move ${label || cardId} to ${panel} panel`} onClick={() => { moveToPanel(panel); }}>
                    {panel === "primary" ? "←" : panel === "secondary" ? "●" : "→"}
                  </button>
                ))}
              </span>
            )}
          </span>
        </div>
      )}
      {customizable && derivedTitle !== undefined && collapsed ? (
        <button type="button" className="ssc-card-disclosure flex min-h-11 w-full items-center justify-between gap-2 text-left" aria-expanded="false" onClick={() => { setCollapsed(route.tab, cardId, false); }}>
          <span className="ssc-card-title text-base font-semibold text-fg">{displayTitle}</span>
          <span aria-hidden className="text-xs text-fg-muted">▸</span>
        </button>
      ) : customizable ? (
        <CardCollapseContext.Provider value={!unlocked && derivedTitle !== undefined ? { label, collapse: () => { setCollapsed(route.tab, cardId, true); } } : null}>
          {title !== undefined ? <><CardTitle>{propTitle}</CardTitle>{children}</> : children}
        </CardCollapseContext.Provider>
      ) : foldable ? (
        <FoldableBody title={propTitle} collapseKey={collapseKey} defaultCollapsed={defaultCollapsed}>{children}</FoldableBody>
      ) : children}
      {unlocked && customizable && resizableHeight && !collapsed && (
        <button
          type="button"
          aria-label={`Resize ${label || cardId} height${height === undefined ? "" : `, currently ${String(height)} pixels`}`}
          title="Drag to resize; use arrow keys for precise adjustment; double-click to reset"
          className="sticky -bottom-4 z-10 mt-2 block h-4 w-full cursor-row-resize touch-none rounded bg-accent/20 text-center text-[10px] leading-4 text-accent hover:bg-accent/40"
          onPointerDown={beginHeightResize}
          onDoubleClick={() => { setCardHeight(route.tab, cardId, undefined); }}
          onKeyDown={(event) => {
            if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
            event.preventDefault();
            setCardHeight(route.tab, cardId, Math.max(180, Math.min(1200, Math.round((height ?? measuredHeight(event.currentTarget)) + (event.key === "ArrowDown" ? 20 : -20)))));
          }}
        >⋯</button>
      )}
    </div>
  );
  // As soon as a panel/order is customized, render the card directly in its
  // panel target. Real screens group cards in section wrappers; leaving a
  // reordered card inside its original wrapper makes CSS `order` local to
  // that wrapper and is why Night summary and several other cards appeared
  // immovable even though their saved order changed.
  const hasExplicitPanelPlacement = placement?.panel !== undefined || anchorPlacement?.panel !== undefined;
  return target === undefined || !customizable || !hasExplicitPanelPlacement ? card : createPortal(card, target);
}

function isSingleColumnLayout(): boolean {
  return typeof window.matchMedia !== "function" || !window.matchMedia("(min-width: 640px)").matches;
}

function nodeText(node: ReactNode): string {
  if (Array.isArray(node)) return node.map(nodeText).filter(Boolean).join(" ");
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (!isValidElement(node)) return "";
  return nodeText((node as ReactElement<{ children?: ReactNode }>).props.children);
}

function findCardTitle(node: ReactNode): ReactNode | undefined {
  let found: ReactNode | undefined;
  Children.forEach(node, (child) => {
    if (found !== undefined || !isValidElement(child)) return;
    const element = child as ReactElement<{ children?: ReactNode }>;
    if (element.type === CardTitle || element.type === "h2" || element.type === "h3") found = element.props.children;
    else found = findCardTitle(element.props.children);
  });
  return found;
}

function slug(value: string): string {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function readLegacyCollapsed(key: string | undefined, fallback: boolean): boolean {
  if (key === undefined) return fallback;
  try {
    const raw = window.localStorage.getItem(`ssc.collapsed.${key}`);
    return raw === null ? fallback : raw === "1";
  } catch {
    return fallback;
  }
}

function FoldableBody({ title, collapseKey, defaultCollapsed, children }: { title: ReactNode; collapseKey: string; defaultCollapsed: boolean; children: ReactNode }) {
  const [collapsed, toggle] = useCollapsed(collapseKey, defaultCollapsed);
  return (
    <>
      <button type="button" onClick={toggle} aria-expanded={!collapsed} className={`ssc-card-disclosure flex min-h-11 w-full items-center justify-between gap-2 text-left ${collapsed ? "" : "mb-3"}`}>
        <span className="ssc-card-title text-base font-semibold text-fg">{title}</span>
        <span aria-hidden className="text-xs text-fg-muted">{collapsed ? "▸" : "▾"}</span>
      </button>
      {!collapsed && children}
    </>
  );
}

export function CardTitle({ children }: { children: ReactNode }) {
  const control = useContext(CardCollapseContext);
  const title = children;
  if (control !== null && control.label === nodeText(children)) {
    return (
      <h3 className="mb-3 ssc-card-title text-base font-semibold text-fg">
        <button type="button" onClick={control.collapse} aria-label={`Collapse ${control.label}`} className="ssc-card-disclosure flex min-h-11 w-full items-center justify-between gap-2 text-left">
          <span>{title}</span>
          <span aria-hidden className="text-xs">▾</span>
        </button>
      </h3>
    );
  }
  return <h3 className="mb-3 ssc-card-title text-base font-semibold text-fg">{title}</h3>;
}
