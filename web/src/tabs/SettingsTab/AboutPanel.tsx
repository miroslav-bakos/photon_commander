// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { timedApiFetch } from "../../performance/diagnostics";
import logo from "../../assets/brand/logo.svg";
import { useState } from "react";
import { useRestartApplicationMutation, useHostRebootStatusQuery, useHostRebootMutation, useHostShutdownStatusQuery, useHostShutdownMutation } from "../../api/settings";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";

async function reloadWhenBackendReturns() {
  await new Promise((resolve) => { window.setTimeout(resolve, 1_500); });
  for (let attempt = 0; attempt < 120; attempt += 1) {
    try {
      const response = await timedApiFetch(new Request(new URL(`/api/health?restart=${String(Date.now())}`, window.location.origin), { cache: "no-store" }));
      if (response.ok) {
        window.location.reload();
        return;
      }
    } catch {
      // The expected interval between the old process exiting and the new one binding.
    }
    await new Promise((resolve) => { window.setTimeout(resolve, 500); });
  }
}

async function reconnectAfterHostReboot(previousEpoch: string) {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await new Promise((resolve) => { window.setTimeout(resolve, 1_000); });
    try {
      const response = await timedApiFetch(new Request(new URL("/api/state", window.location.origin), { cache: "no-store" }));
      if (response.ok) {
        const state = await response.json() as { epoch?: string };
        if (state.epoch && state.epoch !== previousEpoch) { window.location.reload(); return; }
      }
    } catch { /* Expected while the Pi boots. */ }
  }
}

/**
 * Static About/Credits panel (ssc-8i3.3) — no server state to load, unlike
 * every other panel in this file's `Group`, since there is nothing here that
 * varies per install.
 *
 * The source-code link is conditional on `__SSC_SOURCE_URL__`
 * (`vite.config.ts`'s `resolveSourceUrl`, injected via Vite `define`), which
 * is `null` whenever this checkout has no git remote configured — this repo
 * currently doesn't. Omitting the link is the architecture policy invariant 7 reading:
 * a fabricated or hardcoded repo URL would be wrong the moment the real
 * remote differs from whatever was guessed, and silently wrong is worse than
 * absent.
 */
export function AboutPanel() {
  const rebootStatus = useHostRebootStatusQuery();
  const shutdownStatus = useHostShutdownStatusQuery();
  const reboot = useHostRebootMutation();
  const shutdown = useHostShutdownMutation();
  const [hostAction, setHostAction] = useState<"reboot" | "shutdown" | null>(null);
  const restart = useRestartApplicationMutation();
  const [restarting, setRestarting] = useState(false);
  return (
    <Card>
      <CardTitle>About</CardTitle>
      <img src={logo} alt="Photon Commander" className="h-auto w-full" />
      <p className="mt-3 text-center text-base font-medium text-fg">
        Keep photons under your control—until they become electrons.
      </p>
      <p className="mt-2 text-center text-sm text-fg-muted">
        Control, planning, and automated imaging for smart telescopes.
      </p>
      <p className="mt-2 text-center text-xs text-fg-muted">
        Independent software compatible with ZWO Seestar telescopes. Previously known as
        Seestar Commander; not affiliated with or endorsed by ZWO.
      </p>
      <p className="mt-3 text-xs text-fg-muted">
        Free software under GPL-3.0-only. Third-party components retain their own licences.
        <br />
        Copyright © 2026 Miroslav Bakoš.
      </p>
      <div className="mt-4 border-t border-border pt-4">
        <p className="text-sm font-medium text-fg">Maintenance</p>
        <p className="mt-1 text-xs text-fg-muted">
          Restarts the backend service, then reloads this frontend when Photon Commander is ready.
          Capture, stacking, and broadcasts currently in progress will stop.
        </p>
        <Button
          className="mt-3"
          variant="danger"
          disabled={restart.isPending || restarting || hostAction !== null}
          onClick={() => {
            if (!window.confirm("Restart Photon Commander now? Active capture, stacking, and broadcasts will stop.")) return;
            restart.mutate(undefined, {
              onSuccess: () => {
                setRestarting(true);
                void reloadWhenBackendReturns();
              },
            });
          }}
        >
          {restarting ? "Restarting Photon Commander…" : "Restart backend + frontend"}
        </Button>
        <div className="mt-4 border-t border-border pt-4">
          <p className="text-sm font-medium">Raspberry Pi power</p>
          <p className="mt-1 text-xs text-fg-muted">Reboot the whole Pi, or shut it down safely before disconnecting power. After shutdown, power it on physically.</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button variant="danger" disabled={!rebootStatus.data?.available || restarting || hostAction !== null || reboot.isPending || shutdown.isPending}
              onClick={() => {
                if (!window.confirm("Reboot the whole Raspberry Pi now? Photon Commander will disconnect while it boots.")) return;
                reboot.mutate(undefined, { onSuccess: (response) => { setHostAction("reboot"); void reconnectAfterHostReboot(response.previous_epoch); } });
              }}>Reboot Raspberry Pi</Button>
            <Button variant="danger" disabled={!shutdownStatus.data?.available || restarting || hostAction !== null || reboot.isPending || shutdown.isPending}
              onClick={() => {
                if (!window.confirm("Shut down the whole Raspberry Pi now? You will need to power it on physically.")) return;
                shutdown.mutate(undefined, { onSuccess: () => { setHostAction("shutdown"); } });
              }}>Shut down Raspberry Pi</Button>
          </div>
          {!rebootStatus.data?.available && <p className="mt-2 text-xs text-fg-muted">{rebootStatus.data?.reason ?? "Checking Raspberry Pi power controls…"}</p>}
          {shutdownStatus.data && !shutdownStatus.data.available && shutdownStatus.data.reason !== rebootStatus.data?.reason && <p className="mt-2 text-xs text-fg-muted">{shutdownStatus.data.reason}</p>}
          {(reboot.isError || shutdown.isError) && <p role="alert" className="mt-2 text-sm text-danger">{reboot.error?.message ?? shutdown.error?.message}</p>}
          {hostAction === "shutdown" && <p role="status" className="mt-2 text-sm">Shutdown requested. Wait for the Pi to finish shutting down before unplugging power.</p>}
          {hostAction === "reboot" && <p role="status" className="mt-2 text-sm">Reboot requested. <Button onClick={() => { window.location.reload(); }}>Reconnect</Button></p>}
        </div>
        {restart.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">{restart.error.message}</p>
        )}
      </div>
    </Card>
  );
}
