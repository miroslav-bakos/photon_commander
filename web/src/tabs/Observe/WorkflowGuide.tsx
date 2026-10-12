// SPDX-License-Identifier: GPL-3.0-only
import { Link } from "react-router-dom";
import { useConnectionStore } from "../../store/connectionStore";

export function WorkflowGuide() {
  const connected = useConnectionStore((state) => state.status === "open" && state.snapshot?.link?.control_connected === true);
  return <nav aria-label="Your observing night" className="grid grid-cols-2 gap-2 sm:grid-cols-4">
    {[
      { label: "1 · Connect", to: "/settings", detail: connected ? "Telescope connected" : "Discover your telescope" },
      { label: "2 · Prepare", to: "/planning", detail: "Choose targets and check your plan" },
      { label: "3 · Observe", to: "/live", detail: "Capture, progress and controls" },
      { label: "4 · Review", to: "/calendar", detail: "Saved frames and night results" },
    ].map((step) => <Link key={step.label} to={step.to} className="min-h-[44px] rounded-lg border border-border bg-surface px-3 py-2 text-sm text-fg hover:border-accent"><span className="font-semibold">{step.label}</span><span className="mt-1 hidden text-xs text-fg-muted sm:block">{step.detail}</span></Link>)}
  </nav>;
}
