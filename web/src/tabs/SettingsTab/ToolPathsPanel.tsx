// SPDX-License-Identifier: GPL-3.0-only
import { useState } from "react";
import { useSaveToolPaths, useToolPathsQuery } from "../../api/externalTools";
import { Button } from "../../components/Button";
import { Field } from "../../components/Field";
import { HostPathPicker } from "../../components/HostPathPicker";

export function ToolPathsPanel() {
  const paths = useToolPathsQuery();
  const save = useSaveToolPaths();
  const [cli, setCli] = useState(paths.data?.astap_cli ?? "");
  const [database, setDatabase] = useState(paths.data?.astap_data_dir ?? "");
  const key = JSON.stringify(paths.data);
  const [seen, setSeen] = useState(key);
  if (key !== seen) { setSeen(key); setCli(paths.data?.astap_cli ?? ""); setDatabase(paths.data?.astap_data_dir ?? ""); }
  if (paths.isPending) return <p>Loading ASTAP paths…</p>;
  if (paths.isError) return <p role="alert" className="text-danger">{paths.error.message}</p>;
  return <section aria-label="ASTAP paths" className="space-y-3 rounded-md border border-border p-3">
    <h3 className="font-medium">ASTAP on this computer</h3>
    <p className="text-sm text-fg-muted">Install ASTAP and D20 for the Seestar main camera, plus W08 for its wide camera. D50 is an alternative for smaller fields. Empty paths use automatic detection or server environment settings.</p>
    <Field label="ASTAP executable" value={cli} onChange={(event) => { setCli(event.target.value); }} />
    <HostPathPicker label="ASTAP executable" initialPath={cli} directory={false} onSelect={setCli} />
    <Field label="ASTAP star database folder" value={database} onChange={(event) => { setDatabase(event.target.value); }} />
    <HostPathPicker label="star database folder" initialPath={database} directory onSelect={setDatabase} />
    <Button variant="primary" disabled={save.isPending} onClick={() => { save.mutate({ astap_cli: cli.trim() || null, astap_data_dir: database.trim() || null }); }}>Save ASTAP paths</Button>
    {save.isError && <p role="alert" className="text-danger">{save.error.message}</p>}
    {save.isSuccess && <p role="status">ASTAP paths saved. Check the solver status below before observing.</p>}
  </section>;
}
