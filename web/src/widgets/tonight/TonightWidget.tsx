// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import { Badge } from "../../components/Badge";
import { useConnectionStore } from "../../store/connectionStore";
import { describeMoonEvent, formatClock } from "./moonEventMessage";
import { MoonGlyph } from "./MoonGlyph";
import { phaseName } from "./moonPhase";
import { ScoreBreakdown } from "./ScoreBreakdown";
import {
  isLocationRequired,
  isTonightRouteMissing,
  useTonightQuery,
  type ConditionsCategory,
  type TonightResponse,
} from "./api";

const VERDICT_LABEL: Record<ConditionsCategory, string> = {
  excellent: "Excellent",
  good: "Good",
  fair: "Fair",
  poor: "Poor",
  very_poor: "Very poor",
};

const VERDICT_TONE: Record<ConditionsCategory, "success" | "warning" | "danger"> = {
  excellent: "success",
  good: "success",
  fair: "warning",
  poor: "warning",
  very_poor: "danger",
};

/** Shared shell for every state this widget can be in: a compact, always-
 * visible trigger plus a detail panel that opens on tap (or click) without
 * ever reflowing the header — the panel is `absolute`, so its width has no
 * effect on where the Live indicator sits (architecture policy: "must not crowd it or
 * wrap awkwardly on a phone").
 *
 * `<details>`/`<summary>` rather than a custom popover: free keyboard
 * support (Enter/Space toggles, it is a real focusable, labelled control)
 * and free touch support (a `<summary>` tap target). Optional lazy content
 * mounts only when opened, keeping hidden forecast queries off startup —
 * this is the "tap" half of "reasoning available on hover or tap"; there is
 * no separate hover affordance; on a device with no pointer precision
 * (architecture policy's "cold, gloved, imprecise finger") hover does not exist
 * anyway, so tap is the one interaction this relies on everywhere.
 */
export function Disclosure({ trigger, children, label, lazy = false }: { trigger: React.ReactNode; children: React.ReactNode; label?: string; lazy?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="group relative" onToggle={(event) => { setOpen(event.currentTarget.open); }}>
      <summary aria-label={label} className="flex min-h-11 min-w-11 cursor-pointer list-none items-center gap-1.5 rounded-md px-1.5 marker:content-none [&::-webkit-details-marker]:hidden">
        {trigger}
      </summary>
      <div className="absolute top-full right-0 z-30 mt-2 w-72 max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-surface p-3 text-sm shadow-[0_1px_0_var(--color-shadow)]">
        {(!lazy || open) && children}
      </div>
    </details>
  );
}

const LOCATION_MESSAGE = (
  <>
    No observing location is set. Add one in Settings → Location to see tonight&rsquo;s capture conditions and the
    moon.
  </>
);

export function TonightWidget() {
  const location = useConnectionStore((s) => s.snapshot?.location ?? null);
  const hemisphere: "north" | "south" = (location?.latitude_deg ?? 0) < 0 ? "south" : "north";
  const tonight = useTonightQuery(location !== null);

  if (location === null) {
    return <Disclosure trigger={<Badge tone="neutral">Tonight</Badge>}>{LOCATION_MESSAGE}</Disclosure>;
  }

  if (tonight.isPending) {
    return (
      <span className="text-xs text-fg-muted" aria-live="polite">
        Tonight…
      </span>
    );
  }

  if (tonight.isError) {
    if (isTonightRouteMissing(tonight.error)) {
      // Not a user-facing failure: this server predates /api/tonight. Say
      // nothing rather than show wording written for an actual outage.
      return null;
    }
    if (isLocationRequired(tonight.error)) {
      return <Disclosure trigger={<Badge tone="neutral">Tonight</Badge>}>{LOCATION_MESSAGE}</Disclosure>;
    }
    return (
      <Disclosure trigger={<Badge tone="danger">Tonight</Badge>}>
        Could not load tonight&rsquo;s conditions
        {tonight.error instanceof Error ? `: ${tonight.error.message}` : "."}
      </Disclosure>
    );
  }

  return <TonightSummary tonight={tonight.data} hemisphere={hemisphere} />;
}

