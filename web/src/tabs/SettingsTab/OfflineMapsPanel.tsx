// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import { useClearSkymapCacheMutation, useDeleteAllCutoutsMutation, useSkymapSurveysQuery } from "../../api/skymap";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { useConnectionStore } from "../../store/connectionStore";

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex] ?? "TB"}`;
}

/** Cache management only. Map tiles and target-field images enter this cache
 * through ordinary viewing; the former whole-sky, plan and catalogue batch
 * download controls and their public APIs no longer exist. */
export function OfflineMapsPanel() {
  const surveysQuery = useSkymapSurveysQuery();
  const skymap = useConnectionStore((state) => state.snapshot?.skymap);
  const clearSurvey = useClearSkymapCacheMutation();
  const clearCutouts = useDeleteAllCutoutsMutation();
  const [confirmSurvey, setConfirmSurvey] = useState<string | null>(null);
  const [confirmCutouts, setConfirmCutouts] = useState(false);

  if (surveysQuery.isPending) return <Card>Loading map cache...</Card>;
  if (surveysQuery.isError) {
    return <Card className="text-danger">Could not load the survey registry: {surveysQuery.error.message}</Card>;
  }

  const liveById = new Map((skymap?.surveys ?? []).map((survey) => [survey.id, survey]));
  const cachedSurveys = surveysQuery.data.surveys
    .map((survey) => ({
      id: survey.id,
      label: survey.label,
      bundled: liveById.get(survey.id)?.bundled ?? survey.bundled,
      cachedBytes: liveById.get(survey.id)?.cached_bytes ?? survey.cached_bytes,
    }))
    .filter((survey) => survey.cachedBytes > 0 || survey.bundled);
  const cutoutCount = skymap?.cutouts?.filter((cutout) => cutout.status === "complete").length ?? 0;

  return (
    <Card>
      <CardTitle>Map cache</CardTitle>
      <p className="mb-4 text-sm text-fg-muted">
        Imagery you view is cached automatically on this device. Photon Commander does not download
        whole-sky, plan, or catalogue batches.
        {skymap && ` ${formatBytes(skymap.cache_bytes)} of ${formatBytes(skymap.cache_max_bytes)} tile cache used.`}
      </p>

      {cachedSurveys.length === 0 ? (
        <p className="text-sm text-fg-muted">No survey tiles are cached yet.</p>
      ) : (
        <div className="divide-y divide-border">
          {cachedSurveys.map((survey) => (
            <div key={survey.id} className="flex min-h-14 items-center justify-between gap-3 py-2">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-fg">{survey.label}</p>
                <p className="text-xs text-fg-muted">
                  {survey.bundled ? "Bundled with Photon Commander" : `${formatBytes(survey.cachedBytes)} cached`}
                </p>
              </div>
              {!survey.bundled && survey.cachedBytes > 0 && (
                confirmSurvey === survey.id ? (
                  <div className="flex gap-2">
                    <Button variant="secondary" onClick={() => { setConfirmSurvey(null); }}>Cancel</Button>
                    <Button
                      variant="danger"
                      disabled={clearSurvey.isPending}
                      onClick={() => {
                        clearSurvey.mutate(survey.id, { onSuccess: () => { setConfirmSurvey(null); } });
                      }}
                    >
                      Clear
                    </Button>
                  </div>
                ) : (
                  <Button variant="secondary" onClick={() => { setConfirmSurvey(survey.id); }}>Clear</Button>
                )
              )}
            </div>
          ))}
        </div>
      )}

      <div className="mt-4 flex min-h-14 items-center justify-between gap-3 border-t border-border pt-3">
        <div>
          <p className="text-sm font-medium text-fg">Target-field images</p>
          <p className="text-xs text-fg-muted">
            {String(cutoutCount)} cached | {formatBytes(skymap?.cutouts_bytes ?? 0)}
          </p>
        </div>
        {(skymap?.cutouts_bytes ?? 0) > 0 && (
          confirmCutouts ? (
            <div className="flex gap-2">
              <Button variant="secondary" onClick={() => { setConfirmCutouts(false); }}>Cancel</Button>
              <Button
                variant="danger"
                disabled={clearCutouts.isPending}
                onClick={() => {
                  clearCutouts.mutate(undefined, { onSuccess: () => { setConfirmCutouts(false); } });
                }}
              >
                Clear all
              </Button>
            </div>
          ) : (
            <Button variant="secondary" onClick={() => { setConfirmCutouts(true); }}>Clear all</Button>
          )
        )}
      </div>

      {clearSurvey.isError && <p className="mt-3 text-sm text-danger">{clearSurvey.error.message}</p>}
      {clearCutouts.isError && <p className="mt-3 text-sm text-danger">{clearCutouts.error.message}</p>}
    </Card>
  );
}
