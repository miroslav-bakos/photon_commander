// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Card, CardTitle } from "../../components/Card";
import { useAutofocusStateQuery, type AutofocusSampleState } from "../../api/focuser";
import type { ReactNode } from "react";
import { translate, useLanguage } from "../../i18n/locale";

const WIDTH = 640;
const HEIGHT = 260;
const LEFT = 54;
const RIGHT = 16;
const TOP = 16;
const BOTTOM = 38;

const STAGE_COLOR: Record<AutofocusSampleState["stage"], string> = {
  baseline: "var(--ssc-fg-muted)",
  coarse: "var(--ssc-accent)",
  fine: "var(--ssc-success)",
  validation: "var(--ssc-warning)",
  control: "var(--ssc-danger)",
};
const STAGE_LABEL: Record<AutofocusSampleState["stage"], string> = {
  baseline: "Baseline", coarse: "Coarse sweep", fine: "Fine sweep",
  validation: "Validation", control: "Control frame",
};

function paddedRange(values: number[], minimumPadding: number): [number, number] {
  const low = Math.min(...values);
  const high = Math.max(...values);
  const padding = Math.max(minimumPadding, (high - low) * 0.1);
  return [low - padding, high + padding];
}

/** Position versus HFR, updated from the authoritative persisted autofocus
 * telemetry. SVG keeps the plot sharp and responsive without adding a chart
 * dependency to the Capture bundle. */
function AutofocusSurface({ embedded, children }: { embedded: boolean; children: ReactNode }) {
  return embedded
    ? <div className="mt-4 border-t border-border pt-4">{children}</div>
    : <Card layoutId="autofocus-curve" layoutLabel="Autofocus curve">{children}</Card>;
}

