// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The tab's brief calls this out explicitly: a jog that keeps moving
 * because a release was missed is dangerous. Every one of pointerup,
 * pointercancel, pointerleave, a window blur and an unmount must
 * independently be able to send the release (`rate_deg_s: 0`) — this file
 * pins each one separately rather than trusting that one code path covers
 * all five.
 */
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { JogPad } from "./JogPad";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

interface MoveAxisCall {
  axis: string;
  rate_deg_s: number;
}

function stubMoveAxis(): MoveAxisCall[] {
  const calls: MoveAxisCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(new URL(String(input), "http://localhost"), init);
      const pathname = new URL(request.url).pathname;
      if (request.method === "POST" && pathname === "/api/mount/move-axis") {
        calls.push(JSON.parse(await request.clone().text()) as MoveAxisCall);
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ detail: "unexpected" }), { status: 500 }));
    }),
  );
  return calls;
}

/** The "primary +" button — one clear target for every test here. */
function primaryPlusButton() {
  return screen.getByRole("button", { name: /Primary axis, positive direction/ });
}

describe("JogPad", () => {
  it("selects FAST and uses the device maximum rate by default", async () => {
    const calls = stubMoveAxis();
    renderWithQueryClient(<JogPad canMovePrimary canMoveSecondary axisRateMaxDegS={6} />);

    expect(screen.getByRole("button", { name: /Fast/ })).toHaveAttribute("aria-pressed", "true");
    fireEvent.pointerDown(primaryPlusButton(), { pointerId: 1 });
    await vi.waitFor(() => { expect(calls).toHaveLength(1); });
    expect(calls[0]).toEqual({ axis: "primary", rate_deg_s: 6 });
  });

  it("sends a positive rate on press and a release (rate_deg_s: 0) on pointer-up", async () => {
    const calls = stubMoveAxis();
    renderWithQueryClient(<JogPad canMovePrimary canMoveSecondary axisRateMaxDegS={6} />);
    const button = primaryPlusButton();

    fireEvent.pointerDown(button, { pointerId: 1 });
    await vi.waitFor(() => { expect(calls).toHaveLength(1); });
    expect(calls[0]?.axis).toBe("primary");
    expect(calls[0]?.rate_deg_s).toBeGreaterThan(0);

    fireEvent.pointerUp(button, { pointerId: 1 });
    await vi.waitFor(() => { expect(calls).toHaveLength(2); });
    expect(calls[1]).toEqual({ axis: "primary", rate_deg_s: 0 });
  });

  it("releases on pointer-cancel", async () => {
    const calls = stubMoveAxis();
    renderWithQueryClient(<JogPad canMovePrimary canMoveSecondary axisRateMaxDegS={6} />);
    const button = primaryPlusButton();

    fireEvent.pointerDown(button, { pointerId: 1 });
    await vi.waitFor(() => { expect(calls).toHaveLength(1); });

    fireEvent.pointerCancel(button, { pointerId: 1 });
    await vi.waitFor(() => { expect(calls).toHaveLength(2); });
    expect(calls[1]).toEqual({ axis: "primary", rate_deg_s: 0 });
  });

  it("releases on pointer-leave (a gloved finger sliding off the button)", async () => {
    const calls = stubMoveAxis();
    renderWithQueryClient(<JogPad canMovePrimary canMoveSecondary axisRateMaxDegS={6} />);
    const button = primaryPlusButton();

    fireEvent.pointerDown(button, { pointerId: 1 });
    await vi.waitFor(() => { expect(calls).toHaveLength(1); });

    fireEvent.pointerLeave(button, { pointerId: 1 });
    await vi.waitFor(() => { expect(calls).toHaveLength(2); });
    expect(calls[1]).toEqual({ axis: "primary", rate_deg_s: 0 });
  });

  it("releases on the window losing focus", async () => {
    const calls = stubMoveAxis();
    renderWithQueryClient(<JogPad canMovePrimary canMoveSecondary axisRateMaxDegS={6} />);
    const button = primaryPlusButton();

    fireEvent.pointerDown(button, { pointerId: 1 });
    await vi.waitFor(() => { expect(calls).toHaveLength(1); });

    fireEvent(window, new Event("blur"));
    await vi.waitFor(() => { expect(calls).toHaveLength(2); });
    expect(calls[1]).toEqual({ axis: "primary", rate_deg_s: 0 });
  });

  it("releases on unmount — a route change must not leave the mount jogging", async () => {
    const calls = stubMoveAxis();
    const { unmount } = renderWithQueryClient(<JogPad canMovePrimary canMoveSecondary axisRateMaxDegS={6} />);
    const button = primaryPlusButton();

    fireEvent.pointerDown(button, { pointerId: 1 });
    await vi.waitFor(() => { expect(calls).toHaveLength(1); });

    unmount();
    await vi.waitFor(() => { expect(calls).toHaveLength(2); });
    expect(calls[1]).toEqual({ axis: "primary", rate_deg_s: 0 });
  });

  it("does not send a release for a button that was never pressed", async () => {
    const calls = stubMoveAxis();
    const { unmount } = renderWithQueryClient(<JogPad canMovePrimary canMoveSecondary axisRateMaxDegS={6} />);
    unmount();
    // Give any stray effect a tick to fire before asserting nothing did.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls).toHaveLength(0);
  });

  it("does not render a jog button for an axis the device cannot move", () => {
    stubMoveAxis();
    renderWithQueryClient(<JogPad canMovePrimary={false} canMoveSecondary axisRateMaxDegS={6} />);
    expect(screen.queryByRole("button", { name: /Primary axis/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Secondary axis, positive/ })).toBeInTheDocument();
  });

  it("disables jogging entirely when the device has not reported a max rate", () => {
    stubMoveAxis();
    renderWithQueryClient(<JogPad canMovePrimary canMoveSecondary axisRateMaxDegS={null} />);
    expect(screen.getByText(/has not reported a maximum jog rate/)).toBeInTheDocument();
    expect(primaryPlusButton()).toBeDisabled();
  });

  it("blocks a parked jog and briefly explains how to proceed", () => {
    vi.useFakeTimers();
    const calls = stubMoveAxis();
    renderWithQueryClient(<JogPad canMovePrimary canMoveSecondary axisRateMaxDegS={6} parked />);

    fireEvent.pointerDown(primaryPlusButton(), { pointerId: 1 });

    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent(/mount is parked/i);
    expect(notice).toHaveTextContent(/unpark before moving/i);
    expect(notice).toHaveTextContent(/unpark \+ slew/i);
    expect(notice.querySelector("svg")).toBeInTheDocument();
    expect(calls).toHaveLength(0);

    act(() => { vi.advanceTimersByTime(5000); });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
