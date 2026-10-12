// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Card, CardTitle } from "../../components/Card";
import { useSetSkymapAutofetchEnabledMutation, useSkymapAutofetchSettingsQuery } from "../../api/settings";
import { useSkymapSurveysQuery } from "../../api/skymap";

const AUTOFETCH_CUTOUT_SOURCE_ID = "unwise";

/**
 * The sky map's cutout-autofetch switch (`ssc/settings/models.py`'s
 * `SkymapSettings.cutout_autofetch_enabled`). The backend always fetches
 * **unWISE** once per resolved target. `schedule_autofetch` intentionally
 * has no `source_id` parameter, so callers cannot silently widen automatic
 * network access to another survey.
 *
 * **Off by default** — commit `b0c4155` removed automatic cutout fetching
 * on resolve because it was surprising and expensive, and
 * `SkymapSettings`'s own docstring says explicitly not to flip the default
 * back. Mirrors `ObjectInfoSettingsPanel`'s copy style: this sends a
 * resolved target's coordinates over the network on every resolve once on,
 * so the copy says exactly what that means.
 *
 * The credit line is fetched, never a literal in this component
 * (`THIRD_PARTY_NOTICES.md` requirement 4, `SurveyCreditModel`'s own doc
 * comment): `GET /api/skymap/surveys`'s `cutout_sources` already carries
 * unWISE's, the same live registry `OfflineMapsPanel` and the sky map's own
 * credit bar already read.
 */
export function SkymapAutofetchPanel() {
  const settings = useSkymapAutofetchSettingsQuery();
  const setEnabled = useSetSkymapAutofetchEnabledMutation();
  const surveys = useSkymapSurveysQuery();

  const credit = surveys.data?.cutout_sources.find((source) => source.id === AUTOFETCH_CUTOUT_SOURCE_ID)?.credit;

  if (settings.isPending) return <Card>Loading cutout autofetch setting…</Card>;
  if (settings.isError) {
    return <Card className="text-danger">Could not load the cutout autofetch setting: {settings.error.message}</Card>;
  }

  // Reflects the server's own value rather than assuming it (brief
  // verification requirement 3) — nothing here defaults it to `false`
  // locally; it is exactly what the query returned.
  const enabled = settings.data.cutout_autofetch_enabled;

  return (
    <Card>
      <CardTitle>Target-field cutout autofetch</CardTitle>
      <p className="mb-3 text-sm text-fg-muted">
        When a target resolves, fetch one unWISE cutout of that field, over the network —
        the same one-request-per-target fetch the map&rsquo;s own &ldquo;Fetch&rdquo; button
        already offers for the target field, just triggered automatically instead of waiting
        for a click. <strong className="font-medium text-fg">Off by default</strong>: an
        earlier version of this app fetched a cutout automatically and it was disabled
        (commit <code className="rounded bg-surface-raised px-1 py-0.5 text-xs">b0c4155</code>)
        because it was surprising and expensive.
      </p>
      <p className="mb-3 text-sm text-fg-muted">
        It can fill infrared detail for a resolved target even when the selected background
        survey has no coverage at that position. Automatic fetching is limited to unWISE.
      </p>
      {credit !== undefined && (
        <p className="mb-3 text-xs text-fg-muted">
          {credit.url !== "" ? (
            <a href={credit.url} target="_blank" rel="noreferrer" className="underline">
              {credit.label}
            </a>
          ) : (
            credit.label
          )}
        </p>
      )}
      <label className="flex min-h-11 w-fit cursor-pointer items-center gap-2 text-sm text-fg">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => {
            setEnabled.mutate(e.target.checked);
          }}
          disabled={setEnabled.isPending}
        />
        Automatically fetch a target-field cutout when a target resolves
      </label>
      {setEnabled.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {setEnabled.error instanceof Error ? setEnabled.error.message : "Could not save this setting."}
        </p>
      )}
    </Card>
  );
}
