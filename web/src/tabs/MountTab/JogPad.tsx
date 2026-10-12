// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { SegmentedControl } from "../../components/SegmentedControl";
import { CircleParking } from "lucide-react";
import { useMountMoveAxisMutation, useMountStopMutation } from "../../api/mount";
import type { MountAxis } from "../../api/mount";

/** Every cell in the pad — arrows, STOP and the empty corners — is this one
 * square, so the cross stays a cross and nothing is wider than its neighbour.
 * 4rem clears the 44px touch-target floor with room for a gloved thumb. */
const JOG_CELL = "h-16 w-16";

/**
 * "Point it away from the tree" — the tab's actual job (DESIGN.md §4.4b /
 * the tab's brief). Press-and-hold sends `move-axis` at a rate; release
 * sends `rate_deg_s: 0`. A jog that keeps moving because a release was
 * missed is dangerous, so every one of pointerup, pointercancel,
 * pointerleave, a window blur and this component unmounting sends the same
 * release — independently of each other, so missing any one still leaves
 * the others as a backstop.
 *
 * Two axes only (primary/secondary — ASCOM's own generic names, correct
 * regardless of alt-az or EQ: `MoveAxis`'s axis parameter means the same
 * thing either way, unlike `AlignmentMode`, which DESIGN.md §4.4b found to
 * be unreliable on this device). No 2D joystick: four independent buttons
 * are unambiguous under a cold, gloved, imprecise finger in the dark, where
 * a joystick's centre dead-zone is exactly the kind of precision that fails.
 */

const SLOW_FRACTION = 0.15;
const JOG_HEARTBEAT_MS = 500;
const PARKED_NOTICE_MS = 5000;

function jogRates(maxDegS: number | null): { slow: number; fast: number } | null {
  if (maxDegS === null || maxDegS <= 0) return null;
  const slow = Math.max(0.1, maxDegS * SLOW_FRACTION);
  return { slow: Math.min(slow, maxDegS), fast: maxDegS };
}

/** The panic control, at the centre of the pad.
 *
 * Never disabled and never gated on "is anything actually moving": a control
 * conditioned on that is a control that can be wrong in exactly the moment it
 * matters, and stopping a stationary mount is harmless. It is also the one
 * control here that is not rate-limited server-side, for the same reason.
 */
/** One triangle, rotated per direction, rather than the ▲◀▶▼ glyphs this
 * used to draw. Font arrows are not one shape at four rotations: horizontal
 * and vertical glyphs carry different metrics and optical sizing, so the
 * left/right pair never matched the up/down pair however they were sized
 * (owner, 2026-09-10). Drawing it once and rotating guarantees they do.
 *
 * `currentColor` so it inherits the button's pressed/disabled colours with
 * no second code path, and `aria-hidden` because the button already carries
 * the real accessible name.
 */
function JogArrow({ rotateDeg }: { rotateDeg: number }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className="h-6 w-6"
      style={{ transform: `rotate(${String(rotateDeg)}deg)` }}
    >
      <polygon points="12,5 20,19 4,19" fill="currentColor" />
    </svg>
  );
}

export function StopButton() {
  const stop = useMountStopMutation();
  return (
    <button
      type="button"
      onClick={() => { stop.mutate(); }}
      title={stop.isError ? "Stop failed — press again." : "Stop all mount motion"}
      className={`${JOG_CELL} rounded-lg text-base font-bold text-bg shadow-[0_1px_0_var(--color-shadow)] transition-[filter] hover:brightness-110 active:brightness-95 ${
        stop.isError ? "bg-warning" : "bg-danger"
      }`}
    >
      STOP
    </button>
  );
}

/** One button's press-and-hold-to-release lifecycle. Isolated per button
 * (per axis, per direction) so two fingers on two buttons never share
 * release state. */
