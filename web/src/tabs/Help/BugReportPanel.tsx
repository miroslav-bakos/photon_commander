// SPDX-License-Identifier: GPL-3.0-only
import { useState } from "react";
import { LoaderCircle } from "lucide-react";
import { api, unwrap } from "../../api/client";
import type { components } from "../../api/schema";
import { Button } from "../../components/Button";

type Report = components["schemas"]["BugReport"];
export function BugReportPanel() {
  const [report, setReport] = useState<Report | null>(null);
  const [category, setCategory] = useState<Report["category"]>("other");
  const [operation, setOperation] = useState<"preparing" | "sending" | null>(null);
  const busy = operation !== null;
  const [message, setMessage] = useState("");
  const [consent, setConsent] = useState(false);
  const [sent, setSent] = useState(false);
  const [details, setDetails] = useState("");
  async function prepare() {
    setOperation("preparing"); setMessage(""); setConsent(false); setSent(false); setReport(null);
    try { setReport(await unwrap(api.POST("/api/bug-reports/preview", { body: { category } }))); }
    catch { setMessage("Could not prepare the report. Check the connection and try again."); }
    finally { setOperation(null); }
  }
  async function send() {
    if (report === null || !consent || sent) return;
    setOperation("sending"); setMessage("");
    try {
      await unwrap(api.POST("/api/bug-reports/send", { body: { report_id: report.report_id, details } }));
      setSent(true); setMessage(`Accepted for email delivery. Report ID: ${report.report_id}`);
    } catch { setMessage("Delivery was not confirmed. Download this report or retry with the same report ID."); }
    finally { setOperation(null); }
  }
  function download() {
    if (report === null) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `photon-report-${report.report_id}.json`; anchor.click();
    window.setTimeout(() => { URL.revokeObjectURL(url); }, 1000);
  }
  return <section id="bug-report" aria-labelledby="bug-report-title" className="space-y-3 rounded-lg border border-border bg-surface p-4">
    <h2 id="bug-report-title" className="text-lg font-semibold">Report a bug</h2>
    <p className="text-sm text-fg-muted">Send privacy-safe beta diagnostics to miroslav@miroslavbakos.com. Includes event codes, severity, relative timings and anonymous error traces. Excludes location, coordinates, names, addresses, settings, images, raw messages and credentials.</p>
    <p className="text-xs text-fg-muted">Nothing is sent until you review and press Send. The website hosting provider may retain connection IPs in access logs; they are not included in the report.</p>
    <p className="text-xs text-fg-muted">Email delivery includes a ZIP attachment.</p>
    <label className="block text-sm" htmlFor="bug-report-details">What happened? (optional)</label>
    <textarea id="bug-report-details" aria-describedby="bug-report-details-help" className="block w-full rounded border border-border bg-bg p-3 text-sm" rows={5} maxLength={4000} value={details} disabled={busy || sent} onChange={(event) => { setDetails(event.target.value); setConsent(false); setReport(null); setMessage(""); }} />
    <p id="bug-report-details-help" className="text-xs text-fg-muted">Describe what you were doing, what you expected and what happened. Up to 4,000 characters. This text is sent as written in the email body, separately from the sanitized logs. Do not include passwords, location or other personal information.</p>
    <label className="block text-sm">Problem area<select className="ml-2 min-h-11 rounded border border-border bg-bg px-3" value={category} disabled={busy} onChange={(event) => { setCategory(event.target.value as Report["category"]); setReport(null); setConsent(false); setSent(false); }}>
      <option value="connection">Connection</option><option value="capture">Capture</option><option value="planning">Planning</option><option value="alignment">Alignment</option><option value="interface">Interface</option><option value="other">Other</option>
    </select></label>
    <Button disabled={busy} onClick={() => { void prepare(); }} aria-busy={operation === "preparing"}>{operation === "preparing" && <LoaderCircle aria-hidden="true" className="h-4 w-4 motion-safe:animate-spin" />}{operation === "preparing" ? "Preparing report…" : "Prepare bug report"}</Button>
    {report !== null && <>
      <p className="text-sm">{report.events.length} diagnostic events · Report ID: {report.report_id}</p>
      {(!report.logging_enabled || report.events.length === 0) && <p className="text-sm text-warning">Detailed diagnostic history is unavailable. This report may have limited debugging value.</p>}
      <details><summary className="min-h-11 cursor-pointer py-3">Review exact diagnostic contents</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-bg p-3 text-xs">{JSON.stringify(report, null, 2)}</pre></details>
      <label className="flex min-h-11 items-center gap-2 text-sm"><input type="checkbox" checked={consent} disabled={busy || sent} onChange={(event) => { setConsent(event.target.checked); }} />I agree to send these diagnostics and my entered details to miroslav@miroslavbakos.com.</label>
      <div className="flex flex-wrap gap-2"><Button disabled={busy || !consent || sent} onClick={() => { void send(); }} aria-busy={operation === "sending"}>{operation === "sending" ? "Sending report…" : "Send bug report"}</Button><Button disabled={busy} onClick={download}>Download report</Button></div>
    </>}
    <p role="status" aria-live="polite" className="text-sm">{operation === "preparing" ? "Preparing privacy-safe diagnostics. This may take a moment; nothing is being sent." : operation === "sending" ? "Sending your report. Please wait for confirmation." : message}</p>
  </section>;
}
