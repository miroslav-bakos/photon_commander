// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §3.2's time scrubber: "A slider along the bottom re-renders the
 * whole overlay for any instant of the night, with a second handle for
 * session length." Two separate range inputs rather than one dual-thumb
 * control — `sessionLengthHours` changing means a genuinely new
 * `FramingRequest` (refetched, debounced, in `PlanningTab`), while
 * `instantSeconds` changing is pure client-side interpolation
 * (`overlay.ts`'s `interpolatePositionAngleDeg`) against the one sample set
 * already in hand — conflating them into one control would blur that a
 * scrub-frame must stay off the network and a length-change must not.
 *
 * The Play button exists because "watching the frame rotate" (skymap-viz.md)
 * is the whole point of this tab — a compressed real-time sweep from session
 * start to end, looping, is a much more direct answer to "will this work
 * tonight" than dragging a slider by hand.
 */
import { useEffect, useRef, useState } from "react";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import type { MountMode } from "../../api/astro";

const MIN_SESSION_HOURS = 0.25;
const MAX_SESSION_HOURS = 12;
const SESSION_STEP_HOURS = 0.25;
/** A full session compressed into this many real seconds of playback. */
const PLAYBACK_SECONDS = 10;

function formatClock(startTime: Date, instantSeconds: number): string {
  const at = new Date(startTime.getTime() + instantSeconds * 1000);
  return at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function TimeScrubber({
  mount,
  startTime,
  sessionLengthHours,
  instantSeconds,
  onSessionLengthChange,
  onInstantChange,
  embedded = false,
}: {
  mount: MountMode;
  startTime: Date;
  sessionLengthHours: number;
  instantSeconds: number;
  onSessionLengthChange: (hours: number) => void;
  onInstantChange: (seconds: number) => void;
  /** A modal already supplies the titled surface; avoid nesting a movable,
   * collapsible application card inside it. */
  embedded?: boolean;
}) {
  const [playing, setPlaying] = useState(false);
  const rafRef = useRef<number | null>(null);
  const playStartedAtRef = useRef<number>(0);
  const sessionLengthSeconds = sessionLengthHours * 3600;

  useEffect(() => {
    if (!playing) return;
    playStartedAtRef.current = performance.now();

    const tick = (now: number) => {
      const elapsedMs = now - playStartedAtRef.current;
      const fraction = (elapsedMs / (PLAYBACK_SECONDS * 1000)) % 1;
      onInstantChange(fraction * sessionLengthSeconds);
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);

    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, sessionLengthSeconds]);

  const isAltAz = mount === "altaz";

  const content = (
    <>
      <div className="flex items-center justify-between">
        {isAltAz && (
          <Button
            variant="secondary"
            onClick={() => {
              setPlaying((p) => !p);
            }}
          >
            {playing ? "Pause" : "Play"}
          </Button>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="flex items-center justify-between text-sm font-medium text-fg-muted" htmlFor="session-length">
          <span>Session length</span>
          <span className="tabular-nums text-fg">{sessionLengthHours.toFixed(2)}h</span>
        </label>
        <input
          id="session-length"
          type="range"
          min={MIN_SESSION_HOURS}
          max={MAX_SESSION_HOURS}
          step={SESSION_STEP_HOURS}
          value={sessionLengthHours}
          onChange={(e) => {
            onSessionLengthChange(Number.parseFloat(e.target.value));
          }}
          className="h-11 w-full accent-accent"
        />
      </div>

      {isAltAz ? (
        <div className="flex flex-col gap-1.5">
          <label className="flex items-center justify-between text-sm font-medium text-fg-muted" htmlFor="instant">
            <span>Instant</span>
            <span className="tabular-nums text-fg">
              {formatClock(startTime, instantSeconds)} (+{(instantSeconds / 3600).toFixed(2)}h)
            </span>
          </label>
          <input
            id="instant"
            type="range"
            min={0}
            max={sessionLengthSeconds}
            step={30}
            value={Math.min(instantSeconds, sessionLengthSeconds)}
            onChange={(e) => {
              setPlaying(false);
              onInstantChange(Number.parseFloat(e.target.value));
            }}
            className="h-11 w-full accent-accent"
          />
        </div>
      ) : (
        <p className="text-sm text-fg-muted">
          EQ mode: the frame does not rotate — nothing to scrub through.
        </p>
      )}
    </>
  );

  if (embedded) return <div className="space-y-3">{content}</div>;
  return <Card title="Time scrubber" collapseKey="planning.scrubber" className="space-y-3">{content}</Card>;
}
