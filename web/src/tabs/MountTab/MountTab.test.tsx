// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pins the tab's brief-level requirements that aren't already covered by
 * `JogPad.test.tsx`'s release-safety tests:
 * - a `null` field renders as "unknown", never a numeric-looking `0`;
 * - a 409's `detail` (the Park-below-horizon refusal) is shown verbatim;
 * - a stale reading is visibly flagged, not rendered as if it were live;
 * - a control the device's `capabilities` says it does not have is absent,
 *   not merely disabled;
 * - `unpark` is never worded as "open arm".
 *
 * Exercised against the real `useConnectionStore` + `api/mount.ts`, not a
 * mocked component tree — `StateSnapshot.mount` is not in the generated
 * schema yet (see `api/mount.ts`'s header), so it is attached with a local
 * cast the same way that file reads it back.
 */
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MountCapabilities, MountState } from "../../api/mount";
import type { CaptureState } from "../../api/capture";
import { useConnectionStore } from "../../store/connectionStore";
import type { StateSnapshot } from "../../api/types";
import { testSnapshot } from "../../test/fixtures";
import { renderWithQueryClient } from "../../test/queryClient";
import { MountTab } from "./MountTab";

vi.mock("../Capture/FocuserPanel", () => ({ FocuserPanel: () => <p>Focuser</p> }));
vi.mock("../Capture/FilterWheelPanel", () => ({ FilterWheelPanel: () => <p>Filter wheel</p> }));
vi.mock("../Capture/DewHeaterPanel", () => ({ DewHeaterPanel: () => <p>Dew heater</p> }));
vi.mock("./DitheringPanel", () => ({ DitheringPanel: () => <p>Dithering</p> }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useConnectionStore.getState().reset();
  window.location.hash = "";
});

const fullCapabilities: MountCapabilities = {
  can_slew_async: true,
  can_sync: true,
  can_park: true,
  can_unpark: true,
  can_find_home: true,
  can_set_tracking: true,
  can_move_primary_axis: true,
  can_move_secondary_axis: true,
  axis_rate_max_deg_s: 6,
  tracking_rates: ["sidereal", "lunar", "solar"],
};

function mountState(overrides: Partial<MountState> = {}): MountState {
  return {
    polled_at: "2026-09-10T07:00:00Z",
    stale: false,
    ra_hours: 12.34,
    dec_deg: 45.6,
    alt_deg: 30.1,
    az_deg: 180,
    sidereal_time_hours: 8.06,
    tracking: true,
    tracking_rate: "sidereal",
    at_park: false,
    at_home: true,
    slewing: false,
    equatorial_system: "topocentric",
    capabilities: fullCapabilities,
    ...overrides,
  };
}

function setMountSnapshot(mount: MountState | null, connected = true) {
  const base = testSnapshot();
  const snapshot = {
    ...base,
    link: {
      ...base.link,
      status: connected ? "ready" : "disconnected",
      control_connected: connected,
    },
    mount,
  } as StateSnapshot & { mount: MountState | null };
  useConnectionStore.setState({ snapshot, seq: 1 });
}

function setDitheringSnapshot(mount: MountState): void {
  const base = testSnapshot();
  const capture: CaptureState = {
    in_progress: true,
    last_frame_id: 4,
    last_captured_at: "2026-09-12T22:00:00Z",
    last_error: null,
    folder_configured: true,
    stage: "dithering",
    frame_type: "light",
  };
  useConnectionStore.setState({
    snapshot: { ...base, mount, capture } as unknown as StateSnapshot,
    seq: 2,
  });
}

function json(body: unknown, status: number): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: status === 204 ? undefined : { "content-type": "application/json" },
  });
}

/** Routes every `/api/mount/*` call; nothing in these tests needs a real
 * device behind it. */
