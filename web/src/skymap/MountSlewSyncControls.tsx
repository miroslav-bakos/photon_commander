// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Slew/Sync, from a resolved J2000 position — DESIGN.md §3.5.1's "the
 * button is the same action from either place: the popup, where a user has
 * clicked an object, or the search panel, where they have resolved a name."
 * `ObjectInfoPanel.tsx` (this dispatch) is the first caller; `TargetPicker.tsx`
 * (`web/src/tabs/PlanningTab/**`, `frontend-react`'s file — not edited here)
 * is the intended second one, importing `MountSlewSyncControls` with the
 * already-resolved `target.raH`/`target.decDeg` it holds. See this file's
 * own report note for exactly what a follow-up dispatch should import.
 *
 * Three things this file will not do silently (each is DESIGN.md §3.5.1 or
 * a coordinator note on this brief, 2026-09-10):
 *
 *  - **Frame.** `POST /api/mount/slew`/`sync` accept J2000. The server's
 *    `DeviceService` converts at command time into the live mount's declared
 *    frame, using its reported site and the pinned ephemeris. Keeping that
 *    boundary server-side avoids a second browser implementation and makes a
 *    sync safe: an unconverted sync would write a durable pointing error.
 *  - **Parked/home.** Confirms first, and the confirmation issues the pair
 *    (`unpark` then the action) — never `unpark` alone, which permits
 *    motion without performing it and would report success while leaving
 *    the scope shut (measured, DESIGN.md §10).
 *  - **A 409's `detail`** — the device's own refusal message — is shown
 *    verbatim, from whichever of `unpark`/`slew`/`sync` last failed.
 */
import { useEffect, useState } from "react";
import { Button } from "../components/Button";
import { PlateSolveIcon, SlewScopeIcon, SyncIcon } from "../components/ActionIcons";
import {
  type TargetCenteringProgress,
  type TargetCenteringResponse,
  useMountCenterProgressQuery,
  useMountCenterMutation,
  useMountState,
  useMountSyncMutation,
} from "../api/mount";
import { useSolveHereMutation } from "../api/polarAlignment";
import type { PlateSolveHereResponse } from "../api/polarAlignment";
import { useLatestOperation } from "../api/operations";
import { MAP_TOOLBAR_BUTTON_SIZE_CLASSES, MAP_TOOLBAR_PANEL_CLASSES } from "./mapToolbarStyle";

export interface MountSlewSyncTarget {
  label: string;
  raHoursJ2000: number;
  decDegJ2000: number;
}

type PendingAction = "slew" | "sync" | null;
const DEFAULT_CENTERING_TOLERANCE_ARCSEC = 30;

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function MountSlewSyncControls({
  target,
  className = "",
  toleranceText: controlledToleranceText,
  onToleranceTextChange,
  showToleranceControl = true,
  toolbar = false,
  resolveTarget,
  allowSync = true,
}: {
  target: MountSlewSyncTarget;
  className?: string;
  toleranceText?: string;
  onToleranceTextChange?: (value: string) => void;
  showToleranceControl?: boolean;
  toolbar?: boolean;
  resolveTarget?: () => Promise<MountSlewSyncTarget>;
  allowSync?: boolean;
}) {
  const mount = useMountState();
  const [pending, setPending] = useState<PendingAction>(null);
  const [targetResolutionError, setTargetResolutionError] = useState<string | null>(null);
  const [centeringOverlayOpen, setCenteringOverlayOpen] = useState(false);
  const [localToleranceText, setLocalToleranceText] = useState(String(DEFAULT_CENTERING_TOLERANCE_ARCSEC));
  const toleranceText = controlledToleranceText ?? localToleranceText;
  const setToleranceText = onToleranceTextChange ?? setLocalToleranceText;
  const center = useMountCenterMutation();
  const centeringProgress = useMountCenterProgressQuery(centeringOverlayOpen);
  const sync = useMountSyncMutation();
  const solveHere = useSolveHereMutation();
  const centerOperation = useLatestOperation("target_centering");
  const solveOperation = useLatestOperation("solve_here");
  const centerActive = centerOperation?.status === "queued" || centerOperation?.status === "running";
  const solveActive = solveOperation?.status === "queued" || solveOperation?.status === "running";
  const recoveredCenterResult = centerOperation?.status === "succeeded"
    ? centerOperation.result as unknown as TargetCenteringResponse
    : undefined;
  const recoveredSolveResult = solveOperation?.status === "succeeded"
    ? solveOperation.result as unknown as PlateSolveHereResponse
    : undefined;

  useEffect(() => {
    center.reset();
  }, [target.raHoursJ2000, target.decDegJ2000]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (centeringOverlayOpen && !center.isPending) void centeringProgress.refetch();
  }, [center.isPending, centeringOverlayOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  // Gate on the mount being connected at all (§3.5.1's own instruction): a
  // button that can only 501/refuse is worse than no button.
  if (mount === null || mount === undefined) return null;

  const caps = mount.capabilities;
  const canSlew = caps?.can_slew_async === true;
  const canSync = allowSync && caps?.can_sync === true;
  if (!canSlew && !canSync) return null;

  const needsConfirm = mount.at_park === true || mount.at_home === true;
  const belowHorizon = mount.alt_deg !== null && mount.alt_deg <= 0;
  const plateSolveUnavailableReason = needsConfirm
    ? "Plate solve unavailable while the telescope arm is closed"
    : belowHorizon
      ? "Plate solve unavailable while the telescope is at or below the horizon"
      : null;
  const toleranceArcsec = Number(toleranceText);
  const toleranceValid = Number.isFinite(toleranceArcsec) && toleranceArcsec > 0 && toleranceArcsec <= 1800;
  const solveResult = solveHere.data ?? recoveredSolveResult;
  const hasActionPopover = pending !== null
    || targetResolutionError !== null
    || solveHere.isError
    || solveHere.isPending
    || solveActive
    || solveResult !== undefined
    || sync.isError
    || (!centeringOverlayOpen && (centerActive || centerOperation?.status === "failed" || recoveredCenterResult !== undefined))
    || centeringOverlayOpen;

  async function runSlew(unparkIfNeeded = false): Promise<void> {
    if (!toleranceValid) return;
    setTargetResolutionError(null);
    let slewTarget: MountSlewSyncTarget;
    try {
      slewTarget = resolveTarget === undefined ? target : await resolveTarget();
    } catch (error) {
      setTargetResolutionError(errorMessage(error, "Could not resolve this moving target."));
      return;
    }
    setCenteringOverlayOpen(true);
    await center.mutateAsync({
      ra_hours: slewTarget.raHoursJ2000,
      dec_deg: slewTarget.decDegJ2000,
      tolerance_arcmin: toleranceArcsec / 60,
      exposure_s: 1,
      gain: 200,
      max_attempts: 3,
      ...(unparkIfNeeded ? { unpark_if_needed: true } : {}),
    });
  }
  async function runSync(unparkIfNeeded = false): Promise<void> {
    await sync.mutateAsync({
      ra_hours: target.raHoursJ2000,
      dec_deg: target.decDegJ2000,
      ...(unparkIfNeeded ? { unpark_if_needed: true } : {}),
    });
  }

  function handleClick(action: "slew" | "sync"): void {
    if (needsConfirm) {
      setPending(action);
      return;
    }
    // The mutation's own `isError`/`error` (rendered below) is the UI
    // surface for a rejection here — nothing further to do at the call
    // site, but the rejection still has to be caught or it surfaces as an
    // unhandled promise rejection rather than the mutation's error state.
    (action === "slew" ? runSlew() : runSync()).catch(() => undefined);
  }

  async function handleConfirm(): Promise<void> {
    const action = pending;
    setPending(null);
    if (action === null) return;
    // Unpark alone permits motion, it does not perform any — the arm rises
    // only once a motion command follows (measured, DESIGN.md §10). If
    // unpark itself is refused (409), its own error renders below and
    // neither slew nor sync runs.
    await (action === "slew" ? runSlew(true) : runSync(true)).catch(() => undefined);
  }

  return (
    <div className={`relative flex flex-col gap-1.5 ${className}`}>
      <div
        role={toolbar ? "toolbar" : undefined}
        aria-label={toolbar ? "Target actions" : undefined}
        className={toolbar
          ? MAP_TOOLBAR_PANEL_CLASSES
          : "flex flex-wrap gap-2"}
      >
        {canSlew && (
          <>
            {showToleranceControl && <label className="flex items-center gap-1 text-xs text-fg-muted">
              Desired deviation
              <input
                aria-label="Desired deviation (arcsec)"
                className="w-16 rounded border border-border bg-surface px-1.5 py-1 text-fg"
                type="number"
                min="1"
                max="1800"
                step="1"
                value={toleranceText}
                onChange={(event) => {
                  setToleranceText(event.target.value);
                  center.reset();
                }}
              />
              arcsec
            </label>}
            <Button
              variant="primary"
              className={MAP_TOOLBAR_BUTTON_SIZE_CLASSES}
              aria-label="Slew + center"
              aria-busy={center.isPending || centerActive}
              title="Slew to the target, plate solve, and center it"
              disabled={center.isPending || centerActive || !toleranceValid}
              onClick={() => {
                handleClick("slew");
              }}
            >
              <SlewScopeIcon />
            </Button>
            <Button
              className={MAP_TOOLBAR_BUTTON_SIZE_CLASSES}
              aria-label="Plate solve"
              aria-busy={solveHere.isPending || solveActive}
              title={plateSolveUnavailableReason ?? "Plate solve the current camera frame"}
              disabled={plateSolveUnavailableReason !== null || solveHere.isPending || solveActive || center.isPending || centerActive}
              onClick={() => {
                solveHere.mutate({ exposure_s: 1, gain: 200 });
              }}
            >
              <PlateSolveIcon />
            </Button>
          </>
        )}
        {canSync && (
          <Button
            className={MAP_TOOLBAR_BUTTON_SIZE_CLASSES}
            aria-label="Sync"
            aria-busy={sync.isPending}
            title="Sync the mount to this target position"
            disabled={sync.isPending}
            onClick={() => {
              handleClick("sync");
            }}
          >
            <SyncIcon />
          </Button>
        )}
      </div>

      {hasActionPopover && (
        <div
          data-testid="target-action-popover"
          className="absolute bottom-[calc(100%+0.5rem)] right-0 z-30 max-h-[min(70vh,32rem)] w-[min(24rem,calc(100vw-1rem))] overflow-y-auto rounded-lg border border-border-strong bg-surface-raised p-3 text-sm shadow-xl"
        >
      {pending !== null && (
        <div role="alertdialog" aria-label={`Confirm ${pending}`} className="flex flex-col gap-2 rounded-md border border-border-strong bg-surface-raised p-2 text-xs">
          <p className="text-fg">
            {pending === "slew"
              ? `The mount is parked or at home. Unpark, slew to ${target.label}, and center by plate solving?`
              : // Sync asserts "the scope is pointing at this object right now" —
                // it corrects the mount's pointing model rather than moving
                // anything, so the confirmation says exactly that, not just
                // "are you sure?" (coordinator, 2026-09-10).
                `The mount is parked or at home. Unpark, then tell the mount it is ` +
                `currently pointing at ${target.label}? Only confirm this if that is ` +
                `genuinely true — an incorrect sync injects pointing error that makes ` +
                `every later slew worse.`}
          </p>
          <div className="flex gap-2">
            <Button
              variant="primary"
              onClick={() => {
                void handleConfirm();
              }}
            >
              {pending === "slew" ? "Unpark and center" : "Unpark and sync"}
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                setPending(null);
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}
      {targetResolutionError !== null && (
        <p className="rounded-md border border-danger px-2 py-1.5 text-xs text-danger" role="alert">
          {targetResolutionError}
        </p>
      )}

      {solveHere.isError && (
        <p role="alert" className="text-xs text-danger">
          {errorMessage(solveHere.error, "Plate solve failed.")}
        </p>
      )}
      {(solveHere.isPending || solveActive) && (
        <p role="status" className="text-xs text-fg-muted">
          Capturing a 1-second frame, downloading it, then plate solving…
        </p>
      )}
      {solveResult !== undefined && (
        <p role="status" className="text-xs text-success">
          Solved at RA {solveResult.ra_j2000_hours.toFixed(4)}h, Dec{" "}
          {solveResult.dec_j2000_deg.toFixed(3)}°, rotation{" "}
          {solveResult.rotation_deg.toFixed(2)}°.
        </p>
      )}
      {sync.isError && (
        <p role="alert" className="text-xs text-danger">
          {errorMessage(sync.error, "Sync failed.")}
        </p>
      )}
      {!centeringOverlayOpen && centerActive && (
        <p role="status" className="text-xs text-fg-muted">{centerOperation.message}</p>
      )}
      {!centeringOverlayOpen && centerOperation?.status === "failed" && (
        <p role="alert" className="text-xs text-danger">{centerOperation.error}</p>
      )}
      {!centeringOverlayOpen && recoveredCenterResult !== undefined && (
        <p role="status" className="text-xs text-success">
          Last centering converged to {recoveredCenterResult.attempts.at(-1)?.error_arcmin.toFixed(2) ?? "unknown"}′.
        </p>
      )}
      {centeringOverlayOpen && (
        <CenteringProgressOverlay
          target={target}
          progress={centeringProgress.data}
          result={center.data ?? recoveredCenterResult}
          requestPending={center.isPending || centerActive}
          requestError={center.isError
            ? errorMessage(center.error, "Target centering failed.")
            : centerOperation?.status === "failed" ? centerOperation.error : null}
          onClose={() => {
            setCenteringOverlayOpen(false);
          }}
        />
      )}
        </div>
      )}
    </div>
  );
}

const CENTERING_STEPS = [
  { stage: "slewing", label: "Slew" },
  { stage: "settling", label: "Settle" },
  { stage: "tracking", label: "Tracking" },
  { stage: "capturing_solving", label: "Capture + solve" },
  { stage: "evaluating", label: "Confirm" },
] as const;

function CenteringProgressOverlay({
  target,
  progress,
  result,
  requestPending,
  requestError,
  onClose,
}: {
  target: MountSlewSyncTarget;
  progress: TargetCenteringProgress | undefined;
  result: TargetCenteringResponse | undefined;
  requestPending: boolean;
  requestError: string | null;
  onClose: () => void;
}) {
  const isThisTarget =
    typeof progress?.target_ra_j2000_h === "number" &&
    typeof progress.target_dec_j2000_deg === "number" &&
    Math.abs(progress.target_ra_j2000_h - target.raHoursJ2000) < 1e-8 &&
    Math.abs(progress.target_dec_j2000_deg - target.decDegJ2000) < 1e-8;
  const current = isThisTarget ? progress : undefined;
  const attempts = current?.attempts ?? result?.attempts ?? [];
  const finished = !requestPending && (result !== undefined || requestError !== null);
  const succeeded = result?.converged === true || current?.status === "succeeded";
  const stageIndex = CENTERING_STEPS.findIndex((step) => step.stage === current?.stage);
  const resultMessage = result === undefined
    ? null
    : result.converged
      ? `Centered to ${result.attempts.at(-1)?.error_arcmin.toFixed(2) ?? "—"}′ in ${String(result.attempts.length)} attempt(s).`
      : `Not within ${result.tolerance_arcmin.toFixed(2)}′ after ${String(result.attempts.length)} attempts; final error ${result.attempts.at(-1)?.error_arcmin.toFixed(2) ?? "—"}′.`;
  const message = requestError ?? resultMessage ?? current?.message ?? "Starting the centering operation…";

  return (
      <section
        role="dialog"
        aria-label="Slew and center progress"
        className="flex flex-col gap-4 text-fg"
      >
        <div>
          <p className="text-xs uppercase tracking-wide text-fg-muted">Slew + center</p>
          <h2 className="mt-1 text-lg font-semibold">{target.label}</h2>
          <p className="mt-1 font-mono text-xs text-fg-muted">
            RA {target.raHoursJ2000.toFixed(5)}h · Dec {target.decDegJ2000.toFixed(4)}°
          </p>
        </div>

        <ol className="grid grid-cols-5 gap-1" aria-label="Centering stages">
          {CENTERING_STEPS.map((step, index) => {
            const complete = succeeded || stageIndex > index || (current?.stage === "correcting" && index < 5);
            const active = !finished && (stageIndex === index || (current?.stage === "correcting" && index === 4));
            return (
              <li key={step.stage} className="flex min-w-0 flex-col items-center gap-1 text-center text-[10px] text-fg-muted">
                <span
                  className={`h-2.5 w-2.5 rounded-full border ${
                    complete
                      ? "border-success bg-success"
                      : active
                        ? "border-accent bg-accent"
                        : "border-border-strong bg-surface"
                  }`}
                />
                <span>{step.label}</span>
              </li>
            );
          })}
        </ol>

        <div
          role={requestError === null ? "status" : "alert"}
          className={`rounded-md border p-3 text-sm ${
            requestError !== null
              ? "border-danger text-danger"
              : succeeded
                ? "border-success text-success"
                : "border-border text-fg"
          }`}
        >
          <p className="font-medium">
            {requestPending
              ? `Attempt ${String(current?.attempt ?? 1)} of ${String(current?.max_attempts ?? 3)}`
              : succeeded
                ? "Location confirmed"
                : "Centering stopped"}
          </p>
          <p className="mt-1 text-xs">{message}</p>
        </div>

        {attempts.length > 0 && (
          <div>
            <p className="mb-2 text-xs font-medium text-fg-muted">Plate-solve checks</p>
            <div className="flex flex-col gap-1">
              {attempts.map((attempt) => (
                <div key={attempt.attempt} className="flex items-center justify-between rounded bg-surface px-3 py-2 text-xs">
                  <span>Attempt {attempt.attempt}</span>
                  <span className={attempt.error_arcmin <= (current?.tolerance_arcmin ?? result?.tolerance_arcmin ?? 0) ? "text-success" : "text-warning"}>
                    {attempt.error_arcmin.toFixed(2)}′ error
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {finished && (
          <Button variant="primary" className="self-end" onClick={onClose}>
            Close
          </Button>
        )}
      </section>
  );
}
