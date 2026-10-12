// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pins the brief's named scenarios:
 * - a `None`/`null` `rms_residual_arcsec` never renders as "0.00";
 * - the direction control's sign actually reaches `step_deg`;
 * - a 409 refusal surfaces its `detail` verbatim, not a generic toast;
 * - the default start sends `start_point: "best"` with no coordinate fields;
 * - `ideal: false` renders its warnings, `ideal: null` renders none;
 * - solve-here shows solved vs. mount pointing and their separation, renders
 *   `null` mount fields as "unknown" rather than a bogus separation, and
 *   shows a refusal's `detail` verbatim.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  PlateSolveHereResponse,
  PolarAlignmentAssessment,
  PolarAlignmentResult,
  PolarAlignmentStartInfo,
  PolarAlignmentState,
  PolarRefinementResult,
} from "../../api/polarAlignment";
import { useConnectionStore } from "../../store/connectionStore";
import { usePolarDebugStore } from "../../store/polarDebugStore";
import { testSnapshot } from "../../test/fixtures";
import { renderWithQueryClient } from "../../test/queryClient";
import { WedgeSimulatorCard } from "../MountTab/WedgeSimulatorCard";
import { PolarAlignTab } from "./PolarAlignTab";
import { usePolarSimulationSession } from "./usePolarSimulationWorkflow";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useConnectionStore.getState().reset();
  usePolarDebugStore.setState({ enabled: false, simulatePolarAlignment: false, altitudeAdjustmentArcsec: 0, azimuthAdjustmentArcsec: 0, stepArcsec: 60 });
  usePolarSimulationSession.setState({ state: null, siteLatitude: 48.15, busy: false });
});

