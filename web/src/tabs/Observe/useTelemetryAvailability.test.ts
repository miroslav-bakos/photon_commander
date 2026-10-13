// SPDX-License-Identifier: GPL-3.0-only
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MountState } from "../../api/mount";
import { useConnectionStore } from "../../store/connectionStore";
import { testSnapshot } from "../../test/fixtures";
import { useTelemetryAvailability } from "./useTelemetryAvailability";

const reading: MountState = {
  polled_at: "2026-10-13T00:00:00Z", stale: false,
  ra_hours: 2, dec_deg: 30, alt_deg: 40, az_deg: 180, sidereal_time_hours: 3,
  tracking: true, tracking_rate: "sidereal", at_park: false, at_home: false,
  slewing: false, equatorial_system: "topocentric", capabilities: null,
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-13T00:00:00Z"));
  const snapshot = testSnapshot();
  if (snapshot.link == null) throw new Error("Missing fixture link");
  useConnectionStore.setState({ status: "open", snapshot: {
    ...snapshot, link: { ...snapshot.link, status: "ready", control_connected: true },
  } });
});
afterEach(() => { useConnectionStore.getState().reset(); vi.useRealTimers(); });

it("does not alternate live/stale between authoritative polls or on a fast client clock", () => {
  const { result, rerender } = renderHook(({ mount }) => useTelemetryAvailability(mount), { initialProps: { mount: reading } });
  expect(result.current.mountUnavailable).toBeNull();
  act(() => { vi.advanceTimersByTime(6000); });
  expect(result.current.mountUnavailable).toBeNull();
  rerender({ mount: { ...reading, polled_at: "2026-10-13T00:00:06Z" } });
  act(() => { vi.setSystemTime(new Date("2026-10-13T00:05:06Z")); vi.advanceTimersByTime(1000); });
  expect(result.current.mountUnavailable).toBeNull();
  expect(result.current.now).toBe(Date.now());
});

it("immediately shows server staleness and recovers only on a fresh server reading", () => {
  const { result, rerender } = renderHook(({ mount }) => useTelemetryAvailability(mount), { initialProps: { mount: reading } });
  rerender({ mount: { ...reading, stale: true } });
  expect(result.current.mountUnavailable).toBe("Stale");
  act(() => { vi.setSystemTime(new Date("2026-10-12T23:00:00Z")); vi.advanceTimersByTime(1000); });
  expect(result.current.mountUnavailable).toBe("Stale");
  rerender({ mount: { ...reading, polled_at: "2026-10-13T00:00:10Z" } });
  expect(result.current.mountUnavailable).toBeNull();
});

it("preserves disconnected and degraded link semantics despite retained mount readings", () => {
  const { result } = renderHook(() => useTelemetryAvailability(reading));
  act(() => { useConnectionStore.setState({ status: "reconnecting" }); });
  expect(result.current.mountUnavailable).toBe("Disconnected");
  const snapshot = useConnectionStore.getState().snapshot;
  if (snapshot?.link == null) throw new Error("Missing fixture snapshot or link");
  const link = snapshot.link;
  act(() => { useConnectionStore.setState({ status: "open", snapshot: { ...snapshot, link: { ...link, control_connected: false } } }); });
  expect(result.current.mountUnavailable).toBe("Disconnected");
  act(() => { useConnectionStore.setState({ snapshot: { ...snapshot, link: { ...link, status: "degraded" } } }); });
  expect(result.current.mountUnavailable).toBe("Unknown");
  act(() => { useConnectionStore.setState({ snapshot: { ...snapshot, devices: { polled_at: reading.polled_at, stale: true, items: [] } } }); });
  expect(result.current.mountUnavailable).toBe("Unknown");
});

it("does not call missing state or unpolled readings live", () => {
  const { result, rerender } = renderHook(({ mount }: { mount: MountState | null }) => useTelemetryAvailability(mount), { initialProps: { mount: null as MountState | null } });
  expect(result.current.mountUnavailable).toBe("Unknown");
  rerender({ mount: { ...reading, polled_at: null } });
  expect(result.current.mountUnavailable).toBe("Stale");
  rerender({ mount: { ...reading, polled_at: "invalid" } });
  expect(result.current.mountUnavailable).toBe("Stale");
  act(() => { useConnectionStore.setState({ snapshot: null }); });
  expect(result.current.mountUnavailable).toBe("Unknown");
});
