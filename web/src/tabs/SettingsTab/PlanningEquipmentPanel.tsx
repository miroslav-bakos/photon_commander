// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useAstroProfilesQuery } from "../../api/astro";
import { useAutoLpFilterSettingsQuery, useSetAutoLpFilterMutation, useWideCompanionSettingsQuery, useSetWideCompanionMutation } from "../../api/settings";
import { Button } from "../../components/Button";
import { Badge } from "../../components/Badge";
import { Card, CardTitle } from "../../components/Card";
import { DraftNumberField } from "../../components/DraftNumberField";
import { useConnectionStore } from "../../store/connectionStore";
import { usePlanningUiStore, type BestTargetCategory } from "../PlanningTab/planningUiStore";

const SELECT_CLASSES = "mt-1 min-h-11 w-full rounded-md border border-border-strong bg-bg px-3 text-base text-fg";

const TARGET_CATEGORIES: { id: BestTargetCategory; label: string }[] = [
  { id: "galaxies", label: "Galaxies" },
  { id: "emission_nebulae", label: "Emission and diffuse nebulae" },
  { id: "reflection_nebulae", label: "Reflection nebulae" },
  { id: "dark_nebulae", label: "Dark nebulae" },
  { id: "planetary_nebulae", label: "Planetary nebulae" },
  { id: "supernova_remnants", label: "Supernova remnants" },
  { id: "star_clusters", label: "Star clusters and asterisms" },
];