it("runs artificial captures and solves through the normal PA screen without device commands", async () => {
  const requests: { altitude_adjustment_deg: number; azimuth_adjustment_deg: number; point_count: number }[] = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: Request) => {
    const request = input;
    const pathname = new URL(request.url).pathname;
    if (pathname !== "/api/mount/polar-alignment/simulate") {
      throw new Error(`Unexpected hardware route: ${pathname}`);
    }
    const body = await request.json() as (typeof requests)[number];
    requests.push(body);
    return json({
      simulated: true, site_latitude_deg: 48.15, site_longitude_deg: 17.11,
      points: Array.from({ length: body.point_count }, (_, index) => ({
        index, when: "2026-10-02T20:00:00Z", ra_j2000_hours: 10 + index, dec_j2000_deg: 20,
      })),
      result: baseResult({
        total_error_deg: body.altitude_adjustment_deg === -1 ? 0.001 : 1.2,
        point_count: body.point_count, has_residual_check: body.point_count >= 5,
      }),
    }, 200, pathname);
  }));
  useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
  usePolarDebugStore.setState({ enabled: true });
  const pointStages = new Set<string>();
  const unsubscribe = usePolarSimulationSession.subscribe((session) => {
    for (const point of session.state?.points ?? []) pointStages.add(point.status);
  });
  const view = renderWithQueryClient(<><WedgeSimulatorCard /><PolarAlignTab /></>);

  fireEvent.click(screen.getByRole("button", { name: "Artificial sky" }));
  fireEvent.change(screen.getByLabelText("Points"), { target: { value: "5" } });
  fireEvent.click(screen.getByRole("button", { name: "Measure alignment" }));
  await waitFor(() => { expect(requests).toHaveLength(1); });
  expect(requests[0]).toEqual({ altitude_adjustment_deg: 0, azimuth_adjustment_deg: 0, point_count: 5 });
  expect(screen.getByText(/Artificial sky · no device commands/)).toBeInTheDocument();
  await screen.findByRole("button", { name: /Recalculate · 5 points/ }, { timeout: 6000 });
  expect(screen.getByText("Point 1")).toBeInTheDocument();
  expect(screen.getByText("Point 3")).toBeInTheDocument();
  expect(screen.getByText("Measurement 1")).toBeInTheDocument();
  expect(screen.getByText("Which knob, and how far")).toBeInTheDocument();
  expect(screen.getByText(/1.20° · SIMULATED/)).toBeInTheDocument();
  expect(pointStages).toEqual(new Set(["pending", "slewing", "capturing", "solving", "solved"]));

  view.unmount();
  renderWithQueryClient(<><WedgeSimulatorCard /><PolarAlignTab /></>);
  expect(screen.getByRole("button", { name: "Artificial sky" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByText("Measurement 1")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Step size"), { target: { value: "3600" } });
  fireEvent.click(screen.getByRole("button", { name: "Altitude ↓" }));
  fireEvent.click(screen.getByRole("button", { name: "Azimuth → East" }));
  fireEvent.click(screen.getByRole("button", { name: /Recalculate · 5 points/ }));
  await waitFor(() => { expect(requests).toHaveLength(2); });
  expect(requests[1]).toEqual({ altitude_adjustment_deg: -1, azimuth_adjustment_deg: 1, point_count: 5 });
  await screen.findByRole("button", { name: /Recalculate · 5 points/ }, { timeout: 6000 });
  expect(screen.getAllByText(/Recheck 2/)).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Real mount" }));
  expect(screen.queryByText(/Artificial sky · no device commands/)).not.toBeInTheDocument();
  unsubscribe();
}, 15_000);

interface StartCall {
  point_count: number;
  step_deg: number;
  exposure_s: number;
  gain: number | null;
  auto_refine: boolean;
  start_point: string;
  start_ra_hours?: number | null;
  start_dec_deg?: number | null;
  unpark_if_needed?: boolean;
}

function json(body: unknown, status: number, url: string): Response {
  const response = new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  Object.defineProperty(response, "url", { value: `http://localhost${url}` });
  return response;
}

/** `api/polarAlignment.ts` goes through the generated `openapi-fetch` client
 * (`client.ts`), which calls `globalThis.fetch(request)` with one `Request`
 * object rather than `(path, init)` — unlike `api/mount.ts`'s hand-rolled
 * `call()` that `JogPad.test.tsx` stubs. Routed the same way
 * `SchedulingTab.test.tsx` does for exactly this reason. */
function stubStart(respondStatus: number, detail?: string): { calls: () => StartCall[]; unparkCalls: () => number } {
  const calls: StartCall[] = [];
  let unparkCalls = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (input: Request | string, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(`http://localhost${input}`, init);
      const { pathname } = new URL(request.url);
      if (request.method === "POST" && pathname === "/api/mount/unpark") {
        unparkCalls += 1;
        return new Response(null, { status: 204 });
      }
      if (request.method === "POST" && pathname === "/api/mount/polar-alignment/start") {
        calls.push(JSON.parse(await request.text()) as StartCall);
        if (respondStatus >= 200 && respondStatus < 300) {
          return json({ status: "running" }, respondStatus, pathname);
        }
        return json({ detail: detail ?? "refused" }, respondStatus, pathname);
      }
      return json({ detail: `unexpected ${request.method} ${pathname}` }, 500, pathname);
    }),
  );
  return { calls: () => calls, unparkCalls: () => unparkCalls };
}

function stubRecheck(): { calls: () => unknown[] } {
  const calls: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (input: Request | string, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(`http://localhost${input}`, init);
      const { pathname } = new URL(request.url);
      if (request.method === "POST" && pathname === "/api/mount/polar-alignment/recheck") {
        calls.push(JSON.parse(await request.text()) as unknown);
        return json({ status: "running" }, 202, pathname);
      }
      return json({ detail: `unexpected ${request.method} ${pathname}` }, 500, pathname);
    }),
  );
  return { calls: () => calls };
}

/** Stubs `POST .../solve-here` the same way `stubStart` stubs `.../start` —
 * same single-`Request`-object shape, different path. */
function stubSolveHere(respondStatus: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((request: Request) => {
      const { pathname } = new URL(request.url);
      if (request.method === "POST" && pathname === "/api/mount/polar-alignment/solve-here") {
        return json(body, respondStatus, pathname);
      }
      return json({ detail: `unexpected ${request.method} ${pathname}` }, 500, pathname);
    }),
  );
}

