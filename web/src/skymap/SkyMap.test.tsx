// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { setLanguage } from "../i18n/locale";

/**
 * Regression for a live bug (owner, 2026-09-10: "the latest sliders
 * introduced dont work for opacity"): the `useEffect` that builds
 * `DisplayOptions` and calls `backend.setDisplay` read four opacity fields
 * (`skyLinesOpacity`/`catalogueOpacity`/`mosaicOpacity`/`solarSystemOpacity`)
 * plus `target`, but its dependency array only listed `solarSystemOpacity` —
 * eslint's `react-hooks/exhaustive-deps` had already flagged the other three
 * plus `target` and it was waved through. A test asserting only the
 * *initial* `setDisplay` call would still pass with that bug present (the
 * first render always reads current props); this asserts the *update* path
 * — changing a slider after mount must produce a second `setDisplay` call
 * carrying the new value, with nothing else in the dependency list having
 * changed.
 *
 * `createSkyMapBackend` is mocked to a stub that never touches a real
 * `<iframe>` document (jsdom does not run one), so this test exercises only
 * `SkyMap.tsx`'s own effect wiring, not the vector frame.
 */
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { useCallback, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../test/queryClient";
import { testSnapshot } from "../test/fixtures";
import { useConnectionStore } from "../store/connectionStore";
import { SkyMap } from "./SkyMap";
import { PaneLayout } from "../components/PaneLayout";
import { LayoutRouteProvider } from "../layout/LayoutContext";
import { useLayoutStore } from "../layout/layoutStore";
import type { DisplayOptions, FovShape, SkyMapBackend, ViewState } from "./types";

const hoisted = vi.hoisted(() => {
  const onViewChangeRef: { current: ((v: ViewState) => void) | null } = { current: null };
  const onFovMovedRef: { current: ((ra: number, dec: number) => void) | null } = { current: null };
  const setDisplay = vi.fn<(display: DisplayOptions) => void>();
  const setOverlay = vi.fn<(shapes: FovShape[]) => void>();
  const goto = vi.fn();
  const frames: HTMLIFrameElement[] = [];
  return { onViewChangeRef, onFovMovedRef, setDisplay, setOverlay, goto, frames };
});

function stubBackend(): SkyMapBackend {
  return {
    goto: hoisted.goto,
    zoomBy: vi.fn(),
    onViewChange: (cb) => {
      hoisted.onViewChangeRef.current = cb;
    },
    onFovMoved: (cb) => {
      hoisted.onFovMovedRef.current = cb;
    },
    onObjectClick: vi.fn(),
    setOverlay: hoisted.setOverlay,
    addCatalogLayer: vi.fn(),
    worldToScreen: () => [0, 0],
    screenToWorld: () => [0, 0],
    onCreditChange: vi.fn(),
    onImageryLoadingChange: vi.fn(),
    onDigitalZoomChange: vi.fn(),
    setSurveys: vi.fn(),
    setLayers: vi.fn(),
    setDisplay: hoisted.setDisplay,
    setCutoutLayers: vi.fn(),
    dispose: vi.fn(),
  };
}

vi.mock("./createSkyMapBackend", () => ({
  FRAME_SRC: "about:blank?build=test-revision",
  createSkyMapBackend: (frame: HTMLIFrameElement) => {
    hoisted.frames.push(frame);
    return stubBackend();
  },
}));

function jsonResponse(body: unknown, status: number, url: string): Response {
  const response = new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  Object.defineProperty(response, "url", { value: `http://localhost${url}` });
  return response;
}

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((request: Request) => {
      const { pathname } = new URL(request.url);
      if (pathname === "/api/skymap/surveys") {
        return Promise.resolve(jsonResponse({ surveys: [], cutout_sources: [] }, 200, pathname));
      }
      // Nothing else should be reachable: `view`/`location`/`target` all
      // stay null/unset in this test, so every other query stays disabled.
      return Promise.resolve(jsonResponse({ detail: `unexpected fetch ${pathname}` }, 500, pathname));
    }),
  );
}

