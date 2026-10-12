// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState, type ReactNode } from "react";
import { PlateSolveIcon, PolarAlignmentIcon, StopIcon } from "../../components/ActionIcons";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { SegmentedControl } from "../../components/SegmentedControl";
import { Field } from "../../components/Field";
import { PaneLayout } from "../../components/PaneLayout";
import {
  useCancelPolarAlignmentMutation,
  usePolarAlignmentState,
  useRecheckPolarAlignmentMutation,
  useSolveHereMutation,
  useStartPolarAlignmentMutation,
  useStopPolarRefinementMutation,
  type PlateSolveHereResponse,
  type PolarAlignmentPointStatus,
  type PolarAlignmentAssessment,
  type PolarAlignmentStartInfo,
  type PolarAlignmentStartMode,
} from "../../api/polarAlignment";
import { useMountState } from "../../api/mount";
import { useLatestOperation } from "../../api/operations";
import { useConnectionStore } from "../../store/connectionStore";
import { usePolarDebugStore } from "../../store/polarDebugStore";
import { useNow } from "../MountTab/useNow";
import { usePolarSimulationWorkflow } from "./usePolarSimulationWorkflow";
import {
  RESULT_STALE_AFTER_MINUTES,
  UNKNOWN,
  altitudeInstruction,
  angularSeparationArcmin,
  azimuthInstruction,
  formatAngleMagnitude,
  formatArcsec,
  formatDecDeg,
  formatDeg2,
  formatRaHours,
  minutesSince,
  s30ProPaSubLimitSeconds,
} from "./format";

type Direction = "east" | "west";

/** The tab's own name for the toggle. `"custom"` is the UI's word for the
 * API's `"explicit"` — kept distinct because "explicit" reads oddly as a
 * button label, but mapped 1:1 onto `PolarAlignmentStartMode` at the
 * request boundary so there is exactly one place the two vocabularies meet. */
type StartMode = "best" | "current" | "seestar" | "custom";

const START_MODE_TO_API: Record<StartMode, PolarAlignmentStartMode> = {
  best: "best",
  current: "current",
  seestar: "seestar",
  custom: "explicit",
};

function InfoDisclosure({
  label,
  children,
  className = "",
  placement = "above",
}: {
  label: string;
  children: ReactNode;
  className?: string;
  placement?: "above" | "below";
}) {
  return (
    <details className={`group relative w-fit ${className}`}>
      <summary
        aria-label={label}
        className="flex min-h-11 min-w-11 cursor-pointer list-none items-center justify-center rounded-full text-xl text-fg-muted marker:content-none hover:bg-surface-raised hover:text-fg [&::-webkit-details-marker]:hidden"
      >
        <span aria-hidden="true">ⓘ</span>
      </summary>
      <div className={`absolute right-0 z-30 w-72 max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-surface p-3 text-left text-xs text-fg-muted shadow-[0_1px_0_var(--color-shadow)] ${placement === "below" ? "top-full mt-1" : "bottom-full mb-1"}`}>
        {children}
      </div>
    </details>
  );
}

/**
 * DESIGN.md §4.5: "its own tab, placed after Scheduling and before Live —
 * the order a night is actually set up in: plan it, schedule it, align the
 * mount, then watch it run." No captured-frame preview here — the service
 * publishes none (this brief's own scope note) — so the numbers a person at
 * the tripod needs are the layout's centrepiece instead of an overlay on a
 * picture.
 */
export function PolarAlignTab() {
  const realState = usePolarAlignmentState();
  const start = useStartPolarAlignmentMutation();
  const cancel = useCancelPolarAlignmentMutation();
  const mount = useMountState();
  const recheck = useRecheckPolarAlignmentMutation();
  const stopRefine = useStopPolarRefinementMutation();
  const deviceModel = useConnectionStore((connection) => connection.snapshot?.device?.model ?? null);
  const debugEnabled = usePolarDebugStore((debug) => debug.enabled);
  const simulationMode = usePolarDebugStore((debug) => debug.simulatePolarAlignment);
  const setSimulationMode = usePolarDebugStore((debug) => debug.setSimulatePolarAlignment);
  const simulation = usePolarSimulationWorkflow();
  const simulated = debugEnabled && simulationMode;
  const state = simulated ? simulation.state : realState;

  return (
    <div className={state?.result != null ? "[&_[data-pane-cell=secondary]]:order-first sm:[&_[data-pane-cell=secondary]]:order-none" : ""}>
      {debugEnabled && (
        <Card className="mb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>PA mode</CardTitle>
            <SegmentedControl
              semantics="pressed"
              label="PA mode"
              value={simulated ? "simulation" : "real"}
              options={[{ value: "real", label: "Real mount" }, { value: "simulation", label: "Artificial sky" }]}
              onChange={(mode) => { setSimulationMode(mode === "simulation"); }}
            />
          </div>
          {simulated && (
            <div className="mt-2 text-sm">
              <p className="font-medium text-warning">Simulation · the controls below use artificial captures and plate solves. No mount or camera commands are sent.</p>
              <p className="mt-1 text-fg-muted">Move the simulated wedge on the Mount screen, then use Recalculate here. Initial axis error is +1° altitude and −1° azimuth.</p>
              <Button className="mt-2" onClick={simulation.reset} disabled={simulation.busy}>Clear simulated run</Button>
            </div>
          )}
        </Card>
      )}
      <PaneLayout
        primary={<SettingsAndRun state={state} start={start} recheck={recheck} cancel={cancel} stopRefine={stopRefine} mount={mount} simulation={simulated ? simulation : null} />}
        secondary={<ResultPane state={state} siteLatitudeDeg={simulated ? simulation.siteLatitude : mount?.site_latitude_deg ?? 0} deviceModel={simulated ? null : deviceModel} stopRefine={stopRefine} simulated={simulated} />}
        detail={
          <div className="space-y-3">
            <PointsCard state={state} />
            <MeasurementHistoryCard state={state} deviceModel={simulated ? null : deviceModel} />
          </div>
        }
      />
    </div>
  );
}

