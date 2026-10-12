// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, unwrap, unwrapVoid } from "./client";

const sessionKey = ["auth", "session"] as const;

/** Unauthenticated on purpose (backend comment on the route): this is how the
 * client finds out *whether* it is paired at all. */
export function useSessionQuery() {
  return useQuery({
    queryKey: sessionKey,
    queryFn: async () => unwrap(api.GET("/api/auth/session")),
    // A session can expire server-side at any time; re-check on focus rather
    // than trusting a stale "authenticated: true" indefinitely.
    refetchOnWindowFocus: true,
  });
}

/** Only answers for a loopback caller (the desktop shell) or an already-paired
 * session — a phone on the LAN gets a 403 here, which is why the QR/token in
 * the URL is the path that actually works for it. */
export function usePairingQuery(enabled: boolean) {
  return useQuery({
    queryKey: ["auth", "pairing"],
    queryFn: async () => unwrap(api.GET("/api/auth/pairing")),
    enabled,
    retry: false,
  });
}

export function usePairMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { token: string; label?: string | null }) =>
      unwrap(api.POST("/api/auth/pair", { body })),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: sessionKey });
    },
  });
}

/** The QR/token for pairing a second device — usable once a session already
 * exists (the "loopback or session" guard on the route), unlike the gate's
 * own use of this endpoint before any session exists. */
export function usePairingInfoQuery(enabled: boolean) {
  return useQuery({
    queryKey: ["auth", "pairing", "info"],
    queryFn: async () => unwrap(api.GET("/api/auth/pairing")),
    enabled,
  });
}

export function useRotatePairingMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrap(api.POST("/api/auth/pairing/rotate")),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["auth", "pairing"] });
    },
  });
}

export function useLogoutMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrapVoid(api.POST("/api/auth/logout")),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: sessionKey });
    },
  });
}

const securityKey = ["settings", "security"] as const;

/**
 * Whether pairing is currently enforced (`auth_required`, default `true` —
 * owner request 2026-09-09: "disable and hide pairing functionality and make
 * it loginless"). `GET` is deliberately safe to call before any pairing has
 * happened: the backend's `require_session` treats every caller as
 * already-permitted while `auth_required` is off, so this succeeds with no
 * session cookie at all in that case, and 401s exactly when a real pairing
 * step is still needed. That — not a boolean parsed out of
 * `/api/auth/session`, which has no opinion on `auth_required` — is what
 * `PairingGate` uses to distinguish open access from an actual 401 pairing
 * requirement. Transport or server errors are retried, never interpreted as
 * a request to pair.
 */
export function useSecurityQuery() {
  return useQuery({
    queryKey: securityKey,
    queryFn: async () => unwrap(api.GET("/api/settings/security")),
    retry: (attempt, error) => !(error instanceof ApiError && error.status === 401) && attempt < 2,
    refetchOnWindowFocus: true,
    refetchInterval: (query) => query.state.status === "error"
      && !(query.state.error instanceof ApiError && query.state.error.status === 401) ? 5000 : false,
  });
}

export function useSetAuthRequiredMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (authRequired: boolean) =>
      unwrap(api.PUT("/api/settings/security/auth-required", { body: { auth_required: authRequired } })),
    onSuccess: async (data) => {
      queryClient.setQueryData(securityKey, data);
      // Turning pairing on mints a fresh session for *this* browser (the
      // backend's own contract: the tab that flips the switch must not be
      // the one locked out by it) — refresh `/api/auth/session` so the rest
      // of the app picks that up immediately rather than on the next reload.
      await queryClient.invalidateQueries({ queryKey: sessionKey });
    },
  });
}
