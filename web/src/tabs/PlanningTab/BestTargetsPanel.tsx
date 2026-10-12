// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * "What should I image tonight?" — the ranked list `POST /api/astro/best-targets`
 * returns, shown in the Planning tab's detail column while no target is
 * resolved (owner's request: "when nothing is selected, on the end of right
 * panel in the planning, show list of 10 top targets for tonight").
 *
 * Every number here is the server's. Nothing in this file re-ranks, re-filters
 * or re-derives a score, so the UI cannot quietly disagree with
 * `ssc.astro.best_targets` about what "best" means — the same rule the framing
 * and night panels follow.
 *
 * The point of showing the *components* (hours above the limit, peak altitude,
 * moon separation, and why the Moon does or does not matter for this object)
 * rather than a bare rank is that a recommendation you cannot argue with is
 * useless: the owner knows things the scorer does not — a neighbour's tree, a
 * filter they do not own, a target they simply photographed last week — and
 * needs enough to overrule it.
 */
import { Badge } from "../../components/Badge";
import { Card } from "../../components/Card";
import type { BestTargetModel, BestTargetsResponse } from "../../api/astro";
import type { components } from "../../api/schema";
import type { PlanningTarget } from "./types";

type MoonImpact = components["schemas"]["MoonImpact"];
type SizeFit = components["schemas"]["SizeFit"];

/** Why the Moon does or does not cost this target anything tonight. Phrased
 * as the *reason*, not the rating, so the row reads as an explanation. */
const MOON_IMPACT_LABEL: Record<MoonImpact, string> = {
  moon_down: "Moon down",
  narrowband_tolerant: "Narrowband",
  far_enough: "Moon far",
  interfering: "Moon interferes",
};

/** `interfering` is the only one that costs the target real signal, so it is
 * the only one drawn as a warning; the rest are reassurances. */
const MOON_IMPACT_TONE: Record<MoonImpact, "accent" | "warning"> = {
  moon_down: "accent",
  narrowband_tolerant: "accent",
  far_enough: "accent",
  interfering: "warning",
};

const SIZE_FIT_LABEL: Record<SizeFit, string> = {
  fits_field: "Fits the field",
  exceeds_field: "Larger than the field",
  too_small_to_resolve: "Small in this field",
};

function formatLocalClock(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** `size_major_arcmin` is `null` whenever the catalogue simply does not carry
 * a size — never rendered as 0, which would read as "a point source". */
function formatSize(arcmin: number | null): string | null {
  if (arcmin === null) return null;
  return arcmin >= 60 ? `${(arcmin / 60).toFixed(1)}°` : `${arcmin.toFixed(0)}′`;
}

export function BestTargetsPanel({
  data,
  isPending,
  isError,
  error,
  onPick,
}: {
  data: BestTargetsResponse | undefined;
  isPending: boolean;
  isError: boolean;
  error: unknown;
  /** Picking a row resolves it as the Planning target, which is the whole
   * point of the list — it is a shortcut into the normal flow, not a
   * separate mode. */
  onPick: (target: PlanningTarget) => void;
}) {
  if (isError) {
    return (
      <Card title="Best tonight" collapseKey="planning.best" className="text-sm text-danger">
        Could not work out tonight&rsquo;s best targets:{" "}
        {error instanceof Error ? error.message : "request failed"}
      </Card>
    );
  }
  if (isPending) {
    return (
      <Card title="Best tonight" collapseKey="planning.best" className="text-sm text-fg-muted">
        Ranking the catalogue for tonight&hellip;
      </Card>
    );
  }
  if (data === undefined) return null;

  return (
    <Card title="Best tonight" collapseKey="planning.best" className="space-y-3">
      <p className="text-xs text-fg-muted">
        {data.darkness_hours.toFixed(1)} h of astronomical darkness · Moon{" "}
        {(data.moon_illuminated_fraction * 100).toFixed(0)}% lit · ranked{" "}
        <span className="tabular-nums">{data.candidates_considered.toLocaleString()}</span> candidates above{" "}
        {data.min_altitude_deg.toFixed(0)}°
      </p>
      {/* The server's own caveat, when it has one — e.g. a night with no
          astronomical darkness at all. Surfaced rather than swallowed
          (CLAUDE.md invariant 7). */}
      {data.note !== null && <p className="text-xs text-warning">{data.note}</p>}
      {data.targets.length === 0 ? (
        // An empty list is a real answer, not a failure: at a high enough
        // latitude in midsummer there may be no astronomical darkness at
        // all, and nothing clears the altitude limit during it.
        <p className="text-sm text-fg-muted">
          Nothing clears your altitude limit during tonight&rsquo;s darkness.
        </p>
      ) : (
        <ol className="space-y-2">
          {data.targets.map((t, index) => (
            <BestTargetRow key={t.designation} target={t} rank={index + 1} onPick={onPick} />
          ))}
        </ol>
      )}
    </Card>
  );
}

function BestTargetRow({
  target,
  rank,
  onPick,
}: {
  target: BestTargetModel;
  rank: number;
  onPick: (target: PlanningTarget) => void;
}) {
  const size = formatSize(target.size_major_arcmin);
  return (
    <li>
      <button
        type="button"
        className="w-full rounded-md border border-border bg-surface-raised p-2 text-left hover:border-border-strong"
        onClick={() => {
          onPick({
            name: target.common_name ?? target.designation,
            raH: target.ra_j2000_h,
            decDeg: target.dec_j2000_deg,
            magnitude: target.magnitude,
            sizeMajorArcmin: target.size_major_arcmin,
            // The ranking endpoint does not carry a position angle — the
            // catalogue's own value arrives when the target is resolved
            // properly. `null` is honest here; a 0 would be a fabricated
            // orientation (architecture policy invariant 7).
            positionAngleDeg: null,
            objectType: target.object_type,
          });
        }}
      >
        <div className="flex items-baseline justify-between gap-2">
          <span className="min-w-0 truncate text-sm font-medium text-fg">
            <span className="mr-1.5 tabular-nums text-fg-muted">{rank}.</span>
            {target.common_name ?? target.designation}
          </span>
          <Badge tone={MOON_IMPACT_TONE[target.moon_impact]}>{MOON_IMPACT_LABEL[target.moon_impact]}</Badge>
        </div>
        <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 text-xs text-fg-muted">
          <dt>Up</dt>
          <dd className="tabular-nums text-fg">
            {target.hours_above_limit.toFixed(1)} h · peaks {target.peak_altitude_deg.toFixed(0)}° at{" "}
            {formatLocalClock(target.peak_when)}
          </dd>
          <dt>Moon</dt>
          <dd className="tabular-nums text-fg">
            {target.moon_separation_deg.toFixed(0)}° away, {target.moon_altitude_at_peak_deg >= 0 ? "up" : "down"} at peak
          </dd>
        </dl>
        <p className="mt-1 truncate text-xs text-fg-muted">
          {target.common_name === null ? target.object_type : `${target.designation} · ${target.object_type}`}
          {target.magnitude !== null && ` · mag ${target.magnitude.toFixed(1)}`}
          {size !== null && ` · ${size}`}
          {target.size_fit !== null && ` · ${SIZE_FIT_LABEL[target.size_fit]}`}
        </p>
      </button>
        <details className="-mt-1 rounded-b-md border border-t-0 border-border bg-surface-raised px-2 pb-2 pt-1">
          <summary className="min-h-8 cursor-pointer text-xs font-medium text-accent">Why {target.score.toFixed(0)}/100?</summary>
          <dl className="mt-1 grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 text-xs text-fg-muted">
            <dt>Time above limit</dt><dd className="tabular-nums text-success">+{target.altitude_hours_score.toFixed(1)}</dd>
            <dt>Peak altitude</dt><dd className="tabular-nums text-success">+{target.altitude_peak_score.toFixed(1)}</dd>
            <dt>Brightness</dt><dd className="tabular-nums text-success">+{target.magnitude_bonus.toFixed(1)}</dd>
            <dt>Moon interference</dt><dd className="tabular-nums text-danger">−{target.moon_penalty.toFixed(1)}</dd>
            <dt>Field-size mismatch</dt><dd className="tabular-nums text-danger">−{target.size_penalty.toFixed(1)}</dd>
          </dl>
        </details>
    </li>
  );
}
