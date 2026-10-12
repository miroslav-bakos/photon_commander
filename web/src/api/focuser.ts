// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `/api/focuser/*` — the Capture screen's focuser panel (bead ssc-bkv.7).
 * Every shape here is a real generated alias (`schema.d.ts` was regenerated
 * against the merged `ssc/api/routes/focuser.py` for this change, `npm run
 * api:codegen`), the same pattern `api/polarAlignment.ts` uses.
 *
 * Unlike polar alignment, focuser position is **not** part of
 * `StateSnapshot` and has no `*.changed` WS event — `ssc/state/` was not
 * wired to push it (`ssc/api/routes/focuser.py`'s own docstring: this is a
 * direct ASCOM member, polled, not a piece of run state). So this is the
 * "polled device info that isn't in the WS snapshot" shape `api/device.ts`
 * already uses for the address book, not the WS-snapshot shape
 * `api/polarAlignment.ts` uses — a `useQuery` against `GET
 * /api/focuser/position`, refetched on an interval so a panel left open
 * during a move keeps showing where the focuser actually is, matching the
 * route's own docstring: "Accepted, not arrived — poll `/position` … for
 * completion, the same shape as the Mount tab's `/slew`." The interval is
 * cheap to justify against architecture policy's rate-limit caution in
 * `app/Providers.tsx`: only the mutations below carry the `focuser` rate
 * limiter server-side (`rate_limit("focuser")` in the route file); `GET
 * /position` carries none.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap, unwrapVoid } from "./client";
import { useAlpacaDeviceConnected } from "./deviceAvailability";
import { acknowledgeCommand, pendingCommandKey, waitForOperation } from "./operations";
import type { components } from "./schema";

export type FocuserPositionResponse = components["schemas"]["FocuserPositionResponse"];
export type FocuserMoveRequest = components["schemas"]["FocuserMoveRequest"];
export type AutofocusRequest = components["schemas"]["AutofocusRequest"];
export type AutofocusState = components["schemas"]["AutofocusState"];
export type AutofocusSampleState = components["schemas"]["AutofocusSampleState"];

const positionKey = ["focuser", "position"] as const;
const autofocusKey = ["focuser", "autofocus"] as const;

/** Poll interval while a panel is mounted — fast enough that a move's
 * progress is visible, not so fast it competes with the rate-limited
 * mutations below for the same control link. */
const POSITION_POLL_MS = 2000;

/**
 * Live `Focuser.Position`. `data.position` is itself nullable server-side
 * ("not connected or the device did not answer" — `FocuserPositionResponse`'s
 * own docstring) — never rendered as a bare `0` (invariant 7). Query-level
 * `undefined` (no answer yet / in flight) is a third, separate state a
 * caller must handle, same three-way honesty every other panel on this
 * screen already follows for WS-driven state.
 */
export function useFocuserPositionQuery() {
  const connected = useAlpacaDeviceConnected("focuser");
  return useQuery({
    queryKey: [...positionKey, connected] as const,
    queryFn: async () => unwrap(api.GET("/api/focuser/position")),
    enabled: connected,
    refetchInterval: connected ? POSITION_POLL_MS : false,
  });
}

/** In-memory autofocus telemetry: cheap to poll because the endpoint performs
 * no device I/O. Poll quickly during a run so each measured point appears in
 * the chart shortly after its exposure completes. */
export function useAutofocusStateQuery() {
  return useQuery({
    queryKey: autofocusKey,
    queryFn: async () => unwrap(api.GET("/api/focuser/autofocus")),
    refetchInterval: (query) => query.state.data?.status === "running" ? 500 : 2000,
  });
}

/**
 * `Focuser.Move`. Accepted, not arrived (202, no body — `unwrapVoid`,
 * mirroring `useStartPolarAlignmentMutation`'s discard of a response this
 * tab does not treat as its source of truth). Refetches
 * `useFocuserPositionQuery` on success so the readout starts tracking the
 * move immediately rather than waiting for the next poll tick.
 *
 * Every refusal — 409 `FocuserRefusedError` (device's own message, plus
 * `device_code` when Alpaca supplied one) or 501 `FocuserNotAvailableError`
 * (firmware below 9.16, or no focuser hosted at all) — passes through
 * `client.ts`'s `describeError` untouched into `ApiError.message`, so a
 * caller renders it verbatim rather than a generic "move failed"
 * (architecture policy invariant 7).
 */
export function useMoveFocuserMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: FocuserMoveRequest) => unwrapVoid(api.POST("/api/focuser/move", { body })),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: positionKey });
    },
  });
}

/** `Focuser.Halt`. Deliberately not rate-limited server-side (the route's
 * own docstring, mirroring the Mount tab's `/stop`) — must work whatever
 * else is in flight. Same refusal handling and post-success refetch as
 * `useMoveFocuserMutation`. */
export function useHaltFocuserMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrapVoid(api.POST("/api/focuser/halt")),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: positionKey });
    },
  });
}

/** SSC-driven focus curve. The request completes only after the measured
 * best position has been applied, then the position readout is refreshed. */
export function useStartAutofocusMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: AutofocusRequest) =>
      waitForOperation<Record<string, never> | null>(await unwrap(api.POST("/api/focuser/start-autofocus", {
        body,
        headers: { "Idempotency-Key": pendingCommandKey("autofocus") },
      }))),
    onMutate: async () => {
      await queryClient.invalidateQueries({ queryKey: autofocusKey });
    },
    onSuccess: async () => {
      acknowledgeCommand("autofocus");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: positionKey }),
        queryClient.invalidateQueries({ queryKey: autofocusKey }),
      ]);
    },
    onError: async () => {
      await queryClient.invalidateQueries({ queryKey: autofocusKey });
    },
  });
}
