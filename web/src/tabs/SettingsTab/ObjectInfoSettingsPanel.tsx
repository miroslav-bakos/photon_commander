// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Card, CardTitle } from "../../components/Card";
import { useObjectInfoSettingsQuery, useSetObjectInfoEnabledMutation } from "../../api/settings";

/**
 * M11's click-through info panel's own on/off switch (`ssc/objectinfo`,
 * `THIRD_PARTY_NOTICES.md` "Click-through info sources"). Off by default per
 * that reading's own recommendation — enabling it is a real decision to send
 * an object's name off this machine, so this copy says exactly what that
 * means rather than a marketing gloss: which services it talks to, that it
 * goes through this server rather than the browser (architecture policy invariant 8),
 * and that it caches what comes back. No BYO endpoint field, on purpose: the
 * backend only ever has this one toggle (the compliance finding's
 * conditional-pass was specific to Wikipedia + Wikidata + per-file Commons
 * licence resolution, not an open API a viewer could point anywhere).
 */
export function ObjectInfoSettingsPanel() {
  const settings = useObjectInfoSettingsQuery();
  const setEnabled = useSetObjectInfoEnabledMutation();

  if (settings.isPending) return <Card>Loading object info settings…</Card>;
  if (settings.isError) {
    return <Card className="text-danger">Could not load object info settings: {settings.error.message}</Card>;
  }

  const enabled = settings.data.enabled;

  return (
    <Card>
      <CardTitle>Object info</CardTitle>
      <p className="mb-3 text-sm text-fg-muted">
        Click a catalogue object on the sky map for a short description and, when one
        exists, a photo with its own licence. Off by default. Turning this on sends the
        object&rsquo;s name to Wikipedia and Wikidata — proxied through this server, never
        called directly from your browser — and caches the result, including each
        image&rsquo;s own licence and credit, so a second look does not repeat the
        request.
      </p>
      <label className="flex min-h-11 w-fit cursor-pointer items-center gap-2 text-sm text-fg">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => {
            setEnabled.mutate(e.target.checked);
          }}
          disabled={setEnabled.isPending}
        />
        Show object info when a catalogue object is clicked
      </label>
      {setEnabled.isError && <p className="text-sm text-danger">{setEnabled.error.message}</p>}
    </Card>
  );
}
