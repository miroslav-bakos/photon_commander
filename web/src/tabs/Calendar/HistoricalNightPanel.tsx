// SPDX-License-Identifier: GPL-3.0-only
import { useHistoricalNightQuery, type HistoricalFrameSummary } from "../../api/runs";
import { Card, CardTitle } from "../../components/Card";
import { Button } from "../../components/Button";

function minutes(seconds: number | null): string {
  return seconds === null ? "Unknown" : `${(seconds / 60).toFixed(1)} min`;
}

function Acquisition({ summary }: { summary: HistoricalFrameSummary }) {
  return <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1 text-sm">
    <dt>Saved frames</dt><dd>{summary.saved_frames}</dd>
    <dt>Saved integration</dt><dd>{minutes(summary.captured_integration_s)}{summary.unknown_exposure_frames > 0 && " + unknown exposures"}</dd>
    <dt>Accepted integration</dt><dd>{minutes(summary.accepted_integration_s)}</dd>
    <dt>Rejected integration</dt><dd>{minutes(summary.rejected_integration_s)}</dd>
    <dt>Unmeasured frames</dt><dd>{summary.stack_unmeasured_frames}</dd>
    <dt>Unknown exposure frames</dt><dd>{summary.unknown_exposure_frames}</dd>
    <dt>Incomplete saves</dt><dd>{summary.pending_frames}</dd>
  </dl>;
}

export function HistoricalNightPanel({ nightDate }: { nightDate: string }) {
  const query = useHistoricalNightQuery(nightDate);
  const report = query.data;
  if (report === undefined) return <Card>
    {query.isError ? <><p role="alert">Could not load the complete night report.</p><Button onClick={() => { void query.refetch(); }}>Retry</Button></> : <p>Loading the complete night report…</p>}
  </Card>;
  if (report.executions.length === 0) return <Card>No recorded execution for this night.</Card>;
  return <Card className="space-y-4">
    <CardTitle>Complete observing night</CardTitle>
    {query.isError && <p role="alert" className="text-sm text-warning">Could not refresh the night report. Showing the last received records.</p>}
    <p className="text-sm text-fg-muted">All recorded executions for this night are included. Totals count distinct retained catalogue frames; scheduled exposure intents are excluded.</p>
    <div className="grid gap-4 md:grid-cols-2">
      <section aria-label="Night light frames"><h4 className="mb-2 font-medium">Light frames</h4><Acquisition summary={report.lights} /></section>
      <section aria-label="Recorded calibration"><h4 className="mb-2 font-medium">Recorded calibration</h4><Acquisition summary={report.calibration} /></section>
    </div>
    <p className="text-sm text-fg-muted">Calibration totals include only frames linked to these executions. Missing stack outcomes are unknown, not accepted frames.</p>
    <section aria-label="Historical target totals" className="space-y-2">
      <h4 className="font-medium">Captured targets</h4>
      {report.objects.map((object, index) => <div key={`${object.object_name ?? "unknown"}-${String(index)}`} className="flex flex-wrap justify-between gap-2 rounded border border-border p-2 text-sm">
        <span className="break-words">{object.object_name ?? "Unknown target"}</span>
        <span>{object.lights.saved_frames} · {minutes(object.lights.captured_integration_s)}</span>
      </div>)}
    </section>
    <section aria-label="Historical measurements" className="space-y-1 text-sm">
      <h4 className="font-medium">Recorded capture conditions</h4>
      <p>HFR, FWHM, sky quality and weather: no historical measurements were recorded. Current conditions and forecasts are not substituted.</p>
    </section>
    <section aria-label="Original executed plans" className="space-y-2">
      <h4 className="font-medium">Original executed plans</h4>
      <p className="text-xs text-fg-muted">These are saved execution snapshots. Later edits to your plans do not change this history. Times below are UTC; planned windows are estimates, not captured integration.</p>
      {report.executions.map((execution) => <details key={execution.run.run_id} className="rounded border border-border">
        <summary className="min-h-11 cursor-pointer break-words p-3 text-sm">{execution.plan.name} · Revision {execution.run.plan_revision} · {execution.run.status}</summary>
        <div className="space-y-2 px-3 pb-3 text-sm">
          <p>{execution.run.started_at} · {execution.schedule.night.tz}</p>
          <ol className="space-y-2">
            {execution.schedule.items.map((item) => {
              const source = execution.plan.items.find((entry) => entry.id === item.item_id);
              const acquired = execution.items.find((entry) => entry.item_id === item.item_id);
              return <li key={item.item_id} className="rounded bg-surface-raised p-2">
              <p className="break-words">{item.kind.replaceAll("_", " ")} {item.target?.name ?? ""}{!item.enabled && " · Disabled"}</p>
              <p className="text-xs text-fg-muted">{item.start_utc ?? "Unknown"} → {item.end_utc ?? "Unknown"}</p>
              {source != null && "filter_name" in source && source.filter_name != null && <p>Filter: {source.filter_name}</p>}
              {source?.kind === "filter" && <p>Filter: {source.position}</p>}
              {source != null && "exposure_sec" in source && <p>Exposure: {source.exposure_sec} s</p>}
              {source != null && "gain" in source && <p>Gain: {source.gain}</p>}
              {acquired != null && <p>Saved frames: {acquired.lights.saved_frames} · {minutes(acquired.lights.captured_integration_s)}</p>}
              {item.panels.length > 0 && <p className="text-xs text-fg-muted">{item.panels.length} planned panels</p>}
              {acquired?.panel_attribution_available === false && <p className="text-xs text-warning">Captured panel attribution was not recorded.</p>}
            </li>;
            })}
          </ol>
        </div>
      </details>)}
    </section>
  </Card>;
}