function darkWindowLabel(tonight: TonightResponse): string {
  if (tonight.dark_window_start === null || tonight.dark_window_end === null) {
    return tonight.dark_window_note ?? "No astronomical darkness tonight.";
  }
  const hours = (new Date(tonight.dark_window_end).getTime() - new Date(tonight.dark_window_start).getTime()) / 3_600_000;
  return `${formatClock(tonight.dark_window_start)}–${formatClock(tonight.dark_window_end)} (${hours.toFixed(1)}h)`;
}

function TonightSummary({ tonight, hemisphere }: { tonight: TonightResponse; hemisphere: "north" | "south" }) {
  const { conditions, moon, weather } = tonight;
  const stale = weather !== null && weather.stale_since !== null;

  const verdictBadge =
    conditions !== null ? (
      <Badge tone={VERDICT_TONE[conditions.category]}>
        {stale && <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full bg-warning" />}
        {VERDICT_LABEL[conditions.category]}
      </Badge>
    ) : (
      <Badge tone="neutral">
        {stale && <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full bg-warning" />}
        Tonight
      </Badge>
    );

  return (
    <Disclosure
      trigger={
        <>
          <MoonGlyph illuminatedFraction={moon.illuminated_fraction} waxing={moon.waxing} hemisphere={hemisphere} />
          {verdictBadge}
          {stale && <span className="sr-only"> (forecast is stale)</span>}
        </>
      }
    >
      <TonightDetails tonight={tonight} />
    </Disclosure>
  );
}

export function TonightDetails({ tonight, showWeatherSummary = true }: {
  tonight: TonightResponse;
  showWeatherSummary?: boolean;
}) {
  const { conditions, moon, weather } = tonight;
  const phase = phaseName(moon.illuminated_fraction, moon.waxing);
  const pct = Math.round(Math.min(1, Math.max(0, moon.illuminated_fraction)) * 100).toString();
  const latestRow = weather !== null ? (weather.rows[0] ?? null) : null;

  return (
    <div className="space-y-2">
        {conditions !== null ? (
          <>
            <p className="font-medium text-fg">
              {VERDICT_LABEL[conditions.category]} — score {Math.round(conditions.score)}/100
            </p>
            <ul className="list-disc space-y-0.5 pl-4 text-xs text-fg-muted">
              {conditions.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
            <ScoreBreakdown factors={conditions.factors} />
          </>
        ) : (
          <p className="text-xs text-warning">
            Capture conditions unavailable
            {tonight.conditions_unavailable_reason !== null ? `: ${tonight.conditions_unavailable_reason}` : "."}
          </p>
        )}
        {weather?.stale_since !== null && weather?.stale_since !== undefined && (
          <p className="text-xs text-warning">
            Forecast is stale — fetched {formatClock(weather.fetched_at)}, stale since{" "}
            {formatClock(weather.stale_since)}. Not live data.
          </p>
        )}
        {weather === null && tonight.weather_unavailable_reason !== null && (
          <p className="text-xs text-warning">No forecast available: {tonight.weather_unavailable_reason}</p>
        )}
        {showWeatherSummary && latestRow !== null && (
          <p className="text-xs text-fg-muted">
            As of {formatClock(latestRow.t)} — cloud {latestRow.cloud.total ?? "?"}% total
            {latestRow.dew_point_c !== null ? `, dew point ${latestRow.dew_point_c.toFixed(1)}°C` : ""}
            {latestRow.wind_ms !== null ? `, wind ${(latestRow.wind_ms * 3.6).toFixed(0)} km/h` : ""}
          </p>
        )}
        <dl className="grid grid-cols-2 gap-x-2 gap-y-1 border-t border-border pt-2 text-xs">
          <dt className="text-fg-muted">Moon</dt>
          <dd className="text-right text-fg">
            {phase}, {pct}%
          </dd>
          <dt className="text-fg-muted">Moonrise</dt>
          <dd className="text-right text-fg">{describeMoonEvent("moonrise", moon.moonrise)}</dd>
          <dt className="text-fg-muted">Moonset</dt>
          <dd className="text-right text-fg">{describeMoonEvent("moonset", moon.moonset)}</dd>
          <dt className="text-fg-muted">Dark window</dt>
          <dd className="text-right text-fg">{darkWindowLabel(tonight)}</dd>
        </dl>
    </div>
  );
}