function stubMountApi(
  handlers: Record<string, (request: Request) => Response | Promise<Response>> = {},
) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(new URL(String(input), "http://localhost"), init);
      const pathname = new URL(request.url).pathname;
      const handler = handlers[pathname];
      if (handler !== undefined) return Promise.resolve(handler(request));
      if (request.method === "PUT" || request.method === "POST") return Promise.resolve(json(null, 204));
      return Promise.resolve(json({ detail: `unexpected request to ${pathname}` }, 500));
    }),
  );
}

describe("MountTab", () => {
  it("shows the eight Equipment cards in a responsive four-column grid", () => {
    stubMountApi();
    setMountSnapshot(mountState());
    renderWithQueryClient(<MountTab />);

    const grid = screen.getByLabelText("Equipment controls");
    expect(grid).toHaveClass("grid-cols-1", "md:grid-cols-2", "xl:grid-cols-4");
    for (const label of ["POINTING", "JOG", "TRACKING", "PARK / HOME", "Focuser", "Filter wheel", "Dew heater", "Dithering"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.queryByText("Slew / sync")).not.toBeInTheDocument();
    expect(screen.queryByText("Center after drift")).not.toBeInTheDocument();
  });

  it("scrolls and focuses the exact control card requested by the dashboard", () => {
    const scrollIntoView = vi.fn();
    vi.stubGlobal("scrollTo", vi.fn());
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    window.location.hash = "#/mount?panel=dithering";
    stubMountApi();
    setMountSnapshot(mountState());

    renderWithQueryClient(<MountTab />);

    const panel = document.querySelector<HTMLElement>('[data-control-panel="dithering"]');
    expect(panel).toHaveFocus();
    expect(panel).toHaveClass("ring-2", "ring-accent");
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
  });

  it("shows authoritative dithering status while the capture loop moves the mount", () => {
    stubMountApi();
    setDitheringSnapshot(mountState());
    renderWithQueryClient(<MountTab />);

    expect(screen.getByText("Dithering mount…")).toBeInTheDocument();
    expect(screen.getByText(/next loop exposure waits/i)).toBeInTheDocument();
  });

  it("keeps STOP reachable when there is no mount reading at all", () => {
    // STOP moved into the jog pad's centre, and the pad only renders once a
    // reading exists — so this asserts the one case that regression would
    // have silently taken away. A poll gone quiet mid-slew is exactly when a
    // stop is wanted, and "no reading" never means "not moving".
    stubMountApi();
    setMountSnapshot(null);
    renderWithQueryClient(<MountTab />);
    expect(screen.getByRole("button", { name: "STOP" })).toBeEnabled();
  });

  it("shows 'unknown' for a null field rather than 0", () => {
    stubMountApi();
    setMountSnapshot(mountState({ alt_deg: null, az_deg: null, dec_deg: null }));
    renderWithQueryClient(<MountTab />);

    expect(screen.getAllByText("unknown").length).toBeGreaterThan(0);
    // Never a bare "0.0°" standing in for the unread altitude/azimuth.
    expect(screen.queryByText("0.0°")).not.toBeInTheDocument();
  });

  it("shows the Park refusal's 409 detail verbatim, including the device's reason", async () => {
    stubMountApi({
      "/api/mount/park": () =>
        json({ detail: "Park refused: target is below the horizon", device_code: 1279 }, 409),
    });
    setMountSnapshot(mountState());
    renderWithQueryClient(<MountTab />);

    fireEvent.click(screen.getByRole("button", { name: "Park" }));
    expect(await screen.findByText("Park refused: target is below the horizon")).toBeInTheDocument();
  });

  it("flags a reading whose polled_at is far in the past as stale", () => {
    stubMountApi();
    setMountSnapshot(mountState({ polled_at: "2020-01-01T00:00:00Z", stale: false }));
    renderWithQueryClient(<MountTab />);

    expect(screen.getByText(/^Stale/)).toBeInTheDocument();
  });

  it("trusts the server's own stale flag even when polled_at looks recent", () => {
    stubMountApi();
    setMountSnapshot(mountState({ polled_at: new Date().toISOString(), stale: true }));
    renderWithQueryClient(<MountTab />);

    expect(screen.getByText(/^Stale/)).toBeInTheDocument();
  });

  it("treats a mount with no successful poll yet (polled_at: null) as stale, not as age 0", () => {
    stubMountApi();
    setMountSnapshot(mountState({ polled_at: null, stale: true }));
    renderWithQueryClient(<MountTab />);

    expect(screen.getByText(/^Stale/)).toBeInTheDocument();
    expect(screen.getByText(/unknown age/)).toBeInTheDocument();
  });

  it("omits park/unpark/find-home controls the device does not report", () => {
    stubMountApi();
    setMountSnapshot(
      mountState({
        capabilities: {
          ...fullCapabilities,
          can_park: false,
          can_unpark: false,
          can_find_home: false,
        },
      }),
    );
    renderWithQueryClient(<MountTab />);

    expect(screen.queryByRole("button", { name: "Park" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Unpark/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Find home" })).not.toBeInTheDocument();
  });

  it("never words unpark as opening the arm — it only permits motion", () => {
    stubMountApi();
    setMountSnapshot(mountState());
    renderWithQueryClient(<MountTab />);

    const unpark = screen.getByRole("button", { name: /Unpark/ });
    expect(unpark).toHaveTextContent(/permit motion/i);
    expect(screen.queryByText(/open arm/i)).not.toBeInTheDocument();
  });

  it("shows distinct icons for park, unpark, and find home", () => {
    stubMountApi();
    setMountSnapshot(mountState());
    renderWithQueryClient(<MountTab />);

    expect(screen.getByRole("button", { name: "Park" }).querySelector(".lucide-circle-parking")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Unpark/ }).querySelector(".lucide-circle-parking-off")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find home" }).querySelector(".lucide-map-pin-house")).toBeInTheDocument();
  });

  it("shows Parking with an adjacent red Stop until the requested motion is stopped", async () => {
    let stopCalls = 0;
    stubMountApi({
      "/api/mount/stop": () => {
        stopCalls += 1;
        return json(null, 204);
      },
    });
    setMountSnapshot(mountState({ at_home: false }));
    renderWithQueryClient(<MountTab />);

    fireEvent.click(screen.getByRole("button", { name: "Park" }));
    expect(await screen.findByRole("button", { name: "Parking…" })).toHaveAttribute("aria-busy", "true");
    const stop = screen.getByRole("button", { name: "Stop mount motion" });
    await waitFor(() => { expect(stop).toBeEnabled(); });
    expect(stop).toHaveClass("!bg-danger");

    act(() => { setMountSnapshot(mountState({ at_home: false, slewing: true })); });
    expect(screen.getByRole("button", { name: "Parking…" })).toBeInTheDocument();
    fireEvent.click(stop);
    await waitFor(() => { expect(stopCalls).toBe(1); });
    expect(await screen.findByRole("button", { name: "Park" })).toBeEnabled();
  });

  it("keeps Parking active through settled polls until parked is yes", async () => {
    stubMountApi();
    setMountSnapshot(mountState({ at_home: false, at_park: false, polled_at: "2026-09-15T11:00:00Z" }));
    renderWithQueryClient(<MountTab />);

    fireEvent.click(screen.getByRole("button", { name: "Park" }));
    expect(await screen.findByRole("button", { name: "Parking…" })).toBeInTheDocument();

    act(() => {
      setMountSnapshot(mountState({
        at_home: false,
        at_park: false,
        slewing: false,
        polled_at: "2026-09-15T11:00:01Z",
      }));
    });
    expect(screen.getByRole("button", { name: "Parking…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop mount motion" })).toBeInTheDocument();

    act(() => {
      setMountSnapshot(mountState({
        at_home: false,
        at_park: true,
        slewing: false,
        polled_at: "2026-09-15T11:00:02Z",
      }));
    });
    expect(await screen.findByRole("button", { name: "Park" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Stop mount motion" })).not.toBeInTheDocument();
  });

  it("keeps Finding home active until mount telemetry reaches home", async () => {
    stubMountApi();
    setMountSnapshot(mountState({ at_home: false }));
    renderWithQueryClient(<MountTab />);

    fireEvent.click(screen.getByRole("button", { name: "Find home" }));
    expect(await screen.findByRole("button", { name: "Finding home…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop mount motion" })).toBeInTheDocument();

    act(() => { setMountSnapshot(mountState({ at_home: false, slewing: true })); });
    act(() => {
      setMountSnapshot(mountState({
        at_home: false,
        slewing: false,
        polled_at: "2026-09-15T11:00:01Z",
      }));
    });
    expect(screen.getByRole("button", { name: "Finding home…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop mount motion" })).toBeInTheDocument();

    act(() => { setMountSnapshot(mountState({ at_home: true, slewing: false })); });
    expect(await screen.findByRole("button", { name: "Find home" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Stop mount motion" })).not.toBeInTheDocument();
  });

  it("shows Unparking feedback without presenting Stop for the non-motion command", async () => {
    let finishUnpark: ((response: Response) => void) | undefined;
    stubMountApi({
      "/api/mount/unpark": () => new Promise<Response>((resolve) => { finishUnpark = resolve; }),
    });
    setMountSnapshot(mountState());
    renderWithQueryClient(<MountTab />);

    fireEvent.click(screen.getByRole("button", { name: /Unpark/ }));
    expect(await screen.findByRole("button", { name: "Unparking…" })).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByRole("button", { name: "Stop mount motion" })).not.toBeInTheDocument();
    act(() => { finishUnpark?.(json(null, 204)); });
    expect(await screen.findByRole("button", { name: /Unpark \(permit motion\)/ })).toBeEnabled();
  });

  it("labels the RA/Dec readout with its frame (topocentric, not J2000)", () => {
    stubMountApi();
    setMountSnapshot(mountState());
    renderWithQueryClient(<MountTab />);

    expect(screen.getByText(/RA \(topocentric\)/)).toBeInTheDocument();
    expect(screen.getByText(/EquatorialSystem/)).toBeInTheDocument();
  });

  it("shows a waiting message before any snapshot has arrived, not a blank tab", () => {
    stubMountApi();
    renderWithQueryClient(<MountTab />);
    expect(screen.getByText(/Waiting for state/)).toBeInTheDocument();
  });

  it("shows a no-data message when the snapshot exists but the mount has never been polled", () => {
    stubMountApi();
    setMountSnapshot(null);
    renderWithQueryClient(<MountTab />);
    expect(screen.getByText(/No mount reading yet/)).toBeInTheDocument();
  });

  it("renders an enabled Stop control while connected", () => {
    stubMountApi();
    setMountSnapshot(mountState());
    renderWithQueryClient(<MountTab />);
    expect(screen.getByRole("button", { name: "STOP" })).toBeEnabled();
  });

  it("hides Stop when the mount is disconnected", () => {
    stubMountApi();
    setMountSnapshot(mountState({ stale: true }), false);
    renderWithQueryClient(<MountTab />);
    expect(screen.queryByRole("button", { name: "STOP" })).not.toBeInTheDocument();
  });

  it("sends stop when the panic button is pressed", async () => {
    let stopped = false;
    stubMountApi({
      "/api/mount/stop": () => {
        stopped = true;
        return json(null, 204);
      },
    });
    setMountSnapshot(mountState());
    renderWithQueryClient(<MountTab />);
    fireEvent.click(screen.getByRole("button", { name: "STOP" }));
    await waitFor(() => { expect(stopped).toBe(true); });
  });

});