function baseSolveHereResponse(overrides: Partial<PlateSolveHereResponse> = {}): PlateSolveHereResponse {
  return {
    ra_j2000_hours: 5.5,
    dec_j2000_deg: 45.0,
    date_obs: "2026-09-11T20:00:00Z",
    pixel_scale_arcsec: 1.2,
    rotation_deg: 0.3,
    solver: "astap",
    solver_version: "2024.10.15",
    mount_ra_j2000_hours: 5.5,
    mount_dec_j2000_deg: 45.0,
    mount_altitude_deg: 48.1,
    mount_azimuth_deg: 179.4,
    ...overrides,
  };
}

describe("PolarAlignTab — solve here", () => {
  it("shows solved and mount pointing plus their separation on success", async () => {
    stubSolveHere(
      200,
      baseSolveHereResponse({
        ra_j2000_hours: 5.5,
        dec_j2000_deg: 45.0,
        mount_ra_j2000_hours: 5.5 + 1 / 60, // 1 minute of RA off, at dec 45 — a known non-zero separation
        mount_dec_j2000_deg: 45.0,
      }),
    );
    useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: "Solve here" }));

    await screen.findByText(/Separation from mount pointing:/);
    expect(screen.getByText(/astap 2024\.10\.15/)).toBeInTheDocument();
    // Solved and mount RA differ by 1 RA-minute at dec 45°, a real non-zero separation.
    expect(screen.queryByText(/Separation from mount pointing: unknown/)).not.toBeInTheDocument();
  });

  it("renders 'unknown' for mount fields the mount has not reported yet, never a bogus separation", async () => {
    stubSolveHere(
      200,
      baseSolveHereResponse({
        mount_ra_j2000_hours: null,
        mount_dec_j2000_deg: null,
        mount_altitude_deg: null,
        mount_azimuth_deg: null,
      }),
    );
    useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: "Solve here" }));

    await screen.findByText(/Separation from mount pointing:/);
    expect(screen.getByText(/mount has not reported a position yet/)).toBeInTheDocument();
    expect(screen.getAllByText(/unknown/).length).toBeGreaterThan(0);
  });

  it("shows the backend's own refusal detail verbatim on a 409", async () => {
    const detail =
      "a sweep or refinement loop is already driving the camera: solve-here needs it to itself";
    stubSolveHere(409, { detail });
    useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: "Solve here" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(detail);
  });
});

function baseResult(overrides: Partial<PolarAlignmentResult> = {}): PolarAlignmentResult {
  return {
    axis_altitude_deg: 48.5,
    axis_azimuth_deg: 359.2,
    altitude_error_deg: -0.2,
    azimuth_error_deg: 0.15,
    azimuth_error_sky_deg: 0.1,
    total_error_deg: 0.25,
    altitude_correction_deg: 0.2,
    azimuth_correction_deg: -0.15,
    circle_radius_deg: 41.3,
    arc_span_deg: 60,
    rms_residual_arcsec: null,
    max_residual_arcsec: null,
    residual_dof: 0,
    noise_amplification: 1.4,
    axis_uncertainty_arcsec: 42,
    point_count: 3,
    epoch: "2026-09-11T20:00:00Z",
    refraction_applied: true,
    has_residual_check: false,
    ...overrides,
  };
}

function baseLiveResult(overrides: Partial<PolarRefinementResult> = {}): PolarRefinementResult {
  return {
    axis_altitude_deg: 48.2,
    axis_azimuth_deg: 359.8,
    altitude_error_deg: -0.03,
    azimuth_error_deg: 0.03,
    azimuth_error_sky_deg: 0.02,
    total_error_deg: 0.04,
    altitude_correction_deg: 0.03,
    azimuth_correction_deg: -0.03,
    axis_uncertainty_arcsec: 40,
    epoch: new Date().toISOString(),
    altitude_step_deg: 0.01,
    azimuth_step_deg: 0.01,
    axis_shift_deg: 0.02,
    boresight_shift_deg: 0.02,
    elapsed_seconds: 10,
    sweep_age_seconds: 90,
    refinement_uncertainty_arcsec: 20,
    noise_amplification: 1,
    trusted: true,
    warnings: [],
    ...overrides,
  };
}

