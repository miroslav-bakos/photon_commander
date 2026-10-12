// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The map's layer picker, floated over the map beside the zoom buttons.
 * Renders the survey registry the host fetched (`useSkymapSurveysQuery`) —
 * every *enabled* background layer, one row each — plus the overlay group,
 * the catalogue-outline toggle, the "Sky lines" group, and (since
 * 2026-09-10) a "Target field" cutout group.
 *
 * The disabled AllWISE/SDSS "cannot be a background, only a target-field
 * cutout" rows and the separate "Target field" per-cutout-source group
 * (Fetch/Retry/visibility/opacity per source) were removed 2026-09-08 —
 * product owner's own call, answering a direct question about what is
 * obsolete in this menu — now that a *background* survey's own cache can be
 * deepened around a resolved target directly (`SkymapService`'s Mercator
 * cone prefetch), rather than through a separate cutout-image mechanism.
 * The registry itself is unchanged (`ssc/skymap/surveys.py`'s docstring):
 * a disabled survey is still listed there and over the API, with its
 * reason — this file simply no longer renders a row for one.
 *
 * That scoping rule was itself wrong (owner, emphatic:
 * "BUT THE TOGGLE DISSAPPEARED TOO"): a `SkyMap.tsx` bug (fixed alongside
 * this — see `cutoutLayers.ts`'s `findFieldCutoutIdBySource`) meant a
 * source the server had already fetched, complete, with real bytes on
 * disk, could still resolve to "not-fetched" client-side — and hiding that
 * state meant the one row that could have shown or hidden the image simply
 * wasn't there. Two lessons survive the fix that made the underlying bug
 * rare: "not fetched" is indistinguishable from "does not exist" to a
 * viewer watching autofetch do nothing, and a row must never depend on the
 * resolver having gotten the field-matching right to remain reachable —
 * that is exactly backwards, since a resolver bug is precisely when a
 * viewer needs the control most. So, since 2026-09-10: a source with no
 * background-survey form of its own (`CUTOUT_ONLY_SOURCE_IDS`) always gets a row, "not fetched" included, because this panel
 * is its only control surface, full stop. A source that *does* have a
 * background form (unWISE, AllWISE, SDSS, Legacy Surveys) keeps the
 * narrower rule — hidden while idle, to avoid the pre-2026-09-08 clutter —
 * except that any source whose own `layerPrefs.cutouts` entry is already
 * `visible: true` still gets a row regardless of state, idle included: a
 * stale "on" left over from an earlier, since-cleared target must always
 * have a control able to turn it back off. No source is ever drawn without
 * a row that can hide it.
 *
 * Every target is at least 44 px tall. Colours are theme classes only.
 */
import { useState } from "react";
import { createPortal } from "react-dom";
import type { SurveyModel } from "../api/skymap";
import type { CutoutSourceRow } from "./cutoutLayers";
import { CUTOUT_DEFAULT_OPACITY, type LayerPrefs, type SkyLinePrefs, type SolarSystemPrefs } from "./layerPrefs";

export interface LayersPanelProps {
  surveys: readonly SurveyModel[] | undefined;
  surveysError: string | null;
  prefs: LayerPrefs;
  onChange: (next: LayerPrefs) => void;
  /** Why the alt-az grid and meridian cannot be drawn, or `null` when they can. */
  siteUnavailableReason: string | null;
  /** When the meridian toggle is on but RA = LST is outside the current
   * view, "at RA 21h14m — off-screen" — so the toggle being on is not a
   * silent no-op the viewer has to take on faith. `null` when the meridian
   * is off or already on screen. */
  meridianHint: { text: string } | null;
  onGotoMeridian: () => void;
  /** One row per cutout source (`cutoutLayers.ts`'s `resolveCutoutRows`),
   * already resolved against the current target, the live job list and the
   * offline flag. `null`/`undefined` while the cutout-source registry
   * hasn't loaded yet — renders no "Target field" group rather than an
   * empty one, the same "no group at all" treatment `overlays` gets when
   * `surveys` is `undefined`. */
  cutoutRows?: readonly CutoutSourceRow[] | null;
}

const OVERLAY_DEFAULT_OPACITY = 0.5;

const BACKGROUND_DISPLAY_LABELS: Readonly<Record<string, string>> = {
  "dss2-color-hips-poc": "DSS2 colour",
};

function backgroundDisplayLabel(survey: SurveyModel): string {
  return BACKGROUND_DISPLAY_LABELS[survey.id] ?? survey.label;
}

/** Cutout sources with no background-survey form of their own. */
const CUTOUT_ONLY_SOURCE_IDS = new Set(["unwise"]);

export function LayersPanel({
  surveys,
  surveysError,
  prefs,
  onChange,
  siteUnavailableReason,
  meridianHint,
  onGotoMeridian,
  cutoutRows = null,
}: LayersPanelProps) {
  const [open, setOpen] = useState(false);
  const [pendingRestrictedBackground, setPendingRestrictedBackground] = useState<SurveyModel | null>(null);

  const backgrounds = (surveys ?? []).filter(
    (s) => s.enabled && !s.blend_only && (s.kind === "optical" || s.kind === "infrared"),
  );
  const overlays = (surveys ?? []).filter((s) => s.enabled && s.blend_only);
  // The scoping rule (redesigned 2026-09-10 — see the module doc comment
  // for why the first version was wrong). "no-target" (nothing resolved
  // yet) never gets a row — there is no field to show a control for.
  // "fetching"/"ready"/"unavailable" always do, exactly as before: it
  // appears the moment a fetch (autofetch or manual) starts, not only once
  // it lands, so a slow fetch does not read as though nothing were
  // happening. "not-fetched" is the case that changed: it renders whenever
  // either is true —
  //   - the source has no background-survey form (`CUTOUT_ONLY_SOURCE_IDS`)
  //     and this panel is therefore its only control, full stop; or
  //   - `layerPrefs.cutouts[sourceId].visible` is already `true` — a stale
  //     "on" left over from an earlier target must always have a row able
  //     to turn it back off, never a silent bet that nothing could be
  //     drawn.
  // A dual-form source that has never been fetched and was never turned on
  // still gets no row, preserving the pre-2026-09-08-clutter fix that rule
  // was for.
  const cutoutsWithData = (cutoutRows ?? []).filter((r) => {
    if (r.state.kind !== "not-fetched") return r.state.kind !== "no-target";
    return CUTOUT_ONLY_SOURCE_IDS.has(r.sourceId) || (prefs.cutouts[r.sourceId]?.visible ?? false);
  });

  const setSkyLine = (key: keyof SkyLinePrefs, value: boolean) => {
    onChange({ ...prefs, skyLines: { ...prefs.skyLines, [key]: value } });
  };
  const setSolarSystem = (key: keyof SolarSystemPrefs, value: boolean) => {
    onChange({ ...prefs, solarSystem: { ...prefs.solarSystem, [key]: value } });
  };

  const restrictedSourcesFor = (survey: SurveyModel): SurveyModel[] => [survey];

  const chooseBackground = (survey: SurveyModel) => {
    const restricted = restrictedSourcesFor(survey).filter(
      (candidate) => !candidate.production_cleared && prefs.restrictedImageryConsents[candidate.id] !== true,
    );
    if (restricted.length > 0) {
      setPendingRestrictedBackground(survey);
      return;
    }
    onChange({ ...prefs, background: survey.id });
  };

  return (
    // `min-h-0 flex-1`: this sits inside `SkyMap.tsx`'s own floating column
    // (`top-2 right-2 bottom-10`, a *definite* height between the credit bar
    // and the top offset), so a flex child here can actually shrink to fit
    // it instead of growing past the map card and under the credit bar.
    <div className="flex min-h-0 flex-1 flex-col items-end gap-1">
      <button
        type="button"
        aria-label="Layers"
        aria-expanded={open}
        title="Layers"
        onClick={() => {
          setOpen((v) => !v);
        }}
        className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-md border border-border-strong bg-surface/80 text-xs font-semibold leading-none text-fg backdrop-blur-sm hover:bg-surface-raised"
      >
        ≡
      </button>
      {open && (
        <div
          role="group"
          aria-label="Map layers"
          className="w-72 min-h-0 max-h-full flex-1 overflow-y-auto rounded-md border border-border-strong bg-surface/95 p-2 text-sm text-fg shadow-[0_1px_0_var(--color-shadow)] backdrop-blur-sm"
        >
          {/* Ordered the way a layers panel is read: the top of this list is
              what sits on top of the map, the bottom is what everything else
              is drawn over. Sky lines, then the mosaic grid, then the Sun/
              Moon/planets, then catalogue glyphs, then blend overlays, and
              the background survey last (owner: "survey should be the most
              bottom layer").

              Only the presentation changed. `imageryLayer.ts` has always drawn
              the background first and the overlays over it; listing the
              background at the top read as though it painted over everything
              beneath it. */}
          <Group title="Sky lines">
            <Toggle label="Equatorial grid" checked={prefs.skyLines.equatorialGrid} onChange={(v) => { setSkyLine("equatorialGrid", v); }} />
            <Toggle
              label="Alt-az grid"
              checked={prefs.skyLines.altAzGrid}
              onChange={(v) => { setSkyLine("altAzGrid", v); }}
              reason={siteUnavailableReason}
            />
            <Toggle label="Celestial equator" checked={prefs.skyLines.celestialEquator} onChange={(v) => { setSkyLine("celestialEquator", v); }} />
            <Toggle
              label="Meridian and zenith"
              checked={prefs.skyLines.meridian}
              onChange={(v) => { setSkyLine("meridian", v); }}
              reason={siteUnavailableReason}
            />
            {meridianHint !== null && (
              <div className="flex min-h-11 items-center justify-between gap-2 px-2 text-xs text-fg-muted">
                <span>{meridianHint.text}</span>
                <button
                  type="button"
                  onClick={onGotoMeridian}
                  className="rounded border border-border-strong px-2 py-1 text-xs hover:bg-surface-raised"
                >
                  Go to
                </button>
              </div>
            )}
            <Toggle label="Ecliptic" checked={prefs.skyLines.ecliptic} onChange={(v) => { setSkyLine("ecliptic", v); }} />
            <Toggle
              label="Horizon"
              checked={prefs.skyLines.horizon}
              onChange={(v) => { setSkyLine("horizon", v); }}
              reason={siteUnavailableReason}
            />
            <Toggle
              label="Constellation boundaries"
              checked={prefs.constellationBoundaries}
              onChange={(v) => {
                onChange({ ...prefs, constellationBoundaries: v });
              }}
            />
            {/* Draws or hides the ring only -- Planning's own "Altitude
             * limit" field still sets the limit itself (visibility
             * warnings, the night-altitude chart); this never disables it
             * (layerPrefs.ts's altitudeLimitRing doc comment). */}
            <Toggle
              label="Altitude limit ring"
              checked={prefs.altitudeLimitRing}
              onChange={(v) => {
                onChange({ ...prefs, altitudeLimitRing: v });
              }}
            />
                        <label className="flex min-h-11 items-center gap-2 px-2 text-xs text-fg-muted">
                <span className="w-24 shrink-0">Lines {Math.round(prefs.skyLinesOpacity * 100)}%</span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={prefs.skyLinesOpacity}
                  aria-label="Sky lines opacity"
                  className="h-11 flex-1 accent-accent"
                  onChange={(e) => {
                    onChange({ ...prefs, skyLinesOpacity: Number(e.target.value) });
                  }}
                />
              </label>
          </Group>

          <Group title="Mosaic">
            <label className="flex min-h-11 items-center gap-2 px-2 text-xs text-fg-muted">
              <span className="w-24 shrink-0">Panels {Math.round(prefs.mosaicOpacity * 100)}%</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={prefs.mosaicOpacity}
                aria-label="Mosaic panel opacity"
                className="h-11 flex-1 accent-accent"
                onChange={(e) => {
                  onChange({ ...prefs, mosaicOpacity: Number(e.target.value) });
                }}
              />
            </label>
          </Group>

          <Group title="Solar system">
            {/* Positions are topocentric (`GET /api/astro/bodies`'s own doc
             * comment — a geocentric Moon is up to 1 deg wrong), so the
             * fetch is skipped without a location the same way `SkyMap.tsx`
             * skips it (never a request sent to 409). Disabled with the same
             * `siteUnavailableReason` the alt-az grid/meridian/horizon rows
             * already show, rather than left looking like a live toggle that
             * silently does nothing. */}
            <Toggle
              label="Sun"
              checked={prefs.solarSystem.sun}
              onChange={(v) => { setSolarSystem("sun", v); }}
              reason={siteUnavailableReason}
            />
            <Toggle
              label="Moon"
              checked={prefs.solarSystem.moon}
              onChange={(v) => { setSolarSystem("moon", v); }}
              reason={siteUnavailableReason}
            />
            <Toggle
              label="Planets"
              checked={prefs.solarSystem.planets}
              onChange={(v) => { setSolarSystem("planets", v); }}
              reason={siteUnavailableReason}
            />
            <label className="flex min-h-11 items-center gap-2 px-2 text-xs text-fg-muted">
              <span className="w-24 shrink-0">Bodies {Math.round(prefs.solarSystemOpacity * 100)}%</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={prefs.solarSystemOpacity}
                aria-label="Solar system opacity"
                className="h-11 flex-1 accent-accent"
                onChange={(e) => {
                  onChange({ ...prefs, solarSystemOpacity: Number(e.target.value) });
                }}
              />
            </label>
          </Group>

          <Group title="Catalogue">
            <Row>
              <input
                type="checkbox"
                className="accent-accent"
                checked={prefs.catalogOutlines}
                onChange={(e) => {
                  onChange({ ...prefs, catalogOutlines: e.target.checked });
                }}
              />
              <span>Catalogue outlines</span>
            </Row>
            <Row>
              <input
                type="checkbox"
                className="accent-accent"
                checked={prefs.brightStars}
                onChange={(e) => {
                  onChange({ ...prefs, brightStars: e.target.checked });
                }}
              />
              <span>Named bright stars</span>
            </Row>
                        <label className="flex min-h-11 items-center gap-2 px-2 text-xs text-fg-muted">
                <span className="w-24 shrink-0">Glyphs {Math.round(prefs.catalogueOpacity * 100)}%</span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={prefs.catalogueOpacity}
                  aria-label="Catalogue opacity"
                  className="h-11 flex-1 accent-accent"
                  onChange={(e) => {
                    onChange({ ...prefs, catalogueOpacity: Number(e.target.value) });
                  }}
                />
              </label>
          </Group>

          {overlays.length > 0 && (
            <Group title="Overlays">
              {overlays.map((survey) => {
                const entry = prefs.overlays[survey.id] ?? { visible: false, opacity: OVERLAY_DEFAULT_OPACITY };
                return (
                  <div key={survey.id}>
                    <Row>
                      <input
                        type="checkbox"
                        className="accent-accent"
                        checked={entry.visible}
                        onChange={(e) => {
                          onChange({
                            ...prefs,
                            overlays: { ...prefs.overlays, [survey.id]: { ...entry, visible: e.target.checked } },
                          });
                        }}
                      />
                      <span>{survey.label}</span>
                    </Row>
                    <label className="flex min-h-11 items-center gap-2 px-2 text-xs text-fg-muted">
                        <span className="w-14">Opacity</span>
                        <input
                          type="range"
                          min={0}
                          max={1}
                          step={0.05}
                          value={entry.opacity}
                          aria-label={`${survey.label} opacity`}
                          className="h-11 flex-1 accent-accent"
                          onChange={(e) => {
                            onChange({
                              ...prefs,
                              overlays: {
                                ...prefs.overlays,
                                [survey.id]: { ...entry, opacity: Number.parseFloat(e.target.value) },
                              },
                            });
                          }}
                        />
                        <span className="w-8 text-right tabular-nums">{Math.round(entry.opacity * 100)}%</span>
                    </label>
                  </div>
                );
              })}
            </Group>
          )}

          {cutoutsWithData.length > 0 && (
            <Group title="Target field">
              {cutoutsWithData.map((row) => {
                const entry = prefs.cutouts[row.sourceId] ?? { visible: false, opacity: CUTOUT_DEFAULT_OPACITY };
                return (
                  <CutoutRow
                    key={row.sourceId}
                    row={row}
                    entry={entry}
                    onToggleVisible={(visible) => {
                      onChange({ ...prefs, cutouts: { ...prefs.cutouts, [row.sourceId]: { ...entry, visible } } });
                    }}
                    onOpacityChange={(opacity) => {
                      onChange({ ...prefs, cutouts: { ...prefs.cutouts, [row.sourceId]: { ...entry, opacity } } });
                    }}
                  />
                );
              })}
            </Group>
          )}

          <Group title="Background">
            {surveys === undefined && surveysError === null && <p className="px-2 py-3 text-fg-muted">Loading surveys…</p>}
            {surveysError !== null && <p className="px-2 py-3 text-danger">{surveysError}</p>}
            {backgrounds.map((survey) => (
              <div key={survey.id}>
                <Row>
                  <input
                    type="radio"
                    name="skymap-background"
                    className="accent-accent"
                    checked={prefs.background === survey.id && (survey.production_cleared || prefs.restrictedImageryConsents[survey.id] === true)}
                    onChange={() => {
                      chooseBackground(survey);
                    }}
                  />
                  <span>{backgroundDisplayLabel(survey)}</span>
                </Row>
              </div>
            ))}
            {surveys !== undefined && (
              <Row>
                <input
                  type="radio"
                  name="skymap-background"
                  className="accent-accent"
                  checked={prefs.background === null}
                  onChange={() => {
                    onChange({ ...prefs, background: null });
                  }}
                />
                <span>None (black sky)</span>
              </Row>
            )}
            {prefs.background !== null && (
              // Dims the imagery only — the grid, outlines, labels and the
              // FOV rectangle keep full strength. Added because a bright
              // field (unWISE along the galactic plane) can drown the
              // overlay, and how much is too much is a judgement only the
              // person looking at it can make. Full brightness is the
              // default, so this changes nothing until it is moved.
              <label className="flex min-h-11 items-center gap-2 px-2 text-xs text-fg-muted">
                <span className="w-24 shrink-0">Imagery {Math.round(prefs.backgroundOpacity * 100)}%</span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={5}
                  value={Math.round(prefs.backgroundOpacity * 100)}
                  aria-label="Background imagery brightness"
                  className="min-w-0 flex-1 accent-accent"
                  onChange={(e) => {
                    onChange({ ...prefs, backgroundOpacity: Number(e.target.value) / 100 });
                  }}
                />
              </label>
            )}
          </Group>

          {pendingRestrictedBackground !== null && createPortal(
            <div
              role="alertdialog"
              aria-modal="true"
              aria-labelledby="restricted-imagery-title"
              className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
            >
              <div className="w-full max-w-md rounded-md border border-border-strong bg-surface p-4 shadow-xl">
                <h3 id="restricted-imagery-title" className="text-base font-semibold text-fg">
                  Personal-use imagery
                </h3>
                <p className="mt-2 text-sm text-fg-muted">
                  These remote images are not bundled with Photon Commander. Use them only for personal,
                  non-commercial observing and follow the providers&apos; terms and attribution requirements.
                </p>
                {restrictedSourcesFor(pendingRestrictedBackground).map((survey) => (
                  <div key={survey.id} className="mt-3 border-l-2 border-warning pl-3">
                    <p className="text-sm font-medium text-fg">{backgroundDisplayLabel(survey)}</p>
                    <p className="mt-1 text-xs text-warning">{survey.rights_notice}</p>
                    <a
                      href={survey.credit.url}
                      target="_blank"
                      rel="noreferrer"
                      className="mt-1 inline-block text-xs text-link hover:underline"
                    >
                      {survey.credit.label}
                    </a>
                  </div>
                ))}
                <div className="mt-4 flex justify-end gap-2">
                  <button
                    type="button"
                    className="min-h-11 rounded-md border border-border-strong px-3 text-sm text-fg hover:bg-surface-raised"
                    onClick={() => { setPendingRestrictedBackground(null); }}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="min-h-11 rounded-md bg-accent px-3 text-sm font-semibold text-accent-fg hover:brightness-110"
                    onClick={() => {
                      const acknowledgements = { ...prefs.restrictedImageryConsents };
                      for (const survey of restrictedSourcesFor(pendingRestrictedBackground)) {
                        acknowledgements[survey.id] = true;
                      }
                      onChange({
                        ...prefs,
                        background: pendingRestrictedBackground.id,
                        restrictedImageryConsents: acknowledgements,
                      });
                      setPendingRestrictedBackground(null);
                    }}
                  >
                    Accept and use
                  </button>
                </div>
              </div>
            </div>,
            document.body,
          )}
        </div>
      )}
    </div>
  );
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <fieldset className="mb-1 border-0 p-0">
      <legend className="px-2 pt-1 text-xs font-semibold tracking-wide text-fg-muted uppercase">{title}</legend>
      {children}
    </fieldset>
  );
}

function Row({ children, disabled = false, reason = null }: { children: React.ReactNode; disabled?: boolean; reason?: string | null }) {
  return (
    <label className={`flex min-h-11 flex-col justify-center px-2 ${disabled ? "text-fg-disabled" : ""}`}>
      <span className="flex items-center gap-2">{children}</span>
      {reason !== null && <span className="text-xs text-fg-muted">{reason}</span>}
    </label>
  );
}

function Toggle({
  label,
  checked,
  onChange,
  reason = null,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  reason?: string | null;
}) {
  const disabled = reason !== null;
  return (
    <Row disabled={disabled} reason={reason}>
      <input
        type="checkbox"
        className="accent-accent"
        checked={checked && !disabled}
        disabled={disabled}
        onChange={(e) => {
          onChange(e.target.checked);
        }}
      />
      <span>{label}</span>
    </Row>
  );
}

/** One "Target field" row. `LayersPanel`'s `cutoutsWithData` filter drops
 * "no-target" before mapping, so only the other three of `CutoutRowState`'s
 * four shapes ever reach here — "not-fetched" included, since 2026-09-10
 * (the module doc comment has why). No Fetch/Retry action: "Fetch for this
 * field" already lives elsewhere in the sky map (2026-09-08's own reasoning
 * for removing the previous group's duplicate) — this row's job is to show
 * and control what exists, not to start work.
 *
 * The leading dot reuses `--ssc-dot-*` (tokens.css) rather than inventing a
 * status colour — its meaning inverts between the dark and red-light
 * palettes, but "which state is which token" does not, so this never has to
 * know which palette is active. "not-fetched" reuses the same `unknown`
 * tone "fetching" already uses — the label text tells the two apart
 * ("Not fetched yet" is idle, "Fetching…"/"Queued…" is active work — a
 * fourth dot colour is not worth spending outside the three-tone scale
 * for a distinction the text already makes). */
function CutoutRow({
  row,
  entry,
  onToggleVisible,
  onOpacityChange,
}: {
  row: CutoutSourceRow;
  entry: { visible: boolean; opacity: number };
  onToggleVisible: (visible: boolean) => void;
  onOpacityChange: (opacity: number) => void;
}) {
  const { state } = row;

  if (state.kind === "not-fetched") {
    // No checkbox unless there is something a toggle could actually mean:
    // `entry.visible` true here is only ever a leftover from an earlier
    // target (nothing to fetch has started for *this* one), and it must
    // stay switchable off. Freshly idle and never turned on gets no
    // checkbox at all — there is nothing yet to hide.
    return (
      <div className="flex min-h-11 w-full items-center gap-2 px-2">
        <Dot tone="bg-dot-unknown" />
        {entry.visible && (
          <input
            type="checkbox"
            className="accent-accent"
            aria-label={row.label}
            checked={entry.visible}
            onChange={(e) => {
              onToggleVisible(e.target.checked);
            }}
          />
        )}
        <span>{row.label}</span>
        <span className="ml-auto text-xs text-fg-muted">{state.offline ? "Offline — not fetched" : "Not fetched yet"}</span>
      </div>
    );
  }

  if (state.kind === "fetching") {
    return (
      <div className="flex min-h-11 w-full items-center gap-2 px-2">
        <Dot tone="bg-dot-unknown" />
        <span>{row.label}</span>
        <span className="ml-auto text-xs text-fg-muted tabular-nums">
          {state.status === "queued" ? "Queued…" : "Fetching…"}
        </span>
      </div>
    );
  }

  if (state.kind === "unavailable") {
    return (
      <div className="flex min-h-11 w-full flex-col justify-center gap-0.5 px-2 py-1">
        <div className="flex items-center gap-2">
          <Dot tone="bg-dot-bad" />
          <span>{row.label}</span>
        </div>
        {/* Verbatim: the human-readable sentence, never the raw upstream
         * detail as the primary line (title/tooltip only, like the
         * pre-2026-09-08 row). */}
        <span className="pl-4 text-xs text-fg-muted" title={state.detail ?? undefined}>
          {state.reason}
        </span>
      </div>
    );
  }

  // state.kind === "ready"
  return (
    <div>
      <Row>
        <Dot tone="bg-dot-ok" />
        <input
          type="checkbox"
          className="accent-accent"
          checked={entry.visible}
          onChange={(e) => {
            onToggleVisible(e.target.checked);
          }}
        />
        <span>{row.label}</span>
      </Row>
      {entry.visible && (
        <label className="flex min-h-11 items-center gap-2 px-2 text-xs text-fg-muted">
          <span className="w-14">Opacity</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={entry.opacity}
            aria-label={`${row.label} opacity`}
            className="h-11 flex-1 accent-accent"
            onChange={(e) => {
              onOpacityChange(Number.parseFloat(e.target.value));
            }}
          />
          <span className="w-8 text-right tabular-nums">{Math.round(entry.opacity * 100)}%</span>
        </label>
      )}
    </div>
  );
}

function Dot({ tone }: { tone: "bg-dot-ok" | "bg-dot-unknown" | "bg-dot-bad" }) {
  return <span aria-hidden="true" className={`h-2 w-2 flex-shrink-0 rounded-full ${tone}`} />;
}
