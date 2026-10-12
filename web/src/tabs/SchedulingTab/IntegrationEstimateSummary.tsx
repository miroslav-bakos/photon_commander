// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { IntegrationEstimate } from "./integrationEstimate";
import { formatIntegrationDuration } from "./integrationEstimate";

function minutes(seconds: number): string {
  return `${(seconds / 60).toFixed(1)} min`;
}

export function IntegrationEstimateSummary({ estimate }: { estimate: IntegrationEstimate }) {
  return (
    <section className="border-t border-border pt-3" aria-label="Estimated integration yield">
      <div className="flex items-baseline justify-between gap-3">
        <h4 className="text-sm font-medium text-fg-muted">Estimated yield</h4>
        <strong className="text-sm text-success tabular-nums">
          ≈ {formatIntegrationDuration(estimate.cleanIntegrationS)} clean
        </strong>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs tabular-nums sm:grid-cols-4">
        <div><dt className="text-fg-muted">Frames</dt><dd>{estimate.frames} × {String(estimate.exposureS)} s</dd></div>
        <div><dt className="text-fg-muted">Efficiency</dt><dd>{Math.round(estimate.efficiencyPct)}%</dd></div>
        <div><dt className="text-fg-muted">Frame overhead</dt><dd>≈ {minutes(estimate.frameOverheadS)}</dd></div>
        <div><dt className="text-fg-muted">Dithering</dt><dd>≈ {minutes(estimate.ditherS)} ({estimate.ditherCount})</dd></div>
        <div><dt className="text-fg-muted">Pointing/setup</dt><dd>≈ {minutes(estimate.setupS)}</dd></div>
        <div><dt className="text-fg-muted">Scheduled</dt><dd>{formatIntegrationDuration(estimate.scheduledS)}</dd></div>
      </dl>
      <p className="mt-2 text-xs text-fg-muted">
        Based on measured S30 Pro timing. Clean means captured exposure time before clouds, tracking losses, and live-stack rejection.
      </p>
    </section>
  );
}