function SettingsAndRun({
  state,
  start,
  recheck,
  cancel,
  stopRefine,
  mount,
  simulation,
}: {
  state: ReturnType<typeof usePolarAlignmentState>;
  start: ReturnType<typeof useStartPolarAlignmentMutation>;
  recheck: ReturnType<typeof useRecheckPolarAlignmentMutation>;
  cancel: ReturnType<typeof useCancelPolarAlignmentMutation>;
  stopRefine: ReturnType<typeof useStopPolarRefinementMutation>;
  mount: ReturnType<typeof useMountState>;
  simulation: ReturnType<typeof usePolarSimulationWorkflow> | null;
}) {
  const [exposureText, setExposureText] = useState("2");
  const [gainText, setGainText] = useState("200");
  const [pointCountText, setPointCountText] = useState("3");
  const [totalArcText, setTotalArcText] = useState("60");
  const [direction, setDirection] = useState<Direction>("east");
  const [startMode, setStartMode] = useState<StartMode>("best");
  const [startRaText, setStartRaText] = useState("");
  const [startDecText, setStartDecText] = useState("");
  const solveHere = useSolveHereMutation();
  const solveOperation = useLatestOperation("solve_here");
  const solveActive = solveOperation?.status === "queued" || solveOperation?.status === "running";
  const recoveredSolveResult = solveOperation?.status === "succeeded"
    ? solveOperation.result as unknown as PlateSolveHereResponse
    : undefined;
  const displayedSolveResult = solveHere.data ?? recoveredSolveResult;

  const exposureS = Number(exposureText);
  const exposureValid =
    exposureText.trim() !== "" && Number.isFinite(exposureS) && exposureS > 0 && exposureS <= 120;

  const gainNumber = gainText.trim() === "" ? null : Number(gainText);
  const gainValid =
    gainText.trim() === "" ||
    (gainNumber !== null && Number.isFinite(gainNumber) && Number.isInteger(gainNumber) && gainNumber >= 0);

  const pointCount = Number(pointCountText);
  const pointCountValid =
    Number.isFinite(pointCount) && Number.isInteger(pointCount) && pointCount >= 3 && pointCount <= 12;

  const totalArc = Number(totalArcText);
  const totalArcValid = Number.isFinite(totalArc) && totalArc > 0 && totalArc < 360;
  const separation = pointCountValid ? totalArc / (pointCount - 1) : Number.NaN;

  const startRa = Number(startRaText);
  const startRaValid =
    startMode !== "custom" ||
    (startRaText.trim() !== "" && Number.isFinite(startRa) && startRa >= 0 && startRa < 24);
  const startDec = Number(startDecText);
  const startDecValid =
    startMode !== "custom" ||
    (startDecText.trim() !== "" && Number.isFinite(startDec) && startDec >= -90 && startDec <= 90);

  const formValid = simulation !== null
    ? pointCountValid
    : exposureValid && gainValid && pointCountValid && totalArcValid && startRaValid && startDecValid;

  const running = state?.status === "running";
  const refining = state?.refinement?.status === "running";
  const liveStage = state?.refinement?.stage;
  const liveIteration = state?.refinement?.iteration ?? 0;
  const cameraBusy = running || refining || start.isPending || recheck.isPending || stopRefine.isPending || cancel.isPending || simulation?.busy === true;
  const hasRunBefore = state !== null && state !== undefined && state.status !== null;
  const recheckPointCount =
    state?.points !== undefined && state.points.length > 0
      ? state.points.length
      : state?.result?.point_count ?? pointCount;

  function handleStart(): void {
    if (!formValid || cameraBusy) return;
    if (simulation !== null) {
      void simulation.run(pointCount, false);
      return;
    }
    if (mount?.at_park === true || mount?.at_home === true) {
      const position = mount.at_park === true ? "parked" : "at Home";
      const approved = window.confirm(
        `The mount is ${position}. Polar alignment must open the arm and move the scope. Confirm that it is safe to move before continuing.`,
      );
      if (!approved) return;
    }
    start.mutate({
      point_count: pointCount,
      // step_deg's sign *is* the direction setting (PolarAlignmentStartRequest's
      // own docstring) — increasing RA is conventionally "east" along the
      // equator, so East here is the positive magnitude and West negates it.
      step_deg: direction === "east" ? separation : -separation,
      exposure_s: exposureS,
      gain: gainNumber,
      apply_refraction: true,
      auto_refine: false,
      start_point: START_MODE_TO_API[startMode],
      // The request's own validator refuses a coordinate alongside any
      // mode but "explicit" (schemas.py's `_start_coords_match_start_point`)
      // — so these keys are omitted entirely for "best"/"current", not sent
      // as `null` alongside them.
      ...(startMode === "custom" ? { start_ra_hours: startRa, start_dec_deg: startDec } : {}),
      ...(mount?.at_park === true || mount?.at_home === true ? { unpark_if_needed: true } : {}),
    });
  }

  function handleRecheck(requestedPointCount: number): void {
    if (cameraBusy) return;
    if (simulation !== null) {
      void simulation.run(requestedPointCount, true);
      return;
    }
    if (!exposureValid || !gainValid) return;
    if (mount?.at_park === true || mount?.at_home === true) {
      const position = mount.at_park === true ? "parked" : "at Home";
      const approved = window.confirm(
        `The mount is ${position}. Recheck must move through ${String(requestedPointCount)} sky positions. Confirm that it is safe to move before continuing.`,
      );
      if (!approved) return;
    }
    recheck.mutate({
      point_count: requestedPointCount,
      exposure_s: exposureS,
      gain: gainNumber,
      apply_refraction: true,
      ...(mount?.at_park === true || mount?.at_home === true ? { unpark_if_needed: true } : {}),
    });
  }

  return (
    <div className="space-y-3">
      <Card>
        <CardTitle>Settings</CardTitle>
        {simulation !== null && <p className="mb-3 text-sm text-fg-muted">Artificial sky uses a fixed 60° sweep. Point count controls how many positions are captured and solved; exposure, gain, direction, and start field are hardware settings.</p>}
        <div className="flex flex-wrap gap-3">
          {simulation === null && <>
          <Field
            label="Exposure (s)"
            wrapperClassName="w-28"
            type="number"
            inputMode="decimal"
            step="any"
            min={0}
            max={120}
            value={exposureText}
            onChange={(e) => {
              setExposureText(e.target.value);
            }}
          />
          <Field
            label="Gain (optional)"
            wrapperClassName="w-28"
            type="number"
            inputMode="numeric"
            step={1}
            min={0}
            value={gainText}
            onChange={(e) => {
              setGainText(e.target.value);
            }}
          />
          </>}
          <Field
            label="Points"
            wrapperClassName="w-24"
            type="number"
            inputMode="numeric"
            step={1}
            min={3}
            max={12}
            value={pointCountText}
            hint="5+ adds residual checks"
            onChange={(e) => {
              setPointCountText(e.target.value);
            }}
          />
          {simulation === null && <Field
            label="Total arc (°)"
            wrapperClassName="w-28"
            type="number"
            inputMode="decimal"
            step="any"
            min={0}
            max={359}
            value={totalArcText}
            onChange={(e) => {
              setTotalArcText(e.target.value);
            }}
            hint={pointCountValid && totalArcValid ? `${separation.toFixed(1)}° between points` : undefined}
          />}
        </div>

        {simulation === null && <div className="mt-3">
          <span className="mb-1.5 block text-sm font-medium text-fg-muted">Direction</span>
          <SegmentedControl<Direction>
            semantics="pressed"
            label="Direction"
            value={direction}
            options={[{ value: "east", label: "East (increasing RA)" }, { value: "west", label: "West (decreasing RA)" }]}
            onChange={setDirection}
          />
        </div>}

        {simulation === null && <div className="mt-3">
          <span className="mb-1.5 block text-sm font-medium text-fg-muted">Start point</span>
          <SegmentedControl<typeof startMode>
            semantics="pressed"
            label="Start point"
            value={startMode}
            options={[
              { value: "best", label: "Best field" },
              { value: "current", label: "Current position" },
              { value: "seestar", label: "Seestar app field" },
              { value: "custom", label: "Custom" },
            ]}
            onChange={(mode) => {
              setStartMode(mode);
              if (mode === "seestar") {
                setPointCountText("3");
                setTotalArcText("50");
                setDirection("east");
              }
            }}
          />
          <InfoDisclosure label="About the selected start point" className="mt-1 ml-auto">
            {startMode === "best" && (
              <p>
              Computed for this point count and total arc — a declination held clear of the
              horizon and off the meridian for the whole sweep. The default: it needs no
              knowledge of what you can actually see, only what the geometry allows.
              </p>
            )}
            {startMode === "current" && (
              <p>
              Starts from wherever the mount is pointed right now (RA/Dec read from its last
              poll) — the only choice that cannot slew into a tree, a wall or the mount's own
              limits, since Photon Commander does not know the horizon it is standing in front of.
              </p>
            )}
            {startMode === "seestar" && (
              <p>
              Reproduces the official app geometry recorded here: 3 points, 25° eastward,
              starting 15° west of the meridian at declination equal to this site’s latitude.
              The start coordinate is recalculated for the current time.
              </p>
            )}
            {startMode === "custom" && <p>Uses the J2000 RA and declination entered here.</p>}
          </InfoDisclosure>
          {startMode === "custom" && (
            <div className="mt-2 flex flex-wrap gap-3">
              <Field
                label="Start RA (h)"
                wrapperClassName="w-28"
                type="number"
                inputMode="decimal"
                step="any"
                min={0}
                max={24}
                value={startRaText}
                onChange={(e) => {
                  setStartRaText(e.target.value);
                }}
              />
              <Field
                label="Start Dec (°)"
                wrapperClassName="w-28"
                type="number"
                inputMode="decimal"
                step="any"
                min={-90}
                max={90}
                value={startDecText}
                onChange={(e) => {
                  setStartDecText(e.target.value);
                }}
              />
            </div>
          )}
          <StartFieldInfo start={state?.start ?? null} />
        </div>}

        {simulation === null && <InfoDisclosure label="About polar alignment settings" className="mt-1 ml-auto">
          <p>
            Measure three points, adjust the wedge or tripod, then recalculate with a fresh
            three-point sweep. Use five-point checks when close to the pole: three points have
            no fit residual, and two agreeing five-point sweeps are required for confirmation.
            A single plate solve cannot verify the RA axis after physical movement.
          </p>
          <p className="mt-2">
            Iteration time is not shown because slew, settle, and solve overhead varies; only
            exposure time is known in advance.
          </p>
          <p className="mt-2">
            This is meaningful only when the mount is physically in EQ/polar mode. Verify the
            setup by eye because the device can report “polar” while physically alt-az.
          </p>
        </InfoDisclosure>}
      </Card>

      <Card>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant={hasRunBefore ? "secondary" : "primary"}
            disabled={!formValid || cameraBusy || solveHere.isPending || solveActive}
            onClick={() => {
              handleStart();
            }}
          >
            <PolarAlignmentIcon />
            {running ? "Running…" : start.isPending ? "Starting…" : hasRunBefore ? "New measurement" : "Measure alignment"}
          </Button>
          {state?.status === "completed" && !refining && (
            <Button
              variant="primary"
              disabled={(simulation === null && (!exposureValid || !gainValid)) || cameraBusy || solveHere.isPending || solveActive}
              onClick={() => {
                handleRecheck(Math.max(3, recheckPointCount));
              }}
            >
              <PolarAlignmentIcon />
              {recheck.isPending
                ? "Starting recheck…"
                : `Recalculate · ${String(recheckPointCount)} points`}
            </Button>
          )}
          {state?.status === "completed" && !refining && recheckPointCount < 5 && (
            <Button
              variant="secondary"
              disabled={(simulation === null && (!exposureValid || !gainValid)) || cameraBusy || solveHere.isPending || solveActive}
              onClick={() => { handleRecheck(5); }}
            >
              <PolarAlignmentIcon />
              Verify · 5 points
            </Button>
          )}
          {running && (
            <Button
              variant="danger"
              disabled={simulation === null && cancel.isPending}
              onClick={() => {
                if (simulation !== null) simulation.cancel();
                else cancel.mutate();
              }}
            >
              <StopIcon />
              {cancel.isPending ? "Cancelling…" : "Cancel"}
            </Button>
          )}
        </div>
        {refining && (
          <p role="status" className="mt-2 text-sm text-fg-muted">
            Live adjustment · {liveStage === "capturing" ? "Capturing" : liveStage === "solving" ? "Solving" : "Acquiring reference"} · {liveIteration} solves
          </p>
        )}
        {state?.refinement?.status === "failed" && state.refinement.error && (
          <p role="alert" className="mt-2 text-sm text-danger">Live adjustment stopped: {state.refinement.error}</p>
        )}
        {stopRefine.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {stopRefine.error instanceof Error ? stopRefine.error.message : "Could not stop live adjustment."}
          </p>
        )}

        {/* Shown verbatim: every refusal this route can raise — already
            running, not in polar mode, site/ephemeris/position unknown, no
            solver, or no camera/telescope hosted — explains itself better
            than a generic toast (CLAUDE.md invariant 7, the brief's own
            EQ-mode-honesty requirement). */}
        {start.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {start.error instanceof Error ? start.error.message : "Could not start polar alignment."}
          </p>
        )}
        {cancel.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {cancel.error instanceof Error ? cancel.error.message : "Could not cancel."}
          </p>
        )}
        {recheck.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {recheck.error instanceof Error
              ? recheck.error.message
              : "Could not start the independent recheck."}
          </p>
        )}
        {state?.error !== null && state?.error !== undefined && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {state.error}
          </p>
        )}
      </Card>

      {simulation === null && <Card>
        <CardTitle>Solve here</CardTitle>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            disabled={!exposureValid || !gainValid || cameraBusy || solveHere.isPending || solveActive}
            onClick={() => {
              solveHere.mutate({ exposure_s: exposureS, gain: gainNumber });
            }}
          >
            <PlateSolveIcon />
            {solveHere.isPending || solveActive ? "Solving…" : "Solve here"}
          </Button>
        </div>
        {solveHere.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {solveHere.error instanceof Error ? solveHere.error.message : "Could not solve."}
          </p>
        )}
        {solveOperation?.status === "failed" && (
          <p role="alert" className="mt-2 text-sm text-danger">{solveOperation.error}</p>
        )}
        {displayedSolveResult !== undefined && (
          <SolveHereResult result={displayedSolveResult} />
        )}
        <InfoDisclosure label="About Solve here" className="mt-1 ml-auto">
          One exposure is plate-solved at the current position and compared with the mount’s
          reported coordinates. Nothing slews.
        </InfoDisclosure>
      </Card>}
    </div>
  );
}

