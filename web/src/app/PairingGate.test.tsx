// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { useQueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../test/queryClient";
import { PairingGate } from "./PairingGate";

function stubAuthResponses(initialStatus: number, authRequired = false): (status: number) => void {
  let securityStatus = initialStatus;
  vi.stubGlobal("fetch", vi.fn((request: Request): Promise<Response> => {
    const path = new URL(request.url).pathname;
    if (path === "/api/auth/session") return Promise.resolve(Response.json({ authenticated: false }));
    if (path === "/api/settings/security") {
      return Promise.resolve(Response.json(
        securityStatus === 200 ? { auth_required: authRequired, auth_required_locked: false } : { detail: "unavailable" },
        { status: securityStatus },
      ));
    }
    if (path === "/api/auth/pairing") return Promise.resolve(Response.json({ detail: "not paired" }, { status: 403 }));
    throw new Error(`Unexpected request: ${path}`);
  }));
  return (status) => { securityStatus = status; };
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("PairingGate", () => {
  it("keeps authenticated drafts mounted while security retries, but locks after session revocation", async () => {
    let authenticated = true;
    let securityRetry = false;
    let resolveSecurity!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn((request: Request): Promise<Response> => {
      const path = new URL(request.url).pathname;
      if (path === "/api/auth/session") return Promise.resolve(Response.json({ authenticated }));
      if (path === "/api/settings/security") return securityRetry
        ? new Promise((resolve) => { resolveSecurity = resolve; })
        : Promise.resolve(Response.json({ detail: "not paired" }, { status: 401 }));
      if (path === "/api/auth/pairing") return Promise.resolve(Response.json({}, { status: 403 }));
      throw new Error(`Unexpected request: ${path}`);
    }));
    function Harness() {
      const client = useQueryClient();
      return <>
        <button onClick={() => { void client.refetchQueries({ queryKey: ["settings", "security"] }); }}>Retry security</button>
        <button onClick={() => { void client.refetchQueries({ queryKey: ["auth", "session"] }); }}>Check session</button>
        <PairingGate><input aria-label="Unsaved draft" defaultValue="original" /></PairingGate>
      </>;
    }
    renderWithQueryClient(<Harness />);
    const draft = await screen.findByLabelText("Unsaved draft");
    fireEvent.change(draft, { target: { value: "unsaved change" } });
    securityRetry = true;
    fireEvent.click(screen.getByText("Retry security"));
    await waitFor(() => { expect(resolveSecurity).toBeTypeOf("function"); });
    expect(screen.getByLabelText("Unsaved draft")).toBe(draft);
    expect(draft).toHaveValue("unsaved change");
    await act(async () => { resolveSecurity(Response.json({ auth_required: true })); await Promise.resolve(); });
    expect(screen.getByLabelText("Unsaved draft")).toBe(draft);
    authenticated = false;
    fireEvent.click(screen.getByText("Check session"));
    expect(await screen.findByText("This device is not paired yet.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Unsaved draft")).not.toBeInTheDocument();
  });
  it("opens the app without a pairing screen when pairing is disabled", async () => {
    stubAuthResponses(200);
    renderWithQueryClient(<PairingGate><p>App content</p></PairingGate>);
    expect(await screen.findByText("App content")).toBeInTheDocument();
    expect(screen.queryByText("This device is not paired yet.")).not.toBeInTheDocument();
  });

  it("shows pairing only when the server confirms it is required", async () => {
    stubAuthResponses(401);
    renderWithQueryClient(<PairingGate><p>App content</p></PairingGate>);
    expect(await screen.findByText("This device is not paired yet.")).toBeInTheDocument();
    expect(screen.queryByText("App content")).not.toBeInTheDocument();
  });

  it("shows pairing when the security setting explicitly requires it", async () => {
    stubAuthResponses(200, true);
    renderWithQueryClient(<PairingGate><p>App content</p></PairingGate>);
    expect(await screen.findByText("This device is not paired yet.")).toBeInTheDocument();
  });

  it("shows connection recovery rather than pairing after a transient failure", async () => {
    const setSecurityStatus = stubAuthResponses(503);
    renderWithQueryClient(<PairingGate><p>App content</p></PairingGate>);
    await waitFor(() => {
      expect(screen.getByText("Cannot check connection")).toBeInTheDocument();
    }, { timeout: 6000 });
    expect(screen.getByRole("button", { name: "Retry now" })).toBeInTheDocument();
    expect(screen.queryByText("This device is not paired yet.")).not.toBeInTheDocument();
    setSecurityStatus(200);
    fireEvent.click(screen.getByRole("button", { name: "Retry now" }));
    expect(await screen.findByText("App content")).toBeInTheDocument();
  }, 8000);
});
