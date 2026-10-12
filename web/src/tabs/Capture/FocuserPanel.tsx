// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import { AutofocusIcon, StopIcon } from "../../components/ActionIcons";
import { Card, CardTitle } from "../../components/Card";
import { Button } from "../../components/Button";
import {
  useFocuserPositionQuery,
  useHaltFocuserMutation,
  useMoveFocuserMutation,
  useStartAutofocusMutation,
} from "../../api/focuser";
import { deviceNotesSayDisconnected } from "../../api/deviceStatus";
import { useLatestOperation } from "../../api/operations";
import { AutofocusChart } from "./AutofocusChart";
import { useAlpacaDeviceConnected } from "../../api/deviceAvailability";

/** `message` from an `ApiError` (`client.ts`) if that is what failed, else
 * a generic fallback — the same shape `SlewSyncCard`/`TrackingCard` use for
 * a mutation's own error, so a 409 refusal or 501 "not available" reaches
 * the screen verbatim rather than as a bare "request failed"
 * (architecture policy invariant 7). */
function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

/**
 * Manual focus / autofocus panel for the Capture screen (bead ssc-bkv.7),
 * beside `AlpacaDevicesPanel`/`CaptureStatusPanel` in the `detail` column.
 *
 * Position is not part of `StateSnapshot` (`api/focuser.ts`'s own header)
 * so this is the one panel on this screen driven by a polled `useQuery`
 * rather than the WS-pushed store — three honest states of its own,
 * distinct from but parallel to the `undefined`/`null`/value convention
 * used elsewhere: query still loading, query failed outright (network, a
 * stale server, …), and an answer whose `position` is itself `null`
 * ("not connected or the device did not answer",
 * `FocuserPositionResponse`'s own docstring — never guessed as `0`).
 *
 * Move/Halt/Start autofocus stay clickable in every one of those states —
 * gating them on a known position would hide a real refusal (409/501)
 * behind a disabled button, which is exactly the "silent no-op"
 * architecture policy invariant 7 rules out. Each mutation's own refusal is shown
 * verbatim once it happens instead.
 */
