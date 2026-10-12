// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StateSnapshot } from "../api/types";
import { useConnectionStore } from "../store/connectionStore";
import { renderWithQueryClient } from "../test/queryClient";
import { MountSlewSyncControls, type MountSlewSyncTarget } from "./MountSlewSyncControls";
import type { MountCapabilities, MountState } from "../api/mount";

const TARGET: MountSlewSyncTarget = { label: "M31", raHoursJ2000: 0.712, decDegJ2000: 41.27 };
const CENTERED = {
  converged: true,
  target_ra_j2000_h: TARGET.raHoursJ2000,
  target_dec_j2000_deg: TARGET.decDegJ2000,
  tolerance_arcmin: 1,
  attempts: [{
    attempt: 1,
    commanded_ra_j2000_h: TARGET.raHoursJ2000,
    commanded_dec_j2000_deg: TARGET.decDegJ2000,
    solved_ra_j2000_h: TARGET.raHoursJ2000,
    solved_dec_j2000_deg: TARGET.decDegJ2000,
    error_arcmin: 0.24,
  }],
};
const RUNNING_PROGRESS = {
  status: "running",
  stage: "settling",
  attempt: 1,
  max_attempts: 3,
  target_ra_j2000_h: TARGET.raHoursJ2000,
  target_dec_j2000_deg: TARGET.decDegJ2000,
  tolerance_arcmin: 1,
  attempts: [],
  message: "Waiting for the mount to stop and report the commanded position twice.",
  error: null,
  updated_at: "2026-09-12T21:00:00Z",
};
const SOLVED = {
  ra_j2000_hours: 10,
  dec_j2000_deg: 41.2,
  date_obs: "2026-09-12T21:00:00Z",
  pixel_scale_arcsec: 3.74,
  rotation_deg: 93.5,
  solver: "ASTAP",
  solver_version: "1",
  mount_ra_j2000_hours: 9.99,
  mount_dec_j2000_deg: 41.1,
  mount_altitude_deg: 60,
  mount_azimuth_deg: 180,
};

const FULL_CAPS: MountCapabilities = {
  can_slew_async: true,
  can_sync: true,
  can_park: true,
  can_unpark: true,
  can_find_home: true,
  can_set_tracking: true,
  can_move_primary_axis: true,
  can_move_secondary_axis: true,
  axis_rate_max_deg_s: 4,
  tracking_rates: ["sidereal"],
};

function mountState(overrides: Partial<MountState>): MountState {
  return {
    polled_at: "2026-09-10T22:00:00Z",
    stale: false,
    ra_hours: 5,
    dec_deg: 20,
    alt_deg: 40,
    az_deg: 100,
    sidereal_time_hours: 6,
    tracking: true,
    tracking_rate: "sidereal",
    at_park: false,
    at_home: false,
    slewing: false,
    equatorial_system: "J2000",
    capabilities: FULL_CAPS,
    ...overrides,
  };
}

function setMount(mount: MountState | null): void {
  useConnectionStore.setState({
    snapshot: { mount } as unknown as StateSnapshot,
    seq: 1,
  });
}

function jsonResponse(body: unknown, status: number, url: string): Response {
  const response = new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
  Object.defineProperty(response, "url", { value: `http://localhost${url}` });
  return response;
}

