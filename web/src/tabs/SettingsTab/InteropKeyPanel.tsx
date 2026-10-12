// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { useState } from "react";
import { useDeleteSecretMutation, useSetSecretMutation } from "../../api/settings";
import { Card, CardTitle } from "../../components/Card";
import { Button } from "../../components/Button";
import { useConnectionStore } from "../../store/connectionStore";

export function InteropKeyPanel() {
  const capability = useConnectionStore((s) => s.snapshot?.secrets);
  const configured = useConnectionStore((s) => s.snapshot?.settings?.configured_secret_names?.includes("seestar.interop") ?? false);
  const save = useSetSecretMutation();
  const remove = useDeleteSecretMutation();
  const [error, setError] = useState<string | null>(null);
  return <Card>
    <CardTitle>Seestar battery telemetry</CardTitle>
    <p className="mb-3 text-sm text-fg-muted">Import an interop RSA PEM key you are entitled to use to read battery status in idle gaps. The key is stored in the server OS keyring.</p>
    <label className="block text-sm">
      {configured ? "Interop key configured — replace PEM" : "Import interop PEM"}
      <input className="mt-2 block w-full" type="file" accept=".pem" disabled={!capability?.available || save.isPending}
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (!file) return;
          setError(null);
          if (file.size > 32_768) { setError("PEM file is too large"); return; }
          void file.text().then((value) => {
            if (!value.includes("PRIVATE KEY-----")) { setError("Choose a private RSA PEM key"); return; }
            save.mutate({ name: "seestar.interop", value });
          }).catch(() => { setError("Could not read the PEM file"); });
        }} />
    </label>
    {!capability?.available && <p className="mt-2 text-sm text-warning">{capability?.reason ?? "OS keyring unavailable"}</p>}
    {configured && <Button className="mt-2" disabled={!capability?.available || remove.isPending}
      onClick={() => { remove.mutate("seestar.interop"); }}>Remove interop key</Button>}
    {(error ?? save.error ?? remove.error) && <p className="mt-2 text-sm text-danger">{error ?? save.error?.message ?? remove.error?.message}</p>}
  </Card>;
}