afterEach(() => {
  act(() => { setLanguage("en"); });
  hoisted.onViewChangeRef.current = null;
  hoisted.onFovMovedRef.current = null;
  hoisted.setDisplay.mockClear();
  hoisted.setOverlay.mockClear();
  hoisted.goto.mockClear();
  vi.unstubAllGlobals();
  useConnectionStore.getState().reset();
});

/** Opens the Layers panel — gated behind `view !== null`, which only
 * becomes true once the (stubbed) backend reports a view, exactly as the
 * real frame would after its first paint. */
async function openLayersPanel() {
  act(() => {
    hoisted.onViewChangeRef.current?.({ ra: 10, dec: 20, fovDeg: 30 });
  });
  fireEvent.click(await screen.findByRole("button", { name: "Layers" }));
}

function AltitudeLimitHarness() {
  const [altitudeLimitDeg, setAltitudeLimitDeg] = useState(20);
  return (
    <>
      <button type="button" onClick={() => { setAltitudeLimitDeg(35); }}>Set altitude limit to 35</button>
      <SkyMap altitudeLimitDeg={altitudeLimitDeg} />
    </>
  );
}

function InstantHarness() {
  const [instant, setInstant] = useState<Date | null>(null);
  return <>
    <button type="button" onClick={() => { setInstant(new Date("2026-09-18T20:00:00Z")); }}>Resolve instant</button>
    <SkyMap instant={instant} />
  </>;
}

function MovedTargetHarness() {
  const [target, setTarget] = useState({ label: "M45", raDeg: 56.8, decDeg: 24.1, fovFittedSizeDeg: null });
  return (
    <>
      <button type="button" onClick={() => { setTarget({ ...target, raDeg: 150, decDeg: -12.3 }); }}>
        Move target coordinates
      </button>
      <SkyMap target={target} />
    </>
  );
}

const DRAGGED_PRIMARY: FovShape = {
  kind: "rect",
  id: "primary",
  centerRa: 56.8,
  centerDec: 24.1,
  widthDeg: 4,
  heightDeg: 2,
  positionAngleDeg: 35,
  style: { role: "primary" },
};

function DragMirrorHarness() {
  const onBackendReady = useCallback((backend: SkyMapBackend | null) => {
    if (backend === null) return;
    backend.setOverlay([DRAGGED_PRIMARY]);
    backend.onFovMoved(vi.fn());
  }, []);
  return <SkyMap onBackendReady={onBackendReady} />;
}