function stubMountFetch(responses: {
  unpark?: { status: number; body?: unknown };
  center?: { status: number; body?: unknown };
  sync?: { status: number; body?: unknown };
  solve?: { status: number; body?: unknown };
}) {
  const calls: { path: string; method: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const { pathname } = new URL(url, "http://localhost");
      const method = input instanceof Request ? input.method : (init?.method ?? "GET");
      const body = input instanceof Request
        ? await input.clone().json().catch(() => undefined) as unknown
        : init?.body !== undefined && typeof init.body === "string"
          ? JSON.parse(init.body) as unknown
          : undefined;
      if (pathname === "/api/mount/center/status") {
        return jsonResponse(RUNNING_PROGRESS, 200, pathname);
      }
      calls.push({ path: pathname, method, body });
      if (pathname === "/api/mount/unpark" && responses.unpark !== undefined) {
        return jsonResponse(responses.unpark.body ?? {}, responses.unpark.status, pathname);
      }
      if (pathname === "/api/mount/center" && responses.center !== undefined) {
        return jsonResponse(responses.center.body ?? CENTERED, responses.center.status, pathname);
      }
      if (pathname === "/api/mount/sync" && responses.sync !== undefined) {
        return jsonResponse(responses.sync.body ?? {}, responses.sync.status, pathname);
      }
      if (pathname === "/api/mount/polar-alignment/solve-here" && responses.solve !== undefined) {
        return jsonResponse(responses.solve.body ?? SOLVED, responses.solve.status, pathname);
      }
      return jsonResponse({ detail: `unexpected fetch ${pathname}` }, 500, pathname);
    }),
  );
  return calls;
}

afterEach(() => {
  useConnectionStore.getState().reset();
  vi.unstubAllGlobals();
});

