// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useRemoteAccessQuery, type RemoteAccess } from "../../api/settings";
import { RemoteAccessPanel } from "./RemoteAccessPanel";

vi.mock("../../api/settings", () => ({ useRemoteAccessQuery: vi.fn() }));

const base: RemoteAccess = {
  state: "serving",
  detail: "Private HTTPS remote access is active inside your tailnet.",
  detected_path: "/usr/bin/tailscale",
  dns_name: "observatory.example.ts.net",
  url: "https://observatory.example.ts.net",
  funnel_enabled: false,
  setup_command: "sudo tailscale serve --bg 127.0.0.1:7777",
  status_command: "tailscale serve status",
  disable_command: "sudo tailscale serve off",
};

function show(data: RemoteAccess): void {
  vi.mocked(useRemoteAccessQuery).mockReturnValue({
    data, isPending: false, isError: false, refetch: vi.fn(),
  } as unknown as ReturnType<typeof useRemoteAccessQuery>);
  render(<MemoryRouter><RemoteAccessPanel /></MemoryRouter>);
}

describe("RemoteAccessPanel", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("shows the verified private URL, removal command, and guide", () => {
    show(base);
    expect(screen.getByText("Private access active")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://observatory.example.ts.net" })).toHaveAttribute(
      "href", "https://observatory.example.ts.net",
    );
    expect(screen.getByText("sudo tailscale serve off")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Full setup and troubleshooting guide" })).toHaveAttribute(
      "href", "/help?section=knowledge&article=remote-access",
    );
  });

  it("offers installation setup without claiming it can install Tailscale", () => {
    show({ ...base, state: "not_installed", url: null, dns_name: null, detected_path: null, detail: "Tailscale is not installed on the computer running Photon Commander." });
    expect(screen.getByText("Not installed")).toBeInTheDocument();
    expect(screen.getByText("sudo tailscale serve --bg 127.0.0.1:7777")).toBeInTheDocument();
    expect(screen.getByText(/never installs software/)).toBeInTheDocument();
  });

  it("treats Funnel as a public-exposure error", () => {
    show({ ...base, state: "public_exposure", funnel_enabled: true, detail: "Photon Commander is exposed through Tailscale Funnel." });
    expect(screen.getByRole("alert")).toHaveTextContent("exposed through Tailscale Funnel");
    expect(screen.getByText("sudo tailscale funnel off")).toBeInTheDocument();
  });

  it("warns when Photon Commander itself is not restricted to loopback", () => {
    show({ ...base, state: "unsafe_bind", detail: "Photon Commander is listening on 0.0.0.0, not loopback." });
    expect(screen.getByRole("alert")).toHaveTextContent("0.0.0.0");
    expect(screen.getByText("Network bind is not private")).toBeInTheDocument();
    expect(screen.getByText(/Change the Photon Commander service bind to 127.0.0.1/)).toBeInTheDocument();
  });
});