describe("SkyMap display options", () => {
  it("waits for the session instant before enabling site-dependent lines", async () => {
    stubFetch();
    const base = testSnapshot();
    useConnectionStore.setState({ snapshot: {
      ...base,
      location: {
        latitude_deg: 51.5,
        longitude_deg: -0.1,
        elevation_m: 20,
        timezone: "Europe/London",
        source: "manual",
        accuracy_m: null,
        acquired_at: "2026-08-28T00:00:00Z",
      },
    } });
    renderWithQueryClient(<InstantHarness />);
    await openLayersPanel();
    for (const name of ["Alt-az grid", "Meridian and zenith", "Horizon"]) {
      const checkbox = screen.getByRole("checkbox", { name });
      if (!(checkbox as HTMLInputElement).checked) fireEvent.click(checkbox);
    }
    expect(hoisted.setDisplay.mock.calls.at(-1)?.[0]).toMatchObject({
      altAzGrid: false, meridian: false, horizon: false,
    });

    fireEvent.click(screen.getByRole("button", { name: "Resolve instant" }));
    await waitFor(() => {
      expect(hoisted.setDisplay.mock.calls.at(-1)?.[0]).toMatchObject({
        altAzGrid: true, meridian: true, horizon: true,
      });
    });
  });

  it("keeps the same warm iframe when layout placement moves the skymap card", async () => {
    stubFetch();
    useLayoutStore.getState().resetTab("desktop:/planning");
    hoisted.frames.length = 0;
    renderWithQueryClient(
      <MemoryRouter initialEntries={["/planning"]}>
        <LayoutRouteProvider><PaneLayout secondary={<SkyMap />} /></LayoutRouteProvider>
      </MemoryRouter>,
    );
    await waitFor(() => { expect(hoisted.frames.length).toBeGreaterThan(0); });
    const firstFrame = hoisted.frames.at(-1);
    const attachmentsBeforeMove = hoisted.frames.length;
    act(() => { useLayoutStore.getState().placeCards("desktop:/planning", "secondary", ["sky-map"]); });
    await waitFor(() => {
      const liveFrame = screen.getByTitle("Sky map");
      expect(liveFrame).toBe(firstFrame);
      expect(hoisted.frames.at(-1)).toBe(liveFrame);
      expect(hoisted.frames.length).toBeGreaterThan(attachmentsBeforeMove);
    });
    useLayoutStore.getState().resetTab("desktop:/planning");
  });

  it("navigates the connected iframe after a fallback DOM attachment", () => {
    stubFetch();
    // The native setter is invoked below with its explicit iframe receiver.
    // eslint-disable-next-line @typescript-eslint/unbound-method
    const original = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, "src")?.set;
    const connectedWrites: boolean[] = [];
    const setter = vi.spyOn(HTMLIFrameElement.prototype, "src", "set").mockImplementation(function (this: HTMLIFrameElement, src: string) {
      connectedWrites.push(this.isConnected);
      original?.call(this, src);
    });
    try {
      renderWithQueryClient(<SkyMap />);
      expect(connectedWrites).toContain(true);
    } finally {
      setter.mockRestore();
    }
  });

  it("preserves the build revision and theme parameters in the iframe URL", () => {
    stubFetch();
    renderWithQueryClient(<SkyMap />);
    const frame = screen.getByTitle<HTMLIFrameElement>("Sky map");
    const url = new URL(frame.src);
    expect(url.searchParams.get("build")).toBe("test-revision");
    expect(url.searchParams.has("bg")).toBe(true);
    expect(url.searchParams.has("pointing")).toBe(true);
  });

  it("provides a dark iframe surface before the skymap document loads", () => {
    stubFetch();
    renderWithQueryClient(<SkyMap />);

    const frame = screen.getByTitle("Sky map");
    expect(frame).toHaveClass("bg-bg");
    expect(frame).toHaveStyle({ colorScheme: "dark" });
  });

  it("reports the restored camera when a warm iframe is reattached", async () => {
    stubFetch();
    const onWarmViewRestore = vi.fn();
    const first = renderWithQueryClient(<SkyMap onWarmViewRestore={onWarmViewRestore} />);
    await waitFor(() => { expect(hoisted.onViewChangeRef.current).not.toBeNull(); });

    const restoredView = { ra: 42, dec: -5, fovDeg: 12 };
    act(() => {
      hoisted.onViewChangeRef.current?.(restoredView);
    });
    first.unmount();

    renderWithQueryClient(<SkyMap onWarmViewRestore={onWarmViewRestore} />);

    await waitFor(() => {
      expect(onWarmViewRestore).toHaveBeenCalledWith(restoredView);
    });
  });

  it("does not re-apply the display projection when only target coordinates move", async () => {
    stubFetch();
    renderWithQueryClient(<MovedTargetHarness />);
    await waitFor(() => { expect(hoisted.setDisplay).toHaveBeenCalled(); });
    hoisted.setDisplay.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Move target coordinates" }));

    expect(hoisted.setDisplay).not.toHaveBeenCalled();
  });

  it("never resurrects the pre-drag frame when an auxiliary overlay refresh follows release", async () => {
    stubFetch();
    renderWithQueryClient(<DragMirrorHarness />);
    await waitFor(() => { expect(hoisted.onFovMovedRef.current).not.toBeNull(); });
    hoisted.setOverlay.mockClear();

    act(() => {
      hoisted.onFovMovedRef.current?.(150, -12.3);
      // A view settle rebuilds catalogue/star/constellation layers and calls
      // pushOverlay. This was the first post-release refresh that copied the
      // stale pre-drag primary back into the iframe in the live recording.
      hoisted.onViewChangeRef.current?.({ ra: 150, dec: -12.3, fovDeg: 6 });
    });

    await waitFor(() => {
      const latest = hoisted.setOverlay.mock.calls.at(-1)?.[0] ?? [];
      const primary = latest.find((shape) => shape.id === "primary");
      expect(primary).toEqual(expect.objectContaining({ centerRa: 150 }));
      if (primary?.kind !== "rect") throw new Error("expected mirrored primary rectangle");
      expect(primary.centerDec).toBeCloseTo(-12.3, 9);
    }, { timeout: 1000 });
  });

  it("re-issues setDisplay with a changed sky-lines opacity after mount", async () => {
    stubFetch();
    renderWithQueryClient(<SkyMap />);

    await openLayersPanel();
    hoisted.setDisplay.mockClear();

    const slider = screen.getByRole("slider", { name: "Sky lines opacity" });
    fireEvent.change(slider, { target: { value: "0.4" } });

    expect(hoisted.setDisplay).toHaveBeenCalled();
    const latest = hoisted.setDisplay.mock.calls.at(-1)?.[0];
    expect(latest?.skyLinesOpacity).toBeCloseTo(0.4);
  });

  it("sends caption language updates without replacing the map or moving its view", async () => {
    stubFetch();
    const { container } = renderWithQueryClient(<SkyMap />);
    await waitFor(() => { expect(hoisted.setDisplay).toHaveBeenCalled(); });
    const frame = container.querySelector("iframe");
    expect(frame).not.toBeNull();
    hoisted.goto.mockClear();
    act(() => { setLanguage("sk"); });
    expect(hoisted.setDisplay.mock.calls.at(-1)?.[0].language).toBe("sk");
    expect(container.querySelector("iframe")).toBe(frame);
    expect(hoisted.goto).not.toHaveBeenCalled();
    act(() => { setLanguage("cs"); });
    expect(hoisted.setDisplay.mock.calls.at(-1)?.[0].language).toBe("cs");
    expect(container.querySelector("iframe")).toBe(frame);
  });

  it("re-issues setDisplay with a changed catalogue opacity after mount", async () => {
    stubFetch();
    renderWithQueryClient(<SkyMap />);

    await openLayersPanel();
    hoisted.setDisplay.mockClear();

    const slider = screen.getByRole("slider", { name: "Catalogue opacity" });
    fireEvent.change(slider, { target: { value: "0.25" } });

    expect(hoisted.setDisplay).toHaveBeenCalled();
    const latest = hoisted.setDisplay.mock.calls.at(-1)?.[0];
    expect(latest?.catalogueOpacity).toBeCloseTo(0.25);
  });

  it("re-issues setDisplay with a changed mosaic-panel opacity after mount", async () => {
    stubFetch();
    renderWithQueryClient(<SkyMap />);

    await openLayersPanel();
    hoisted.setDisplay.mockClear();

    const slider = screen.getByRole("slider", { name: "Mosaic panel opacity" });
    fireEvent.change(slider, { target: { value: "0.6" } });

    expect(hoisted.setDisplay).toHaveBeenCalled();
    const latest = hoisted.setDisplay.mock.calls.at(-1)?.[0];
    expect(latest?.mosaicOpacity).toBeCloseTo(0.6);
  });

  it("moves the optional altitude-limit line without resetting the current view", async () => {
    stubFetch();
    renderWithQueryClient(<AltitudeLimitHarness />);

    await openLayersPanel();
    const ring = screen.getByRole("checkbox", { name: "Altitude limit ring" });
    if (!(ring as HTMLInputElement).checked) fireEvent.click(ring);
    await waitFor(() => {
      expect(hoisted.setDisplay.mock.calls.at(-1)?.[0].altitudeLimitDeg).toBe(20);
    });
    hoisted.setDisplay.mockClear();
    hoisted.goto.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Set altitude limit to 35" }));

    await waitFor(() => {
      expect(hoisted.setDisplay.mock.calls.at(-1)?.[0].altitudeLimitDeg).toBe(35);
    });
    expect(hoisted.goto).not.toHaveBeenCalled();
  });
});

