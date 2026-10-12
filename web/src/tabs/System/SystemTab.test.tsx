// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithQueryClient } from "../../test/queryClient";
import type { SystemMetrics } from "../../api/system";
import { LayoutRouteProvider } from "../../layout/LayoutContext";
import { useLayoutStore } from "../../layout/layoutStore";
import { SystemTab } from "./SystemTab";

function metrics(overrides: Partial<SystemMetrics> = {}): SystemMetrics {
  return {
    collected_at: "2026-09-14T18:00:00Z",
    hostname: "photon",
    platform: "Linux",
    architecture: "aarch64",
    model: "Raspberry Pi 5 Model B Rev 1.1",
    os_name: "Debian GNU/Linux 13 (trixie)",
    kernel: "6.18.34+rpt-rpi-2712",
    system_uptime_s: 3660,
    process: { pid: 321, uptime_s: 600, cpu_percent: 7.5, memory_bytes: 64 * 1024 * 1024, threads: 8 },
    cpu: { percent: 18.5, logical_cores: 4, load_1m: 0.5, load_5m: 0.4, load_15m: 0.3, temperature_c: 42.5, throttled_raw: "0x0", throttle_flags: [] },
    memory: { total_bytes: 8 * 1024 ** 3, used_bytes: 2 * 1024 ** 3, available_bytes: 6 * 1024 ** 3, percent_used: 25 },
    swap: { total_bytes: 2 * 1024 ** 3, used_bytes: 0, free_bytes: 2 * 1024 ** 3, percent_used: 0 },
    volumes: [
      { kind: "os", label: "OS / root", path: "/", device: "/dev/mmcblk0p2", filesystem: "ext4", total_bytes: 32 * 1024 ** 3, used_bytes: 4 * 1024 ** 3, free_bytes: 28 * 1024 ** 3, percent_used: 12.5, same_device_as_os: true },
      { kind: "data", label: "Photon data", path: "/var/lib/photon-commander", device: "/dev/sda1", filesystem: "ext4", total_bytes: 1024 ** 4, used_bytes: 100 * 1024 ** 3, free_bytes: 924 * 1024 ** 3, percent_used: 9.8, same_device_as_os: false },
    ],
    network: { interfaces: [{ name: "eth0", ipv4_address: "192.168.100.13" }], bytes_sent: 1000, bytes_received: 2000 },
    ...overrides,
  };
}

function serve(sample: SystemMetrics): void {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(sample), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));
}

afterEach(() => {
  vi.unstubAllGlobals();
  useLayoutStore.setState({ unlocked: false, saved: { version: 1, tabs: {} } });
});

describe("SystemTab", () => {
  it("shows Pi health and distinguishes SD-card and SSD storage", async () => {
    serve(metrics());

    renderWithQueryClient(<SystemTab />);

    expect(await screen.findByRole("heading", { name: "System" })).toBeInTheDocument();
    expect(screen.getByText(/Raspberry Pi 5 Model B/)).toBeInTheDocument();
    expect(screen.getByText("42.5 °C")).toBeInTheDocument();
    expect(screen.getByText(/No throttling or undervoltage/)).toBeInTheDocument();
    expect(screen.getByText("OS / SD card")).toBeInTheDocument();
    expect(screen.getByText("Photon data / SSD")).toBeInTheDocument();
    expect(screen.getByText("/dev/mmcblk0p2")).toBeInTheDocument();
    expect(screen.getByText("/dev/sda1")).toBeInTheDocument();
    expect(screen.getByText("192.168.100.13")).toBeInTheDocument();
  });

  it("states when Pi-only temperature and throttling data are unavailable", async () => {
    const base = metrics();
    serve(metrics({ cpu: { ...base.cpu, temperature_c: null, throttled_raw: null, throttle_flags: null } }));

    renderWithQueryClient(<SystemTab />);

    expect(await screen.findByText("Pi throttling telemetry is unavailable on this host.")).toBeInTheDocument();
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(0);
  });

  it("keeps screen loading diagnostics last and makes System cards editable", async () => {
    serve(metrics());
    useLayoutStore.getState().setUnlocked(true);

    renderWithQueryClient(
      <MemoryRouter initialEntries={["/system"]}>
        <LayoutRouteProvider><SystemTab /></LayoutRouteProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("button", { name: "Move CPU down" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move Screen loading diagnostics up" })).toBeInTheDocument();
    const grid = document.querySelector('[data-layout-panel-target="secondary"]');
    const cards = grid?.querySelectorAll(":scope > [data-layout-card-id]");
    expect(cards?.item(cards.length - 1)).toHaveAttribute("data-layout-card-id", "system.performance");
  });
});
