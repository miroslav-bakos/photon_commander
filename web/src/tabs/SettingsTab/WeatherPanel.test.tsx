// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The two properties worth pinning here are both about honesty:
 *
 * * the OpenWeatherMap key is never rendered back (architecture policy invariant 8) —
 *   the field is empty even when a key is stored, and the UI says "stored"
 *   rather than showing a masked value that would imply it had been fetched;
 * * an unavailable provider always says why, and an unusable keyring refuses
 *   input instead of accepting a key it would silently lose (invariant 7).
 */
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WeatherSettings } from "../../api/settings";
import { WeatherPanel } from "./WeatherPanel";
import { renderWithQueryClient } from "../../test/queryClient";

afterEach(cleanup);

function settings(overrides: Partial<WeatherSettings> = {}): WeatherSettings {
  return {
    provider: "metno",
    metno_contact: null,
    metno_contact_source: "unset",
    openweathermap_key_set: false,
    weatherapi_key_set: false,
    visualcrossing_key_set: false,
    secrets_backend_available: true,
    secrets_backend_reason: null,
    providers: [
      { name: "metno", available: false, reason: "no Met.no contact configured" },
      { name: "openweathermap", available: false, reason: "no OpenWeatherMap API key is configured" },
      { name: "weatherapi", available: false, reason: "no WeatherAPI.com API key is configured" },
      { name: "visualcrossing", available: false, reason: "no Visual Crossing API key is configured" },
    ],
    ...overrides,
  };
}

function mockGet(body: WeatherSettings) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() => {
      const response = new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
      Object.defineProperty(response, "url", { value: "http://localhost/api/settings/weather" });
      return Promise.resolve(response);
    }),
  );
}

const renderPanel = renderWithQueryClient;

describe("WeatherPanel", () => {
  it("says why each provider is not in use", async () => {
    mockGet(settings());
    renderPanel(<WeatherPanel />);
    expect(await screen.findByText(/no Met.no contact configured/)).toBeInTheDocument();
    expect(screen.getByText(/no OpenWeatherMap API key is configured/)).toBeInTheDocument();
  });

  it("selects and persists the forecast provider", async () => {
    const puts: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (request: Request) => {
        if (request.method === "PUT") {
          puts.push(await request.clone().json());
          return new Response(JSON.stringify(settings({ provider: "openweathermap" })), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify(settings()), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    renderPanel(<WeatherPanel />);

    const provider = await screen.findByLabelText("Forecast provider");
    expect(provider).toHaveValue("metno");
    await userEvent.selectOptions(provider, "openweathermap");
    await vi.waitFor(() => {
      expect(puts).toContainEqual({ provider: "openweathermap" });
    });
  });

  it("never renders a stored OpenWeatherMap key back", async () => {
    mockGet(settings({ openweathermap_key_set: true }));
    renderPanel(<WeatherPanel />);
    const field = await screen.findByLabelText(/OpenWeatherMap API key/i);
    // The value is empty and the *placeholder* carries the state. A masked
    // value here would imply the server had sent one.
    expect(field).toHaveValue("");
    expect(field).toHaveAttribute("placeholder", expect.stringMatching(/stored/i));
  });

  it("shows the Met.no contact in plain text — it is published, not secret", async () => {
    mockGet(settings({ metno_contact: "obs@example.invalid", metno_contact_source: "settings" }));
    renderPanel(<WeatherPanel />);
    const field = await screen.findByLabelText(/Met.no contact/i);
    expect(field).toHaveValue("obs@example.invalid");
    expect(field).toHaveAttribute("type", "text");
  });

  it("warns when the environment variable is what is really in force", async () => {
    mockGet(
      settings({ metno_contact: "env@example.invalid", metno_contact_source: "environment" }),
    );
    renderPanel(<WeatherPanel />);
    expect(await screen.findByText(/SSC_WEATHER_CONTACT/)).toBeInTheDocument();
  });

  it("does not claim the environment is in force when the stored value won", async () => {
    mockGet(settings({ metno_contact: "obs@example.invalid", metno_contact_source: "settings" }));
    renderPanel(<WeatherPanel />);
    await screen.findByLabelText(/Met.no contact/i);
    expect(screen.queryByText(/SSC_WEATHER_CONTACT/)).not.toBeInTheDocument();
  });

  it("refuses key entry when the keyring cannot store one", async () => {
    mockGet(
      settings({
        secrets_backend_available: false,
        secrets_backend_reason: "no D-Bus session; gnome-keyring is not unlocked",
      }),
    );
    renderPanel(<WeatherPanel />);
    const field = await screen.findByLabelText(/OpenWeatherMap API key/i);
    expect(field).toBeDisabled();
    expect(screen.getByText(/gnome-keyring is not unlocked/)).toBeInTheDocument();
  });

  it("keeps Save inert until the contact actually differs from the server's", async () => {
    mockGet(settings({ metno_contact: "obs@example.invalid", metno_contact_source: "settings" }));
    renderPanel(<WeatherPanel />);
    await screen.findByLabelText(/Met.no contact/i);
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/Met.no contact/i), "x");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });
});

it("preserves contact edits typed while a save response is in flight", async () => {
  let finish!: (response: Response) => void;
  let saved = false;
  const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn((request: Request) => {
    if (request.method === "PUT") return new Promise<Response>((resolve) => { finish = resolve; });
    if (saved) return Promise.reject(new TypeError("Refresh unavailable"));
    return Promise.resolve(json(settings({ metno_contact: "old@example.com" })));
  }));
  renderPanel(<WeatherPanel />);
  const contact = await screen.findByLabelText("Met.no contact");
  await waitFor(() => { expect(contact).toHaveValue("old@example.com"); });
  fireEvent.change(contact, { target: { value: "first@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => { expect(finish).toBeDefined(); });
  fireEvent.change(contact, { target: { value: "newer@example.com" } });
  saved = true;
  await act(async () => { finish(json(settings({ metno_contact: "first@example.com" }))); await Promise.resolve(); });
  await waitFor(() => { expect(screen.getByRole("button", { name: "Save" })).toBeEnabled(); });
  expect(contact).toHaveValue("newer@example.com");
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not refresh settings");
  expect(contact).toHaveValue("newer@example.com");
});

it("retains the newer contact draft when the save loses its response", async () => {
  let fail!: (error: Error) => void;
  const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn((request: Request) => request.method === "PUT"
    ? new Promise<Response>((_resolve, reject) => { fail = reject; })
    : Promise.resolve(json(settings({ metno_contact: "old@example.com" })))));
  renderPanel(<WeatherPanel />);
  const contact = await screen.findByLabelText("Met.no contact");
  await waitFor(() => { expect(contact).toHaveValue("old@example.com"); });
  fireEvent.change(contact, { target: { value: "first@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => { expect(fail).toBeDefined(); });
  fireEvent.change(contact, { target: { value: "newer@example.com" } });
  await act(async () => { fail(new TypeError("Network response lost")); await Promise.resolve(); });
  await waitFor(() => { expect(screen.getByRole("button", { name: "Save" })).toBeEnabled(); });
  expect(contact).toHaveValue("newer@example.com");
  expect(screen.getByText("Network response lost")).toBeInTheDocument();
});