/**
 * `solve-here`'s own result: a solved pointing next to what the mount
 * reported for the same instant. The mount fields are `null` when the
 * device hasn't reported that member yet (`PlateSolveHereResponse`'s own
 * schema) — rendered as `UNKNOWN`, never folded into the separation figure,
 * per architecture policy invariant 7.
 */
function SolveHereResult({ result }: { result: PlateSolveHereResponse }) {
  // Optional OpenAPI fields decode as `T | undefined` as well as `T | null`
  // (unset vs. explicit `None`) — normalised to `null` once here so every
  // formatter below only has invariant 7's one unknown value to handle.
  const mountRa = result.mount_ra_j2000_hours ?? null;
  const mountDec = result.mount_dec_j2000_deg ?? null;
  const mountAlt = result.mount_altitude_deg ?? null;
  const mountAz = result.mount_azimuth_deg ?? null;
  const separationArcmin =
    mountRa !== null && mountDec !== null
      ? angularSeparationArcmin(result.ra_j2000_hours, result.dec_j2000_deg, mountRa, mountDec)
      : null;

  return (
    <div className="mt-3 rounded-md border border-border bg-surface-raised p-2 text-sm">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div>
          <p className="text-xs font-semibold tracking-wide text-fg-muted uppercase">Solved</p>
          <p className="text-fg">
            {formatRaHours(result.ra_j2000_hours)} / {formatDecDeg(result.dec_j2000_deg)}
          </p>
          <p className="text-xs text-fg-muted">
            {result.solver} {result.solver_version ?? ""} · {formatArcsec(result.pixel_scale_arcsec)}/px ·
            rotation {formatDeg2(result.rotation_deg)}
          </p>
        </div>
        <div>
          <p className="text-xs font-semibold tracking-wide text-fg-muted uppercase">Mount reports</p>
          <p className="text-fg">
            {formatRaHours(mountRa)} / {formatDecDeg(mountDec)}
          </p>
          <p className="text-xs text-fg-muted">
            alt {mountAlt === null ? UNKNOWN : formatDeg2(mountAlt)} / az{" "}
            {mountAz === null ? UNKNOWN : formatDeg2(mountAz)}
          </p>
        </div>
      </div>
      <p className="mt-2 border-t border-border pt-2 text-fg">
        {separationArcmin === null
          ? `Separation from mount pointing: ${UNKNOWN} (mount has not reported a position yet).`
          : `Separation from mount pointing: ${separationArcmin.toFixed(1)}′`}
      </p>
    </div>
  );
}

