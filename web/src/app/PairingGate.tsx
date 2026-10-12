// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect, useRef, useState, type ReactNode, type SyntheticEvent } from "react";
import { usePairMutation, usePairingQuery, useSecurityQuery, useSessionQuery } from "../api/auth";
import { ApiError } from "../api/client";
import { Button } from "../components/Button";
import { Card, CardTitle } from "../components/Card";
import { Field } from "../components/Field";
import { QrCode } from "../components/QrCode";

/** Pulls `?t=<token>` off the URL once, without leaving it in the address bar
 * or browser history (DESIGN.md §1: the QR encodes exactly this token). */
function usePairingTokenFromUrl(): string | null {
  const [token] = useState(() => new URLSearchParams(window.location.search).get("t"));
  useEffect(() => {
    if (token === null) return;
    const url = new URL(window.location.href);
    url.searchParams.delete("t");
    window.history.replaceState({}, "", url);
  }, [token]);
  return token;
}

/**
 * Nothing under `src/app` reaches the tabs until a session exists — *unless*
 * this instance does not require one at all (`auth_required`, enabled by default). When it is off there is no session to establish,
 * so this mounts straight into the app with no pairing screen, no QR, and no
 * "not paired" prompt anywhere — see `AccessPanel` in Settings for the
 * toggle and the open-access indicator. When it is on, everything below
 * behaves exactly as it always has: pairing token → session cookie,
 * exchanged once, then every mutating request and the WS both ride the
 * cookie. This is also the only place that ever sees the pairing token in
 * this app.
 *
 * The gate needs positive evidence: either security explicitly says access
 * is open, an existing session is authenticated, or a 401 from the security
 * endpoint proves pairing is required. A network error, 5xx or mismatched
 * release cannot mean "pairing required"; that used to flash the pairing
 * screen during transient backend interruptions.
 */
export function PairingGate({ children }: { children: ReactNode }) {
  const urlToken = usePairingTokenFromUrl();
  const session = useSessionQuery();
  const security = useSecurityQuery();
  const pair = usePairMutation();
  const attemptedUrlToken = useRef(false);
  const pairingRequired = security.data?.auth_required === true
    || (security.error instanceof ApiError && security.error.status === 401);

  useEffect(() => {
    if (urlToken === null || attemptedUrlToken.current) return;
    if (!pairingRequired) return; // A failed status check is not permission to try pairing.
    if (session.data?.authenticated) return;
    attemptedUrlToken.current = true;
    pair.mutate({ token: urlToken });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once per URL token
  }, [urlToken, pairingRequired, session.data?.authenticated]);

  // A failed pre-pairing security query has no cached data. Its reconnect
  // retry becomes pending even after the session is authenticated; replacing
  // the app here would unmount forms and discard their unsaved drafts.
  // A subsequent unauthenticated session still closes the gate below.
  if (session.data?.authenticated) return <>{children}</>;

  if (session.isPending || security.isPending || (urlToken !== null && pair.isPending)) {
    return <CenteredMessage>Checking session…</CenteredMessage>;
  }

  if (security.data?.auth_required === false && !pairingRequired) {
    return <>{children}</>;
  }

  if (pairingRequired) return <PairingScreen urlTokenFailed={urlToken !== null && pair.isError} />;

  return (
    <div className="flex min-h-dvh items-center justify-center bg-bg p-6 text-fg">
      <Card className="w-full max-w-sm space-y-3 text-center">
        <CardTitle>Cannot check connection</CardTitle>
        <p className="text-sm text-fg-muted">The app could not confirm whether pairing is enabled. It will retry automatically.</p>
        <Button onClick={() => { void security.refetch(); void session.refetch(); }}>Retry now</Button>
      </Card>
    </div>
  );
}

function CenteredMessage({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-bg text-fg-muted">
      <p>{children}</p>
    </div>
  );
}

function PairingScreen({ urlTokenFailed }: { urlTokenFailed: boolean }) {
  // Only a loopback caller (the desktop shell itself) can read this without
  // already being paired — that is the guard that makes the QR meaningful.
  const pairing = usePairingQuery(true);
  const pair = usePairMutation();
  const [manualToken, setManualToken] = useState("");

  function submitManualToken(event: SyntheticEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (manualToken.trim() === "") return;
    pair.mutate({ token: manualToken.trim() });
  }

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-6 bg-bg p-6 text-fg">
      <div className="w-full max-w-sm space-y-6">
        <div className="text-center">
          <h1 className="text-lg font-semibold">Photon Commander</h1>
          <p className="mt-1 text-sm text-fg-muted">This device is not paired yet.</p>
        </div>

        {urlTokenFailed && (
          <Card className="border-danger text-sm text-danger">
            That pairing link did not work — it may have expired or already been used. Ask the
            desktop shell for a fresh QR code, or enter the code below.
          </Card>
        )}

        {pairing.isSuccess && (
          <Card className="flex flex-col items-center gap-4 text-center">
            <CardTitle>Scan from a phone on this network</CardTitle>
            <QrCode value={pairing.data.pairing_url} />
            <p className="text-xs break-all text-fg-muted">{pairing.data.pairing_url}</p>
            <Button variant="primary" onClick={() => { pair.mutate({ token: pairing.data.token }); }} disabled={pair.isPending}>
              {pair.isPending ? "Pairing…" : "Pair this device"}
            </Button>
          </Card>
        )}

        <Card className="space-y-3">
          <CardTitle>Have a pairing code?</CardTitle>
          <form onSubmit={submitManualToken} className="flex flex-col gap-3">
            <Field
              label="Pairing code"
              value={manualToken}
              onChange={(e) => { setManualToken(e.target.value); }}
              autoComplete="off"
              spellCheck={false}
            />
            <Button type="submit" variant="primary" disabled={pair.isPending || manualToken.trim() === ""}>
              {pair.isPending ? "Pairing…" : "Pair"}
            </Button>
            {pair.isError && !urlTokenFailed && (
              <p role="alert" className="text-sm text-danger">
                That code was not accepted.
              </p>
            )}
          </form>
        </Card>
      </div>
    </div>
  );
}
