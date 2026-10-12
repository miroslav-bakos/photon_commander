// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import { Link } from "react-router-dom";
import { useRemoteAccessQuery, type RemoteAccess } from "../../api/settings";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { QrCode } from "../../components/QrCode";

function statusPresentation(state: RemoteAccess["state"]): {
  label: string;
  tone: "success" | "warning" | "danger" | "neutral";
} {
  switch (state) {
    case "serving": return { label: "Private access active", tone: "success" };
    case "public_exposure": return { label: "Public exposure", tone: "danger" };
    case "unsafe_bind": return { label: "Network bind is not private", tone: "danger" };
    case "ready": return { label: "Ready to configure", tone: "warning" };
    case "signed_out": return { label: "Sign-in required", tone: "warning" };
    case "daemon_unavailable": return { label: "Service unavailable", tone: "warning" };
    case "not_installed": return { label: "Not installed", tone: "neutral" };
  }
}

async function copyText(value: string): Promise<void> {
  await navigator.clipboard.writeText(value);
}

function CopyCommand({ label, command }: { label: string; command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-fg-muted">{label}</p>
      <div className="flex min-w-0 items-stretch gap-2">
        <code className="min-w-0 flex-1 overflow-x-auto rounded-md border border-border bg-bg px-3 py-2 text-xs text-fg">{command}</code>
        <Button
          className="shrink-0"
          onClick={() => {
            void copyText(command).then(() => {
              setCopied(true);
              window.setTimeout(() => { setCopied(false); }, 1500);
            });
          }}
        >
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </div>
  );
}

export function RemoteAccessPanel() {
  const remote = useRemoteAccessQuery();
  if (remote.isPending) return <p className="text-sm text-fg-muted">Checking private remote access…</p>;
  if (remote.isError) return <p className="text-sm text-danger">Could not inspect Tailscale: {remote.error.message}</p>;

  const status = statusPresentation(remote.data.state);
  const remoteUrl = remote.data.url ?? null;
  return (
    <div className="space-y-4">
      <div className="rounded-md border border-border bg-surface-raised p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 className="font-medium text-fg">Tailscale Serve</h3>
            <p className="mt-1 text-sm text-fg-muted">Private HTTPS access from your own tailnet; Photon Commander stays on loopback.</p>
          </div>
          <Badge tone={status.tone}>{status.label}</Badge>
        </div>
        <p className={`mt-3 text-sm ${remote.data.state === "public_exposure" || remote.data.state === "unsafe_bind" ? "text-danger" : "text-fg-muted"}`} role={remote.data.state === "public_exposure" || remote.data.state === "unsafe_bind" ? "alert" : undefined}>
          {remote.data.detail}
        </p>
        {remote.data.detected_path != null && <p className="mt-2 break-all font-mono text-xs text-fg-muted">{remote.data.detected_path}</p>}
      </div>

      {remoteUrl !== null && remote.data.state === "serving" && (
        <div className="flex flex-col items-center gap-3 rounded-md border border-border bg-surface-raised p-3 text-center">
          <QrCode value={remoteUrl} />
          <a href={remoteUrl} target="_blank" rel="noreferrer" className="break-all text-sm font-medium text-link underline">
            {remoteUrl}
          </a>
          <p className="text-xs text-fg-muted">Open this address only on a device signed into an allowed Tailscale account.</p>
        </div>
      )}

      {remote.data.state === "not_installed" && (
        <p className="text-sm text-fg-muted">Install Tailscale on this computer and on the remote phone or tablet, then sign both into the same tailnet.</p>
      )}
      {remote.data.state === "unsafe_bind" && (
        <p className="text-sm text-fg-muted">Change the Photon Commander service bind to 127.0.0.1 and restart it. The exact service configuration depends on how Photon Commander was installed.</p>
      )}
      {remote.data.state === "signed_out" && <CopyCommand label="Connect this computer" command="sudo tailscale up" />}
      {(remote.data.state === "ready" || remote.data.state === "not_installed" || remote.data.state === "daemon_unavailable") && (
        <CopyCommand label="Enable persistent private HTTPS after Tailscale is connected" command={remote.data.setup_command} />
      )}
      {remote.data.state === "serving" && (
        <>
          <CopyCommand label="Inspect current routing" command={remote.data.status_command} />
          <CopyCommand label="Disable Photon Commander remote access" command={remote.data.disable_command} />
        </>
      )}
      {remote.data.state === "public_exposure" && (
        <CopyCommand label="Disable the public route immediately" command="sudo tailscale funnel off" />
      )}

      <p className="rounded-md border border-warning bg-surface-raised px-3 py-2 text-sm text-warning">
        Do not use Tailscale Funnel or router port forwarding for routine telescope control. Keep an independent local way to stop motion or remove power.
      </p>
      <div className="flex flex-wrap gap-4 text-sm">
        <Link to="/help?section=knowledge&article=remote-access" className="inline-flex min-h-11 items-center font-medium text-link underline">Full setup and troubleshooting guide</Link>
        <a href="https://tailscale.com/docs/features/tailscale-serve" target="_blank" rel="noreferrer" className="inline-flex min-h-11 items-center font-medium text-link underline">Tailscale Serve documentation</a>
        <Button onClick={() => { void remote.refetch(); }}>Check again</Button>
      </div>
      <p className="text-xs text-fg-muted">Photon Commander only reads local Tailscale status. It never installs software, signs into an account, stores an auth key, changes access policy, or enables Serve/Funnel.</p>
    </div>
  );
}