const START_MODE_LABEL: Record<PolarAlignmentStartMode, string> = {
  best: "best field",
  current: "current position",
  seestar: "the Seestar app field",
  explicit: "the coordinates you entered",
};

/**
 * "Show where it chose to start" (this change's brief), from
 * `PolarAlignmentState.start` — the last run's own report, not a
 * client-side guess: only the server knows what `best_conditioned_field`
 * actually picked for this run's point count and separation.
 *
 * Altitude/azimuth lead, RA/Dec never appear here — a person at a tripod
 * in the dark thinks in the axes they can point at, not equatorial
 * coordinates (this change's brief). Every field below `mode` is `null`
 * for `current`/`explicit` (`PolarAlignmentStartInfo`'s own docstring:
 * "no computed geometry", not "unknown"), so nothing below the mode line
 * renders for those two.
 *
 * `ideal === false` is a real compromise (an altitude floor or the
 * meridian bound) and renders as a warning; `ideal === null` (no computed
 * geometry at all, `current`/`explicit`) renders nothing here — the two
 * must not look alike, per architecture policy invariant 7.
 */
function StartFieldInfo({ start }: { start: PolarAlignmentStartInfo | null }) {
  if (start === null) return null;
  const altitude = start.start_altitude_deg;
  const azimuth = start.start_azimuth_deg;
  return (
    <div className="mt-2 rounded-md border border-border bg-surface-raised p-2 text-xs text-fg-muted">
      <p>
        Last run started from <span className="font-medium text-fg">{START_MODE_LABEL[start.mode]}</span>
        {altitude !== null && azimuth !== null && (
          <>
            {" — altitude "}
            {formatDeg2(altitude)}
            {", azimuth "}
            {formatDeg2(azimuth)}
          </>
        )}
      </p>
      {start.ideal === false && (
        <div role="alert" className="mt-1.5 rounded-md border border-warning bg-surface p-1.5 text-warning">
          <p className="font-medium">
            Compromise field: an altitude floor or the meridian bound this run, so it is not the
            geometrically ideal declination.
          </p>
          {(start.warnings?.length ?? 0) > 0 && (
            <ul className="mt-1 list-disc pl-4">
              {(start.warnings ?? []).map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

const POINT_STATUS_TONE: Record<PolarAlignmentPointStatus, "neutral" | "accent" | "success" | "danger"> = {
  pending: "neutral",
  slewing: "accent",
  settling: "accent",
  capturing: "accent",
  solving: "accent",
  solved: "success",
  failed: "danger",
  aborted: "danger",
  skipped: "neutral",
};

const POINT_STATUS_LABEL: Record<PolarAlignmentPointStatus, string> = {
  pending: "Pending",
  slewing: "Slewing",
  settling: "Settling",
  capturing: "Capturing",
  solving: "Solving",
  solved: "Solved",
  failed: "Failed",
  aborted: "Aborted",
  skipped: "Skipped",
};

function PointsCard({ state }: { state: ReturnType<typeof usePolarAlignmentState> }) {
  if (state === undefined) {
    return (
      <Card title="Points" collapseKey="polarAlign.points">
        <p className="text-sm text-fg-muted">Waiting for state…</p>
      </Card>
    );
  }
  const points = state?.points ?? [];
  return (
    <Card title="Points" collapseKey="polarAlign.points">
      {state?.preflight_reason != null && (
        <p role="status" className="mb-2 text-sm text-fg-muted">{state.preflight_reason}</p>
      )}
      {points.length === 0 ? (
        <p className="text-sm text-fg-muted">No run yet.</p>
      ) : (
        <ul className="space-y-2">
          {points.map((point) => (
            <li
              key={point.index}
              className={`rounded-md border p-2 text-sm ${
                state?.current_point_index === point.index ? "border-accent" : "border-border"
              }`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium text-fg">Point {point.index + 1}</span>
                <div className="flex items-center gap-1.5">
                  {/* The backend counter includes the normal slew and first
                      capture, so 2 is success on the first try. Only actions
                      beyond that baseline represent retry activity. */}
                  {point.attempt > 2 && (
                    <Badge tone="warning">
                      {point.attempt - 2} {point.attempt === 3 ? "retry" : "retries"}
                    </Badge>
                  )}
                  <Badge tone={POINT_STATUS_TONE[point.status]}>{POINT_STATUS_LABEL[point.status]}</Badge>
                </div>
              </div>
              {point.error !== null && (
                <p role="alert" className="mt-1 text-xs text-danger">
                  {point.error}
                </p>
              )}
              {(point.planned_altitude_deg != null || point.actual_altitude_deg != null) && (
                <p className="mt-1 text-xs text-fg-muted">
                  Planned alt {point.planned_altitude_deg == null ? "unknown" : formatDeg2(point.planned_altitude_deg)} · actual alt{" "}
                  {point.actual_altitude_deg == null ? "unknown" : formatDeg2(point.actual_altitude_deg)} · az{" "}
                  {point.actual_azimuth_deg == null ? "unknown" : formatDeg2(point.actual_azimuth_deg)}
                  {point.mount_slewing !== null
                    ? ` · slewing ${point.mount_slewing ? "yes" : "no"}`
                    : ""}
                  {point.mount_tracking !== null
                    ? ` · tracking ${point.mount_tracking ? "yes" : "no"}`
                    : ""}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

type PolarErrorQuality = "good" | "fair" | "bad";

function polarErrorQuality(totalErrorDeg: number, deviceModel: string | null): PolarErrorQuality {
  const seconds = s30ProPaSubLimitSeconds(totalErrorDeg, deviceModel);
  if (seconds !== null) return seconds >= 60 ? "good" : seconds >= 30 ? "fair" : "bad";
  const totalArcsec = Math.abs(totalErrorDeg) * 3600;
  return totalArcsec < 120 ? "good" : totalArcsec <= 300 ? "fair" : "bad";
}

const POLAR_ERROR_TONE: Record<PolarErrorQuality, { border: string; text: string }> = {
  good: { border: "border-success", text: "text-success" },
  fair: { border: "border-warning", text: "text-warning" },
  bad: { border: "border-danger", text: "text-danger" },
};

function MeasurementHistoryCard({ state, deviceModel }: { state: ReturnType<typeof usePolarAlignmentState>; deviceModel: string | null }) {
  const history = state?.history ?? [];
  const newestFirst = history
    .map((entry, index) => ({ entry, index }))
    .sort((left, right) => Date.parse(right.entry.completed_at) - Date.parse(left.entry.completed_at));

  return (
    <Card title="Error history" collapseKey="polarAlign.history">
      {history.length === 0 ? (
        <p className="text-sm text-fg-muted">No completed measurements yet.</p>
      ) : (
        <ol aria-label="Polar alignment error history" className="space-y-2">
          {newestFirst.map(({ entry, index }) => {
            const tone = POLAR_ERROR_TONE[polarErrorQuality(entry.total_error_deg, deviceModel)];
            return (
              <li
                key={`${entry.completed_at}-${String(index)}`}
                className={`rounded-md border-l-4 ${tone.border} bg-surface-raised p-2 text-sm`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-fg">
                    {entry.run_kind === "measure" ? "Measurement" : "Recheck"} {index + 1}
                  </span>
                  <time dateTime={entry.completed_at} className="text-xs text-fg-muted">
                    {new Date(entry.completed_at).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </time>
                </div>
                <dl className={`mt-1 grid grid-cols-3 gap-2 tabular-nums ${tone.text}`}>
                  <div>
                    <dt className="text-xs text-fg-muted">Altitude</dt>
                    <dd className="font-semibold">{formatDeg2(entry.altitude_error_deg)}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-fg-muted">Azimuth</dt>
                    <dd className="font-semibold">{formatDeg2(entry.azimuth_error_deg)}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-fg-muted">Total</dt>
                    <dd className="font-semibold">{formatAngleMagnitude(entry.total_error_deg)}</dd>
                  </div>
                </dl>
              </li>
            );
          })}
        </ol>
      )}
    </Card>
  );
}

function ResultPane({
  state,
  siteLatitudeDeg,
  deviceModel,
  stopRefine,
  simulated,
}: {
  state: ReturnType<typeof usePolarAlignmentState>;
  siteLatitudeDeg: number;
  deviceModel: string | null;
  stopRefine: ReturnType<typeof useStopPolarRefinementMutation>;
  simulated: boolean;
}) {
  const now = useNow(1_000);
  const sweep = state?.result ?? null;
  const refinement = state?.refinement;
  const refining = refinement?.status === "running";
  const liveIteration = refinement?.iteration ?? 0;
  const live = refinement?.result ?? null;
  const result = refining || refinement?.status != null ? null : sweep;
  const assessment: PolarAlignmentAssessment | null = state?.assessment ?? null;
  const stopButton = refining ? (
    <Button
      variant="danger"
      disabled={stopRefine.isPending}
      onClick={() => { stopRefine.mutate(); }}
    >
      <StopIcon />
      {stopRefine.isPending ? "Stopping…" : "Stop live adjustment"}
    </Button>
  ) : null;
  if (result === null) {
    return (
      <Card>
        <CardTitle>{refining ? "Live adjustment" : "Result"}</CardTitle>
        {stopButton && <div className="mb-3">{stopButton}</div>}
        <p className="text-sm text-fg-muted">
          {refining
            ? `Solve ${String(liveIteration)}. A single field cannot verify polar alignment after movement. Stop live capture and run a new five-point measurement.`
            : refinement?.status != null
              ? "Live adjustment ended. Run a new measurement to verify the current alignment."
              : "No completed measurement yet. Start a run from the settings on the left."}
        </p>
        {refining && refinement.error && <p role="alert" className="mt-2 text-sm text-warning">{refinement.error}</p>}
        {refining && live?.warnings.map((warning) => (
          <p key={warning} role="alert" className="mt-2 text-sm text-warning">{warning}</p>
        ))}
      </Card>
    );
  }

  const ageMinutes = minutesSince(result.epoch, now);
  const staleMinutes =
    ageMinutes !== null && ageMinutes >= RESULT_STALE_AFTER_MINUTES ? Math.round(ageMinutes) : null;
  const quality = polarErrorQuality(result.total_error_deg, deviceModel);
  const { text: qualityToneClass, border: qualityBorderClass } = POLAR_ERROR_TONE[quality];
  const toneClass = qualityToneClass;
  const borderClass = qualityBorderClass;
  const altitudeDirection = altitudeInstruction(result.altitude_correction_deg);
  const azimuthDirection = azimuthInstruction(result.azimuth_correction_deg, siteLatitudeDeg);
  const paSubLimitSeconds = s30ProPaSubLimitSeconds(result.total_error_deg, deviceModel);
  const paSubLimitLabel = paSubLimitSeconds === null ? null
    : paSubLimitSeconds >= 120 ? "PA 120s+"
      : paSubLimitSeconds < 10 ? "PA <10s"
        : `PA ~${String(Math.round(paSubLimitSeconds / 5) * 5)}s`;
  const verdict = assessment?.verdict ?? "recheck";
  const verdictTone = simulated ? "accent" : quality === "good" ? "success" : quality === "fair" ? "warning" : "danger";

  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <CardTitle>Which knob, and how far</CardTitle>
        <div className="flex flex-wrap items-center gap-1">
          <Badge tone={verdictTone}>
            {formatAngleMagnitude(result.total_error_deg)} · {simulated ? "SIMULATED" : verdict.toUpperCase()}
            {paSubLimitLabel !== null && <> · {paSubLimitLabel}</>}
          </Badge>
          {paSubLimitLabel !== null && (
            <InfoDisclosure label="About the PA exposure estimate" placement="below">
              Approximate maximum subexposure for less than 0.5 pixel of worst-case polar-alignment
              drift at the S30 Pro tele camera&apos;s field center. Assumes ideal tracking and no wind;
              mount error, seeing, focus, and field rotation can still elongate stars.
            </InfoDisclosure>
          )}
        </div>
      </div>
      {stopButton && <div className="mb-3">{stopButton}</div>}
      {!refining && assessment !== null && (
        <div
          role="status"
          className={`mb-3 rounded-md border p-2 text-sm ${
            verdict === "confirmed"
              ? "border-success text-success"
              : verdict === "inconsistent"
                ? "border-danger text-danger"
                : "border-warning text-warning"
          }`}
        >
          <p className="font-semibold">{assessment.message}</p>
          {assessment.axis_separation_arcmin !== null && (
            <p className="mt-1 text-xs">
              Inter-sweep axis separation {assessment.axis_separation_arcmin.toFixed(2)}′ · limit{" "}
              {assessment.max_axis_separation_arcmin.toFixed(1)}′
            </p>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 sm:gap-3">
        <div className={`relative min-w-0 rounded-lg border-2 ${borderClass} bg-surface-raised p-2 pb-12 text-center sm:p-4 sm:pb-12`}>
          <p className="text-xs font-semibold tracking-wide text-fg-muted uppercase">Altitude</p>
          <p className={`mt-1 text-4xl font-bold sm:text-6xl ${toneClass}`} aria-hidden="true">
            {altitudeDirection === "UP" ? "↑" : "↓"}
          </p>
          <p className={`text-3xl font-bold tabular-nums sm:text-4xl ${toneClass}`}>
            {formatAngleMagnitude(result.altitude_correction_deg)}
          </p>
          <p className={`mt-1 text-base font-bold sm:text-xl ${toneClass}`}>{altitudeDirection}</p>
          <InfoDisclosure
            label="About the altitude correction"
            className="absolute right-1 bottom-0"
          >
            Move the polar axis {altitudeDirection.toLowerCase()} using small wedge adjustments,
            then recalculate. The displayed angle is the required axis movement, not a knob-turn
            angle. The fitted axis uncertainty is ± {formatArcsec(result.axis_uncertainty_arcsec)};
            corrections smaller than that are indistinguishable from measurement noise.
          </InfoDisclosure>
        </div>
        <div className={`relative min-w-0 rounded-lg border-2 ${borderClass} bg-surface-raised p-2 pb-12 text-center sm:p-4 sm:pb-12`}>
          <p className="text-xs font-semibold tracking-wide text-fg-muted uppercase">Azimuth</p>
          <p className={`mt-1 text-4xl font-bold sm:text-6xl ${toneClass}`} aria-hidden="true">
            {azimuthDirection === "WEST" ? "←" : "→"}
          </p>
          <p className={`text-3xl font-bold tabular-nums sm:text-4xl ${toneClass}`}>
            {formatAngleMagnitude(result.azimuth_correction_deg)}
          </p>
          <p className={`mt-1 text-base font-bold sm:text-xl ${toneClass}`}>{azimuthDirection}</p>
          <InfoDisclosure
            label="About the azimuth correction"
            className="absolute right-1 bottom-0"
          >
            Move the polar axis {azimuthDirection.toLowerCase()} using small wedge adjustments,
            then recalculate. The displayed angle is the required axis movement, not a knob-turn
            angle. The fitted axis uncertainty is ± {formatArcsec(result.axis_uncertainty_arcsec)};
            corrections smaller than that are indistinguishable from measurement noise.
          </InfoDisclosure>
        </div>
      </div>

      <div className="mt-2 flex justify-end">
        <InfoDisclosure label="About this polar alignment result">
          {staleMinutes !== null && (
            <p className="mb-2 text-warning">
              Measured {staleMinutes} minute{staleMinutes === 1 ? "" : "s"} ago. Earth rotation
              is accounted for, so age alone does not invalidate the result; run again if the
              tripod or wedge has moved.
            </p>
          )}
        {sweep?.has_residual_check ? (
          <p>
            RMS residual {formatArcsec(sweep.rms_residual_arcsec)} · max{" "}
            {formatArcsec(sweep.max_residual_arcsec)} · {sweep.residual_dof} degrees of freedom
          </p>
        ) : (
          <p>
            No residual check (degrees of freedom: {sweep?.residual_dof ?? 0}) — a {sweep?.point_count ?? 3}-point
            run fits its circle exactly.
          </p>
        )}
        <p className="mt-1">
          Axis at {formatDeg2(result.axis_altitude_deg)} alt / {formatDeg2(result.axis_azimuth_deg)} az ·
          total error {formatAngleMagnitude(result.total_error_deg)} · {sweep?.point_count ?? 3} baseline points
        </p>
        </InfoDisclosure>
      </div>
    </Card>
  );
}
