// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { translate, useLanguage } from "../i18n/locale";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { CaptureIcon, CaptureSetIcon, LoopIcon, StopIcon } from "../components/ActionIcons";
import { SegmentedControl } from "../components/SegmentedControl";
import { Button } from "../components/Button";
import { Card, CardTitle } from "../components/Card";
import { Field } from "../components/Field";
import {
  useBuildCalibrationMasterMutation,
  useCancelCalibrationMutation,
  useCaptureFrameQualityQuery,
  useCalibrationSourceSetsQuery,
  useCaptureState,
  useStartCalibrationMutation,
  useStartExposureMutation,
  useStopCaptureLoopMutation,
  type CaptureExposureRequest,
  type FrameType,
} from "../api/capture";
import { useFilterWheelQuery } from "../api/filterwheel";
import {
  useMountState,
  useMountZenithMutation,
} from "../api/mount";
import { usePlanningUiStore } from "./PlanningTab/planningUiStore";
import { useCaptureFormStore } from "./captureFormStore";
import { useCaptureDitherSettingsQuery } from "../api/settings";
import { useAstroProfilesQuery } from "../api/astro";
import { useConnectionStore } from "../store/connectionStore";

/**
 * Light, dark, flat and bias are buildable. A bias sends the API's explicit zero
 * sentinel, which the backend resolves to the camera's live shortest exposure;
 * a physical camera cannot expose for literal zero time. Flat captures use an
 * explicit panel/sky source and optic id so calibration provenance is retained.
 */
interface FrameTypeOption {
  value: FrameType;
  label: string;
  available: boolean;
  reason?: string;
}

const FRAME_TYPE_OPTIONS: FrameTypeOption[] = [
  { value: "light", label: "Light", available: true },
  { value: "dark", label: "Dark", available: true },
  { value: "flat", label: "Flat", available: true },
  { value: "bias", label: "Bias", available: true },
];

function frameTypeLabel(type: FrameType | null | undefined): string | null {
  if (type === null || type === undefined) return null;
  return FRAME_TYPE_OPTIONS.find((o) => o.value === type)?.label ?? type;
}

function isFlatEligibleFilterName(name: string): boolean {
  return name.trim().toLowerCase() !== "dark";
}

/** Lightness carries state, not hue (`ConnectionsFooter.tsx`'s own rule,
 * repeated here rather than imported — that dot is local to its file): the
 * red-light theme is red-only, so a colour-only "in progress" signal would
 * be the same bug that file's own history documents. `bg-dot-*` are the
 * shared tokens `frontend-react.md` asks every status colour to reuse. */