describe("MountSlewSyncControls", () => {
  it("uses compact icon actions with accessible tooltips", () => {
    setMount(mountState({}));
    stubMountFetch({});
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    expect(screen.getByRole("button", { name: "Slew + center" })).toHaveAttribute("title", expect.stringMatching(/plate solve/i));
    expect(screen.getByRole("button", { name: "Plate solve" })).toHaveAttribute("title", expect.stringMatching(/camera frame/i));
    expect(screen.getByRole("button", { name: "Sync" })).toHaveAttribute("title", expect.stringMatching(/mount/i));
    expect(screen.getByRole("button", { name: "Slew + center" })).toHaveClass("size-11");
    expect(screen.getByRole("button", { name: "Slew + center" }).querySelector("svg")).toHaveClass("size-5");
    expect(screen.getByRole("button", { name: "Plate solve" }).querySelector("svg")).toHaveClass("size-5");
    expect(screen.getByRole("button", { name: "Sync" }).querySelector("svg")).toHaveClass("size-5");
  });

  it("renders an icon-only target toolbar when embedded over the skymap", () => {
    setMount(mountState({}));
    stubMountFetch({});
    renderWithQueryClient(
      <MountSlewSyncControls target={TARGET} toleranceText="30" showToleranceControl={false} toolbar />,
    );

    const toolbar = screen.getByRole("toolbar", { name: "Target actions" });
    expect(toolbar).toHaveClass("bg-surface/80");
    expect(toolbar).toHaveClass("ssc-map-toolbar", "rounded-full");
    expect(toolbar).toContainElement(screen.getByRole("button", { name: "Slew + center" }));
    expect(toolbar).toContainElement(screen.getByRole("button", { name: "Plate solve" }));
    expect(toolbar).toContainElement(screen.getByRole("button", { name: "Sync" }));
    expect(screen.queryByLabelText("Desired deviation (arcsec)")).not.toBeInTheDocument();
  });

  it.each([
    ["parked", { at_park: true }, /arm is closed/i],
    ["at home", { at_home: true }, /arm is closed/i],
    ["on the horizon", { alt_deg: 0 }, /at or below the horizon/i],
    ["below the horizon", { alt_deg: -2 }, /at or below the horizon/i],
  ] as const)("disables plate solve when the scope is %s", (_label, mountOverrides, title) => {
    setMount(mountState(mountOverrides));
    stubMountFetch({});
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    expect(screen.getByRole("button", { name: "Plate solve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Plate solve" })).toHaveAttribute("title", expect.stringMatching(title));
  });

  it.each([null, 0.01, 45])("keeps plate solve available at a safe or unknown altitude (%s)", (altDeg) => {
    setMount(mountState({ alt_deg: altDeg }));
    stubMountFetch({});
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    expect(screen.getByRole("button", { name: "Plate solve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Plate solve" })).toHaveAttribute("title", "Plate solve the current camera frame");
  });

  it("renders nothing when there is no mount", () => {
    setMount(null);
    const { container } = renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when neither slew nor sync is supported", () => {
    setMount(mountState({ capabilities: { ...FULL_CAPS, can_slew_async: false, can_sync: false } }));
    const { container } = renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("recovers the last centering result from backend state after remount", () => {
    useConnectionStore.setState({
      snapshot: {
        mount: mountState({}),
        operations: [{
          id: "center-1",
          kind: "target_centering",
          status: "succeeded",
          message: "Completed.",
          progress_current: 1,
          progress_total: 3,
          result: CENTERED,
          error: null,
          idempotency_key: "request-1",
          cancellable: false,
          created_at: "2026-09-15T00:00:00Z",
          updated_at: "2026-09-15T00:01:00Z",
          finished_at: "2026-09-15T00:01:00Z",
        }],
      } as unknown as StateSnapshot,
      seq: 1,
    });
    stubMountFetch({});

    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    expect(screen.getByRole("status")).toHaveTextContent("Last centering converged to 0.24′");
  });

  it("sends J2000 to the server when the mount's own frame is topocentric", async () => {
    setMount(mountState({ equatorial_system: "topocentric" }));
    const calls = stubMountFetch({ center: { status: 200 } });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Slew + center" }));

    await waitFor(() => {
      expect(calls.find((c) => c.path === "/api/mount/center")?.body).toEqual({
        ra_hours: TARGET.raHoursJ2000,
        dec_deg: TARGET.decDegJ2000,
        tolerance_arcmin: 0.5,
        exposure_s: 1,
        gain: 200,
        max_attempts: 3,
      });
    });
  });

  it("slews directly when the mount is not parked", async () => {
    setMount(mountState({}));
    const calls = stubMountFetch({ center: { status: 200 } });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Slew + center" }));

    await waitFor(() => {
      expect(calls.some((c) => c.path === "/api/mount/center")).toBe(true);
    });
    expect(calls.find((c) => c.path === "/api/mount/center")?.body).toEqual({
      ra_hours: TARGET.raHoursJ2000,
      dec_deg: TARGET.decDegJ2000,
      tolerance_arcmin: 0.5,
      exposure_s: 1,
      gain: 200,
      max_attempts: 3,
    });
    // No confirmation needed, so unpark is never called.
    expect(calls.some((c) => c.path === "/api/mount/unpark")).toBe(false);
  });

  it("confirms before slewing a parked mount, offering unpark-and-slew", () => {
    setMount(mountState({ at_park: true }));
    stubMountFetch({ unpark: { status: 204 }, center: { status: 200 } });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Slew + center" }));

    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unpark and center" })).toBeInTheDocument();
    const popover = screen.getByTestId("target-action-popover");
    expect(popover).toHaveClass("absolute", "bottom-[calc(100%+0.5rem)]", "right-0");
    expect(popover.parentElement).toHaveClass("relative");
  });

  it("cancelling the confirmation issues neither unpark nor slew", () => {
    setMount(mountState({ at_home: true }));
    const calls = stubMountFetch({ unpark: { status: 204 }, center: { status: 200 } });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Slew + center" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it("confirming sends one atomic unpark-and-center intent", async () => {
    setMount(mountState({ at_park: true }));
    const calls = stubMountFetch({ unpark: { status: 204 }, center: { status: 200 } });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Slew + center" }));
    fireEvent.click(screen.getByRole("button", { name: "Unpark and center" }));

    await waitFor(() => {
      expect(calls.map((c) => c.path)).toEqual(["/api/mount/center"]);
    });
    expect(calls[0]?.body).toMatchObject({ unpark_if_needed: true });
  });

  it("shows the atomic workflow's unpark refusal verbatim", async () => {
    setMount(mountState({ at_park: true }));
    const calls = stubMountFetch({
      center: { status: 409, body: { detail: "cannot unpark: axis fault", device_code: 4001 } },
    });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Slew + center" }));
    fireEvent.click(screen.getByRole("button", { name: "Unpark and center" }));

    expect(await screen.findByText("cannot unpark: axis fault")).toBeInTheDocument();
    expect(calls.map((c) => c.path)).toEqual(["/api/mount/center"]);
    expect(calls[0]?.body).toMatchObject({ unpark_if_needed: true });
  });

  it("shows a slew 409's detail verbatim", async () => {
    setMount(mountState({}));
    stubMountFetch({ center: { status: 409, body: { detail: "below the horizon", device_code: 1279 } } });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Slew + center" }));

    expect(await screen.findByText("below the horizon")).toBeInTheDocument();
  });

  it("defaults to 30 arcseconds and reports the final plate-solve error", async () => {
    setMount(mountState({}));
    const calls = stubMountFetch({ center: { status: 200 } });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    expect(screen.getByLabelText("Desired deviation (arcsec)")).toHaveValue(30);
    fireEvent.click(screen.getByRole("button", { name: "Slew + center" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Centered to 0.24′ in 1 attempt(s).");
    expect(calls.find((c) => c.path === "/api/mount/center")?.body).toMatchObject({
      tolerance_arcmin: 0.5,
    });
  });

  it("converts an explicit arcsecond override to the API's arcminutes", async () => {
    setMount(mountState({}));
    const calls = stubMountFetch({ center: { status: 200 } });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.change(screen.getByLabelText("Desired deviation (arcsec)"), {
      target: { value: "45" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Slew + center" }));

    await screen.findByRole("status");
    expect(calls.find((c) => c.path === "/api/mount/center")?.body).toMatchObject({
      tolerance_arcmin: 0.75,
    });
  });

  it("plate-solves the current pointing with fast defaults and reports the result", async () => {
    setMount(mountState({}));
    const calls = stubMountFetch({ solve: { status: 200 } });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Plate solve" }));

    expect(await screen.findByText(/Solved at RA 10\.0000h, Dec 41\.200°, rotation 93\.50°/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/mount/polar-alignment/solve-here")?.body).toEqual({
      exposure_s: 1,
      gain: 200,
    });
  });

  it("explains the centering phases while the request is in flight", async () => {
    setMount(mountState({}));
    let finish: ((response: Response) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const { pathname } = new URL(url, "http://localhost");
      if (pathname === "/api/mount/center/status") {
        return Promise.resolve(jsonResponse(RUNNING_PROGRESS, 200, pathname));
      }
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }));
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Slew + center" }));

    const overlay = await screen.findByRole("dialog", { name: "Slew and center progress" });
    expect(screen.getByTestId("target-action-popover")).toContainElement(overlay);
    expect(overlay).toHaveTextContent("Attempt 1 of 3");
    expect(await screen.findByText(/Waiting for the mount to stop/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
    act(() => {
      finish?.(jsonResponse(CENTERED, 200, "/api/mount/center"));
    });
    expect(await screen.findByText(/Centered to 0\.24′ in 1 attempt/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("explains capture, download, and solve while plate solve is in flight", async () => {
    setMount(mountState({}));
    let finish: ((response: Response) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise<Response>((resolve) => {
      finish = resolve;
    })));
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Plate solve" }));

    expect(await screen.findByText(/Capturing a 1-second frame, downloading it/)).toBeInTheDocument();
    act(() => {
      finish?.(jsonResponse(SOLVED, 200, "/api/mount/polar-alignment/solve-here"));
    });
    expect(await screen.findByText(/Solved at RA 10\.0000h/)).toBeInTheDocument();
  });

  it("confirming Sync sends one atomic intent and names the assertion being made", async () => {
    setMount(mountState({ at_park: true }));
    const calls = stubMountFetch({ unpark: { status: 204 }, sync: { status: 204 } });
    renderWithQueryClient(<MountSlewSyncControls target={TARGET} />);

    fireEvent.click(screen.getByRole("button", { name: "Sync" }));
    expect(screen.getByText(/currently pointing at M31/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Unpark and sync" }));

    await waitFor(() => {
      expect(calls.map((c) => c.path)).toEqual(["/api/mount/sync"]);
    });
    expect(calls[0]?.body).toMatchObject({ unpark_if_needed: true });
  });
});
