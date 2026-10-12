// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect, useRef, useState, type ReactNode, type SyntheticEvent } from "react";

import { useConnectionStore } from "../store/connectionStore";

import { recoveredImageSrc } from "../ws/imageRecovery";

/** Pointer-based double taps work on Safari without browser page zoom. */
export function ImagePreview({ src, alt, label, children, onLoad, onError, defaultNative = false }: {
  src: string; alt: string; label: string; children?: ReactNode; defaultNative?: boolean;
  onLoad?: (event: SyntheticEvent<HTMLImageElement>) => void;
  onError?: () => void;
}) {
  const generation = useConnectionStore((state) => state.generation);
  const status = useConnectionStore((state) => state.status);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [previousSrc, setPreviousSrc] = useState(src);
  if (previousSrc !== src) { setPreviousSrc(src); setFailed(false); setRetry(0); }
  const imageSrc = recoveredImageSrc(src, generation, retry);
  useEffect(() => {
    if (!failed || status !== "open" || !src.startsWith("/api/") || document.visibilityState === "hidden") return;
    const timer = setTimeout(() => { setRetry((value) => value + 1); }, Math.min(5000 * 2 ** Math.min(retry, 3), 30_000));
    return () => { clearTimeout(timer); };
  }, [failed, status, src, retry, generation]);
  const [zoomed, setZoomed] = useState(defaultNative);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const imageRef = useRef<HTMLImageElement>(null);
  const [nativeScale, setNativeScale] = useState(1);
  function measure() {
    const image = imageRef.current;
    if (image && image.naturalWidth > 0 && image.naturalHeight > 0 && image.clientWidth > 0 && image.clientHeight > 0) {
      // object-contain scales by the smaller axis. Undo that fit so an image
      // pixel occupies one CSS pixel, independent of viewport or sensor size.
      setNativeScale(Math.max(image.naturalWidth / image.clientWidth, image.naturalHeight / image.clientHeight));
    }
  }
  useEffect(() => {
    const image = imageRef.current;
    if (!image || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(image);
    return () => { observer.disconnect(); };
  }, []);
  const gesture = useRef<{ id: number; x: number; y: number; lastX: number; lastY: number; time: number; moved: boolean } | null>(null);
  const lastTap = useRef<{ time: number; x: number; y: number } | null>(null);
  function toggle() { setZoomed((value) => !value); setPan({ x: 0, y: 0 }); lastTap.current = null; }
  return (
    <div role="button" aria-label={label} aria-pressed={zoomed} title="Double-tap to zoom or return to fit. Drag to pan while zoomed." tabIndex={0} className="image-preview relative flex min-h-0 select-none items-center justify-center overflow-hidden rounded-md border border-border bg-bg"
      style={{ touchAction: zoomed ? "none" : "pan-y" }}
      onDoubleClick={(event) => { if (event.detail !== 0) { event.preventDefault(); } }}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } else if (event.key === "Escape") { setZoomed(false); setPan({ x: 0, y: 0 }); } }}
      onPointerDown={(event) => {
        if (!event.isPrimary && event.pointerType === "touch") { gesture.current = null; lastTap.current = null; return; }
        gesture.current = { id: event.pointerId, x: event.clientX, y: event.clientY, lastX: event.clientX, lastY: event.clientY, time: event.timeStamp, moved: false };
        if (typeof event.currentTarget.setPointerCapture === "function") event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        const g = gesture.current;
        if (g?.id !== event.pointerId) return;
        if (Math.hypot(event.clientX - g.x, event.clientY - g.y) > 8) g.moved = true;
        const dx = event.clientX - g.lastX; const dy = event.clientY - g.lastY;
        if (zoomed) setPan((p) => ({ x: p.x + dx, y: p.y + dy }));
        g.lastX = event.clientX; g.lastY = event.clientY;
      }}
      onPointerCancel={() => { gesture.current = null; lastTap.current = null; }}
      onPointerUp={(event) => {
        const g = gesture.current; gesture.current = null;
        if (g?.id !== event.pointerId || g.moved || event.timeStamp - g.time > 350) { lastTap.current = null; return; }
        const prior = lastTap.current;
        if (prior !== null && event.timeStamp - prior.time < 350 && Math.hypot(event.clientX - prior.x, event.clientY - prior.y) < 30) toggle();
        else lastTap.current = { time: event.timeStamp, x: event.clientX, y: event.clientY };
      }}
    >
      <img ref={imageRef} src={imageSrc} alt={alt} draggable={false} className="h-full w-full object-contain will-change-transform" style={{ transform: `translate(${String(pan.x)}px, ${String(pan.y)}px) scale(${zoomed ? String(nativeScale) : "1"})` }} onLoad={(event) => { setFailed(false); measure(); onLoad?.(event); }} onError={() => { setFailed(true); onError?.(); }} />
      {children}
    </div>
  );
}