function completedWithLive(result: PolarRefinementResult | null, status: "running" | "stopped" = "running"): PolarAlignmentState {
  return {
    status: "completed",
    run_kind: "measure",
    started_at: new Date().toISOString(),
    ended_at: new Date().toISOString(),
    exposure_started_at: null,
    exposure_duration_s: null,
    current_point_index: null,
    preflight_reason: null,
    points: [],
    result: baseResult(),
    start: null,
    assessment: baseAssessment(),
    error: null,
    refinement: {
      status,
      started_at: new Date().toISOString(),
      ended_at: status === "stopped" ? new Date().toISOString() : null,
      stage: status === "running" ? "capturing" : null,
      exposure_started_at: null,
      exposure_duration_s: null,
      iteration: result === null ? 0 : 1,
      degenerate_count: 0,
      last_degenerate_reason: null,
      error: null,
      result,
    },
  };
}

function baseAssessment(overrides: Partial<PolarAlignmentAssessment> = {}): PolarAlignmentAssessment {
  return {
    verdict: "recheck",
    message: "One low-error sweep is not confirmation. Run an independent recheck.",
    current_total_error_arcmin: 1.2,
    previous_total_error_arcmin: null,
    current_axis_uncertainty_arcsec: 42,
    previous_axis_uncertainty_arcsec: null,
    axis_separation_arcmin: null,
    max_error_arcmin: 2,
    max_axis_separation_arcmin: 2,
    max_axis_uncertainty_arcsec: 120,
    ...overrides,
  };
}

