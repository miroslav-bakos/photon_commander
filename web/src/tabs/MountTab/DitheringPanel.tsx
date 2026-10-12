// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import { useCaptureDitherSettingsQuery, useSetCaptureDitherMutation, type CaptureDitherSettings } from "../../api/settings";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { Field } from "../../components/Field";

export function DitheringPanel() {
  const settings = useCaptureDitherSettingsQuery();

  if (settings.isPending) {
    return <p className="text-sm text-fg-muted">Loading dithering…</p>;
  }
  if (settings.isError) {
    return <p role="alert" className="text-sm text-danger">Could not load dithering.</p>;
  }

  const dither = settings.data;
  const key = `${String(dither.enabled)}:${String(dither.every_n_frames)}:${String(dither.min_pixels)}:${String(dither.max_pixels)}`;
  return <DitheringForm key={key} dither={dither} />;
}

function DitheringForm({ dither }: { dither: CaptureDitherSettings }) {
  const save = useSetCaptureDitherMutation();
  const [enabled, setEnabled] = useState(dither.enabled);
  const [everyText, setEveryText] = useState(String(dither.every_n_frames));
  const [minText, setMinText] = useState(String(dither.min_pixels));
  const [maxText, setMaxText] = useState(String(dither.max_pixels));

  return (
    <Card>
      <CardTitle>Dithering</CardTitle>
      <div className="flex flex-wrap items-end gap-3">
        <Button
          variant={enabled ? "primary" : "secondary"}
          aria-pressed={enabled}
          onClick={() => { setEnabled((value) => !value); }}
        >
          {enabled ? "Enabled" : "Disabled"}
        </Button>
        <Field label="Every (frames)" wrapperClassName="w-32" type="number" min={1} step={1} value={everyText} onChange={(event) => { setEveryText(event.target.value); }} />
        <Field label="From (px)" wrapperClassName="w-28" type="number" min={0} step={1} value={minText} onChange={(event) => { setMinText(event.target.value); }} />
        <Field label="To (px)" wrapperClassName="w-28" type="number" min={0} step={1} value={maxText} onChange={(event) => { setMaxText(event.target.value); }} />
        <Button
          disabled={save.isPending}
          onClick={() => {
            save.mutate({
              enabled,
              every_n_frames: Number(everyText),
              min_pixels: Number(minText),
              max_pixels: Number(maxText),
            });
          }}
        >
          {save.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
      <p className="mt-2 text-xs text-fg-muted">Default S30 Pro range: 30–200 px; every 6 frames.</p>
      {save.isError && <p role="alert" className="mt-2 text-sm text-danger">{save.error.message}</p>}
    </Card>
  );
}
