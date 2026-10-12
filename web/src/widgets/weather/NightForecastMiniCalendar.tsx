// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";
import { CloudSun } from "lucide-react";
import { Link } from "react-router-dom";
import type { ForecastPoint } from "../../api/weather";
import { nightPredictionQueryOptions } from "../tonight/api";
import { forecastNightDates, observingDate, describeNightQuality } from "./nightConditions";

function weekday(date: string): string {
  return new Intl.DateTimeFormat(undefined, { weekday: "short", timeZone: "UTC" })
    .format(new Date(`${date}T00:00:00Z`));
}

export function NightForecastMiniCalendar({ points, timezone }: { points: ForecastPoint[]; timezone: string }) {
  const today = observingDate(new Date(), timezone);
  const dates = useMemo(
    () => [...forecastNightDates(points, timezone, today)].sort(),
    [points, timezone, today],
  );
  const predictions = useQueries({
    queries: dates.map((date) => nightPredictionQueryOptions(date)),
  });

  return (
    <section aria-labelledby="night-forecast-title" data-testid="night-forecast-mini-calendar" className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h3 id="night-forecast-title" className="flex items-center gap-1.5 text-xs font-semibold text-fg">
          <CloudSun aria-hidden="true" className="size-4 text-accent" />Night forecast
        </h3>
        <span className="text-[10px] text-fg-muted">Score / 100</span>
      </div>
      <div className="grid grid-cols-7 gap-1">
        {dates.map((date, index) => {
          const prediction = predictions[index];
          const quality = describeNightQuality(prediction?.data, prediction?.isPending ?? true, prediction?.isError ?? false);
          const stale = prediction?.data?.weather?.stale_since != null;
          return (
            <div
              key={date}
              data-testid={`mini-night-${date}`}
              title={quality.detail}
              aria-label={`${date}: ${quality.label}. ${quality.detail}`}
              className="flex min-h-14 flex-col items-center justify-center gap-0.5 rounded border border-border bg-surface-raised px-0.5 py-1 text-center"
            >
              <span className="text-[9px] leading-none text-fg-muted">{date === today ? "Today" : weekday(date)}</span>
              <time dateTime={date} className="text-[10px] leading-none tabular-nums text-fg">{Number(date.slice(-2))}</time>
              <span className={`text-[10px] font-semibold leading-none ${quality.tone === "success" ? "text-success" : quality.tone === "warning" ? "text-warning" : quality.tone === "danger" ? "text-danger" : "text-fg-muted"}`}>
                {prediction?.data?.conditions === null || prediction?.data?.conditions === undefined
                  ? prediction?.isPending ? "…" : "—"
                  : Math.round(prediction.data.conditions.score)}
                {stale && <span aria-hidden="true" className="ml-0.5 text-warning">*</span>}
              </span>
            </div>
          );
        })}
      </div>
      <Link to="/calendar" className="inline-flex min-h-8 items-center text-xs font-medium text-accent hover:underline">
        Open calendar
      </Link>
    </section>
  );
}