describe("PolarAlignTab — error history", () => {
  it("shows both axes and total for every completed sweep using the result colors", () => {
    const snapshot = testSnapshot({
      polar_alignment: {
        status: "completed",
        run_kind: "recheck",
        started_at: "2026-09-11T20:09:00Z",
        ended_at: "2026-09-11T20:10:00Z",
        exposure_started_at: null,
        exposure_duration_s: null,
        current_point_index: null,
        preflight_reason: null,
        points: [],
        result: baseResult({ total_error_deg: 0.1 }),
        history: [
          {
            run_kind: "measure",
            completed_at: "2026-09-11T20:00:00Z",
            altitude_error_deg: 20 / 3600,
            azimuth_error_deg: 40 / 3600,
            total_error_deg: 1 / 60,
          },
          {
            run_kind: "recheck",
            completed_at: "2026-09-11T20:05:00Z",
            altitude_error_deg: 1 / 60,
            azimuth_error_deg: 2 / 60,
            total_error_deg: 3 / 60,
          },
          {
            run_kind: "recheck",
            completed_at: "2026-09-11T20:10:00Z",
            altitude_error_deg: 4 / 60,
            azimuth_error_deg: 5 / 60,
            total_error_deg: 6 / 60,
          },
        ],
        start: null,
        assessment: null,
        error: null,
      },
    });
    useConnectionStore.setState({ snapshot, seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    const list = screen.getByRole("list", { name: "Polar alignment error history" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveClass("border-danger");
    expect(rows[1]).toHaveClass("border-warning");
    expect(rows[2]).toHaveClass("border-success");

    expect(rows[0]).toHaveTextContent("Recheck 3");
    expect(rows[1]).toHaveTextContent("Recheck 2");
    expect(rows[2]).toHaveTextContent("Measurement 1");
    expect(rows[0]).toHaveTextContent(/Altitude0\.07°Azimuth0\.08°Total0\.10°/);
    expect(rows[1]).toHaveTextContent(/Altitude0\.02°Azimuth0\.03°Total0\.05°/);
    expect(rows[2]).toHaveTextContent(/Altitude0\.01°Azimuth0\.01°Total0\.02°/);
  });
});

describe("PolarAlignTab — result readout honesty", () => {
  it("never renders a null rms_residual_arcsec as 0.00", () => {
    const snapshot = testSnapshot({
      polar_alignment: {
        status: "completed",
        run_kind: "measure",
        started_at: "2026-09-11T19:59:00Z",
        ended_at: "2026-09-11T20:00:00Z",
        exposure_started_at: null,
        exposure_duration_s: null,
        current_point_index: null,
        preflight_reason: null,
        points: [],
        result: baseResult(),
        start: null,
        assessment: null,
        error: null,
      },
    });
    useConnectionStore.setState({ snapshot, seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    // The bug this pins: `formatArcsec(null)` rendering as "0.0″" (a
    // perfect-looking residual) rather than "unknown" plus the no-check
    // explanation. Matched on the arcsec mark specifically — "60.00°" (the
    // arc span, a real and correct value) also contains the substring
    // "0.00" and would otherwise make this assertion pass for the wrong
    // reason.
    expect(screen.queryByText(/0\.0″/)).not.toBeInTheDocument();
    expect(screen.getByText(/no residual check/i)).toBeInTheDocument();
    expect(screen.getByText(/degrees of freedom: 0/i)).toBeInTheDocument();
  });

  it("shows the real RMS/max/dof when a residual check exists", () => {
    const snapshot = testSnapshot({
      polar_alignment: {
        status: "completed",
        run_kind: "measure",
        started_at: "2026-09-11T19:59:00Z",
        ended_at: "2026-09-11T20:00:00Z",
        exposure_started_at: null,
        exposure_duration_s: null,
        current_point_index: null,
        preflight_reason: null,
        points: [],
        result: baseResult({
          point_count: 5,
          residual_dof: 2,
          rms_residual_arcsec: 12.3,
          max_residual_arcsec: 24.6,
          has_residual_check: true,
        }),
        start: null,
        assessment: null,
        error: null,
      },
    });
    useConnectionStore.setState({ snapshot, seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    expect(screen.getByText(/RMS residual 12\.3″/)).toBeInTheDocument();
    expect(screen.getByText(/max 24\.6″/)).toBeInTheDocument();
  });
});

describe("PolarAlignTab — live adjustment", () => {
  it("stops the continuous loop on command", async () => {
    let stops = 0;
    vi.stubGlobal("fetch", vi.fn().mockImplementation((request: Request) => {
      const { pathname } = new URL(request.url);
      if (pathname === "/api/mount/polar-alignment/refine/stop") {
        stops += 1;
        return new Response(null, { status: 204 });
      }
      return json({ detail: "unexpected request" }, 500, pathname);
    }));
    useConnectionStore.setState({ snapshot: testSnapshot({ polar_alignment: completedWithLive(baseLiveResult()) }), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: "Stop live adjustment" }));

    await waitFor(() => { expect(stops).toBe(1); });
  });

  it("withholds correction arrows even when a legacy live result claims trust", () => {
    useConnectionStore.setState({ snapshot: testSnapshot({ polar_alignment: completedWithLive(baseLiveResult()) }), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    expect(screen.getByText(/single field cannot verify polar alignment/)).toBeInTheDocument();
    expect(screen.queryByText("Altitude", { selector: "p" })).not.toBeInTheDocument();
    expect(screen.queryByText("Azimuth", { selector: "p" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New measurement" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Solve here" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Recheck ·/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop live adjustment" })).toBeEnabled();
  });

  it("shows the single-field warning and solver warning without a correction", () => {
    const untrusted = completedWithLive(baseLiveResult({ trusted: false, warnings: ["Reference field is ill-conditioned"] }));
    useConnectionStore.setState({ snapshot: testSnapshot({ polar_alignment: untrusted }), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    expect(screen.getByText(/single field cannot verify polar alignment/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Reference field is ill-conditioned");
    expect(screen.queryByText("Altitude", { selector: "p" })).not.toBeInTheDocument();
  });

  it("does not present an ended live session as a current correction", () => {
    useConnectionStore.setState({ snapshot: testSnapshot({ polar_alignment: completedWithLive(baseLiveResult(), "stopped") }), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    expect(screen.getByText(/Run a new measurement to verify/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start live adjustment" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Recalculate ·/ })).toBeEnabled();
  });
});

describe("PolarAlignTab — direction sets step_deg's sign", () => {
  it("starts with a three-point, independent-sweep measurement", async () => {
    const { calls } = stubStart(202);
    useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: "Measure alignment" }));

    await waitFor(() => {
      expect(calls()).toHaveLength(1);
    });
    expect(calls()[0]).toMatchObject({ point_count: 3, step_deg: 30, exposure_s: 2, auto_refine: false });
    expect(screen.queryByRole("button", { name: "Start live adjustment" })).not.toBeInTheDocument();
  });

  it("sends a positive step_deg for East", async () => {
    const { calls } = stubStart(202);
    useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: /East/ }));
    fireEvent.click(screen.getByRole("button", { name: "Measure alignment" }));

    await waitFor(() => {
      expect(calls()).toHaveLength(1);
    });
    expect(calls()[0]?.step_deg).toBeGreaterThan(0);
  });

  it("sends a negative step_deg for West", async () => {
    const { calls } = stubStart(202);
    useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: /West/ }));
    fireEvent.click(screen.getByRole("button", { name: "Measure alignment" }));

    await waitFor(() => {
      expect(calls()).toHaveLength(1);
    });
    expect(calls()[0]?.step_deg).toBeLessThan(0);
  });
});

describe("PolarAlignTab — closed-arm safety confirmation", () => {
  it("confirms and sends the unpark prerequisite when Home but not Parked", async () => {
    const { calls, unparkCalls } = stubStart(202);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const snapshot = testSnapshot({
      mount: {
        polled_at: "2026-09-11T20:00:00Z",
        stale: false,
        ra_hours: 2.6,
        dec_deg: -90,
        ra_j2000_h: null,
        dec_j2000_deg: null,
        site_latitude_deg: 48.7,
        site_longitude_deg: 17.8,
        alt_deg: -48.7,
        az_deg: 0,
        sidereal_time_hours: 20.6,
        tracking: false,
        tracking_rate: "sidereal",
        at_park: false,
        at_home: true,
        slewing: false,
        equatorial_system: "topocentric",
        jog_primary_active: false,
        jog_secondary_active: false,
        jog_lease_expires_at: null,
        capabilities: null,
      },
    });
    useConnectionStore.setState({ snapshot, seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: "Measure alignment" }));

    await waitFor(() => {
      expect(calls()).toHaveLength(1);
    });
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("at Home"));
    expect(unparkCalls()).toBe(0);
    expect(calls()[0]?.unpark_if_needed).toBe(true);
  });
});

describe("PolarAlignTab — a 409 refusal is explained, not a generic toast", () => {
  it("shows the backend's own detail verbatim", async () => {
    const detail =
      "the mount's AlignmentMode is 0, not 1 (algPolar): a polar-alignment measurement is only " +
      "meaningful on a mount in polar/EQ alignment mode";
    stubStart(409, detail);
    useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: "Measure alignment" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(detail);
  });
});

describe("PolarAlignTab — per-point progress", () => {
  it("does not mistake the normal slew and first capture for a retry", () => {
    const state: PolarAlignmentState = {
      status: "running",
      run_kind: "measure",
      started_at: "2026-09-11T20:00:00Z",
      ended_at: null,
      exposure_started_at: null,
      exposure_duration_s: null,
      current_point_index: 1,
      preflight_reason: null,
      points: [
        {
          index: 0,
          status: "solved",
          target_ra_j2000_h: 1,
          target_dec_j2000_deg: 45,
          planned_altitude_deg: 50,
          actual_altitude_deg: 49.9,
          actual_azimuth_deg: 180,
          mount_slewing: false,
          mount_tracking: true,
          solved_ra_j2000_h: 1.001,
          solved_dec_j2000_deg: 45.001,
          attempt: 2,
          error: null,
        },
        {
          index: 1,
          status: "solving",
          target_ra_j2000_h: 3,
          target_dec_j2000_deg: 45,
          planned_altitude_deg: 45,
          actual_altitude_deg: 45.1,
          actual_azimuth_deg: 195,
          mount_slewing: false,
          mount_tracking: true,
          solved_ra_j2000_h: null,
          solved_dec_j2000_deg: null,
          attempt: 3,
          error: "thin cloud: could not solve",
        },
      ],
      result: null,
      start: null,
      assessment: null,
      error: null,
    };
    useConnectionStore.setState({ snapshot: testSnapshot({ polar_alignment: state }), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    expect(screen.queryByText(/attempt/i)).not.toBeInTheDocument();
    expect(screen.getByText("1 retry")).toBeInTheDocument();
    expect(screen.getByText(/thin cloud/)).toBeInTheDocument();
  });
});

describe("PolarAlignTab — independent recheck", () => {
  it("offers five-point verification after a three-point recalculation", async () => {
    const { calls } = stubRecheck();
    const snapshot = testSnapshot({
      polar_alignment: {
        status: "completed",
        run_kind: "recheck",
        started_at: "2026-09-11T20:00:00Z",
        ended_at: "2026-09-11T20:01:00Z",
        exposure_started_at: null,
        exposure_duration_s: null,
        current_point_index: null,
        preflight_reason: null,
        points: [],
        result: baseResult({ point_count: 3, total_error_deg: 0.02 }),
        start: null,
        error: null,
        assessment: baseAssessment(),
      },
    });
    useConnectionStore.setState({ snapshot, seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: "Verify · 5 points" }));
    await waitFor(() => {
      expect(calls()).toEqual([{ point_count: 5, exposure_s: 2, gain: 200, apply_refraction: true }]);
    });
  });

  it("rechecks with the last point count", async () => {
    const { calls } = stubRecheck();
    const snapshot = testSnapshot({
      polar_alignment: {
        status: "completed",
        run_kind: "measure",
        started_at: "2026-09-11T20:00:00Z",
        ended_at: "2026-09-11T20:01:00Z",
        exposure_started_at: null,
        exposure_duration_s: null,
        current_point_index: null,
        preflight_reason: null,
        points: [],
        result: baseResult({ point_count: 5, total_error_deg: 0.02 }),
        start: null,
        error: null,
        assessment: baseAssessment(),
      },
    });
    useConnectionStore.setState({ snapshot, seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    expect(screen.getByText(/one low-error sweep is not confirmation/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Recalculate · 5 points" }));

    await waitFor(() => {
      expect(calls()).toEqual([{ point_count: 5, exposure_s: 2, gain: 200, apply_refraction: true }]);
    });
  });

  it("shows disagreement between independent fitted axes instead of averaging it", () => {
    const snapshot = testSnapshot({
      polar_alignment: {
        status: "completed",
        run_kind: "recheck",
        started_at: "2026-09-11T20:00:00Z",
        ended_at: "2026-09-11T20:01:00Z",
        exposure_started_at: null,
        exposure_duration_s: null,
        current_point_index: null,
        preflight_reason: null,
        points: [],
        result: baseResult({ point_count: 5, total_error_deg: 0.015 }),
        start: null,
        error: null,
        assessment: baseAssessment({
          verdict: "inconsistent",
          message: "The two low-error sweeps disagree. Do not average them; repeat the measurement.",
          previous_total_error_arcmin: 1.1,
          previous_axis_uncertainty_arcsec: 38,
          axis_separation_arcmin: 5.4,
        }),
      },
    });
    useConnectionStore.setState({ snapshot, seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    expect(screen.getByText(/do not average them/i)).toBeInTheDocument();
    expect(screen.getByText(/Inter-sweep axis separation 5\.40′/)).toBeInTheDocument();
    expect(screen.getByText(/INCONSISTENT/)).toBeInTheDocument();
  });
});

function baseStart(overrides: Partial<PolarAlignmentStartInfo> = {}): PolarAlignmentStartInfo {
  return {
    mode: "best",
    step_deg: 15,
    arc_span_deg: 60,
    point_altitudes_deg: [42.1, 48.2, 52.4, 48.0, 41.8],
    start_altitude_deg: 42.1,
    start_azimuth_deg: 180.5,
    meridian_altitude_deg: 60.2,
    worst_altitude_deg: 35.0,
    declination_offset_deg: 5.0,
    circle_radius_deg: 40.0,
    sweep_amplification: 1.1,
    ideal: true,
    warnings: [],
    ...overrides,
  };
}

describe("PolarAlignTab — start point defaults to the best-conditioned field", () => {
  it("sends start_point 'best' with no coordinate fields when the run is started unchanged", async () => {
    const { calls } = stubStart(202);
    useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    // "Best field" is the default toggle state — nothing to click.
    fireEvent.click(screen.getByRole("button", { name: "Measure alignment" }));

    await waitFor(() => {
      expect(calls()).toHaveLength(1);
    });
    const call = calls()[0];
    expect(call?.start_point).toBe("best");
    expect(call).not.toHaveProperty("start_ra_hours");
    expect(call).not.toHaveProperty("start_dec_deg");
  });

  it("sends the entered coordinates only for Custom, under start_point 'explicit'", async () => {
    const { calls } = stubStart(202);
    useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: "Custom" }));
    fireEvent.change(screen.getByLabelText(/Start RA/), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText(/Start Dec/), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "Measure alignment" }));

    await waitFor(() => {
      expect(calls()).toHaveLength(1);
    });
    const call = calls()[0];
    expect(call?.start_point).toBe("explicit");
    expect(call?.start_ra_hours).toBe(5);
    expect(call?.start_dec_deg).toBe(20);
  });

  it("applies the recorded Seestar app field as a relative 3-by-25-degree preset", async () => {
    const { calls } = stubStart(202);
    useConnectionStore.setState({ snapshot: testSnapshot(), seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    fireEvent.click(screen.getByRole("button", { name: "Seestar app field" }));
    fireEvent.click(screen.getByRole("button", { name: "Measure alignment" }));

    await waitFor(() => {
      expect(calls()).toHaveLength(1);
    });
    expect(calls()[0]).toMatchObject({
      point_count: 3,
      step_deg: 25,
      start_point: "seestar",
    });
    expect(calls()[0]).not.toHaveProperty("start_ra_hours");
    expect(calls()[0]).not.toHaveProperty("start_dec_deg");
  });
});

describe("PolarAlignTab — the chosen start field is shown, compromise or not", () => {
  it("shows altitude/azimuth and the warnings when ideal is false", () => {
    const snapshot = testSnapshot({
      polar_alignment: {
        status: "completed",
        run_kind: "measure",
        started_at: "2026-09-11T19:59:00Z",
        ended_at: "2026-09-11T20:00:00Z",
        exposure_started_at: null,
        exposure_duration_s: null,
        current_point_index: null,
        preflight_reason: null,
        points: [],
        result: null,
        start: baseStart({
          ideal: false,
          warnings: ["altitude floor bound: worst point would otherwise be 8.0° above the horizon"],
        }),
        assessment: null,
        error: null,
      },
    });
    useConnectionStore.setState({ snapshot, seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    expect(screen.getByText(/altitude 42\.10°/)).toBeInTheDocument();
    expect(screen.getByText(/azimuth 180\.50°/)).toBeInTheDocument();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/compromise field/i);
    expect(alert).toHaveTextContent(/altitude floor bound/);
  });

  it("renders no warning when ideal is null (current/explicit: no computed geometry)", () => {
    const snapshot = testSnapshot({
      polar_alignment: {
        status: "completed",
        run_kind: "measure",
        started_at: "2026-09-11T19:59:00Z",
        ended_at: "2026-09-11T20:00:00Z",
        exposure_started_at: null,
        exposure_duration_s: null,
        current_point_index: null,
        preflight_reason: null,
        points: [],
        result: null,
        start: {
          mode: "current",
          step_deg: 15,
          arc_span_deg: 60,
          start_altitude_deg: null,
          start_azimuth_deg: null,
          meridian_altitude_deg: null,
          worst_altitude_deg: null,
          declination_offset_deg: null,
          circle_radius_deg: null,
          sweep_amplification: null,
          ideal: null,
          warnings: [],
        },
        assessment: null,
        error: null,
      },
    });
    useConnectionStore.setState({ snapshot, seq: 1 } as never);
    renderWithQueryClient(<PolarAlignTab />);

    expect(screen.getByText(/Last run started from/)).toHaveTextContent("current position");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/compromise field/i)).not.toBeInTheDocument();
  });
});
