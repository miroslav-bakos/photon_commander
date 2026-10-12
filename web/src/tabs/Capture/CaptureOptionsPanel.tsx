// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { Field } from "../../components/Field";
import {
  useCaptureCenterAfterDriftSettingsQuery,
  useCaptureDitherSettingsQuery,
  useSetCaptureCenterAfterDriftMutation,
  useSetCaptureDitherMutation,
  type CaptureCenterAfterDriftSettings,
  type CaptureDitherSettings,
} from "../../api/settings";

export function CaptureOptionsPanel() {
  const ditherSettings = useCaptureDitherSettingsQuery();
  const centerSettings = useCaptureCenterAfterDriftSettingsQuery();

  if (ditherSettings.isPending || centerSettings.isPending) {
    return <p className="text-sm text-fg-muted">Loading capture options…</p>;
  }
  if (ditherSettings.isError || centerSettings.isError) {
    return <p role="alert" className="text-sm text-danger">Could not load capture options.</p>;
  }

  const dither = ditherSettings.data;
  const center = centerSettings.data;
  const settingsKey = `${String(dither.enabled)}:${String(dither.every_n_frames)}:${String(dither.min_pixels)}:${String(dither.max_pixels)}:${String(center.enabled)}:${String(center.threshold_pixels)}`;
  return <CaptureOptionsForm key={settingsKey} dither={dither} center={center} />;
}

function CaptureOptionsForm({ dither, center }: { dither: CaptureDitherSettings; center: CaptureCenterAfterDriftSettings }) {
  const saveDither = useSetCaptureDitherMutation();
  const saveCenter = useSetCaptureCenterAfterDriftMutation();
  const [ditherEnabled, setDitherEnabled] = useState(dither.enabled);
  const [ditherEveryText, setDitherEveryText] = useState(String(dither.every_n_frames));
  const [ditherMinText, setDitherMinText] = useState(String(dither.min_pixels));
  const [ditherMaxText, setDitherMaxText] = useState(String(dither.max_pixels));
  const [centerEnabled, setCenterEnabled] = useState(center.enabled);
  const [driftThresholdText, setDriftThresholdText] = useState(String(center.threshold_pixels));

  return (
    <div className="space-y-3">
      <Card>
        <CardTitle>Dithering</CardTitle>
        <div className="flex flex-wrap items-end gap-3">
          <Button variant={ditherEnabled ? "primary" : "secondary"} aria-pressed={ditherEnabled} onClick={() => { setDitherEnabled((enabled) => !enabled); }}>
            {ditherEnabled ? "Enabled" : "Disabled"}
          </Button>
          <Field label="Every (frames)" wrapperClassName="w-32" type="number" min={1} step={1} value={ditherEveryText} onChange={(event) => { setDitherEveryText(event.target.value); }} />
          <Field label="From (px)" wrapperClassName="w-28" type="number" min={0} step={1} value={ditherMinText} onChange={(event) => { setDitherMinText(event.target.value); }} />
          <Field label="To (px)" wrapperClassName="w-28" type="number" min={0} step={1} value={ditherMaxText} onChange={(event) => { setDitherMaxText(event.target.value); }} />
          <Button disabled={saveDither.isPending} onClick={() => { saveDither.mutate({ enabled: ditherEnabled, every_n_frames: Number(ditherEveryText), min_pixels: Number(ditherMinText), max_pixels: Number(ditherMaxText) }); }}>
            {saveDither.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
        <p className="mt-2 text-xs text-fg-muted">Default S30 Pro range: 30–200 px; every 6 frames.</p>
        {saveDither.isError && <p role="alert" className="mt-2 text-sm text-danger">{saveDither.error.message}</p>}
      </Card>
      <Card>
        <CardTitle>Center after drift</CardTitle>
        <div className="flex flex-wrap items-end gap-3">
          <Button variant={centerEnabled ? "primary" : "secondary"} aria-pressed={centerEnabled} onClick={() => { setCenterEnabled((enabled) => !enabled); }}>
            {centerEnabled ? "Enabled" : "Disabled"}
          </Button>
          <Field label="Threshold (px)" wrapperClassName="w-36" type="number" min={1} step={1} value={driftThresholdText} onChange={(event) => { setDriftThresholdText(event.target.value); }} />
          <Button disabled={saveCenter.isPending} onClick={() => { saveCenter.mutate({ enabled: centerEnabled, threshold_pixels: Number(driftThresholdText) }); }}>
            {saveCenter.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
        <p className="mt-2 text-xs text-fg-muted">Optional: plate-solve each light and re-center before the next dither when drift exceeds the threshold. Disabled by default.</p>
        {saveCenter.isError && <p role="alert" className="mt-2 text-sm text-danger">{saveCenter.error.message}</p>}
      </Card>
    </div>
  );
}
