// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Font scale gets the identical treatment `theme` already has (architecture policy):
 * server state, mutated via the same `/api/settings/appearance` PUT, never a
 * client-local preference. This file checks the button wiring end of that —
 * `useFontScaleSync`'s own read-from-snapshot half is exercised elsewhere.
 */
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppearancePanel } from "./AppearancePanel";
import { renderWithQueryClient } from "../../test/queryClient";

afterEach(cleanup);

function json(body: unknown, status: number, url: string): Response {
  const response = new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  Object.defineProperty(response, "url", { value: `http://localhost${url}` });
  return response;
}

function settingsResponse(overrides: { workflow_guide_visible?: boolean; theme?: string; font_scale?: string; accent?: string; background_enabled?: boolean; background_opacity_percent?: number } = {}) {
  return {
    location: { active: null, preferred_source: null, sources: {} },
    horizon: { active: false, points: null },
    appearance: {
      ...(overrides.workflow_guide_visible === undefined ? {} : { workflow_guide_visible: overrides.workflow_guide_visible }),
      theme: overrides.theme ?? "dark",
      font_scale: overrides.font_scale ?? "normal",
      accent: overrides.accent ?? "blue",
      background_enabled: overrides.background_enabled ?? false,
      background_opacity_percent: overrides.background_opacity_percent ?? 15,
    },
    device: { host: null, control_port: null, label: null, last_good_at: null, auto_connect: true },
    secret_names: [],
    control_pin_set: false,
  };
}

/** Routes the stubbed fetch by method + path, like `OfflineMapsPanel.test.tsx`. */
function stubFetch() {
  const puts: { pathname: string; body: unknown }[] = [];
  let current = settingsResponse();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      const { pathname } = new URL(request.url);
      if (request.method === "GET" && pathname === "/api/settings") {
        return json(current, 200, pathname);
      }
      if (request.method === "PUT" && pathname === "/api/settings/appearance") {
        const body = (await request.clone().json()) as { theme: string; font_scale: string; accent: string; background_enabled: boolean; background_opacity_percent: number };
        puts.push({ pathname, body });
        current = settingsResponse(body);
        return json(current.appearance, 200, pathname);
      }
      return json({ detail: `unexpected ${request.method} ${pathname}` }, 500, pathname);
    }),
  );
  return puts;
}

describe("AppearancePanel", () => {
  it("sends font_scale: small when Small is clicked", async () => {
    const puts = stubFetch();
    renderWithQueryClient(<AppearancePanel />);
    const button = await screen.findByRole("button", { name: "Small" });
    await userEvent.click(button);
    await waitFor(() => { expect(puts).toHaveLength(1); });
    expect(puts[0]?.body).toMatchObject({ font_scale: "small", theme: "dark" });
  });

  it("sends font_scale: large when Large is clicked", async () => {
    const puts = stubFetch();
    renderWithQueryClient(<AppearancePanel />);
    const button = await screen.findByRole("button", { name: "Large" });
    await userEvent.click(button);
    await waitFor(() => { expect(puts).toHaveLength(1); });
    expect(puts[0]?.body).toMatchObject({ font_scale: "large" });
  });

  it("marks the current font scale as pressed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((request: Request) => {
        const { pathname } = new URL(request.url);
        return json(settingsResponse({ font_scale: "large" }), 200, pathname);
      }),
    );
    renderWithQueryClient(<AppearancePanel />);
    const large = await screen.findByRole("button", { name: "Large" });
    expect(large).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Normal" })).toHaveAttribute("aria-pressed", "false");
  });

  it("previews and persists an accent choice", async () => {
    const puts = stubFetch();
    renderWithQueryClient(<AppearancePanel />);

    const violet = await screen.findByRole("button", { name: "Violet" });
    expect(screen.getByRole("button", { name: "Blue" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(violet);

    await waitFor(() => { expect(puts).toHaveLength(1); });
    expect(puts[0]?.body).toMatchObject({ accent: "violet", theme: "dark", font_scale: "normal" });
    expect(screen.getByRole("button", { name: "Teal" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Indigo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Fuchsia" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Orange" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Lime" })).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "Accent color" })).getAllByRole("button")).toHaveLength(33);
    expect(screen.getByRole("button", { name: "Blue Bright" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Blue Deep" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hydrogen-alpha" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sulfur" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OIII" })).toBeInTheDocument();
  });

  it("persists a new astronomical accent choice", async () => {
    const puts = stubFetch();
    renderWithQueryClient(<AppearancePanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Hydrogen-alpha" }));
    await waitFor(() => { expect(puts[0]?.body).toMatchObject({ accent: "h-alpha" }); });
  });

  it("explains that red-light mode ignores the color accent", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((request: Request) => {
        const { pathname } = new URL(request.url);
        return json(settingsResponse({ theme: "red", accent: "green" }), 200, pathname);
      }),
    );
    renderWithQueryClient(<AppearancePanel />);
    expect(await screen.findByText(/Red-light mode stays monochrome/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Green" })).toHaveAttribute("aria-pressed", "true");
  });

  it("enables the M31 background and persists opacity", async () => {
    const puts = stubFetch();
    renderWithQueryClient(<AppearancePanel />);

    await userEvent.click(await screen.findByRole("checkbox", { name: "Show M31 background" }));
    await waitFor(() => { expect(puts).toHaveLength(1); });
    expect(puts[0]?.body).toMatchObject({ background_enabled: true, background_opacity_percent: 15 });
  });
});


it("restores the observing workflow guide through server appearance settings", async () => {
  const puts = stubFetch();
  renderWithQueryClient(<AppearancePanel />);
  const checkbox = await screen.findByRole("checkbox", { name: "Show observing workflow guide" });
  expect(checkbox).toBeChecked();
  await userEvent.click(checkbox);
  await waitFor(() => { expect(checkbox).not.toBeChecked(); });
  await userEvent.click(checkbox);
  await waitFor(() => { expect(checkbox).toBeChecked(); });
  expect(puts.map((put) => (put.body as { workflow_guide_visible: boolean }).workflow_guide_visible)).toEqual([false, true]);
});
