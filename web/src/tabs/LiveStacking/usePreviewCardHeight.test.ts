// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { usePreviewCardHeight } from "./usePreviewCardHeight";

afterEach(() => { document.getElementById("main")?.remove(); vi.unstubAllGlobals(); });

it("budgets the whole preview from the shell content area and follows shell resizing", () => {
  const main = document.createElement("main");
  main.id = "main";
  main.style.padding = "16px 16px 24px";
  let shellHeight = 620;
  Object.defineProperty(main, "clientHeight", { get: () => shellHeight });
  document.body.append(main);
  let resize: () => void = () => undefined;
  const disconnect = vi.fn();
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { resize = callback; }
    observe = vi.fn();
    disconnect = disconnect;
  });
  const { result, unmount } = renderHook(usePreviewCardHeight);
  expect(result.current).toBe(580);
  act(() => { shellHeight = 360; resize(); });
  expect(result.current).toBe(320);
  unmount();
  expect(disconnect).toHaveBeenCalledOnce();
});

it("retains the CSS viewport fallback outside the application shell", () => {
  expect(renderHook(usePreviewCardHeight).result.current).toBeUndefined();
});
