// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { ImagePreview } from "../../components/ImagePreview";
import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, Copy, Download, FileImage, Home, Move, RefreshCw, Trash2, X } from "lucide-react";
import {
  archiveImageUrl,
  downloadArchiveSelectionZip,
  type ArchiveEntry,
  useArchiveHeaderQuery,
  useArchiveQualityQuery,
  useArchiveEntriesQuery,
  useDeleteArchiveFilesMutation,
  useDeleteArchiveFolderMutation,
  useTransferArchiveFilesMutation,
} from "../../api/capture";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import {
  buildArchiveFolders,
  childFolders,
  directEntries,
  type ArchiveFolder,
} from "./archiveModel";

interface DeleteConfirmation { kind: "selection"; files: string[]; directories: string[] }
interface TransferDialog { mode: "move" | "copy"; destination: string }
type SortKey = "name" | "date" | "modified" | "type" | "size";
type SortDirection = "ascending" | "descending";
interface ArchivePreferences { thumbnails: boolean; sort: { key: SortKey; direction: SortDirection } }

const compactButton = "min-h-10 rounded-md border border-border px-3 py-2 text-sm text-fg hover:bg-surface-raised";
const archivePreferencesKey = "photon.archive.preferences.v1";
const fileSelectionKey = (key: string): string => `file:${key}`;
const folderSelectionKey = (key: string): string => `folder:${key}`;

function loadArchivePreferences(): ArchivePreferences {
  const fallback: ArchivePreferences = { thumbnails: false, sort: { key: "name", direction: "ascending" } };
  try {
    const parsed = JSON.parse(globalThis.localStorage.getItem(archivePreferencesKey) ?? "null") as Partial<ArchivePreferences> | null;
    const key = parsed?.sort?.key;
    const direction = parsed?.sort?.direction;
    return {
      thumbnails: parsed?.thumbnails === true,
      sort: {
        key: key !== undefined && ["name", "date", "modified", "type", "size"].includes(key) ? key : fallback.sort.key,
        direction: direction === "descending" ? "descending" : "ascending",
      },
    };
  } catch { return fallback; }
}

function saveArchivePreferences(preferences: ArchivePreferences): void {
  try { globalThis.localStorage.setItem(archivePreferencesKey, JSON.stringify(preferences)); } catch { /* Storage may be disabled. */ }
}

function formatBytes(value: number): string {
  if (value < 1024) return `${String(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KiB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MiB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GiB`;
}

