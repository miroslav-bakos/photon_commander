// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useRef, useState, type ChangeEvent } from "react";
import { exportServerConfiguration, importServerConfiguration } from "../../api/settings";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { useLayoutStore } from "../../layout/layoutStore";
import { CONFIGURATION_FORMAT, MAX_CONFIGURATION_FILE_BYTES, parseConfigurationFile, type ConfigurationFile } from "./configurationFile";

export function ExportImportPanel() {
  const [warnings, setWarnings] = useState<string[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const [pending, setPending] = useState<ConfigurationFile | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resetAllLayouts = useLayoutStore((state) => state.resetAll);

  async function exportSettings(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const server = await exportServerConfiguration();
      const persistedClient = ((server.settings.client as { entries?: Record<string, string> } | undefined)?.entries ?? {});
      const file: ConfigurationFile = {
        format: CONFIGURATION_FORMAT,
        schema_version: 1,
        exported_at: new Date().toISOString(),
        server,
        client: { local_storage: persistedClient },
      };
      const url = URL.createObjectURL(new Blob([JSON.stringify(file, null, 2)], { type: "application/json" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `photon-commander-settings-${file.exported_at.slice(0, 10)}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not export settings.");
    } finally {
      setBusy(false);
    }
  }

  async function chooseFile(event: ChangeEvent<HTMLInputElement>): Promise<void> {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file === undefined) return;
    setError(null);
    setPending(null);
    if (file.size > MAX_CONFIGURATION_FILE_BYTES) {
      setError("Configuration file is larger than 2 MiB.");
      return;
    }
    try {
      setPending(parseConfigurationFile(await file.text()));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not read configuration.");
    }
  }

  async function applyImport(): Promise<void> {
    if (pending === null) return;
    setBusy(true);
    setError(null);
    try {
      const result = await importServerConfiguration({
        ...pending.server,
        settings: {
          ...pending.server.settings,
          client: { entries: pending.client.local_storage },
        },
      });
      if ((result.warnings ?? []).length > 0) {
        setWarnings(result.warnings);
        setPending(null);
        // Keep the imported UI settings unapplied until the warning is acknowledged.
      } else {
        window.location.reload();
      }
      setBusy(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not import settings.");
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardTitle>Configuration backup</CardTitle>
      <p className="text-sm text-fg-muted">
        Export server settings and the backend-saved layouts, collapsed cards, theme-related choices, and screen values shared by every client.
        Secret values, pairing sessions, captures, and caches are never included.
      </p>
      <p className="mt-2 text-xs text-warning">
        The file can contain your observing location, weather contact, local telescope address, and filesystem paths. Store it privately.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button disabled={busy || warnings.length > 0} onClick={() => { void exportSettings(); }}>{busy ? "Working…" : "Export settings"}</Button>
        <Button variant="secondary" disabled={busy || warnings.length > 0} onClick={() => { input.current?.click(); }}>Choose file to import…</Button>
        <input ref={input} className="sr-only" type="file" accept="application/json,.json" aria-label="Configuration file" onChange={(event) => { void chooseFile(event); }} />
        <Button variant="secondary" disabled={busy || warnings.length > 0} onClick={() => { resetAllLayouts(); }}>Reset all layouts</Button>
      </div>
      {pending !== null && (
        <div className="mt-3 rounded-md border border-warning p-3">
          <p className="text-sm font-medium text-fg">Review import</p>
          <p className="mt-1 text-xs text-fg-muted">
            Exported {new Date(pending.exported_at).toLocaleString()} · {Object.keys(pending.server.settings).length} server sections · {Object.keys(pending.client.local_storage).length} browser values.
          </p>
          <p className="mt-2 text-xs text-warning">Existing included settings will be replaced. Secrets, captures, and active sessions remain untouched.</p>
          <div className="mt-3 flex gap-2">
            <Button variant="primary" disabled={busy || warnings.length > 0} onClick={() => { void applyImport(); }}>Apply and reload</Button>
            <Button variant="secondary" disabled={busy || warnings.length > 0} onClick={() => { setPending(null); }}>Cancel</Button>
          </div>
        </div>
      )}
      {warnings.length > 0 && <div role="alert" className="mt-3 rounded-md border border-warning p-3">
        {warnings.map((warning) => <p key={warning} className="text-sm text-warning">{warning}</p>)}
        <Button className="mt-3" onClick={() => { window.location.reload(); }}>Continue and reload</Button>
      </div>}
      {error !== null && <p role="alert" className="mt-3 text-sm text-danger">{error}</p>}
    </Card>
  );
}
