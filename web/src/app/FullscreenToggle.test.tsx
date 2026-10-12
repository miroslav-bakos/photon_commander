// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * jsdom has no real Fullscreen API, so every browser hook this component
 * reads (`document.fullscreenEnabled`, `requestFullscreen`, `exitFullscreen`,
 * `fullscreenchange`, `matchMedia`) is faked here rather than exercised for
 * real — Playwright-against-a-real-Chromium covered the real thing
 * separately for this task. What this file checks is the component's own
 * logic: which states hide it, and that a tap drives the real APIs rather
 * than tracking fullscreen in component state of its own.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FullscreenToggle } from "./FullscreenToggle";

afterEach(cleanup);

type DisplayMode = "browser" | "standalone" | "fullscreen";

function stubMatchMedia(mode: DisplayMode) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query === `(display-mode: ${mode})`,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
}

/** Fakes just enough of the Fullscreen API for the component to drive it
 * through a real request/exit round trip, including the `fullscreenchange`
 * event it listens for rather than trusting its own optimistic state. */
function stubFullscreenApi(enabled: boolean) {
  let element: Element | null = null;
  Object.defineProperty(document, "fullscreenEnabled", { value: enabled, configurable: true });
  Object.defineProperty(document, "fullscreenElement", {
    configurable: true,
    get: () => element,
  });
  const requestFullscreen = vi.fn(() => {
    element = document.documentElement;
    document.dispatchEvent(new Event("fullscreenchange"));
    return Promise.resolve();
  });
  const exitFullscreen = vi.fn(() => {
    element = null;
    document.dispatchEvent(new Event("fullscreenchange"));
    return Promise.resolve();
  });
  document.documentElement.requestFullscreen = requestFullscreen;
  document.exitFullscreen = exitFullscreen;
  return { requestFullscreen, exitFullscreen };
}

/** This project's jsdom environment does not provide a real
 * `window.localStorage` (a Node/jsdom version interaction, not anything
 * SSC's own code does) — `layerPrefs.test.ts` sidesteps the same gap by
 * injecting a storage object; `rememberPreference` here is not built to take
 * one, so the global itself is stubbed instead. */
function memoryLocalStorage(): Storage {
  const data = new Map<string, string>();
  return {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => {
      data.set(key, value);
    },
    removeItem: (key) => {
      data.delete(key);
    },
    clear: () => {
      data.clear();
    },
    key: (index) => [...data.keys()][index] ?? null,
    get length() {
      return data.size;
    },
  };
}

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryLocalStorage());
});

describe("FullscreenToggle", () => {
  it("renders nothing when the Fullscreen API is unsupported (iPhone Safari)", () => {
    stubFullscreenApi(false);
    stubMatchMedia("browser");
    const { container } = render(<FullscreenToggle />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when already running as an installed standalone PWA", () => {
    stubFullscreenApi(true);
    stubMatchMedia("standalone");
    const { container } = render(<FullscreenToggle />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when already running fullscreen (display-mode)", () => {
    stubFullscreenApi(true);
    stubMatchMedia("fullscreen");
    const { container } = render(<FullscreenToggle />);
    expect(container).toBeEmptyDOMElement();
  });

  it("requests fullscreen on tap, then remembers the choice and reflects the real state", async () => {
    const { requestFullscreen, exitFullscreen } = stubFullscreenApi(true);
    stubMatchMedia("browser");
    const user = userEvent.setup();
    render(<FullscreenToggle />);

    const button = screen.getByRole("button", { name: "Enter fullscreen" });
    expect(button).toHaveAttribute("aria-pressed", "false");

    await act(async () => {
      await user.click(button);
    });

    expect(requestFullscreen).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Exit fullscreen" })).toHaveAttribute("aria-pressed", "true");
    expect(window.localStorage.getItem("ssc.fullscreen-preferred")).toBe("true");

    await act(async () => {
      await user.click(screen.getByRole("button", { name: "Exit fullscreen" }));
    });

    expect(exitFullscreen).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Enter fullscreen" })).toHaveAttribute("aria-pressed", "false");
    expect(window.localStorage.getItem("ssc.fullscreen-preferred")).toBe("false");
  });
});