function formatExposure(entries: ArchiveEntry[]): string {
  const seconds = entries.reduce((sum, entry) => sum + (entry.exposure_s ?? 0), 0);
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${String(minutes)}m ${Math.round(seconds % 60).toString()}s` : `${Math.floor(minutes / 60).toString()}h ${(minutes % 60).toString()}m`;
}

function ScopeFacts({ entries }: { entries: ArchiveEntry[] }) {
  const filters = [...new Set(entries.map((entry) => entry.filter_name).filter((value): value is string => value !== null))];
  const temperatures = entries.map((entry) => entry.ccd_temp_c).filter((value): value is number => value !== null);
  const averageTemp = temperatures.length === 0 ? null : temperatures.reduce((sum, value) => sum + value, 0) / temperatures.length;
  return (
    <p className="mt-1 text-xs text-fg-muted">
      {entries.length.toString()} files · {formatExposure(entries)} · {formatBytes(entries.reduce((sum, entry) => sum + entry.size_bytes, 0))}
      {filters.length > 0 ? ` · ${filters.join(", ")}` : ""}
      {averageTemp === null ? "" : ` · avg ${averageTemp.toFixed(1)} °C`}
    </p>
  );
}

function FolderGlyph() {
  return (
    <svg data-folder-icon aria-hidden="true" viewBox="0 0 24 24" className="size-5 shrink-0 text-accent" fill="currentColor">
      <path d="M3 5h7l2.25 3H21v11H3V5Z" />
    </svg>
  );
}

function FullscreenArchiveViewer({ entries, currentKey, onChange, onClose }: {
  entries: ArchiveEntry[]; currentKey: string; onChange: (key: string) => void; onClose: () => void;
}) {
  const index = Math.max(0, entries.findIndex((entry) => entry.key === currentKey));
  const entry = entries[index];
  const [failed, setFailed] = useState(false);
  const [imageLoaded, setImageLoaded] = useState(false);
  const [headerPanelOpen, setHeaderPanelOpen] = useState(() => typeof window.matchMedia !== "function" || window.matchMedia("(min-width: 640px)").matches);
  const viewerRef = useRef<HTMLDivElement>(null);
  const header = useArchiveHeaderQuery(entry?.kind === "fits" && headerPanelOpen ? entry.key : null);
  const quality = useArchiveQualityQuery(entry?.kind === "fits" && headerPanelOpen ? entry.key : null);
  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const viewer = viewerRef.current;
    const focusable = () => Array.from(viewer?.querySelectorAll<HTMLElement>(
      'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex="-1"])',
    ) ?? []).filter((element) => !element.closest('[hidden], [inert]')
      && getComputedStyle(element).display !== "none" && getComputedStyle(element).visibility !== "hidden");
    focusable()[0]?.focus();
    function retainFocus(event: FocusEvent): void {
      if (viewer && !viewer.contains(event.target as Node)) (focusable()[0] ?? viewer).focus();
    }
    function trapTab(event: KeyboardEvent): void {
      if (event.key !== "Tab") return;
      const controls = focusable();
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (!first || !last) { event.preventDefault(); viewer?.focus(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === viewer)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === viewer)) {
        event.preventDefault(); first.focus();
      }
    }
    document.addEventListener("focusin", retainFocus);
    viewer?.addEventListener("keydown", trapTab);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("focusin", retainFocus);
      viewer?.removeEventListener("keydown", trapTab);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);
  useEffect(() => {
    function keyboard(event: KeyboardEvent): void {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft" && index > 0) onChange(entries[index - 1]?.key ?? currentKey);
      if (event.key === "ArrowRight" && index < entries.length - 1) onChange(entries[index + 1]?.key ?? currentKey);
    }
    globalThis.addEventListener("keydown", keyboard);
    return () => { globalThis.removeEventListener("keydown", keyboard); };
  }, [currentKey, entries, index, onChange, onClose]);
  if (entry === undefined) return null;

  return (
    <div ref={viewerRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label={`Preview ${entry.name}`} className="fixed inset-0 z-[100] flex flex-col bg-bg pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] text-fg outline-none">
      <div className="z-10 flex min-h-14 items-center justify-between gap-2 border-b border-border bg-surface px-2 sm:px-4">
        <div className="min-w-0"><p className="truncate text-sm font-medium">{entry.name}</p><p className="text-xs text-fg-muted">{entry.kind === "fits" ? "Auto-stretched FITS preview" : "Image preview"} · {index + 1} of {entries.length}</p></div>
        <div className="flex gap-1">
          <button type="button" className={compactButton} aria-label="Close preview" onClick={onClose}><X className="size-5" /></button>
        </div>
      </div>
      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        {entry.kind === "fits" && headerPanelOpen && <aside aria-label="FITS details" className="z-10 h-full w-[min(18rem,75vw)] shrink-0 overflow-y-auto border-r border-border bg-surface/90 p-2 backdrop-blur">
          <section aria-label="Star quality" className="mb-3 rounded-md border border-border p-2 text-xs">
            <h2 className="mb-2 text-sm font-semibold">Star quality</h2>
            {quality.isPending && <p className="text-fg-muted">Measuring stars…</p>}
            {quality.isError && <p className="text-fg-muted">Star quality unavailable.</p>}
            {quality.data !== undefined && <dl className="grid grid-cols-2 gap-y-1"><dt>HFR</dt><dd>{quality.data.hfr_px === null ? "—" : `${quality.data.hfr_px.toFixed(2)} px`}</dd><dt>FWHM</dt><dd>{quality.data.fwhm_px === null ? "—" : `${quality.data.fwhm_px.toFixed(2)} px`}</dd><dt>Stars</dt><dd>{quality.data.star_count}</dd></dl>}
          </section>
          <details open><summary className="sticky top-0 cursor-pointer bg-surface/95 py-2 text-sm font-semibold">FITS header</summary>{header.isPending && <p className="py-2 text-xs text-fg-muted">Reading header…</p>}{header.isError && <p role="alert" className="py-2 text-xs text-danger">{header.error.message}</p>}{header.data !== undefined && <table className="w-full table-fixed text-xs"><tbody>{Object.entries(header.data).map(([name, value]) => <tr key={name} className="border-t border-border"><th className="w-24 break-all py-1 pr-2 text-left align-top font-medium text-accent">{name}</th><td className="break-all py-1 align-top text-fg-muted">{String(value)}</td></tr>)}</tbody></table>}</details>
        </aside>}
        {entry.kind === "fits" && <button type="button" data-archive-pane-toggle aria-label={headerPanelOpen ? "Hide FITS details" : "Show FITS details"} aria-expanded={headerPanelOpen} onClick={() => { setHeaderPanelOpen((open) => !open); }} style={{ left: headerPanelOpen ? "min(18rem, 75vw)" : "20px" }} className="fixed top-1/2 z-[110] flex size-10 -translate-x-1/2 -translate-y-1/2 items-center justify-center bg-surface/95 text-xl text-accent shadow-md hover:bg-surface-raised"><span aria-hidden className={headerPanelOpen ? "rotate-180" : ""}>▸</span></button>}
      <div className="archive-preview relative min-h-0 min-w-0 flex-1 [&>.image-preview]:h-full [&>.image-preview]:min-h-0">
        {!entry.preview_available ? <p role="alert" className="m-auto p-4 text-sm">Preview unavailable. The original is untouched.</p> : (
          <ImagePreview src={archiveImageUrl(entry)} alt={entry.name} label="Archive image viewer" onLoad={() => { setFailed(false); setImageLoaded(true); }} onError={() => { setFailed(true); setImageLoaded(true); }} />
        )}
        {failed && entry.preview_available && <p role="alert" className="absolute inset-x-0 top-0 bg-bg/90 p-4 text-sm">Preview unavailable. Retrying when the connection returns. The original is untouched.</p>}
        {!imageLoaded && !failed && entry.preview_available && <div role="status" className="pointer-events-none absolute inset-0 flex items-center justify-center bg-bg/55 backdrop-blur-[1px]"><span className="rounded-md border border-border bg-surface/85 px-4 py-3 text-sm shadow-xl">{entry.kind === "fits" ? "Auto-stretching FITS preview…" : "Loading image…"}</span></div>}
        <button type="button" aria-label="Previous image" disabled={index === 0} className="absolute top-1/2 left-12 flex size-12 items-center justify-center rounded-full bg-surface/80 disabled:opacity-25" onClick={() => { onChange(entries[index - 1]?.key ?? currentKey); }}><ChevronLeft className="size-8" /></button>
        <button type="button" aria-label="Next image" disabled={index === entries.length - 1} className="absolute top-1/2 right-3 flex size-12 items-center justify-center rounded-full bg-surface/80 disabled:opacity-25" onClick={() => { onChange(entries[index + 1]?.key ?? currentKey); }}><ChevronRight className="size-8" /></button>
      </div>
      </div>
    </div>
  );
}

function FileList({ entries, folders, parentKey, thumbnails, sort, onSortChange, selectedKeys, selectedEntryKey, onToggle, onSelect, onPreview, onOpenFolder }: {
  entries: ArchiveEntry[]; folders: ArchiveFolder[]; thumbnails: boolean; selectedKeys: Set<string>; selectedEntryKey: string | null;
  parentKey: string | null;
  sort: { key: SortKey; direction: SortDirection }; onSortChange: (sort: { key: SortKey; direction: SortDirection }) => void;
  onToggle: (key: string) => void; onSelect: (entry: ArchiveEntry) => void; onPreview: (entry: ArchiveEntry) => void; onOpenFolder: (key: string) => void;
}) {
  const lastTouch = useRef<{ key: string; time: number } | null>(null);
  const touchStart = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const rows = useMemo(() => {
    const folderRows = folders.map((folder) => ({ kind: "folder" as const, folder }));
    const fileRows = entries.map((entry) => ({ kind: "file" as const, entry }));
    const factor = sort.direction === "ascending" ? 1 : -1;
    return [...folderRows, ...fileRows].sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === "folder" ? -1 : 1;
      const leftValue = left.kind === "folder" ? folderValue(left.folder, sort.key, entries) : entryValue(left.entry, sort.key);
      const rightValue = right.kind === "folder" ? folderValue(right.folder, sort.key, entries) : entryValue(right.entry, sort.key);
      return factor * leftValue.localeCompare(rightValue, undefined, { numeric: true });
    });
  }, [entries, folders, sort]);

  function changeSort(key: SortKey): void {
    onSortChange(sort.key === key
      ? { key, direction: sort.direction === "ascending" ? "descending" : "ascending" }
      : { key, direction: "ascending" });
  }
  function touchFile(entry: ArchiveEntry, now: number): void {
    if (lastTouch.current?.key === entry.key && now - lastTouch.current.time < 450) { onPreview(entry); lastTouch.current = null; }
    else { onSelect(entry); lastTouch.current = { key: entry.key, time: now }; }
  }
  return (
    <div className="mt-3 overflow-x-auto rounded-md border border-border">
      <div className="flex flex-wrap items-center gap-2 p-2 sm:hidden">
        <label className="text-xs">Sort files <select aria-label="Sort files" value={sort.key} className="min-h-11 rounded border border-border bg-surface px-2" onChange={(event) => { onSortChange({ key: event.target.value as SortKey, direction: sort.direction }); }}>{(["name", "date", "modified", "type", "size"] as const).map((key) => <option key={key} value={key}>{key === "date" ? "Date taken" : key}</option>)}</select></label>
        <button type="button" className="min-h-11 rounded border border-border px-2 text-xs" onClick={() => { onSortChange({ ...sort, direction: sort.direction === "ascending" ? "descending" : "ascending" }); }}>{sort.direction === "ascending" ? "Ascending" : "Descending"}</button>
      </div>
      <table className="w-full sm:min-w-[760px] border-collapse text-left text-sm" aria-label="Archive files">
        <thead className="bg-surface-raised text-xs text-fg-muted"><tr>
          <th className="w-12 p-2"><span className="sr-only">Select</span></th>
          <SortHeader label="Name" sortKey="name" current={sort} onSort={changeSort} />
          <SortHeader label="Date taken" sortKey="date" current={sort} onSort={changeSort} />
          <SortHeader label="Modified" sortKey="modified" current={sort} onSort={changeSort} />
          <SortHeader label="Type" sortKey="type" current={sort} onSort={changeSort} />
          <SortHeader label="Size" sortKey="size" current={sort} onSort={changeSort} />
        </tr></thead>
        <tbody>{parentKey !== null && <tr className="border-t border-border bg-surface-raised/30 hover:bg-surface-raised/60">
          <td className="p-2" />
          <td className="w-full max-w-0 p-0 sm:w-auto sm:max-w-none"><button type="button" aria-label="Open parent folder" className="flex min-h-12 w-full items-center gap-2 p-2 text-left" onClick={() => { onOpenFolder(parentKey); }}><FolderGlyph /><span className="font-medium">..</span></button></td>
          <td colSpan={4} className="p-2 text-xs text-fg-muted">Parent folder</td>
        </tr>}{rows.map((row) => row.kind === "folder" ? (
          <tr key={`folder:${row.folder.key}`} className="border-t border-border hover:bg-surface-raised/60">
            <td className="p-2"><input type="checkbox" className="h-5 w-5" aria-label={`Select folder ${row.folder.name}`} checked={selectedKeys.has(folderSelectionKey(row.folder.key))} onClick={(event) => { event.stopPropagation(); }} onChange={() => { onToggle(folderSelectionKey(row.folder.key)); }} /></td>
            <td className="w-full max-w-0 p-0 sm:w-auto sm:max-w-none"><button type="button" aria-label={`Open ${row.folder.name} folder contents`} className="flex min-h-12 w-full items-center gap-2 p-2 text-left" onDoubleClick={() => { onOpenFolder(row.folder.key); }} onClick={() => { onOpenFolder(row.folder.key); }}><FolderGlyph /><span className="min-w-0 truncate font-medium">{row.folder.name}</span></button></td>
            <td className="hidden p-2 text-fg-muted sm:table-cell">—</td><td className="hidden p-2 text-fg-muted sm:table-cell">—</td><td className="hidden p-2 text-fg-muted sm:table-cell">Folder</td><td className="hidden p-2 text-right text-fg-muted sm:table-cell">{formatBytes(row.folder.totalBytes)}</td>
          </tr>
        ) : (
          <tr key={row.entry.key} className={`border-t border-border hover:bg-surface-raised/60 ${selectedEntryKey === row.entry.key ? "bg-accent-muted" : ""}`} onClick={() => { onSelect(row.entry); }} onDoubleClick={(event) => { if ((event.target as HTMLElement).closest("input, label") === null) onPreview(row.entry); }} onPointerDown={(event) => { if (event.pointerType === "touch") touchStart.current = { x: event.clientX, y: event.clientY, moved: false }; }} onPointerMove={(event) => { const start = touchStart.current; if (start !== null && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 8) start.moved = true; }} onPointerCancel={() => { touchStart.current = null; lastTouch.current = null; }} onPointerUp={(event) => { const start = touchStart.current; touchStart.current = null; if (event.pointerType !== "touch") return; if (start === null || start.moved || (event.target as HTMLElement).closest("input, label") !== null) { lastTouch.current = null; return; } touchFile(row.entry, event.timeStamp); }}>
            <td className="p-2"><input type="checkbox" className="h-5 w-5" aria-label={`Select ${row.entry.name}`} checked={selectedKeys.has(fileSelectionKey(row.entry.key))} onClick={(event) => { event.stopPropagation(); }} onChange={() => { onToggle(fileSelectionKey(row.entry.key)); }} /></td>
            <td className="w-full max-w-0 p-2 sm:w-auto sm:max-w-none"><button type="button" className="flex min-h-11 w-full min-w-0 items-center gap-2 text-left" aria-label={`Open ${row.entry.name}`} onClick={(event) => { event.stopPropagation(); onSelect(row.entry); }} onDoubleClick={(event) => { event.stopPropagation(); onPreview(row.entry); }} onKeyDown={(event) => { if (event.key === "Enter") onPreview(row.entry); }}>
              {thumbnails && row.entry.preview_available ? <img src={archiveImageUrl(row.entry, true)} alt="" loading="lazy" className="h-12 w-16 shrink-0 rounded bg-bg object-contain" /> : <FileImage aria-hidden="true" className="size-5 shrink-0 text-accent" />}
              <span className="min-w-0 text-left"><span className="block truncate sm:max-w-72" title={row.entry.key}>{row.entry.name}</span><span className="block text-xs text-fg-muted sm:hidden">{row.entry.kind.toUpperCase()} · {formatBytes(row.entry.size_bytes)} · {new Date(row.entry.date_obs).toLocaleDateString()}</span></span>
            </button></td>
            <td className="hidden whitespace-nowrap p-2 text-fg-muted sm:table-cell">{new Date(row.entry.date_obs).toLocaleString()}</td>
            <td className="hidden whitespace-nowrap p-2 text-fg-muted sm:table-cell">{new Date(row.entry.modified_at).toLocaleString()}</td>
            <td className="hidden p-2 text-fg-muted sm:table-cell">{row.entry.kind.toUpperCase()}</td>
            <td className="hidden p-2 text-right text-fg-muted sm:table-cell">{formatBytes(row.entry.size_bytes)}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function SortHeader({ label, sortKey, current, onSort }: { label: string; sortKey: SortKey; current: { key: SortKey; direction: SortDirection }; onSort: (key: SortKey) => void }) {
  const active = current.key === sortKey;
  return <th className={`p-0 ${sortKey === "name" ? "" : "hidden sm:table-cell"} ${sortKey === "size" ? "text-right" : ""}`} aria-sort={active ? current.direction : "none"}><button type="button" className={`flex min-h-11 w-full items-center gap-1 p-2 ${sortKey === "size" ? "justify-end" : ""}`} onClick={() => { onSort(sortKey); }}>{label}{active && <ChevronDown aria-hidden="true" className={`size-3 transition-transform ${current.direction === "ascending" ? "rotate-180" : ""}`} />}</button></th>;
}

function entryValue(entry: ArchiveEntry, key: SortKey): string {
  if (key === "name") return entry.name;
  if (key === "date") return entry.date_obs;
  if (key === "modified") return entry.modified_at;
  if (key === "type") return entry.kind;
  return entry.size_bytes.toString().padStart(20, "0");
}

function folderValue(folder: ArchiveFolder, key: SortKey, entries: ArchiveEntry[]): string {
  if (key === "name") return folder.name;
  if (key === "type") return "folder";
  if (key === "size") return folder.totalBytes.toString().padStart(20, "0");
  const direct = entries.filter((entry) => entry.directory === folder.key);
  const values = direct.map((entry) => key === "date" ? entry.date_obs : entry.modified_at).sort();
  return values.at(-1) ?? "";
}

export function ArchiveTab() {
  const actionsMenuRef = useRef<HTMLDetailsElement>(null);
  const initialPreferences = useMemo(() => loadArchivePreferences(), []);
  const archive = useArchiveEntriesQuery();
  const deleteFiles = useDeleteArchiveFilesMutation();
  const deleteFolder = useDeleteArchiveFolderMutation();
  const transferFiles = useTransferArchiveFilesMutation();
  const entries = useMemo(() => archive.data?.entries ?? [], [archive.data?.entries]);
  const folders = useMemo(() => buildArchiveFolders(archive.data?.directories ?? [], entries), [archive.data?.directories, entries]);
  const [folderKey, setFolderKey] = useState(".");
  const [thumbnails, setThumbnails] = useState(initialPreferences.thumbnails);
  const [sort, setSort] = useState(initialPreferences.sort);
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [selectedEntryKey, setSelectedEntryKey] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<DeleteConfirmation | null>(null);
  const [transferDialog, setTransferDialog] = useState<TransferDialog | null>(null);
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [zipPending, setZipPending] = useState(false);
  const [zipError, setZipError] = useState<string | null>(null);

  const scopeEntries = useMemo(() => directEntries(entries, folderKey), [entries, folderKey]);
  const currentFolder = folders.find((folder) => folder.key === folderKey);
  const subfolders = childFolders(folders, folderKey);
  const scopeTitle = folderKey === "." ? archive.data?.root_name ?? "Captures" : currentFolder?.name ?? "Folder";
  const selectedFileKeys = [...selectedKeys].filter((key) => key.startsWith("file:")).map((key) => key.slice(5));
  const selectedDirectoryKeys = [...selectedKeys].filter((key) => key.startsWith("folder:")).map((key) => key.slice(7));
  const visibleSelectionKeys = [
    ...subfolders.map((folder) => folderSelectionKey(folder.key)),
    ...scopeEntries.map((entry) => fileSelectionKey(entry.key)),
  ];
  const deleteBusy = deleteFiles.isPending || deleteFolder.isPending;
  const actionBusy = deleteBusy || transferFiles.isPending || zipPending;
  const previewEntries = useMemo(() => scopeEntries.filter((entry) => entry.preview_available), [scopeEntries]);
  useEffect(() => { saveArchivePreferences({ thumbnails, sort }); }, [sort, thumbnails]);

  function closeActionsMenu(): void {
    if (actionsMenuRef.current !== null) actionsMenuRef.current.open = false;
  }

  function openFolder(key: string): void {
    setFolderKey(key);
    setSelectedKeys(new Set());
    setSelectedEntryKey(null);
    setConfirmation(null);
    setTransferDialog(null);
  }
  function toggleKey(key: string): void { setSelectedKeys((current) => { const next = new Set(current); if (next.has(key)) next.delete(key); else next.add(key); return next; }); setConfirmation(null); }
  function selectAll(): void { setSelectedKeys((current) => visibleSelectionKeys.length > 0 && visibleSelectionKeys.every((key) => current.has(key)) ? new Set() : new Set(visibleSelectionKeys)); setConfirmation(null); }
  async function confirmDelete(): Promise<void> {
    if (confirmation === null) return;
    try {
      const results = [];
      if (confirmation.files.length > 0) results.push(await deleteFiles.mutateAsync({ keys: confirmation.files }));
      for (const directory of confirmation.directories) results.push(await deleteFolder.mutateAsync({ directory }));
      const filesDeleted = results.reduce((sum, result) => sum + result.files_deleted, 0);
      const bytesDeleted = results.reduce((sum, result) => sum + result.bytes_deleted, 0);
      setNotice(`Deleted ${filesDeleted.toString()} file${filesDeleted === 1 ? "" : "s"} (${formatBytes(bytesDeleted)}).`);
      setSelectedKeys(new Set()); setSelectedEntryKey(null); setConfirmation(null);
    } catch { /* mutation error is rendered below */ }
  }
  async function downloadSelection(): Promise<void> {
    if (selectedKeys.size === 0) return;
    closeActionsMenu();
    setZipPending(true);
    setZipError(null);
    try {
      await downloadArchiveSelectionZip({ keys: selectedFileKeys, directories: selectedDirectoryKeys });
    } catch (error) {
      setZipError(error instanceof Error ? error.message : "Could not download the selected archive items.");
    } finally {
      setZipPending(false);
    }
  }
  async function confirmTransfer(): Promise<void> {
    if (transferDialog === null || selectedKeys.size === 0) return;
    try {
      const result = await transferFiles.mutateAsync({ keys: selectedFileKeys, directories: selectedDirectoryKeys, destination: transferDialog.destination, mode: transferDialog.mode });
      setNotice(`${result.mode === "move" ? "Moved" : "Copied"} ${result.files_transferred.toString()} file${result.files_transferred === 1 ? "" : "s"} in ${String(result.directories_transferred + selectedFileKeys.length)} selected item${result.directories_transferred + selectedFileKeys.length === 1 ? "" : "s"} to ${result.destination === "." ? archive.data?.root_name ?? "the capture folder" : result.destination}.`);
      setSelectedKeys(new Set()); setTransferDialog(null);
    } catch { /* mutation error is rendered below */ }
  }

  return (
    <section aria-labelledby="archive-title" className="flex min-h-0 w-full flex-1 flex-col gap-3 pb-4">
      <div className="flex flex-wrap items-center justify-between gap-2"><div><h2 id="archive-title" className="text-xl font-semibold">Archive</h2><p className="text-sm text-fg-muted">Browse and manage the capture folder.</p></div><label className="flex min-h-11 items-center gap-2 text-sm"><input type="checkbox" checked={thumbnails} onChange={(event) => { setThumbnails(event.target.checked); }} /> Thumbnails</label></div>
      {archive.isPending && <p role="status" className="text-sm text-fg-muted">Scanning capture folders…</p>}
      {archive.isError && <p role="alert" className="text-sm text-danger">{archive.error.message}</p>}
      {archive.data?.truncated === true && <p role="status" className="text-sm text-warning">This large archive is showing only the newest catalogue entries; the folder tree remains complete.</p>}
      {notice !== null && <p role="status" className="text-sm text-success">{notice}</p>}
      <Card title={scopeTitle} collapseKey="archive:contents" layoutId="archive-contents">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Button aria-label="Open capture home folder" title="Capture home" disabled={folderKey === "."} onClick={() => { openFolder("."); }}><Home className="size-5" /></Button>
            <Button aria-label="Refresh archive files" title="Refresh files" disabled={archive.isFetching} onClick={() => { void archive.refetch(); }}><RefreshCw className={`size-5 ${archive.isFetching ? "animate-spin" : ""}`} /></Button>
            <span className="max-w-full break-all text-xs text-fg-muted">{folderKey === "." ? archive.data?.root_name ?? "Captures" : `${archive.data?.root_name ?? "Captures"}/${folderKey}`}</span>
          </div>
          <ScopeFacts entries={scopeEntries} />
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button disabled={visibleSelectionKeys.length === 0} onClick={selectAll}>{visibleSelectionKeys.length > 0 && visibleSelectionKeys.every((key) => selectedKeys.has(key)) ? "Clear selection" : "Select all"}</Button>
          <details ref={actionsMenuRef} className="relative"><summary className={`${compactButton} flex cursor-pointer list-none items-center gap-2 ${selectedKeys.size === 0 ? "pointer-events-none opacity-50" : ""}`}>Actions ({selectedKeys.size.toString()}) <ChevronDown className="size-4" /></summary><div className="absolute left-0 z-20 mt-1 min-w-44 rounded-md border border-border bg-surface p-1 shadow-xl"><button type="button" className="flex min-h-11 w-full items-center gap-2 rounded px-3 hover:bg-surface-raised" onClick={() => { closeActionsMenu(); setTransferDialog({ mode: "move", destination: "." }); }}><Move className="size-4" /> Move</button><button type="button" className="flex min-h-11 w-full items-center gap-2 rounded px-3 hover:bg-surface-raised" onClick={() => { closeActionsMenu(); setTransferDialog({ mode: "copy", destination: "." }); }}><Copy className="size-4" /> Copy</button><button type="button" className="flex min-h-11 w-full items-center gap-2 rounded px-3 hover:bg-surface-raised" disabled={zipPending} onClick={() => { void downloadSelection(); }}><Download className="size-4" /> {zipPending ? "Preparing ZIP…" : "Download a ZIP"}</button><button type="button" className="flex min-h-11 w-full items-center gap-2 rounded px-3 text-danger hover:bg-surface-raised" onClick={() => { closeActionsMenu(); setConfirmation({ kind: "selection", files: selectedFileKeys, directories: selectedDirectoryKeys }); }}><Trash2 className="size-4" /> Delete</button></div></details>
        </div>
        {transferDialog !== null && <div role="dialog" aria-label={`${transferDialog.mode} selected items`} className="mt-3 rounded-md border border-accent bg-accent-muted p-3 text-sm"><label className="block font-medium">Destination folder<select className="mt-2 min-h-11 w-full rounded-md border border-border bg-surface px-3" value={transferDialog.destination} onChange={(event) => { setTransferDialog({ ...transferDialog, destination: event.target.value }); }}>{folders.map((folder) => <option key={folder.key} value={folder.key}>{folder.key === "." ? archive.data?.root_name ?? "Capture folder" : folder.key}</option>)}</select></label><div className="mt-3 flex gap-2"><Button variant="primary" disabled={actionBusy} onClick={() => { void confirmTransfer(); }}>{transferFiles.isPending ? `${transferDialog.mode === "move" ? "Moving" : "Copying"}…` : transferDialog.mode === "move" ? "Move here" : "Copy here"}</Button><Button disabled={actionBusy} onClick={() => { setTransferDialog(null); }}>Cancel</Button></div></div>}
        {confirmation !== null && <div role="alertdialog" aria-label="Confirm permanent deletion" className="mt-3 rounded-md border border-danger bg-danger/10 p-3 text-sm"><p>{`Permanently delete ${String(confirmation.files.length + confirmation.directories.length)} selected item${confirmation.files.length + confirmation.directories.length === 1 ? "" : "s"}?`} This cannot be undone.</p><div className="mt-3 flex gap-2"><Button variant="danger" disabled={deleteBusy} onClick={() => { void confirmDelete(); }}>{deleteBusy ? "Deleting…" : "Delete permanently"}</Button><Button disabled={deleteBusy} onClick={() => { setConfirmation(null); }}>Cancel</Button></div></div>}
        {(deleteFiles.isError || deleteFolder.isError || transferFiles.isError) && <p role="alert" className="mt-3 text-sm text-danger">{(deleteFiles.error ?? deleteFolder.error ?? transferFiles.error)?.message}</p>}
        {zipError !== null && <p role="alert" className="mt-3 text-sm text-danger">{zipError}</p>}
        <FileList entries={scopeEntries} folders={subfolders} parentKey={folderKey === "." ? null : currentFolder?.parent ?? "."} thumbnails={thumbnails} sort={sort} onSortChange={setSort} selectedKeys={selectedKeys} selectedEntryKey={selectedEntryKey} onToggle={toggleKey} onSelect={(entry) => { setSelectedEntryKey(entry.key); }} onPreview={(entry) => { setSelectedEntryKey(entry.key); setPreviewKey(entry.key); }} onOpenFolder={openFolder} />
        {scopeEntries.length === 0 && subfolders.length === 0 && <p className="mt-4 text-sm text-fg-muted">Nothing is stored here yet.</p>}
      </Card>
      {archive.data !== undefined && <div data-archive-storage className="order-last mx-auto w-full sm:w-[30vw] pt-3"><div className="mb-2 flex flex-wrap justify-between gap-2 text-sm"><span>{formatBytes(archive.data.storage_free_bytes)} free</span><span className="text-fg-muted">{formatBytes(archive.data.storage_total_bytes - archive.data.storage_free_bytes)} used</span><span className="text-fg-muted">{formatBytes(archive.data.storage_total_bytes)} total</span></div><div role="progressbar" aria-label="Capture storage used" aria-valuemin={0} aria-valuemax={100} aria-valuenow={archive.data.storage_total_bytes === 0 ? 0 : Math.round(100 * (archive.data.storage_total_bytes - archive.data.storage_free_bytes) / archive.data.storage_total_bytes)} className="h-2 overflow-hidden rounded-full bg-surface-raised"><div className="h-full rounded-full bg-accent" style={{ width: `${archive.data.storage_total_bytes === 0 ? "0" : String(100 * (archive.data.storage_total_bytes - archive.data.storage_free_bytes) / archive.data.storage_total_bytes)}%` }} /></div></div>}
      {previewKey !== null && <FullscreenArchiveViewer key={previewKey} entries={previewEntries} currentKey={previewKey} onChange={setPreviewKey} onClose={() => { setPreviewKey(null); }} />}
    </section>
  );
}
