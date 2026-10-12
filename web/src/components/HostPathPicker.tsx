// SPDX-License-Identifier: GPL-3.0-only
import { useState } from "react";
import { useHostPathsQuery } from "../api/externalTools";
import { Button } from "./Button";
import { Field } from "./Field";

export function HostPathPicker({ label, initialPath, directory, onSelect }: {
  label: string; initialPath: string; directory: boolean; onSelect: (path: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState<string | undefined>();
  const [draft, setDraft] = useState("");
  const listing = useHostPathsQuery(open, path);
  function choose(value: string) { onSelect(value); setOpen(false); }
  if (!open) return <Button onClick={() => {
    const parent = directory ? initialPath : initialPath.slice(0, Math.max(initialPath.lastIndexOf("/"), initialPath.lastIndexOf("\\")) + 1);
    setPath(parent.length ? parent : undefined); setDraft(parent); setOpen(true);
  }}>Browse {label}</Button>;
  return <section aria-label={`Browse ${label}`} className="space-y-2 rounded-md border border-border p-3">
    <p className="text-xs text-fg-muted">Folders on the computer running Photon Commander.</p>
    <div className="flex flex-wrap gap-2">
      <Field label="Server folder" value={draft} onChange={(event) => { setDraft(event.target.value); }} />
      <Button onClick={() => { setPath(draft.trim() || undefined); }}>Open path</Button>
      <Button onClick={() => { setOpen(false); }}>Close browser</Button>
    </div>
    {listing.isPending && <p role="status">Loading server folders…</p>}
    {listing.isError && <p role="alert" className="text-danger">{listing.error.message}</p>}
    {listing.data && <>
      <p className="break-all font-mono text-xs">{listing.data.path}</p>
      <div className="flex flex-wrap gap-2">
        {listing.data.parent && <Button onClick={() => { setPath(listing.data.parent ?? undefined); setDraft(listing.data.parent ?? ""); }}>Parent folder</Button>}
        {listing.data.roots.map((root) => <Button key={root} onClick={() => { setPath(root); setDraft(root); }}>{root}</Button>)}
        {directory && <Button variant="primary" onClick={() => { choose(listing.data.path); }}>Choose this folder</Button>}
      </div>
      <ul className="max-h-64 overflow-auto">
        {listing.data.entries.filter((entry) => directory ? entry.directory : true).map((entry) => <li key={entry.path}>
          <button type="button" className="min-h-11 w-full break-all rounded px-2 py-2 text-left text-link hover:bg-surface-raised focus-visible:outline-2 focus-visible:outline-focus-ring" onClick={() => {
            if (entry.directory) { setPath(entry.path); setDraft(entry.path); }
            else choose(entry.path);
          }}>{entry.directory ? "Folder: " : "File: "}{entry.name}</button>
        </li>)}
      </ul>
      {listing.data.truncated && <p className="text-warning">Large folder: only part of the listing is shown. Enter a more specific path.</p>}
    </>}
  </section>;
}