describe("SkyMap solved camera field", () => {
  it("uses a configured rectangular field when Alpaca omits focal length", async () => {
    stubFetch();
    const base = testSnapshot();
    useConnectionStore.setState({
      snapshot: {
        ...base,
        mount: {
          polled_at: "2026-09-12T21:00:00Z",
          stale: false,
          ra_hours: 9.99,
          dec_deg: 41.1,
          ra_j2000_h: 9.99,
          dec_j2000_deg: 41.1,
          site_latitude_deg: 48.2,
          site_longitude_deg: 16.4,
          alt_deg: 60,
          az_deg: 180,
          sidereal_time_hours: 20,
          tracking: true,
          tracking_rate: "sidereal",
          at_park: false,
          at_home: false,
          slewing: false,
          equatorial_system: "j2000",
          jog_primary_active: false,
          jog_secondary_active: false,
          jog_lease_expires_at: null,
          capabilities: null,
        },
        camera_fov: null,
      },
      seq: 1,
    });

    renderWithQueryClient(<SkyMap mountFovDeg={{ widthDeg: 2.24, heightDeg: 3.99, rotationDeg: 0 }} />);

    await waitFor(() => {
      const shapes = hoisted.setOverlay.mock.calls.flatMap(([items]) => items);
      expect(shapes).toContainEqual(expect.objectContaining({
        kind: "rect",
        widthDeg: 2.24,
        heightDeg: 3.99,
      }));
    });
  });

  it("draws the known camera rectangle before the first plate solve", async () => {
    stubFetch();
    const base = testSnapshot();
    useConnectionStore.setState({
      snapshot: {
        ...base,
        mount: {
          polled_at: "2026-09-12T21:00:00Z",
          stale: false,
          ra_hours: 9.99,
          dec_deg: 41.1,
          ra_j2000_h: 9.99,
          dec_j2000_deg: 41.1,
          site_latitude_deg: 48.2,
          site_longitude_deg: 16.4,
          alt_deg: 60,
          az_deg: 180,
          sidereal_time_hours: 20,
          tracking: true,
          tracking_rate: "sidereal",
          at_park: false,
          at_home: false,
          slewing: false,
          equatorial_system: "j2000",
          jog_primary_active: false,
          jog_secondary_active: false,
          jog_lease_expires_at: null,
          capabilities: null,
        },
        camera_fov: {
          width_deg: 2.24,
          height_deg: 3.99,
          rotation_deg: null,
          center_ra_j2000_h: null,
          center_dec_j2000_deg: null,
          solved_at: null,
        },
      },
      seq: 1,
    });

    renderWithQueryClient(<SkyMap />);

    await waitFor(() => {
      const shapes = hoisted.setOverlay.mock.calls.flatMap(([items]) => items);
      expect(shapes).toContainEqual(expect.objectContaining({
        kind: "rect",
        centerRa: 9.99 * 15,
        centerDec: 41.1,
        widthDeg: 2.24,
        heightDeg: 3.99,
        positionAngleDeg: 0,
      }));
    });
  });

  it("draws the plate-solved portrait field as a rotated rectangle", async () => {
    stubFetch();
    const base = testSnapshot();
    useConnectionStore.setState({
      snapshot: {
        ...base,
        mount: {
          polled_at: "2026-09-12T21:00:00Z",
          stale: false,
          ra_hours: 9.99,
          dec_deg: 41.1,
          ra_j2000_h: 9.99,
          dec_j2000_deg: 41.1,
          site_latitude_deg: 48.2,
          site_longitude_deg: 16.4,
          alt_deg: 60,
          az_deg: 180,
          sidereal_time_hours: 20,
          tracking: true,
          tracking_rate: "sidereal",
          at_park: false,
          at_home: false,
          slewing: false,
          equatorial_system: "j2000",
          jog_primary_active: false,
          jog_secondary_active: false,
          jog_lease_expires_at: null,
          capabilities: null,
        },
        camera_fov: {
          width_deg: 2.24,
          height_deg: 3.99,
          rotation_deg: 93.5,
          center_ra_j2000_h: 10,
          center_dec_j2000_deg: 41.2,
          solved_at: "2026-09-12T21:00:00Z",
        },
      },
      seq: 1,
    });

    renderWithQueryClient(<SkyMap mountFovDeg={{ widthDeg: 10, heightDeg: 20, rotationDeg: 0 }} />);

    await waitFor(() => {
      const shapes = hoisted.setOverlay.mock.calls.flatMap(([items]) => items);
      expect(shapes).toContainEqual(expect.objectContaining({
        kind: "rect",
        centerRa: 150,
        centerDec: 41.2,
        widthDeg: 2.24,
        heightDeg: 3.99,
        positionAngleDeg: 93.5,
      }));
    });
  });
});
