// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { SegmentedControl } from "../../components/SegmentedControl";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { useSetAppearanceMutation, useSettingsQuery } from "../../api/settings";
import { useConnectionStore } from "../../store/connectionStore";
import { useAutoThemeResolution } from "../../theme/useAutoThemeResolution";

const EFFECTIVE_THEME_LABEL: Record<"dark" | "red", string> = { dark: "Dark", red: "Red-light" };
const BASE_ACCENTS = [
  { value: "blue", label: "Blue" },
  { value: "violet", label: "Violet" },
  { value: "green", label: "Green" },
  { value: "amber", label: "Amber" },
  { value: "rose", label: "Rose" },
  { value: "teal", label: "Teal" },
  { value: "indigo", label: "Indigo" },
  { value: "fuchsia", label: "Fuchsia" },
  { value: "orange", label: "Orange" },
  { value: "lime", label: "Lime" },
] as const;
const ACCENTS = [
  ...BASE_ACCENTS.flatMap(({ value, label }) => [
    { value, label },
    { value: `${value}-bright`, label: `${label} Bright` },
    { value: `${value}-deep`, label: `${label} Deep` },
  ]),
  { value: "h-alpha", label: "Hydrogen-alpha" },
  { value: "sulfur", label: "Sulfur" },
  { value: "oiii", label: "OIII" },
];

function formatClock(when: Date): string {
  return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * Theme is server state (`settings.theme` in the live snapshot), not a local
 * preference — every open client should agree on it. This panel edits it via
 * the same REST mutation the rest of Settings uses; `useThemeSync` is what
 * actually applies `data-theme`, from the WS event this mutation triggers.
 * Font scale (`settings.font_scale`) gets the identical treatment, via
 * `useFontScaleSync` and `data-font-scale`.
 */
export function AppearancePanel() {
  const settings = useSettingsQuery();
  const setAppearance = useSetAppearanceMutation();
  const location = useConnectionStore((s) => s.snapshot?.location ?? null);
  const isAuto = settings.data?.appearance.theme === "auto";
  // Shares one `useNightQuery` cache entry with `useThemeSync`'s own call
  // (identical request body) — this is display only, not a second fetch.
  const autoResolution = useAutoThemeResolution(isAuto);

  if (settings.isPending) return <Card>Loading appearance…</Card>;
  if (settings.isError) {
    return <Card className="text-danger">Could not load appearance settings.</Card>;
  }

  const { appearance } = settings.data;

  return (
    <Card>
      <CardTitle>Appearance</CardTitle>
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm text-fg-muted">Theme</span>
        <SegmentedControl label="Theme" semantics="pressed" value={appearance.theme}
          options={[{ value: "dark", label: "Dark" }, { value: "red", label: "Red-light" }, { value: "auto", label: "Auto (dusk/dawn)" }]}
          onChange={(theme) => { setAppearance.mutate({ ...appearance, theme }); }} />
      </div>
      {isAuto && location === null && (
        <p className="mt-2 text-xs text-warning">
          Auto needs a location to know sunset/sunrise — add one in Settings → Location. Showing Dark until then.
        </p>
      )}
      {isAuto && location !== null && autoResolution === null && (
        <p className="mt-2 text-xs text-fg-muted">Working out tonight's sunset/sunrise…</p>
      )}
      {isAuto && autoResolution !== null && (
        <p className="mt-2 text-xs text-fg-muted">
          Currently <span className="font-medium text-fg">{EFFECTIVE_THEME_LABEL[autoResolution.effective]}</span>
          {autoResolution.nextChangeAt !== null && <> · switches at {formatClock(autoResolution.nextChangeAt)}</>}
        </p>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <span className="text-sm text-fg-muted">Text size</span>
        <SegmentedControl label="Text size" semantics="pressed" value={appearance.font_scale}
          options={[{ value: "small", label: "Small" }, { value: "normal", label: "Normal" }, { value: "large", label: "Large" }]}
          onChange={(font_scale) => { setAppearance.mutate({ ...appearance, font_scale }); }} />
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <span className="text-sm text-fg-muted">Accent color</span>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Accent color">
          {ACCENTS.map((option) => (
            <Button
              key={option.value}
              variant={appearance.accent === option.value ? "primary" : "secondary"}
              aria-pressed={appearance.accent === option.value}
              onClick={() => {
                setAppearance.mutate({ ...appearance, accent: option.value });
              }}
            >
              <span className="size-4 rounded-full border border-border-strong" style={{ backgroundColor: `var(--ssc-accent-choice-${option.value})` }} aria-hidden="true" />
              {option.label}
            </Button>
          ))}
        </div>
        {appearance.theme === "red" && (
          <p className="w-full text-xs text-fg-muted">
            Red-light mode stays monochrome; this accent is used in Dark mode.
          </p>
        )}
      </div>
      <label className="mt-4 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={appearance.workflow_guide_visible !== false}
          disabled={setAppearance.isPending}
          onChange={(event) => { setAppearance.mutate({ ...appearance, workflow_guide_visible: event.currentTarget.checked }); }} />
        Show observing workflow guide
      </label>
      <div className="mt-4 border-t border-border pt-3">
        <label className="flex min-h-11 items-center gap-2 text-sm text-fg">
          <input
            type="checkbox"
            checked={appearance.background_enabled}
            onChange={(event) => {
              setAppearance.mutate({ ...appearance, background_enabled: event.currentTarget.checked });
            }}
          />
          Show M31 background
        </label>
        <label className="mt-2 grid gap-1 text-sm text-fg-muted">
          <span>Background opacity · {appearance.background_opacity_percent}%</span>
          <input
            type="range"
            min={0}
            max={40}
            step={1}
            value={appearance.background_opacity_percent}
            disabled={!appearance.background_enabled}
            className="h-11 w-full accent-accent"
            aria-label="Background opacity"
            onChange={(event) => {
              setAppearance.mutate({
                ...appearance,
                background_opacity_percent: Number(event.currentTarget.value),
              });
            }}
          />
        </label>
        <p className="text-xs text-fg-muted">
          Hidden automatically in red-light mode. Panels remain opaque for readability.
        </p>
      </div>
      {setAppearance.isError && <p role="alert" className="mt-3 text-sm text-danger">Could not save appearance settings.</p>}
    </Card>
  );
}
