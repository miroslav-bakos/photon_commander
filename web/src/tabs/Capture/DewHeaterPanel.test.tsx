// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useConnectionStore } from "../../store/connectionStore";
import { testConnectedDevices, testSnapshot } from "../../test/fixtures";
import { renderWithQueryClient } from "../../test/queryClient";
import { DewHeaterPanel } from "./DewHeaterPanel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useConnectionStore.getState().reset();
});

describe("DewHeaterPanel weather recommendation", () => {
  beforeEach(() => {
    useConnectionStore.setState({ snapshot: testSnapshot({ devices: testConnectedDevices("switch") }) });
  });

  it("shows only Disconnected for an Alpaca Connected=True refusal", async () => {
    const rawNote =
      "switch.getswitch: [0x407] switch.getswitch requires Connected = True (this device is not connected)";
    vi.stubGlobal("fetch", vi.fn().mockImplementation((request: Request) => {
      const pathname = new URL(request.url).pathname;
      const body = pathname === "/api/heater"
        ? { enabled: null, note: rawNote }
        : {
            enabled: false, on_margin_c: 5, off_margin_c: 7, evaluated_at: null,
            dew_margin_c: null, action: null, note: null,
          };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }));

    renderWithQueryClient(<DewHeaterPanel />);
    expect(await screen.findByText("Disconnected")).toBeInTheDocument();
    expect(screen.queryByText(rawNote)).not.toBeInTheDocument();
    expect(screen.queryByText(/Status:/)).not.toBeInTheDocument();
  });

  it("warns when the heater is off and outside air is close to its dew point", async () => {
    useConnectionStore.setState({
      snapshot: testSnapshot({ location: {
        latitude_deg: 48.15,
        longitude_deg: 17.11,
        elevation_m: 130,
        timezone: "Europe/Bratislava",
        source: "manual",
        accuracy_m: null,
        acquired_at: new Date().toISOString(),
      } }),
      seq: 1,
    } as never);
    vi.stubGlobal("fetch", vi.fn().mockImplementation((request: Request) => {
      const pathname = new URL(request.url).pathname;
      const body = pathname === "/api/heater"
        ? { enabled: false, note: null }
        : pathname === "/api/heater/automation"
          ? {
              enabled: false, on_margin_c: 5, off_margin_c: 7, evaluated_at: null,
              dew_margin_c: null, action: null, note: null,
            }
          : {
            location: useConnectionStore.getState().snapshot?.location,
            fetched_at: new Date().toISOString(),
            stale_since: null,
            points: [{
              t: new Date().toISOString(), cloud_total: 20, cloud_low: 10, cloud_mid: 0,
              cloud_high: 10, precip_prob: 0, humidity: 91, dew_point_c: 7.2,
              temp_c: 9.0, wind_ms: 1, wind_direction_deg: 0, visibility_m: 10000,
              source: "metno",
            }],
          };
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));

    renderWithQueryClient(<DewHeaterPanel />);

    const warning = await screen.findByRole("alert");
    expect(warning).toHaveTextContent("Dew heater recommended");
    expect(warning).toHaveTextContent("1.8°C above the dew point");
    expect(await screen.findByRole("button", { name: "Automatic off" })).toBeEnabled();
    expect(screen.getByLabelText("Dew heater turn-on margin")).toHaveValue(5);
  });

  it("persists opt-in automatic control with the configured hysteresis", async () => {
    const requests: { method: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (request: Request) => {
      const pathname = new URL(request.url).pathname;
      if (pathname === "/api/heater/automation") {
        if (request.method === "PUT") {
          requests.push({ method: request.method, body: await request.clone().json() });
        }
        return new Response(JSON.stringify({
          enabled: request.method === "PUT", on_margin_c: 5, off_margin_c: 7,
          evaluated_at: null, dew_margin_c: null, action: null, note: null,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(JSON.stringify({ enabled: false, note: null }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }));

    renderWithQueryClient(<DewHeaterPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Automatic off" }));

    expect(requests).toEqual([{ method: "PUT", body: {
      enabled: true, on_margin_c: 5, off_margin_c: 7,
    } }]);
  });
});
