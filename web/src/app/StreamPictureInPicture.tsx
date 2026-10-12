// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { recoveredImageSrc } from "../ws/imageRecovery";
import { useConnectionStore } from "../store/connectionStore";
import { useRef, useState } from "react";
import { MoveDiagonal2, PictureInPicture2, X } from "lucide-react";
import { useLocation } from "react-router-dom";
import { useLiveStackRtspQuery } from "../api/capture";
import { useStreamPipStore } from "../store/streamPipStore";

function streamSourceLabel(source: "wide_camera" | "live_stack" | null | undefined): string {
  if (source === "live_stack") return "Live stack";
  if (source === "wide_camera") return "Seestar S30 Wide Camera";
  return "Stream";
}

const DEFAULT_WIDTH_PX = 352;
const MIN_WIDTH_PX = 180;
const MAX_WIDTH_PX = 960;
const KEYBOARD_RESIZE_STEP_PX = 32;
const PORTRAIT_WIDTH_PER_HEIGHT = 9 / 16;

function clampedWidth(width: number): number {
  const viewportLimit = Math.max(MIN_WIDTH_PX, window.innerWidth - 24);
  return Math.round(Math.min(Math.max(width, MIN_WIDTH_PX), Math.min(MAX_WIDTH_PX, viewportLimit)));
}

function callPointerCapture(
  element: HTMLButtonElement,
  method: "setPointerCapture" | "releasePointerCapture",
  pointerId: number,
): void {
  const capture = Reflect.get(element, method);
  if (typeof capture === "function") Reflect.apply(capture, element, [pointerId]);
}

/** Carries the Stream screen's existing encoded preview between routes. This
 * never starts a camera or stream: without an active encoded frame it renders
 * nothing, and its query is disabled until the user opts in. */
export function StreamPictureInPicture() {
  const generation = useConnectionStore((state) => state.generation);
  const { pathname } = useLocation();
  const enabled = useStreamPipStore((state) => state.enabled);
  const setEnabled = useStreamPipStore((state) => state.setEnabled);
  const status = useLiveStackRtspQuery(enabled && pathname !== "/streaming");
  const stream = status.data;
  const containerRef = useRef<HTMLElement>(null);
  const resizeStart = useRef<{ pointerId: number; x: number; y: number; width: number } | null>(null);
  const [widthPx, setWidthPx] = useState(DEFAULT_WIDTH_PX);

  if (
    !enabled
    || pathname === "/streaming"
    || stream?.active !== true
    || stream.encoded_version == null
  ) return null;

  const source = streamSourceLabel(stream.source);

  return (
    <aside
      ref={containerRef}
      aria-label={`Stream picture in picture: ${source}`}
      className="fixed right-3 bottom-[calc(6.5rem+env(safe-area-inset-bottom))] z-40 overflow-hidden rounded-lg border border-border-strong bg-surface-raised shadow-2xl lg:bottom-12"
      style={{
        width: `${String(widthPx)}px`,
        maxWidth: "min(calc(100vw - 1.5rem), calc((100dvh - 10rem) * 0.5625))",
      }}
    >
      <div className="flex min-h-11 items-center justify-between gap-2 border-b border-border px-3 py-1.5">
        <span className="inline-flex min-w-0 items-center gap-2 text-xs font-medium text-fg">
          <PictureInPicture2 aria-hidden="true" className="size-4 shrink-0" strokeWidth={1.8} />
          <span className="truncate">{source}</span>
        </span>
        <button
          type="button"
          aria-label="Close stream picture in picture"
          title="Close stream picture in picture"
          className="inline-flex size-9 shrink-0 items-center justify-center rounded-md text-fg-muted hover:bg-surface hover:text-fg"
          onClick={() => { setEnabled(false); }}
        >
          <X aria-hidden="true" className="size-5" strokeWidth={1.8} />
        </button>
      </div>
      <div aria-label="Portrait stream viewport" className="aspect-[9/16] w-full overflow-hidden bg-bg">
        <img
          src={recoveredImageSrc(`/api/capture/live-stack/rtsp/preview?v=${encodeURIComponent(stream.encoded_version)}`, generation)}
          alt={`RTSP stream: ${source}`}
          className="block size-full object-cover object-center"
        />
      </div>
      <button
        type="button"
        aria-label="Resize stream picture in picture"
        title="Drag to resize stream picture in picture"
        className="absolute bottom-1 left-1 inline-flex size-9 touch-none cursor-nesw-resize items-center justify-center rounded-md border border-border-strong bg-surface-raised/90 text-fg-muted shadow hover:text-fg"
        onPointerDown={(event) => {
          const measured = containerRef.current?.getBoundingClientRect().width ?? widthPx;
          resizeStart.current = {
            pointerId: event.pointerId,
            x: event.clientX,
            y: event.clientY,
            width: measured > 0 ? measured : widthPx,
          };
          callPointerCapture(event.currentTarget, "setPointerCapture", event.pointerId);
        }}
        onPointerMove={(event) => {
          const start = resizeStart.current;
          if (start?.pointerId !== event.pointerId) return;
          const horizontalDelta = start.x - event.clientX;
          const verticalDelta = (start.y - event.clientY) * PORTRAIT_WIDTH_PER_HEIGHT;
          const delta = Math.abs(horizontalDelta) >= Math.abs(verticalDelta)
            ? horizontalDelta
            : verticalDelta;
          setWidthPx(clampedWidth(start.width + delta));
        }}
        onPointerUp={(event) => {
          if (resizeStart.current?.pointerId !== event.pointerId) return;
          resizeStart.current = null;
          callPointerCapture(event.currentTarget, "releasePointerCapture", event.pointerId);
        }}
        onPointerCancel={() => { resizeStart.current = null; }}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
            event.preventDefault();
            setWidthPx((width) => clampedWidth(width + KEYBOARD_RESIZE_STEP_PX));
          } else if (event.key === "ArrowRight" || event.key === "ArrowDown") {
            event.preventDefault();
            setWidthPx((width) => clampedWidth(width - KEYBOARD_RESIZE_STEP_PX));
          } else if (event.key === "Home") {
            event.preventDefault();
            setWidthPx(clampedWidth(DEFAULT_WIDTH_PX));
          }
        }}
      >
        <MoveDiagonal2 aria-hidden="true" className="size-5" strokeWidth={1.8} />
      </button>
    </aside>
  );
}
