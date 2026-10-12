// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * DESIGN.md §3.2's horizon warnings, the text half: skymap-viz.md's item 3
 * ("Surface the visibility intervals where a user planning a session will
 * see them") and item 4 ("if no profile is set, say so"). The map's
 * `buildAltitudeLimitRingShape`/`buildHorizonProfileMarkerShape` (`overlay.ts`)
 * draw an *approximation* of the horizon profile at the scrubbed instant —
 * this panel is the exact, azimuth-correct answer for the whole session,
 * from `NightResponse.visibility_intervals`
 * (`ssc.astro.events.horizon_visibility`), never recomputed here. That
 * approximation is stated here in visible text, not only in `overlay.ts`'s
 * comments (architecture policy invariant 7 — the map's `FovShape.label` for that
 * marker is never actually drawn: `frames/vector/main.ts`'s `syncLabels`
 * only renders labels for `catalog`/`skyline`/`coordGrid` roles, and the marker's
 * role is `keep`/`warning`/`danger`, so its own caveat text was reachable
 * only by reading source).
 *
 * A target blocked for part of the session is a *stronger* warning than the
 * rotation ones `FramingStatsPanel` renders (skymap-viz.md): those degrade
 * the data, this means no data at all for that stretch. Coloured danger, not
 * warning, whenever any clear interval is shorter than the full session.
 */
import { Badge } from "../../components/Badge";
import { Card, CardTitle } from "../../components/Card";
import type { HorizonIntervalModel, MountMode, NightResponse } from "../../api/astro";

function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function intervalSeconds(interval: HorizonIntervalModel): number {
  return (new Date(interval.end).getTime() - new Date(interval.start).getTime()) / 1000;
}

export function VisibilityPanel({
  night,
  sessionStartTime,
  sessionEndTime,
  startAboveHorizon,
  mount,
}: {
  night: NightResponse;
  sessionStartTime: Date;
  sessionEndTime: Date;
  /** `FramingVisibilityModel.start_above_horizon` — whether the target
   * clears the mask at the moment the session begins, from `/api/astro/framing`
   * rather than re-derived from `intervals` here (the two must agree by
   * construction — both come from the same server-side horizon profile —
   * but this is the number the framing response actually names). */
  startAboveHorizon: boolean;
  /** `FramingResponse.mount` — gates the caveat below: the map only ever
   * draws `buildHorizonProfileMarkerShape`'s dashed marker in alt-az mode
   * (`PlanningTab`'s own `if (response.mount === "altaz")`), so the caveat
   * about that marker would be pointing at a shape that does not exist on
   * an EQ plan. */
  mount: MountMode;
}) {
  const sessionSeconds = (sessionEndTime.getTime() - sessionStartTime.getTime()) / 1000;
  const intervals = night.visibility_intervals ?? [];
  const clearSeconds = intervals.reduce((total, interval) => total + intervalSeconds(interval), 0);
  const clearFraction = sessionSeconds > 0 ? clearSeconds / sessionSeconds : 0;
  const fullyClear = intervals.length === 1 && clearFraction >= 0.999;
  const fullyBlocked = intervals.length === 0;

  return (
    <Card className="space-y-3">
      <CardTitle>Visibility over the skyline</CardTitle>

      {!night.horizon_profile_active && (
        <p className="rounded-md border border-warning bg-surface-raised px-2 py-1.5 text-xs text-warning">
          No horizon profile set — this plan assumes a clear sky down to the altitude limit only. Draw your
          skyline in Settings → Horizon to check real obstructions (trees, roofline, neighbours).
        </p>
      )}

      {night.horizon_profile_active && mount === "altaz" && (
        <p className="rounded-md border border-border bg-surface-raised px-2 py-1.5 text-xs text-fg-muted">
          The map's dashed horizon marker is approximate: it holds your skyline's altitude at the target's
          bearing at session start for the whole scrub, not its real altitude as the target's azimuth
          changes. The list below is the exact, per-azimuth answer for the whole session.
        </p>
      )}

      {!startAboveHorizon && (
        <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-xs text-danger">
          Below the {night.horizon_profile_active ? "horizon profile" : "altitude limit"} at session start.
        </p>
      )}

      <div className="flex items-center justify-between text-sm">
        <span className="text-fg-muted">Clear of the skyline</span>
        <Badge tone={fullyClear ? "success" : fullyBlocked ? "danger" : "warning"}>
          {(clearFraction * 100).toFixed(0)}% of the session
        </Badge>
      </div>

      {fullyBlocked && (
        <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-xs text-danger">
          Never clears the skyline during this session — no data, not degraded data, for the whole window.
        </p>
      )}

      {!fullyClear && !fullyBlocked && (
        <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-xs text-danger">
          Blocked for part of the session — this is a stronger warning than a rotation caveat: it means no
          data at all while blocked, not merely degraded framing.
        </p>
      )}

      <ul className="space-y-1 text-sm">
        {intervals.map((interval) => (
          // `start` is unique within one target's interval list — the
          // solver never returns two overlapping intervals.
          <li key={interval.start} className="flex items-center justify-between gap-2">
            <span className="text-fg">
              {interval.start_is_window_edge ? "already clear at " : ""}
              {formatClock(interval.start)}
              {" – "}
              {formatClock(interval.end)}
              {interval.end_is_window_edge ? " (still clear at window end)" : ""}
            </span>
            <span className="tabular-nums text-fg-muted">{(intervalSeconds(interval) / 3600).toFixed(1)}h</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}