export function AutofocusChart({ embedded = false }: { embedded?: boolean }) {
  const language = useLanguage();
  const query = useAutofocusStateQuery();
  const state = query.data;

  if (query.isPending) {
    return <AutofocusSurface embedded={embedded}><CardTitle>Autofocus curve</CardTitle><p className="text-sm text-fg-muted">Loading autofocus state…</p></AutofocusSurface>;
  }
  if (state === undefined) {
    return <AutofocusSurface embedded={embedded}><CardTitle>Autofocus curve</CardTitle><p role="alert" className="text-sm text-danger">Could not load autofocus progress.</p></AutofocusSurface>;
  }

  const samples = state.samples;
  const medianStars = samples.length === 0 ? 0 : [...samples].sort((a, b) => a.star_count - b.star_count)[Math.floor(samples.length / 2)]?.star_count ?? 0;
  const credibleStarFloor = Math.max(3, Math.floor(medianStars * 0.25));
  const credible = samples.filter((sample) => sample.star_count >= credibleStarFloor);
  const best = credible.reduce<AutofocusSampleState | null>((current, sample) => current === null || sample.hfr_px < current.hfr_px ? sample : current, null);
  const allX = [...samples.map((sample) => sample.position), ...state.curve.map((point) => point.position)];
  const allY = [...samples.map((sample) => sample.hfr_px), ...state.curve.map((point) => point.hfr_px)];
  const staleWarning = query.isError ? <p role="alert" className="text-sm text-warning">Could not refresh autofocus progress. Showing the last received measurements.</p> : null;
  const interruptedWarning = state.status === "interrupted" ? <p role="alert" className="text-sm text-warning">Autofocus was interrupted by a restart. These are saved measurements; the current focus position is unverified. Start a new autofocus run.</p> : null;

  if (allX.length === 0 || allY.length === 0) {
    return (
      <AutofocusSurface embedded={embedded}>
        <CardTitle>Autofocus curve</CardTitle>
        {staleWarning}
        {interruptedWarning}
        {state.error !== null && <p role="alert" className="text-sm text-danger">{state.error}</p>}
        <p className="text-sm text-fg-muted">
          {state.status === "running" ? "Waiting for the first measurable autofocus sample." : state.status === "failed" ? "Autofocus ended before a measurable sample was available." : "Start autofocus to plot focus position against HFR."}
        </p>
      </AutofocusSurface>
    );
  }

  const [minX, maxX] = paddedRange(allX, 1);
  const [minY, maxY] = paddedRange(allY, 0.05);
  const plotWidth = WIDTH - LEFT - RIGHT;
  const plotHeight = HEIGHT - TOP - BOTTOM;
  const x = (value: number) => LEFT + ((value - minX) / (maxX - minX)) * plotWidth;
  const y = (value: number) => TOP + ((maxY - value) / (maxY - minY)) * plotHeight;
  const curvePoints = state.curve.map((point) => `${String(x(point.position))},${String(y(point.hfr_px))}`).join(" ");
  const status = translate(state.status === "running"
    ? `Measuring ${state.stage ?? "focus"}…`
    : state.status === "completed"
      ? `Complete${state.final_position === null ? "" : ` at ${String(state.final_position)}`}`
      : state.status === "failed"
        ? "Failed"
        : state.status === "interrupted"
          ? "Interrupted"
        : "Idle", language);

  return (
    <AutofocusSurface embedded={embedded}>
      {staleWarning}
      {interruptedWarning}
      <div className="flex items-center justify-between gap-3">
        <CardTitle>Autofocus curve</CardTitle>
        <span className="text-xs text-fg-muted">{status} · {String(samples.length)} points</span>
      </div>
      <svg
        viewBox={`0 0 ${String(WIDTH)} ${String(HEIGHT)}`}
        className="mt-2 h-auto w-full"
        role="img"
        aria-label={`Autofocus curve with ${String(samples.length)} measured points`}
      >
        <line x1={LEFT} y1={TOP} x2={LEFT} y2={HEIGHT - BOTTOM} stroke="var(--ssc-border-strong)" />
        <line x1={LEFT} y1={HEIGHT - BOTTOM} x2={WIDTH - RIGHT} y2={HEIGHT - BOTTOM} stroke="var(--ssc-border-strong)" />
        {[0, 0.5, 1].map((fraction) => {
          const value = minY + (maxY - minY) * fraction;
          const py = y(value);
          return <g key={fraction}>
            <line x1={LEFT} y1={py} x2={WIDTH - RIGHT} y2={py} stroke="var(--ssc-border)" />
            <text x={LEFT - 6} y={py + 4} textAnchor="end" fill="var(--ssc-fg-muted)" style={{ font: "11px sans-serif" }}>{value.toFixed(2)}</text>
          </g>;
        })}
        {[minX, (minX + maxX) / 2, maxX].map((value) => (
          <text key={value} x={x(value)} y={HEIGHT - 16} textAnchor="middle" fill="var(--ssc-fg-muted)" style={{ font: "11px sans-serif" }}>{Math.round(value)}</text>
        ))}
        <text x={12} y={TOP + plotHeight / 2} textAnchor="middle" fill="var(--ssc-fg-muted)" transform={`rotate(-90 12 ${String(TOP + plotHeight / 2)})`} style={{ font: "11px sans-serif" }}>HFR (px)</text>
        <text x={LEFT + plotWidth / 2} y={HEIGHT - 2} textAnchor="middle" fill="var(--ssc-fg-muted)" style={{ font: "11px sans-serif" }}>Focus position</text>
        {state.candidate_position != null && (
          <line data-candidate x1={x(state.candidate_position)} y1={TOP} x2={x(state.candidate_position)} y2={HEIGHT - BOTTOM} stroke="var(--ssc-warning)" strokeDasharray="4 4" />
        )}
        {curvePoints !== "" && <polyline data-focus-curve points={curvePoints} fill="none" stroke="var(--ssc-fg)" strokeWidth={2} />}
        {samples.map((sample, index) => (
          <circle
            data-focus-sample
            key={`${sample.stage}-${String(sample.position)}-${String(index)}`}
            cx={x(sample.position)}
            cy={y(sample.hfr_px)}
            r={sample.star_count >= credibleStarFloor ? 4.5 : 3}
            fill={STAGE_COLOR[sample.stage]}
            opacity={sample.star_count >= credibleStarFloor ? 1 : 0.35}
            stroke="var(--ssc-surface-raised)"
            strokeWidth={1.5}
          >
            <title>{`${translate(STAGE_LABEL[sample.stage], language)}: position ${String(sample.position)}, HFR ${sample.hfr_px.toFixed(3)} px, ${String(sample.star_count)} stars`}</title>
          </circle>
        ))}
      </svg>
      {state.error !== null && <p role="alert" className="mt-2 text-xs text-danger">{state.error}</p>}
      <div className="mt-2 rounded-md border border-border bg-bg px-3 py-2 text-xs">
        <p className="font-medium text-fg">Decision</p>
        <p className="mt-1 text-fg-muted">
          {state.status === "completed"
            ? state.final_position === state.candidate_position
              ? `Applied the validated curve minimum at ${String(state.final_position)}.`
              : `Kept ${String(state.final_position)} after the candidate did not beat the fresh control frame.`
            : state.status === "failed"
              ? "No focus move was accepted; the starting position was restored when possible. Review weak points and retry with a longer exposure."
              : best === null
                ? "Waiting for measurable stars."
                : `Best well-supported point so far is ${String(best.position)} at HFR ${best.hfr_px.toFixed(2)} px.`}
        </p>
        {samples.some((sample) => sample.star_count < credibleStarFloor) && <p className="mt-1 text-warning">Faded points have too few stars relative to this run and are excluded from candidate selection.</p>}
      </div>
      <div className="mt-2 max-h-36 overflow-y-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-fg-muted"><tr><th className="py-1">Position</th><th>HFR</th><th>Stars</th><th>Use</th></tr></thead>
          <tbody>{samples.map((sample, index) => <tr key={`diagnostic-${sample.stage}-${String(sample.position)}-${String(index)}`} className="border-t border-border"><td className="py-1 tabular-nums">{sample.position}</td><td className="tabular-nums">{sample.hfr_px.toFixed(3)}</td><td className="tabular-nums">{sample.star_count}</td><td className={sample.star_count >= credibleStarFloor ? "text-success" : "text-warning"}>{sample.star_count >= credibleStarFloor ? "Fit" : "Weak"}</td></tr>)}</tbody>
        </table>
      </div>
      <p className="mt-1 text-xs text-fg-muted">Coarse <span className="text-accent">●</span> · fine <span className="text-success">●</span> · validation <span className="text-warning">●</span> · control <span className="text-danger">●</span></p>
    </AutofocusSurface>
  );
}