/** Equipment defaults used by Planning and its sky-map actions. */
export function PlanningEquipmentPanel() {
  const profilesQuery = useAstroProfilesQuery();
  const wideCompanion = useWideCompanionSettingsQuery();
  const saveWideCompanion = useSetWideCompanionMutation();
  const autoLp = useAutoLpFilterSettingsQuery();
  const saveAutoLp = useSetAutoLpFilterMutation();
  const activeProfileId = useConnectionStore(
    (state) => state.snapshot?.settings?.hardware_profile_id ?? null,
  );
  const chosenProfileId = usePlanningUiStore((state) => state.chosenProfileId);
  const chosenOpticId = usePlanningUiStore((state) => state.chosenOpticId);
  const setChosenOpticId = usePlanningUiStore((state) => state.setChosenOpticId);
  const centeringToleranceText = usePlanningUiStore((state) => state.centeringToleranceText);
  const setCenteringToleranceText = usePlanningUiStore((state) => state.setCenteringToleranceText);
  const altitudeLimitDeg = usePlanningUiStore((state) => state.altitudeLimitDeg);
  const setAltitudeLimitDeg = usePlanningUiStore((state) => state.setAltitudeLimitDeg);
  const bestTargetCategories = usePlanningUiStore((state) => state.bestTargetCategories);
  const setBestTargetCategories = usePlanningUiStore((state) => state.setBestTargetCategories);

  const profiles = profilesQuery.data?.profiles ?? [];
  const profile = profiles.find((candidate) => candidate.id === chosenProfileId)
    ?? profiles.find((candidate) => candidate.id === activeProfileId)
    ?? profiles[0]
    ?? null;
  const optic = profile?.optics.find((candidate) => candidate.id === chosenOpticId) ?? profile?.optics[0] ?? null;

  return (
    <Card>
      <CardTitle>Planning defaults</CardTitle>
      <div className="space-y-4">
        {profile !== null ? (
          <label className="block max-w-md text-sm font-medium text-fg-muted" htmlFor="equipment-planning-lens">
            Lens
            <select
              id="equipment-planning-lens"
              className={SELECT_CLASSES}
              value={optic?.id ?? ""}
              onChange={(event) => { setChosenOpticId(event.target.value); }}
            >
              {profile.optics.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.label} ({candidate.focal_length_mm}mm)
                </option>
              ))}
            </select>
            {optic?.distortion === "approximate" && (
              <Badge tone="warning">Wide lens — overlay is indicative only, barrel distortion ignored</Badge>
            )}
          </label>
        ) : (
          <p className="text-sm text-warning">Choose a telescope model above before selecting a Planning lens.</p>
        )}

        <label className="flex flex-col gap-1.5 text-sm font-medium text-fg-muted" htmlFor="equipment-centering-deviation">
          Desired centering deviation
          <span className="flex items-center gap-2 font-normal">
            <input
              id="equipment-centering-deviation"
              aria-label="Desired deviation (arcsec)"
              className="w-24 rounded-md border border-border-strong bg-bg px-3 py-2 text-base text-fg"
              type="number"
              min="1"
              max="1800"
              step="1"
              value={centeringToleranceText}
              onChange={(event) => { setCenteringToleranceText(event.target.value); }}
            />
            arcsec
          </span>
        </label>

        <DraftNumberField
          label="Altitude limit"
          hint="The floor used by the map ring and visibility warnings. A horizon profile adds real obstructions on top of it."
          min={0}
          max={85}
          step={1}
          value={altitudeLimitDeg}
          onValueChange={(value) => { if (value !== null) setAltitudeLimitDeg(value); }}
        />

        <fieldset>
          <legend className="text-sm font-medium text-fg">Best Tonight target families</legend>
          <p className="mt-1 text-xs text-fg-muted">Choose what may appear in recommendations. Star clusters are excluded by default.</p>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {TARGET_CATEGORIES.map((category) => (
              <label key={category.id} className="flex min-h-10 items-center gap-2 text-sm text-fg-muted">
                <input
                  type="checkbox"
                  checked={bestTargetCategories.includes(category.id)}
                  onChange={(event) => {
                    setBestTargetCategories(event.target.checked
                      ? [...bestTargetCategories, category.id]
                      : bestTargetCategories.filter((candidate) => candidate !== category.id));
                  }}
                />
                {category.label}
              </label>
            ))}
          </div>
        </fieldset>

        <label className="flex max-w-xl items-start gap-3 text-sm text-fg-muted">
          <input type="checkbox" className="mt-1" checked={wideCompanion.data?.enabled ?? false}
            disabled={wideCompanion.data === undefined || saveWideCompanion.isPending}
            onChange={(event) => { saveWideCompanion.mutate({ enabled: event.target.checked }); }} />
          <span><span className="block font-medium text-fg">Experimental: save Wide companion exposures</span>
            Scheduled Tele lights also save independent Wide FITS at the same exposure duration, up to 60 seconds. Wide streaming is disabled. Calibration remains manual; Wide darks require a covered lens.
          </span>
        </label>
        {wideCompanion.data === undefined && <p role="status" className="text-sm text-fg-muted">
          {wideCompanion.isError ? "Could not load the Wide companion setting." : "Loading the Wide companion setting…"}
        </p>}
        {wideCompanion.isError && <Button onClick={() => { void wideCompanion.refetch(); }}>Retry now</Button>}
        {saveWideCompanion.isPending && <p role="status">Saving Wide companion setting…</p>}
        {saveWideCompanion.isError && <p role="alert" className="text-sm text-danger">{saveWideCompanion.error.message}</p>}

        <label className="flex max-w-xl items-start gap-3 text-sm text-fg-muted">
          <input
            type="checkbox"
            className="mt-1"
            checked={autoLp.data?.enabled ?? false}
            disabled={autoLp.data === undefined || saveAutoLp.isPending}
            onChange={(event) => { saveAutoLp.mutate({ enabled: event.target.checked }); }}
          />
          <span>
            <span className="block font-medium text-fg">Automatically use LP for emission targets</span>
            Applies to newly scheduled emission nebulae, H II regions, planetary nebulae, and supernova remnants. Explicit plan filters stay unchanged.
          </span>
        </label>
        {autoLp.data === undefined && <p role="status" className="text-sm text-fg-muted">
          {autoLp.isError ? "Could not load the LP preference. Retry to read the saved setting." : "Loading the saved LP preference. The control will become available after the server responds."}
        </p>}
        {autoLp.isError && <Button onClick={() => { void autoLp.refetch(); }}>Retry now</Button>}
        {saveAutoLp.isPending && <p role="status" className="text-sm text-fg-muted">Saving the LP preference. Waiting for server confirmation.</p>}
        {saveAutoLp.isError && <p role="alert" className="text-sm text-danger">{saveAutoLp.error.message}</p>}
      </div>
    </Card>
  );
}