function useJogHold(axis: MountAxis, rateDegS: number | null) {
  // `mutate` is stable across renders (TanStack Query memoizes it against the
  // mutation observer, not against props), so it is safe to depend on
  // directly — no ref indirection needed to keep `release`'s identity
  // (and therefore the unmount-cleanup effect below) stable.
  const moveAxis = useMountMoveAxisMutation();
  const { mutate } = moveAxis;
  const activeRef = useRef(false);
  const heartbeatRef = useRef<number | null>(null);
  const [pressed, setPressed] = useState(false);

  const release = useCallback(() => {
    if (!activeRef.current) return;
    activeRef.current = false;
    if (heartbeatRef.current !== null) {
      window.clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
    setPressed(false);
    mutate({ axis, rate_deg_s: 0 });
  }, [axis, mutate]);

  const press = useCallback(
    (sign: 1 | -1) => {
      if (rateDegS === null || activeRef.current) return;
      activeRef.current = true;
      setPressed(true);
      mutate({ axis, rate_deg_s: sign * rateDegS });
      heartbeatRef.current = window.setInterval(() => {
        mutate({ axis, rate_deg_s: sign * rateDegS });
      }, JOG_HEARTBEAT_MS);
    },
    [axis, mutate, rateDegS],
  );

  // A held jog must not survive the window losing focus — a phone call, an
  // app switch, the screen locking.
  useEffect(() => {
    window.addEventListener("blur", release);
    return () => {
      window.removeEventListener("blur", release);
    };
  }, [release]);

  // Nor survive this component going away — a tab switch, a route change.
  // `release` only ever changes identity when `axis` does (stable for the
  // lifetime of one button), so this fires on genuine unmount.
  useEffect(() => release, [release]);

  return { press, release, pressed, error: moveAxis.error };
}

function JogButton({
  axis,
  sign,
  rateDegS,
  rotateDeg,
  ariaLabel,
  onParkedAttempt,
}: {
  axis: MountAxis;
  sign: 1 | -1;
  rateDegS: number | null;
  rotateDeg: number;
  ariaLabel: string;
  onParkedAttempt?: () => void;
}) {
  const { press, release, pressed } = useJogHold(axis, rateDegS);
  const isDisabled = rateDegS === null;

  function onPointerDown(e: ReactPointerEvent<HTMLButtonElement>) {
    e.preventDefault();
    if (onParkedAttempt !== undefined) {
      onParkedAttempt();
      return;
    }
    press(sign);
  }

  return (
    <button
      type="button"
      disabled={isDisabled}
      aria-label={ariaLabel}
      aria-pressed={pressed}
      onPointerDown={onPointerDown}
      onPointerUp={release}
      onPointerCancel={release}
      onPointerLeave={release}
      onContextMenu={(e) => { e.preventDefault(); }}
      className={`${JOG_CELL} touch-none grid place-items-center rounded-lg border transition-colors select-none disabled:cursor-not-allowed disabled:opacity-40 ${
        pressed
          ? "border-accent bg-accent-muted text-accent"
          : "border-border-strong bg-surface-raised text-fg hover:bg-surface"
      }`}
    >
      <JogArrow rotateDeg={rotateDeg} />
    </button>
  );
}

export function JogPad({
  canMovePrimary,
  canMoveSecondary,
  axisRateMaxDegS,
  parked = false,
  showStop = true,
}: {
  canMovePrimary: boolean;
  canMoveSecondary: boolean;
  axisRateMaxDegS: number | null;
  parked?: boolean;
  showStop?: boolean;
}) {
  const [fast, setFast] = useState(true);
  const [showParkedNotice, setShowParkedNotice] = useState(false);
  const parkedNoticeTimer = useRef<number | null>(null);
  const rates = jogRates(axisRateMaxDegS);
  const rate = rates === null ? null : fast ? rates.fast : rates.slow;

  const showParkedGuidance = useCallback(() => {
    setShowParkedNotice(true);
    if (parkedNoticeTimer.current !== null) window.clearTimeout(parkedNoticeTimer.current);
    parkedNoticeTimer.current = window.setTimeout(() => {
      parkedNoticeTimer.current = null;
      setShowParkedNotice(false);
    }, PARKED_NOTICE_MS);
  }, []);

  useEffect(() => () => {
    if (parkedNoticeTimer.current !== null) window.clearTimeout(parkedNoticeTimer.current);
  }, []);

  if (!canMovePrimary && !canMoveSecondary) {
    return <p className="text-sm text-fg-muted">This mount does not report a jog axis.</p>;
  }

  return (
    <div className="space-y-3">
      {showParkedNotice && (
        <div
          role="status"
          className="flex items-start gap-2 rounded-md border border-accent bg-accent-muted p-3 text-sm text-fg"
        >
          <CircleParking aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-accent" strokeWidth={1.8} />
          <span>
            The mount is parked. Unpark before moving it manually, or use Unpark + Slew to move to a target.
          </span>
        </div>
      )}
      {rates === null && (
        <p className="text-sm text-warning">
          The device has not reported a maximum jog rate yet — jogging is unavailable until it does.
        </p>
      )}
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs tracking-wide text-fg-muted uppercase">Jog speed</span>
        <SegmentedControl label="Jog speed" semantics="pressed" value={fast ? "fast" : "slow"}
          options={[
            { value: "slow", label: `Slow${rates !== null ? ` (${rates.slow.toFixed(2)}°/s)` : ""}` },
            { value: "fast", label: `Fast${rates !== null ? ` (${rates.fast.toFixed(2)}°/s)` : ""}` },
          ]}
          onChange={(speed) => { setFast(speed === "fast"); }} />
      </div>
      {/* 8 / 4 5 6 / 2 — the numpad cross the owner asked for: five equal
          squares, empty corners, the block centred in its column rather than
          left-aligned against it. `w-fit` plus `mx-auto` is what centres it;
          `JOG_CELL` is what makes every cell the same square, STOP included,
          so nothing is wider or taller than its neighbour. */}
      <div className="mx-auto grid w-fit grid-cols-3 grid-rows-3 gap-2">
        <div className={JOG_CELL} />
        {canMoveSecondary ? (
          <JogButton
            axis="secondary"
            sign={1}
            rateDegS={rate}
            rotateDeg={0}
            ariaLabel="Secondary axis, positive direction (Dec/Alt) — press and hold"
            onParkedAttempt={parked ? showParkedGuidance : undefined}
          />
        ) : (
          <div className={JOG_CELL} />
        )}
        <div className={JOG_CELL} />
        {canMovePrimary ? (
          <JogButton
            axis="primary"
            sign={-1}
            rateDegS={rate}
            rotateDeg={270}
            ariaLabel="Primary axis, negative direction (RA/Az) — press and hold"
            onParkedAttempt={parked ? showParkedGuidance : undefined}
          />
        ) : (
          <div className={JOG_CELL} />
        )}
        {/* STOP lives in the pad's centre rather than as a separate bar
            (owner, 2026-09-10: "that way is really fast accessible"). It is
            where the thumb already rests between jogs, so it is one short
            movement from any direction — which is the whole requirement for
            a panic control, and better than the "hold" hint that was here.
            The hint is not lost: every jog button's `ariaLabel` already ends
            "press and hold", and the behaviour is self-evident on first
            press in a way a 10px word never was. */}
        {showStop ? <StopButton /> : <div className={JOG_CELL} />}
        {canMovePrimary ? (
          <JogButton
            axis="primary"
            sign={1}
            rateDegS={rate}
            rotateDeg={90}
            ariaLabel="Primary axis, positive direction (RA/Az) — press and hold"
            onParkedAttempt={parked ? showParkedGuidance : undefined}
          />
        ) : (
          <div className={JOG_CELL} />
        )}
        <div className={JOG_CELL} />
        {canMoveSecondary ? (
          <JogButton
            axis="secondary"
            sign={-1}
            rateDegS={rate}
            rotateDeg={180}
            ariaLabel="Secondary axis, negative direction (Dec/Alt) — press and hold"
            onParkedAttempt={parked ? showParkedGuidance : undefined}
          />
        ) : (
          <div className={JOG_CELL} />
        )}
        <div className={JOG_CELL} />
      </div>
      <p className="text-xs text-fg-muted">
        Primary axis = RA / Az, secondary axis = Dec / Alt (ASCOM's own axis numbering — unlike this
        device's <code>AlignmentMode</code>, that meaning does not depend on how the mount is physically
        set up).
      </p>
    </div>
  );
}