export function FocuserPanel() {
  const connected = useAlpacaDeviceConnected("focuser");
  const position = useFocuserPositionQuery();
  const move = useMoveFocuserMutation();
  const halt = useHaltFocuserMutation();
  const autofocus = useStartAutofocusMutation();
  const autofocusOperation = useLatestOperation("autofocus");
  const autofocusActive = autofocusOperation?.status === "queued" || autofocusOperation?.status === "running";

  const [targetText, setTargetText] = useState("");
  const [targetTouched, setTargetTouched] = useState(false);
  const [autofocusStepText, setAutofocusStepText] = useState("10");
  const [autofocusFineStepText, setAutofocusFineStepText] = useState("2");
  const [autofocusExposureText, setAutofocusExposureText] = useState("2");
  const [autofocusSettleText, setAutofocusSettleText] = useState("2");
  const displayedTargetText =
    !targetTouched && position.data?.position != null ? String(position.data.position) : targetText;
  const target = Number(displayedTargetText);
  const targetValid =
    displayedTargetText.trim() !== "" && Number.isInteger(target) && target >= 0;
  const autofocusStep = Number(autofocusStepText);
  const autofocusFineStep = Number(autofocusFineStepText);
  const autofocusExposure = Number(autofocusExposureText);
  const autofocusSettle = Number(autofocusSettleText);
  const autofocusSettingsValid =
    autofocusStepText.trim() !== "" &&
    Number.isInteger(autofocusStep) &&
    autofocusStep >= 1 &&
    autofocusFineStepText.trim() !== "" &&
    Number.isInteger(autofocusFineStep) &&
    autofocusFineStep >= 1 &&
    autofocusFineStep < autofocusStep &&
    autofocusExposureText.trim() !== "" &&
    Number.isFinite(autofocusExposure) &&
    autofocusExposure > 0 &&
    autofocusExposure <= 30 &&
    autofocusSettleText.trim() !== "" &&
    Number.isFinite(autofocusSettle) &&
    autofocusSettle >= 2 &&
    autofocusSettle <= 60;

  const anyPending = !connected || move.isPending || halt.isPending || autofocus.isPending || autofocusActive;
  const disconnected =
    !connected || (position.data !== undefined && deviceNotesSayDisconnected(
      position.data.note,
      position.data.is_moving_note,
      position.data.step_size_note,
    ));

  // `Focuser.StepSize` (ASCOM IFocuserV3) when the device reports it; this
  // hardware declares it unimplemented (`ssc/alpaca/devices/focuser.py`), so
  // it is always `null` here in practice. The fallback of 1 is not a guessed
  // physical distance — it is the smallest possible change to an integer
  // `Position`, a fact of the ASCOM type itself (architecture policy invariant 7: this
  // is honest math on a known-integer field, not a papered-over unknown).
  const stepSize = position.data?.step_size ?? 1;

  function nudge(sign: 1 | -1): void {
    setTargetTouched(true);
    const base = targetValid ? target : (position.data?.position ?? 0);
    const next = Math.max(0, base + sign * stepSize);
    setTargetText(String(next));
  }

  return (
    <Card>
      <CardTitle>Focuser</CardTitle>

      {position.isPending && connected && <p className="text-sm text-fg-muted">Waiting for position…</p>}
      {!connected && <p className="text-sm text-fg-muted">Disconnected</p>}
      {position.isError && (
        <p role="alert" className="text-sm text-danger">
          {errorMessage(position.error, "Could not read focuser position.")}
        </p>
      )}
      {connected && position.isSuccess && disconnected && <p className="text-sm text-fg-muted">Disconnected</p>}
      {position.isSuccess && !disconnected && (
        <p className="text-sm">
          <span className="text-fg-muted">Position: </span>
          <span className="tabular-nums text-fg">{position.data.position ?? "unknown"}</span>
        </p>
      )}
      {position.isSuccess && !disconnected && position.data.position === null && (
        <p className="mt-1 text-xs text-fg-muted">
          {position.data.note ?? "No reading — connect the scope from Settings, or this device has no focuser."}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs tracking-wide text-fg-muted uppercase">Move to (steps)</span>
          <div className="flex items-center gap-1">
            <Button
              aria-label={`Decrease by ${String(stepSize)}`}
              disabled={anyPending}
              onClick={() => {
                nudge(-1);
              }}
            >
              −{stepSize}
            </Button>
            <input
              type="number"
              inputMode="numeric"
              step={1}
              min={0}
              value={displayedTargetText}
              onChange={(e) => {
                setTargetTouched(true);
                setTargetText(e.target.value);
              }}
              className="min-h-11 w-32 rounded-md border border-border-strong bg-surface-raised px-2 text-fg"
            />
            <Button
              aria-label={`Increase by ${String(stepSize)}`}
              disabled={anyPending}
              onClick={() => {
                nudge(1);
              }}
            >
              +{stepSize}
            </Button>
          </div>
        </label>
        <Button
          variant="primary"
          disabled={!targetValid || anyPending}
          onClick={() => {
            move.mutate({ position: target });
          }}
        >
          Move
        </Button>
        <Button
          variant="danger"
          disabled={anyPending}
          onClick={() => {
            halt.mutate();
          }}
        >
          <StopIcon />
          Halt
        </Button>
      </div>

      <div className="mt-3 flex flex-wrap items-end gap-3 border-t border-border pt-3">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs tracking-wide text-fg-muted uppercase">AF step</span>
          <input
            aria-label="Autofocus step"
            type="number"
            inputMode="numeric"
            step={1}
            min={1}
            value={autofocusStepText}
            onChange={(event) => {
              setAutofocusStepText(event.target.value);
            }}
            className="min-h-11 w-24 rounded-md border border-border-strong bg-surface-raised px-2 text-fg"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs tracking-wide text-fg-muted uppercase">Exposure (s)</span>
          <input
            aria-label="Autofocus exposure seconds"
            type="number"
            inputMode="decimal"
            step={0.1}
            min={0.1}
            max={30}
            value={autofocusExposureText}
            onChange={(event) => {
              setAutofocusExposureText(event.target.value);
            }}
            className="min-h-11 w-24 rounded-md border border-border-strong bg-surface-raised px-2 text-fg"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs tracking-wide text-fg-muted uppercase">Settle (s)</span>
          <input
            aria-label="Autofocus settle seconds"
            type="number"
            inputMode="decimal"
            step={0.5}
            min={2}
            max={60}
            value={autofocusSettleText}
            onChange={(event) => {
              setAutofocusSettleText(event.target.value);
            }}
            className="min-h-11 w-24 rounded-md border border-border-strong bg-surface-raised px-2 text-fg"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs tracking-wide text-fg-muted uppercase">Fine step</span>
          <input
            aria-label="Autofocus fine step"
            type="number"
            inputMode="numeric"
            step={1}
            min={1}
            max={Math.max(1, autofocusStep - 1)}
            value={autofocusFineStepText}
            onChange={(event) => {
              setAutofocusFineStepText(event.target.value);
            }}
            className="min-h-11 w-24 rounded-md border border-border-strong bg-surface-raised px-2 text-fg"
          />
        </label>
        <Button
          disabled={anyPending || !autofocusSettingsValid}
          onClick={() => {
            autofocus.mutate({
              step: autofocusStep,
              fine_step: autofocusFineStep,
              exposure_seconds: autofocusExposure,
              settle_seconds: autofocusSettle,
            });
          }}
        >
          <AutofocusIcon />
          {autofocus.isPending || autofocusActive ? "Autofocusing…" : "Start autofocus"}
        </Button>
      </div>

      {move.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {errorMessage(move.error, "Move failed.")}
        </p>
      )}
      {halt.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {errorMessage(halt.error, "Halt failed.")}
        </p>
      )}
      {autofocus.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {errorMessage(autofocus.error, "Could not start autofocus.")}
        </p>
      )}
      {autofocusOperation?.status === "failed" && (
        <p role="alert" className="mt-2 text-sm text-danger">{autofocusOperation.error}</p>
      )}
      {(autofocus.isSuccess || autofocusOperation?.status === "succeeded") && (
        <p className="mt-2 text-sm text-fg-muted">
          Autofocus complete. The position above is the measured best focus.
        </p>
      )}
      <AutofocusChart embedded />
    </Card>
  );
}
