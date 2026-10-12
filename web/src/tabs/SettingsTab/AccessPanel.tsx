// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useSecurityQuery, useSetAuthRequiredMutation } from "../../api/auth";
import { Card, CardTitle } from "../../components/Card";
import { PairingPanel } from "./PairingPanel";

/**
 * Whether this instance requires pairing at all (`auth_required`, default
 * `false` — owner request 2026-09-09: "disable and hide pairing
 * functionality and make it loginless"). Off by default because on the
 * owner's own network every device is already a peer; a genuine toggle
 * rather than a removal, because a cautious operator — or anyone else
 * running this app on a network where that is not true —
 * still needs pairing to work exactly as it always has.
 *
 * `PairingPanel`'s "pair another device" QR flow only has something to do
 * once pairing is actually required, so it renders only then. Nothing here
 * deletes it.
 */
export function AccessPanel() {
  const security = useSecurityQuery();
  const setAuthRequired = useSetAuthRequiredMutation();

  if (security.isPending) return <Card>Loading network access settings…</Card>;
  if (security.isError) {
    return (
      <Card className="text-danger">Could not load network access settings: {security.error.message}</Card>
    );
  }

  const { auth_required: authRequired, auth_required_locked: locked } = security.data;

  return (
    <>
      <Card>
        <CardTitle>Network access</CardTitle>
        <p className="mb-3 text-sm text-fg-muted">
          {authRequired
            ? "Pairing required — a device must pair before it can view or control this instance."
            : "Open access — any device on this network can already view and control this instance. No pairing needed."}
        </p>
        <label className="flex min-h-11 w-fit cursor-pointer items-center gap-2 text-sm text-fg">
          <input
            type="checkbox"
            checked={authRequired}
            onChange={(e) => {
              setAuthRequired.mutate(e.target.checked);
            }}
            disabled={setAuthRequired.isPending || locked}
          />
          Require devices to pair before they can control the telescope
        </label>
        <p className="mt-2 text-xs text-fg-muted">
          {locked
            ? "Fixed for this instance by its startup configuration (SSC_AUTH_REQUIRED) — this control cannot change it."
            : "Turning this on means every other device on this network — including your phone — will need to pair before it works again."}
        </p>
        {setAuthRequired.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {setAuthRequired.error.message}
          </p>
        )}
      </Card>
      {authRequired && <PairingPanel />}
    </>
  );
}
