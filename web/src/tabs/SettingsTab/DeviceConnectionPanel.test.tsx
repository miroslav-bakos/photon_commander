// SPDX-License-Identifier: GPL-3.0-only

import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useConnectionStore } from "../../store/connectionStore";
import { renderWithQueryClient } from "../../test/queryClient";
import { testSnapshot } from "../../test/fixtures";
import { DeviceConnectionPanel } from "./DeviceConnectionPanel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DeviceConnectionPanel hardware profiles", () => {
  it("shows the actionable reason when a remembered native Alpaca link is down", () => {
    useConnectionStore.setState({
      snapshot: testSnapshot({
        link: {
          status: "disconnected",
          endpoint: "192.0.2.30:32323",
          reason: "Enable Alpaca Protocol in the Seestar app, then reconnect.",
          control_connected: false,
          events_connected: false,
          events_dropped: 0,
          connect_attempts: 1,
          changed_at: "2026-09-14T08:00:00Z",
        },
      }),
    });

    renderWithQueryClient(<DeviceConnectionPanel />);

    expect(screen.getByRole("alert")).toHaveTextContent("Enable Alpaca Protocol");
  });

  it("lists all models and persists an explicit confirmation", async () => {
    useConnectionStore.setState({
      snapshot: testSnapshot({
        link: {
          status: "ready",
          endpoint: "192.0.2.30:32323",
          reason: null,
          control_connected: true,
          events_connected: false,
          events_dropped: 0,
          connect_attempts: 0,
          changed_at: "2026-09-13T18:00:00Z",
        },
      }),
    });
    const saved: string[] = [];
    const profiles = ["S30", "S30 Pro", "S50", "S50 Pro"].map((model) => ({
      id: `zwo-${model.toLowerCase().replaceAll(" ", "")}`,
      name: `ZWO Seestar ${model}`,
      vendor: "ZWO",
      mount: "altaz",
      eq_mode_available: true,
      aliases: [model],
      source_urls: [],
      capabilities: {
        tele_camera: true,
        wide_camera: model !== "S50",
        wide_camera_autofocus: model.includes("Pro"),
        focuser: true,
        filter_wheel: true,
        dew_heater: true,
      },
      defaults: {},
      limits: {},
      optics: [{ id: "tele", label: "Telephoto" }],
      calibration: { measured: false },
    }));
    vi.stubGlobal("fetch", vi.fn(async (input: string | Request, init?: RequestInit) => {
      const request = typeof input === "string" ? null : input;
      const path = typeof input === "string" ? input : new URL(input.url).pathname;
      const method = init?.method ?? request?.method ?? "GET";
      if (path.endsWith("/api/astro/profiles")) {
        return new Response(JSON.stringify({ profiles }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (path.endsWith("/api/device/hardware-profile") && method === "PUT") {
        const raw = typeof init?.body === "string"
          ? init.body
          : (await request?.clone().text()) ?? "";
        saved.push((JSON.parse(raw) as { profile_id: string }).profile_id);
        return new Response(JSON.stringify({
          host: null,
          control_port: null,
          label: null,
          last_good_at: null,
          hardware_profile_id: saved.at(-1),
          hardware_profile_source: "manual",
          auto_connect: true,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(JSON.stringify({
        remembered: {
          host: null,
          control_port: null,
          label: null,
          last_good_at: null,
          hardware_profile_id: null,
          hardware_profile_source: null,
          auto_connect: true,
        },
        known: [],
      }), { status: 200, headers: { "content-type": "application/json" } });
    }));

    renderWithQueryClient(<DeviceConnectionPanel />);
    expect(screen.getByText("ready")).toBeInTheDocument();
    const selector = await screen.findByLabelText("Telescope model");
    await screen.findByRole("option", { name: "ZWO Seestar S50 Pro" });
    expect(screen.getAllByRole("option")).toHaveLength(5);
    await userEvent.selectOptions(selector, "zwo-s50pro");
    expect(saved).toEqual(["zwo-s50pro"]);
  });
});

describe("DeviceConnectionPanel preferred Ethernet address", () => {
  it("loads and saves Ethernet separately from the Wi-Fi fallback", async () => {
    useConnectionStore.setState({ snapshot: testSnapshot() });
    const savedEthernet: unknown[] = [];
    const savedWifi: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: string | Request, init?: RequestInit) => {
      const request = typeof input === "string" ? null : input;
      const path = typeof input === "string" ? input : new URL(input.url).pathname;
      const method = init?.method ?? request?.method ?? "GET";
      if (path.endsWith("/api/astro/profiles")) {
        return new Response(JSON.stringify({ profiles: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      const remembered = {
        host: "192.168.1.77",
        control_port: 32323,
        ethernet_host: "10.42.0.2",
        ethernet_control_port: 32323,
        label: null,
        last_good_at: null,
        hardware_profile_id: null,
        hardware_profile_source: null,
        auto_connect: true,
      };
      if (path.endsWith("/api/device/ethernet-address") && method === "PUT") {
        const raw = typeof init?.body === "string"
          ? init.body
          : (await request?.clone().text()) ?? "";
        savedEthernet.push(JSON.parse(raw) as unknown);
        return new Response(JSON.stringify(remembered), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (path.endsWith("/api/device/wifi-address") && method === "PUT") {
        const raw = typeof init?.body === "string"
          ? init.body
          : (await request?.clone().text()) ?? "";
        savedWifi.push(JSON.parse(raw) as unknown);
        return new Response(JSON.stringify(remembered), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ remembered, known: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }));

    renderWithQueryClient(<DeviceConnectionPanel />);
    const ethernet = await screen.findByLabelText("Ethernet IP");
    await waitFor(() => {
      expect(ethernet).toHaveValue("10.42.0.2");
    });
    await userEvent.clear(ethernet);
    await userEvent.type(ethernet, "10.42.0.9");
    await userEvent.click(screen.getByRole("button", { name: "Save preferred Ethernet" }));

    await waitFor(() => {
      expect(savedEthernet).toEqual([{ host: "10.42.0.9", control_port: 32323 }]);
    });

    const wifi = screen.getByLabelText("Wi-Fi fallback IP");
    expect(wifi).toHaveValue("192.168.1.77");
    await userEvent.clear(wifi);
    await userEvent.type(wifi, "192.168.1.88");
    await userEvent.click(screen.getByRole("button", { name: "Save Wi-Fi fallback" }));

    await waitFor(() => {
      expect(savedWifi).toEqual([{ host: "192.168.1.88", control_port: 32323 }]);
    });
  });
});