function Dot({ className }: { className: string }) {
  return <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${className}`} />;
}

function formatWhen(iso: string | null): string {
  if (iso === null) return "never";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return iso;
  return when.toLocaleString();
}

function formatMetric(value: number | null | undefined, suffix = ""): string {
  return value == null ? "Unknown" : `${value.toFixed(2)}${suffix}`;
}

function captureStageLabel(stage: string | null | undefined): string {
  switch (stage) {
    case "dithering":
      return "Dithering";
    case "downloading":
      return "Downloading";
    case "transferring":
      return "Transferring";
    case "saving":
      return "Saving";
    case "exposing":
    default:
      return "Exposing";
  }
}

/**
 * Triggers one exposure over `POST /api/capture/exposure` (DESIGN.md §9.5;
 * verified live 2026-09-10: a 1s exposure catalogued in 8.13s, a 16.6 MB
 * FITS with real headers). Lives on the Live tab — "what the scope is doing
 * now" already lives on the Capture tab (`CaptureTab`) and a capture in
 * flight is exactly that.
 *
 * **`202` means accepted, not done.** This component never tracks "is a
 * capture running" in its own state — `useCaptureState()` reads
 * `snapshot.capture.in_progress` from the WS-fed store, so a page reload
 * (or this component remounting) shows the same thing the server does
 * (brief verification requirement 1). The mutation below only ever disables
 * the button for the round trip of *starting* the request; the button stays
 * disabled afterwards because `captureState.in_progress` says so, not
 * because the mutation is still pending.
 */
export function CapturePanel({ frameType: controlledFrameType, onFrameTypeChange, showFrameType = true }: { frameType?: FrameType; onFrameTypeChange?: (frameType: FrameType) => void; showFrameType?: boolean }) {
  const language = useLanguage();
  const captureState = useCaptureState();
  const startExposure = useStartExposureMutation();
  const stopCaptureLoop = useStopCaptureLoopMutation();
  const startCalibration = useStartCalibrationMutation();
  const buildCalibrationMaster = useBuildCalibrationMasterMutation();
  const cancelCalibration = useCancelCalibrationMutation();
  const filterWheel = useFilterWheelQuery();
  const mount = useMountState();
  const slewZenith = useMountZenithMutation();
  const ditherSettings = useCaptureDitherSettingsQuery();
  const profiles = useAstroProfilesQuery();
  const profileId = useConnectionStore((s) => s.snapshot?.settings?.hardware_profile_id);
  const wideAvailable = profiles.data?.profiles.find((p) => p.id === profileId)?.capabilities.wide_camera === true;
  const lastQuality = useCaptureFrameQualityQuery(captureState?.last_frame_id);
  const resolvedTargetName = usePlanningUiStore((s) => s.target?.name ?? null);
  const captureForm = useCaptureFormStore();
  const [coverConfirmation, setCoverConfirmation] = useState({ optic: "", kind: "", confirmed: false });
  const {
    frameType: localFrameType,
    durationText,
    loopCountText,
    gainText,
    objectText,
    flatSource,
    opticId,
    calibrationCountText,
    selectedFilters,
    generateMaster,
    keepOnlyMaster,
    update: updateCaptureForm,
    syncPlanningTarget,
  } = captureForm;
  const frameType = controlledFrameType ?? localFrameType;
  if (coverConfirmation.optic !== opticId || coverConfirmation.kind !== frameType) {
    setCoverConfirmation({ optic: opticId, kind: frameType, confirmed: false });
  }
  const coveredLensConfirmed = coverConfirmation.optic === opticId && coverConfirmation.kind === frameType && coverConfirmation.confirmed;
  function setCoveredLensConfirmed(confirmed: boolean): void {
    setCoverConfirmation({ optic: opticId, kind: frameType, confirmed });
  }
  const calibrationSourceSets = useCalibrationSourceSetsQuery(frameType !== "light");

  const durationS = Number(durationText);
  const requestedLoopCount = frameType === "light" && loopCountText.trim() !== "" ? Number(loopCountText) : null;
  const loopCountValid = requestedLoopCount === null || (Number.isSafeInteger(requestedLoopCount) && requestedLoopCount >= 1);
  const isBias = frameType === "bias";
  const isDark = frameType === "dark";
  const isFlat = frameType === "flat";
  const pairedLightTooLong = opticId === "both" && frameType === "light" && durationS > 60;
  const durationValid = isBias || isFlat || (durationText.trim() !== "" && Number.isFinite(durationS) && durationS > 0 && !pairedLightTooLong);
  const gainNumber = gainText.trim() === "" ? null : Number(gainText);
  const gainValid = gainText.trim() === "" || (Number.isFinite(gainNumber) && Number.isInteger(gainNumber));
  const calibrationCount = Number(calibrationCountText);
  const calibrationCountValid = Number.isInteger(calibrationCount) && calibrationCount >= 1 && calibrationCount <= 500;

  const selectFrameType = (next: FrameType) => {
    onFrameTypeChange?.(next);
    updateCaptureForm({
      ...(controlledFrameType === undefined ? { frameType: next } : {}),
      calibrationCountText: next === "bias" ? "100" : next === "dark" ? "50" : "30",
    });
  };

  useEffect(() => {
    syncPlanningTarget(resolvedTargetName);
  }, [resolvedTargetName, syncPlanningTarget]);
  const flatFilterOptions =
    filterWheel.data?.names
      ?.map((name, position) => ({ name, position }))
      .filter(({ name }) => isFlatEligibleFilterName(name)) ?? [];
  const eligibleFlatFilterPositions = new Set(flatFilterOptions.map(({ position }) => position));
  const resolvedSelectedFilters = selectedFilters === null
    ? [...eligibleFlatFilterPositions]
    : selectedFilters.filter((position) => eligibleFlatFilterPositions.has(position));

  const inProgress = captureState?.in_progress === true;
  const looping = captureState?.loop_active === true;
  const cameraBusy = inProgress || looping || startExposure.isPending || startCalibration.isPending || captureState?.calibration?.status === "running";
  const cameraValid = opticId === "tele" || ((opticId === "wide" || opticId === "both") && wideAvailable);
  const includesWide = opticId === "wide" || opticId === "both";
  const needsCover = includesWide && (isDark || isBias);
  const wideCalibration = needsCover && !coveredLensConfirmed;
  const wideDitherBlocked = opticId === "wide" && frameType === "light" && ditherSettings.data?.enabled !== false;
  const loopFrameCount = captureState?.loop_completed_frames ?? 0;
  // Read once and reused below (in-progress line, last-frame line) rather
  // than re-derived per usage — both describe the same `captureState`.
  const frameTypeDisplay = frameTypeLabel(captureState?.frame_type);
  const captureStateFrameTypeLabel = frameTypeDisplay === null ? null : translate(frameTypeDisplay, language);
  const activityLabel = translate(captureStageLabel(captureState?.stage), language);
  // Before the first snapshot arrives (`undefined`) folder configuration is
  // unknown, not "configured" — the form stays usable (a 409 will still say
  // exactly what is wrong, shown verbatim below) rather than guessing.
  const folderKnownMissing = captureState !== undefined && captureState !== null && !captureState.folder_configured;
  const flatPositionError = slewZenith.error;
  const reusableSets = (calibrationSourceSets.data?.sets ?? []).filter(
    (source) => source.frame_type === frameType,
  );

  function exposureRequest(): CaptureExposureRequest {
    return {
      duration_s: isBias ? 0 : durationS,
      gain: gainNumber,
      // A dark needs no target. Computed from `frameType`, not merely left
      // disabled in the form, so a stale target typed while `light` was
      // selected can never ride along on a dark's request body.
      object: isDark || isBias ? null : objectText.trim() === "" ? null : objectText.trim(),
      frame_type: frameType,
      capture_mode: "single",
      optic_id: opticId === "both" ? "both" : opticId === "wide" ? "wide" : "tele",
      ...(isFlat ? { flat_source: flatSource } : {}),
    };
  }

  function handleCapture(): void {
    if (isFlat || (includesWide && frameType !== "light") || !cameraValid || wideCalibration || !durationValid || !gainValid || !loopCountValid || cameraBusy) return;
    startExposure.mutate(exposureRequest());
  }

  function handleLoop(): void {
    if (isFlat || (includesWide && frameType !== "light") || !cameraValid || wideCalibration || wideDitherBlocked || !durationValid || !gainValid || !loopCountValid || cameraBusy) return;
    startExposure.mutate({ ...exposureRequest(), capture_mode: "loop", ...(requestedLoopCount === null ? {} : { frame_limit: requestedLoopCount }) });
  }

  function stopLoop(): void {
    if (!looping || stopCaptureLoop.isPending) return;
    stopCaptureLoop.mutate();
  }

  function handleCalibration(): void {
    if (frameType === "light" || !cameraValid || wideCalibration || !calibrationCountValid || !gainValid || cameraBusy) return;
    startCalibration.mutate({
      kind: frameType,
      count: calibrationCount,
      gain: gainNumber,
      ...(frameType === "dark" ? { duration_s: durationS } : {}),
      optic_id: opticId.trim(),
      ...(needsCover ? { covered_lens_confirmed: coveredLensConfirmed } : {}),
      ...(frameType === "flat"
        ? { filter_positions: opticId === "wide" ? [] : resolvedSelectedFilters, flat_source: flatSource }
        : {}),
      generate_master: generateMaster,
      ...(keepOnlyMaster ? { keep_only_master: true } : {}),
    });
    setCoveredLensConfirmed(false);
  }

  async function positionForFlatPanel(): Promise<void> {
    const confirmed = window.confirm(
      translate("The mount will unpark and slew straight up to the zenith. Remove any loose phone or flat panel until motion finishes, and make sure the telescope and cables are clear. Continue?"),
    );
    if (!confirmed) return;
    await slewZenith.mutateAsync({ unpark_if_needed: true });
  }

  return (
    <Card>
      <CardTitle>Capture</CardTitle>

      <div className="mb-3">
        <SegmentedControl label="Capture camera" value={opticId}
          options={[{ value: "tele", label: "Tele" }, { value: "wide", label: "Wide", disabled: !wideAvailable }, { value: "both", label: "Both", disabled: !wideAvailable }]}
          disabled={cameraBusy}
          onChange={(camera) => { setCoveredLensConfirmed(false); updateCaptureForm({ opticId: camera }); }} />
        {!wideAvailable && <p className="mt-1 text-xs text-fg-muted">Wide camera requires a confirmed hardware profile with Wide support.</p>}
        {needsCover && <label className="mt-2 flex items-start gap-2 text-sm text-warning">
          <input type="checkbox" checked={coveredLensConfirmed} disabled={cameraBusy}
            onChange={(event) => { setCoveredLensConfirmed(event.target.checked); }} />
          Wide lens is fully covered with an opaque cap. The Tele Dark filter does not cover Wide.
        </label>}
        {opticId === "both" && frameType !== "light" && <p className="mt-1 text-xs text-fg-muted">Calibration sets run separately for Tele and Wide. Keep both lenses prepared until completion.</p>}
        {opticId === "both" && frameType === "light" && <p role="status" className="mt-1 text-xs text-fg-muted">Tele and Wide expose together for up to 60 seconds. Files are saved separately; Wide preview is paused during paired capture.</p>}
        {pairedLightTooLong && <p role="alert" className="mt-1 text-xs text-warning">Both cameras require an exposure of at most 60 seconds. Choose Tele for a longer exposure.</p>}
        {wideDitherBlocked && <p role="status" className="mt-1 text-xs text-warning">Disable dithering in Capture options before starting a Wide loop; its pixel scale is not calibrated.</p>}
      </div>

      <div className="mb-3 flex items-center gap-2 text-sm">
        <Dot className={inProgress || looping ? "bg-dot-unknown" : "bg-dot-ok"} />
        <span className="text-fg">
          {inProgress
            ? captureStateFrameTypeLabel === null
              ? looping ? `Looping · ${activityLabel}…` : `${activityLabel}…`
              : looping ? `Looping · ${activityLabel} (${captureStateFrameTypeLabel})…` : `${activityLabel} (${captureStateFrameTypeLabel})…`
            : looping
              ? "Looping · Starting next frame…"
            : "Idle"}
        </span>
      </div>

      {folderKnownMissing && (
        <p className="mb-3 rounded-md border border-warning bg-surface-raised p-2 text-sm text-warning">
          No capture folder is configured, so a capture cannot be started yet. Set one in{" "}
          <Link to="/settings" className="underline">
            Settings
          </Link>
          {" "}before capturing.
        </p>
      )}

      <div className="capture-fields grid grid-cols-2 gap-3">
        {!isFlat && (
          <Field
            label="Duration (s)"
            wrapperClassName="min-w-0"
            type="number"
            inputMode="decimal"
            step="any"
            min={0}
            value={durationText}
            disabled={isBias}
            hint={isBias ? "Bias uses the camera's shortest supported exposure." : undefined}
            onChange={(e) => {
              updateCaptureForm({ durationText: e.target.value });
            }}
          />
        )}
        {!isFlat && frameType === "light" && <Field
          label="Loop frames (optional)"
          wrapperClassName="min-w-0"
          type="number"
          min={1}
          step={1}
          value={loopCountText}
          disabled={looping}
          hint={loopCountValid ? "Blank repeats until stopped." : "Enter a positive whole number."}
          onChange={(e) => { updateCaptureForm({ loopCountText: e.target.value }); }}
        />}
        <Field
          label="Gain (optional)"
          wrapperClassName="min-w-0"
          type="number"
          inputMode="numeric"
          step={1}
          value={gainText}
          onChange={(e) => {
            updateCaptureForm({ gainText: e.target.value });
          }}
        />
        {!isFlat && (
          <Field
            label="Target name (optional)"
            wrapperClassName="col-span-2 min-w-0"
            type="text"
            value={objectText}
            disabled={isDark || isBias}
            placeholder={isDark ? "Darks don't need a target" : isBias ? "Biases don't need a target" : undefined}
            hint={isDark ? "A dark needs no target." : isBias ? "A bias needs no target." : undefined}
            onChange={(e) => {
              updateCaptureForm({ objectText: e.target.value });
            }}
          />
        )}
      </div>

      {showFrameType && (
        <div className="mt-3">
          <span className="mb-1.5 block text-sm font-medium text-fg-muted">Frame type</span>
          <SegmentedControl label="Frame type" semantics="pressed" value={frameType}
            options={FRAME_TYPE_OPTIONS.map((option) => ({ value: option.value, label: option.label, disabled: !option.available, title: option.available ? undefined : option.reason }))}
            onChange={selectFrameType} />
          <p className="mt-1.5 text-xs text-fg-muted">
            Bias uses the camera's live minimum exposure. The flat wizard finds a safe exposure
            independently for each selected filter and warns when illumination banding is detected.
          </p>
        </div>
      )}

      {isFlat && (
        <div className="mt-3 rounded-md border border-border p-3">
          <p className="mb-2 text-sm font-medium text-fg-muted">Flat wizard</p>
          <p className="mb-2 text-xs text-fg-muted">
            Illuminate the selected optic with a flat panel or the twilight sky before starting.
            Exposure starts at the camera's shortest supported duration and is increased automatically
            until the illumination reaches the safe target level.
          </p>
          <div className="mb-3 rounded-md border border-warning/60 bg-surface-raised p-2">
            <p className="text-xs text-fg-muted">
              For a phone or panel, Photon Commander can point the objective straight up so its face is horizontal.
            </p>
            <Button
              className="mt-2"
              variant="secondary"
              disabled={slewZenith.isPending || mount === null || mount === undefined}
              onClick={() => { void positionForFlatPanel(); }}
            >
              {slewZenith.isPending ? "Positioning and stopping tracking…" : "Position for flat panel…"}
            </Button>
          </div>
          <SegmentedControl label="Flat source" semantics="pressed" value={flatSource}
            options={[{ value: "panel", label: "Panel" }, { value: "sky", label: "Twilight sky" }]}
            onChange={(source) => { updateCaptureForm({ flatSource: source }); }} />
          <div className="mt-3">
            <span className="mb-1.5 block text-sm font-medium text-fg-muted">Filters</span>
            {filterWheel.data?.names == null ? (
              <p className="text-xs text-fg-muted">Connect the filter wheel to select flat sets.</p>
            ) : flatFilterOptions.length === 0 ? (
              <p className="text-xs text-fg-muted">No illuminated filter positions are available for flats.</p>
            ) : (
              <div className="flex flex-wrap gap-3">
                {flatFilterOptions.map(({ name, position }) => (
                  <label key={`${String(position)}:${name}`} className="flex items-center gap-1.5 text-sm text-fg">
                    <input
                      type="checkbox"
                      checked={resolvedSelectedFilters.includes(position)}
                      onChange={(event) => {
                        updateCaptureForm({
                          selectedFilters: event.target.checked
                            ? [...resolvedSelectedFilters, position].sort((a, b) => a - b)
                            : resolvedSelectedFilters.filter((selected) => selected !== position),
                        });
                      }}
                    />
                    {name.trim() || `Slot ${String(position + 1)}`}
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {frameType !== "light" && (
        <div className="mt-3 rounded-md border border-border p-3">
          <p className="mb-2 text-sm font-medium text-fg-muted">Automatic calibration set</p>
          <Field
            label={isFlat ? "Frames per filter" : "Number of frames"}
            wrapperClassName="w-40"
            type="number"
            min={1}
            max={500}
            step={1}
            value={calibrationCountText}
            onChange={(event) => { updateCaptureForm({ calibrationCountText: event.target.value }); }}
          />
          <label className="mt-3 flex items-center gap-2 text-sm text-fg">
            <input
              type="checkbox"
              checked={generateMaster}
              onChange={(event) => {
                updateCaptureForm({
                  generateMaster: event.target.checked,
                  ...(!event.target.checked ? { keepOnlyMaster: false } : {}),
                });
              }}
            />
            Generate master automatically
          </label>
          <label className="mt-3 flex items-center gap-2 text-sm text-fg">
            <input
              type="checkbox"
              checked={keepOnlyMaster}
              disabled={!generateMaster}
              onChange={(event) => { updateCaptureForm({ keepOnlyMaster: event.target.checked }); }}
            />
            Keep only master; delete individual frames after verification
          </label>
          <p className="mt-2 text-xs text-fg-muted">
            {keepOnlyMaster
              ? "Individual frames are deleted only after every requested master passes verification. A failed build keeps them."
              : "Raw calibration frames are retained. Generated masters can be archived and restored from Archive."}
          </p>
          {isFlat && generateMaster && (
            <p className="mt-2 text-xs text-fg-muted">
              Before any flat is captured, Photon Commander verifies that at least 10 retained
              bias frames match the selected lens, gain and camera dimensions.
            </p>
          )}
          <p className="mt-2 text-xs text-fg-muted">
            Stored under Calibration/{isFlat ? "flats/<filter>" : isDark ? "darks/singles" : "bias/singles"}/tele or wide.
          </p>
          {reusableSets.length > 0 && (
            <div className="mt-3 border-t border-border pt-3">
              <p className="text-sm font-medium text-fg-muted">Retained raw sets</p>
              <ul className="mt-2 space-y-2">
                {reusableSets.map((source) => (
                  <li
                    key={source.frame_ids.join("-")}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-2"
                  >
                    <span className="text-xs text-fg-muted">
                      {source.optic_id ?? "tele"} · {source.frame_count} {source.filter_name ?? source.frame_type} frames · gain {source.gain ?? "unknown"}
                      {source.exposure_s === null ? "" : ` · ${String(source.exposure_s)} s`}
                      {` · ${String(source.width)}×${String(source.height)}`}
                      {` · ${formatWhen(source.ended_at)}`}
                      {source.frame_type === "flat" && source.compatible_bias_frames > 0
                        ? ` · ${String(source.compatible_bias_frames)} compatible biases`
                        : ""}
                    </span>
                    <Button
                      variant="secondary"
                      disabled={!source.can_build || inProgress || buildCalibrationMaster.isPending}
                      title={source.blocker ?? undefined}
                      onClick={() => { buildCalibrationMaster.mutate(source.frame_ids); }}
                    >
                      {buildCalibrationMaster.isPending ? "Starting build…" : "Build master from saved frames"}
                    </Button>
                    {source.blocker !== null && (
                      <p className="basis-full text-xs text-warning">{source.blocker}</p>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {calibrationSourceSets.isError && (
            <p role="alert" className="mt-2 text-sm text-danger">
              {calibrationSourceSets.error.message}
            </p>
          )}
        </div>
      )}

      <div className="mt-3">
        {!isFlat && (
          <>
            <Button
              variant="primary"
              disabled={!cameraValid || (includesWide && frameType !== "light") || wideCalibration || !durationValid || !gainValid || cameraBusy}
              onClick={handleCapture}
            >
              <CaptureIcon />
              {inProgress ? "Capture in progress…" : startExposure.isPending ? "Starting…" : "Capture"}
            </Button>
            {looping ? (
              <Button className="ml-2" variant="danger" disabled={stopCaptureLoop.isPending} onClick={stopLoop}>
                <StopIcon />
                {stopCaptureLoop.isPending ? "Stopping…" : "Stop loop"}
              </Button>
            ) : (
              <Button
                className="ml-2"
                variant="secondary"
                disabled={!cameraValid || (includesWide && frameType !== "light") || wideCalibration || wideDitherBlocked || !durationValid || !gainValid || !loopCountValid || cameraBusy}
                onClick={handleLoop}
              >
                <LoopIcon />
                Loop
              </Button>
            )}
          </>
        )}
        {isFlat && looping && (
          <Button variant="danger" disabled={stopCaptureLoop.isPending} onClick={stopLoop}>
            <StopIcon />
            {stopCaptureLoop.isPending ? "Stopping…" : "Stop loop"}
          </Button>
        )}
        {frameType !== "light" && (
          <Button
            variant={isFlat ? "primary" : "secondary"}
            className={isFlat ? undefined : "ml-2"}
            disabled={
              !cameraValid || wideCalibration || cameraBusy || !durationValid || !gainValid || !calibrationCountValid ||
              (isFlat && opticId !== "wide" && resolvedSelectedFilters.length === 0)
            }
            onClick={handleCalibration}
          >
            <CaptureSetIcon />
            {startCalibration.isPending
              ? "Starting set…"
              : isFlat
                ? "Start automatic flat set"
                : `Capture ${frameType} set`}
          </Button>
        )}
      </div>

      {looping && (
        <>
          <p role="status" className="mt-2 text-xs text-fg-muted">
            Loop active · {loopFrameCount}{captureState.loop_frame_limit != null ? ` / ${String(captureState.loop_frame_limit)}` : ""} completed. Stop prevents the next frame; the current exposure finishes safely.
          </p>
          {ditherSettings.data?.enabled === false && (
            <p role="alert" className="mt-2 text-sm text-warning">
              Dithering is disabled in Capture options; this loop will not dither.
            </p>
          )}
        </>
      )}

      {captureState?.calibration != null && captureState.calibration.status !== "idle" && (
        <div className="mt-3 rounded-md border border-border p-3 text-sm">
          <p className="text-fg">
            {captureState.calibration.message ?? captureState.calibration.status} ·{" "}
            {captureState.calibration.completed_frames}/{captureState.calibration.total_frames}
          </p>
          {captureState.calibration.current_filter != null && (
            <p className="mt-1 text-xs text-fg-muted">Filter: {captureState.calibration.current_filter}</p>
          )}
          {(() => {
            const latestProbe = captureState.calibration.probes.at(-1);
            return latestProbe == null ? null : (
            <p className="mt-1 text-xs text-fg-muted">
              Latest probe: {(latestProbe.median_fraction * 100).toFixed(1)}% median,
              {" "}{(latestProbe.banding_fraction * 100).toFixed(2)}% banding
            </p>
            );
          })()}
          {captureState.calibration.status === "running" && (
            <Button className="mt-2" variant="secondary" disabled={cancelCalibration.isPending} onClick={() => { cancelCalibration.mutate(); }}>
              Stop after current exposure
            </Button>
          )}
          {captureState.calibration.master_paths.map((path) => (
            <p key={path} className="mt-1 break-all text-xs text-fg-muted">Master: {path}</p>
          ))}
        </div>
      )}

      {/* Shown verbatim: a 409 (no folder configured, or one already
          running) or 501 (this surface cannot expose) explains itself
          better than a generic message (the brief's own requirement). */}
      {startExposure.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {startExposure.error instanceof Error ? startExposure.error.message : "Capture failed."}
        </p>
      )}
      {stopCaptureLoop.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {stopCaptureLoop.error instanceof Error
            ? stopCaptureLoop.error.message
            : "Capture loop could not be stopped."}
        </p>
      )}
      {startCalibration.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {startCalibration.error instanceof Error ? startCalibration.error.message : "Calibration run failed."}
        </p>
      )}
      {buildCalibrationMaster.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {buildCalibrationMaster.error instanceof Error
            ? buildCalibrationMaster.error.message
            : "Master build could not start."}
        </p>
      )}
      {flatPositionError !== null && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {flatPositionError instanceof Error
            ? flatPositionError.message
            : "Could not position the telescope for a flat panel."}
        </p>
      )}

      {captureState?.last_error !== null && captureState?.last_error !== undefined && (
        <p role="alert" className="mt-2 text-sm text-danger">
          Last capture: {captureState.last_error}
        </p>
      )}

      {(captureState?.calibration == null || captureState.calibration.status === "idle") && captureState?.last_frame_id !== null && captureState?.last_frame_id !== undefined && (
        <div className="mt-2 text-xs text-fg-muted">
          <p>
            Last frame #{captureState.last_frame_id}
            {captureStateFrameTypeLabel !== null && ` (${captureStateFrameTypeLabel})`} ·{" "}
            {formatWhen(captureState.last_captured_at ?? null)}
          </p>
          <p>
            Last HFR / FWHM: {formatMetric(lastQuality.data?.hfr_px)} / {formatMetric(lastQuality.data?.fwhm_px)}
            {lastQuality.data !== undefined && ` · ${String(lastQuality.data.star_count)} stars`}
          </p>
        </div>
      )}

    </Card>
  );
}
