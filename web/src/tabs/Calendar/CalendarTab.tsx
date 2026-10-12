// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect, useMemo, useRef, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { CalendarDays, ChevronLeft, ChevronRight, CloudSun } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { usePlansQuery, type PlanSummary } from "../../api/plans";
import { useForecastQuery } from "../../api/weather";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { useConnectionStore } from "../../store/connectionStore";
import { nightPredictionQueryOptions, type TonightResponse } from "../../widgets/tonight/api";
import { ScoreBreakdown } from "../../widgets/tonight/ScoreBreakdown";
import { describeNightQuality, forecastNightDates, observingDate } from "../../widgets/weather/nightConditions";
import { useSchedulingUiStore } from "../SchedulingTab/schedulingUiStore";
import { useRunHistoryQuery } from "../../api/runs";
import { NightReportPanel } from "./NightReportPanel";
import { HistoricalNightPanel } from "./HistoricalNightPanel";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

interface CalendarDay {
  iso: string;
  day: number;
  inMonth: boolean;
}

function monthKey(date = new Date()): string {
  return `${String(date.getFullYear())}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function shiftMonth(month: string, delta: number): string {
  const [year = 1970, index = 1] = month.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, index - 1 + delta, 1));
  return `${String(shifted.getUTCFullYear())}-${String(shifted.getUTCMonth() + 1).padStart(2, "0")}`;
}

function monthLabel(month: string): string {
  const [year = 1970, index = 1] = month.split("-").map(Number);
  return new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric", timeZone: "UTC" })
    .format(new Date(Date.UTC(year, index - 1, 1)));
}

function calendarDays(month: string): CalendarDay[] {
  const [year = 1970, index = 1] = month.split("-").map(Number);
  const first = new Date(Date.UTC(year, index - 1, 1));
  const mondayOffset = (first.getUTCDay() + 6) % 7;
  const daysInMonth = new Date(Date.UTC(year, index, 0)).getUTCDate();
  const cellCount = Math.ceil((mondayOffset + daysInMonth) / 7) * 7;
  return Array.from({ length: cellCount }, (_, cell) => {
    const date = new Date(Date.UTC(year, index - 1, 1 - mondayOffset + cell));
    return {
      iso: date.toISOString().slice(0, 10),
      day: date.getUTCDate(),
      inMonth: date.getUTCMonth() === index - 1,
    };
  });
}

function NightQuality({ prediction, pending, error, placeAbove }: {
  prediction?: TonightResponse;
  pending: boolean;
  error: boolean;
  placeAbove: boolean;
}) {
  const quality = describeNightQuality(prediction, pending, error);
  const conditions = prediction?.conditions;
  const staleSince = prediction?.weather?.stale_since;
  return (
    <details className="group relative inline-flex max-w-full">
      <summary
        className="cursor-pointer list-none marker:content-none [&::-webkit-details-marker]:hidden"
        aria-label={`Night quality: ${quality.label}. Open score factors.`}
        title={quality.detail}
        data-night-quality
      >
        <Badge tone={quality.tone} className="max-w-full truncate px-0.5 py-0.5 text-[10px] sm:px-1.5">
          <CloudSun aria-hidden="true" className="size-3 shrink-0" />
          <span className="sm:hidden">{conditions == null ? (pending ? "…" : "—") : Math.round(conditions.score)}</span>
          <span className="hidden truncate sm:inline">{quality.label}</span>
        </Badge>
      </summary>
      <div
        role="tooltip"
        aria-label="Night quality details"
        className={`absolute right-0 z-40 max-h-64 w-64 max-w-[calc(100vw-2rem)] overflow-y-auto overscroll-contain rounded border border-border bg-surface p-2.5 text-left text-xs text-fg shadow-lg ${placeAbove ? "bottom-full mb-1" : "top-full mt-1"} hidden group-open:block`}
      >
        {conditions !== null && conditions !== undefined ? (
          <div className="space-y-2">
            <p className="font-semibold">{quality.label}</p>
            <ul className="list-disc space-y-0.5 pl-4 text-fg-muted">
              {conditions.reasons.map((reason) => <li key={reason}>{reason}</li>)}
            </ul>
            <ScoreBreakdown factors={conditions.factors} />
            {staleSince !== null && staleSince !== undefined && (
              <p className="text-warning">Forecast stale since {new Date(staleSince).toLocaleString()}.</p>
            )}
          </div>
        ) : (
          <p>{quality.detail}</p>
        )}
      </div>
    </details>
  );
}

function ScheduledPlan({ plan, onOpen }: {
  plan: PlanSummary;
  onOpen: () => void;
}) {
  return (
    <div className="group relative">
      <button
        type="button"
        className="flex min-h-11 w-full flex-col items-start gap-1 rounded border border-border bg-surface-raised px-2 py-1.5 text-left hover:border-accent focus-visible:outline-2 focus-visible:outline-accent"
        onClick={onOpen}
        aria-label={`Open ${plan.name} scheduled for ${plan.night_date ?? "an unscheduled night"}`}
      >
        <span className="w-full truncate text-xs font-medium text-fg">{plan.name}</span>
      </button>
    </div>
  );
}

export function CalendarTab() {
  const navigate = useNavigate();
  const snapshotPlans = useConnectionStore((state) => state.snapshot?.plans);
  const location = useConnectionStore((state) => state.snapshot?.location ?? null);
  const plansQuery = usePlansQuery();
  const forecastQuery = useForecastQuery(location !== null);
  const plans = useMemo(() => snapshotPlans ?? plansQuery.data?.plans ?? [], [plansQuery.data?.plans, snapshotPlans]);
  const [month, setMonth] = useState(monthKey);
  const days = useMemo(() => calendarDays(month), [month]);
  const history = useRunHistoryQuery(days[0]?.iso ?? `${month}-01`, days.at(-1)?.iso ?? `${month}-01`);
  const [selectedNight, setSelectedNight] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const detailsHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (selectedNight !== null) {
      detailsHeading.current?.focus({ preventScroll: true });
      detailsHeading.current?.scrollIntoView({ block: "start" });
    }
  }, [selectedNight]);
  const nightRuns = history.data?.runs.filter((run) => run.night_date === selectedNight) ?? [];
  const activeRunId = nightRuns.find((run) => run.run_id === selectedRun)?.run_id ?? nightRuns[0]?.run_id;
  const selectNight = (date: string) => { setSelectedNight(date); setSelectedRun(null); };
  const datedPlans = useMemo(
    () => plans.filter((plan): plan is PlanSummary & { night_date: string } => plan.night_date !== null),
    [plans],
  );
  const unscheduled = useMemo(() => plans.filter((plan) => plan.night_date === null), [plans]);
  const timezone = location?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  const today = observingDate(new Date(), timezone);
  const forecastDates = useMemo(
    () => location === null ? new Set<string>() : forecastNightDates(forecastQuery.data?.points ?? [], timezone, today),
    [forecastQuery.data?.points, location, timezone, today],
  );
  const visibleDates = useMemo(() => new Set(days.map((day) => day.iso)), [days]);
  const predictionDates = useMemo(
    () => [...new Set([
      ...datedPlans.map((plan) => plan.night_date).filter((date) => visibleDates.has(date)),
      ...[...forecastDates].filter((date) => visibleDates.has(date)),
    ])].sort(),
    [datedPlans, forecastDates, visibleDates],
  );
  const predictions = useQueries({
    queries: predictionDates.map((date) => nightPredictionQueryOptions(date, location !== null)),
  });
  const predictionByDate = new Map(predictionDates.map((date, index) => [date, predictions[index]]));

  const openPlan = (plan: PlanSummary) => {
    useSchedulingUiStore.getState().select(plan.id, null, plan.night_date);
    void navigate("/scheduling");
  };

  return (
    <section aria-labelledby="calendar-title" className="min-w-0 space-y-4 sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="calendar-title" className="flex items-center gap-2 text-xl font-semibold"><CalendarDays className="size-5" />Calendar</h2>
          <p className="text-sm text-fg-muted">Saved observing plans by scheduled night</p>
        </div>
        <div className="flex items-center gap-1">
          <Button aria-label="Previous month" title="Previous month" onClick={() => { setMonth((value) => shiftMonth(value, -1)); setSelectedNight(null); }}><ChevronLeft className="size-4" /></Button>
          <Button onClick={() => { setMonth(monthKey()); setSelectedNight(null); }}>Today</Button>
          <Button aria-label="Next month" title="Next month" onClick={() => { setMonth((value) => shiftMonth(value, 1)); setSelectedNight(null); }}><ChevronRight className="size-4" /></Button>
        </div>
      </div>

      <Card padded={false} className="min-w-0">
        <div className="min-w-0">
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <CardTitle>{monthLabel(month)}</CardTitle>
            {location === null && <span className="text-xs text-warning">Set an observing location to calculate night quality.</span>}
          </div>
          <div className="grid grid-cols-7 border-b border-border bg-surface-raised">
            {WEEKDAYS.map((weekday) => <div key={weekday} className="px-2 py-1.5 text-center text-xs font-semibold text-fg-muted">{weekday}</div>)}
          </div>
          <div className="grid grid-cols-7">
            {days.map((day, dayIndex) => {
              const dayPlans = datedPlans.filter((plan) => plan.night_date === day.iso);
              const prediction = predictionByDate.get(day.iso);
              return (
                <div key={day.iso} data-testid={`calendar-day-${day.iso}`} className={`min-w-0 min-h-28 border-r border-b border-border p-0.5 sm:p-1.5 last:border-r-0 ${day.inMonth ? "bg-surface" : "bg-surface-raised/45 text-fg-disabled"}`}>
                  <div className="mb-1 flex min-h-6 flex-wrap items-center justify-between gap-0.5">
                    {day.iso <= today ? <button type="button" className="min-h-[44px] min-w-[44px] rounded text-xs tabular-nums hover:bg-accent-muted focus-visible:outline-2 focus-visible:outline-accent" aria-label={`View night report for ${day.iso}`} aria-pressed={selectedNight === day.iso} onClick={() => { selectNight(day.iso); }}><time dateTime={day.iso}>{day.day}</time></button> : <time dateTime={day.iso} className="text-xs tabular-nums">{day.day}</time>}
                    {prediction !== undefined || forecastDates.has(day.iso) ? (
                      <NightQuality
                        prediction={prediction?.data}
                        pending={location !== null && (prediction?.isPending ?? false)}
                        error={location === null || (prediction?.isError ?? false)}
                        placeAbove={days.length - dayIndex <= 7}
                      />
                    ) : null}
                  </div>
                  <div className="space-y-1">
                    {dayPlans.map((plan) => (
                      <ScheduledPlan
                        key={plan.id}
                        plan={plan}
                        onOpen={() => { if (day.iso < today) selectNight(day.iso); else openPlan(plan); }}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </Card>

      {history.isError && <p role="alert" className="text-sm text-warning">Night history unavailable. Retry when the connection returns.</p>}
      {selectedNight !== null && <section aria-label="Selected night details" className="space-y-3">
        <h3 ref={detailsHeading} tabIndex={-1} style={{ scrollMarginTop: 48 }} className="text-lg font-semibold">Night summary · {selectedNight}</h3>
        <HistoricalNightPanel key={selectedNight} nightDate={selectedNight} />
        {history.isPending ? <p>Loading night history…</p> : history.isError ? null : nightRuns.length === 0 ? <p>No recorded execution for this night.</p> : <>
          <label className="flex flex-wrap items-center gap-2 text-sm">Execution
            <select className="min-h-[44px] max-w-full rounded border border-border bg-surface px-3 text-fg" value={activeRunId} onChange={(event) => { setSelectedRun(event.target.value); }}>
              {nightRuns.map((run) => <option key={run.run_id} value={run.run_id}>{run.plan_name} · {new Date(run.started_at).toLocaleString()} · {run.status}</option>)}
            </select>
          </label>
          {nightRuns.length > 1 && <p className="text-xs text-fg-muted">Multiple executions are recorded for this night. Select one to review its results.</p>}
          {activeRunId !== undefined && <NightReportPanel key={activeRunId} runId={activeRunId} />}
        </>}
      </section>}

      {unscheduled.length > 0 && (
        <Card title="Unscheduled plans" collapseKey="calendar.unscheduled" className="space-y-2">
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {unscheduled.map((plan) => (
              <button key={plan.id} type="button" className="min-h-11 rounded border border-border bg-surface-raised px-3 py-2 text-left text-sm hover:border-accent" onClick={() => { openPlan(plan); }}>
                {plan.name}
              </button>
            ))}
          </div>
        </Card>
      )}
    </section>
  );
}
