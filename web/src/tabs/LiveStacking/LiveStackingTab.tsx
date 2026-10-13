// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { ImagePreview } from "../../components/ImagePreview";
import { usePreviewCardHeight } from "./usePreviewCardHeight";
import {
  useRef,
  useState,
  type CSSProperties,
  type Dispatch,
  type SetStateAction,
} from "react";
import {
  useLiveStackMastersQuery,
  useLiveStackQuery,
  useResetLiveStackMutation,
  useSaveLiveStackMutation,
  useStopLiveStackMutation,
  useUploadLiveStackMasterMutation,
  browseLiveStackDirectories,
  type FrameType,
  type LiveStackMaster,
  type LiveStackResponse,
  type LiveStackDirectoryResponse,
} from "../../api/capture";
import { Button } from "../../components/Button";
import { StopIcon } from "../../components/ActionIcons";
import { Card, CardTitle } from "../../components/Card";
import { PaneLayout } from "../../components/PaneLayout";
import { Field } from "../../components/Field";
import { useLatestOperation } from "../../api/operations";
import { CaptureStatusPanel } from "../Capture/CaptureStatusPanel";
import { ObservingModeSwitch } from "../Observe/ObservingModeSwitch";
import { useSystemMetricsQuery } from "../../api/system";
import { ActionableError } from "../../components/ActionableError";

type MasterKind = Exclude<FrameType, "light">;


function formatIntegration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds.toFixed(seconds % 1 === 0 ? 0 : 1)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return `${String(minutes)}m ${remainder.toFixed(0)}s`;
  const hours = Math.floor(minutes / 60);
  return `${String(hours)}h ${String(minutes % 60)}m`;
}

function LiveStackViewer({ src, alt, status }: { src: string; alt: string; status: LiveStackResponse }) {
  const [nativeSize, setNativeSize] = useState<{ width: number; height: number } | null>(null);

  const rejected = status.rejected_count ?? 0;
  const received = status.frame_count + rejected;
  const calibration = `dark ${status.dark_master == null ? "no" : "yes"} / flat ${status.flat_master == null ? "no" : "yes"} / bias ${status.bias_master == null ? "no" : "yes"}`;
  const targetHeader = status.target_header ?? status.object_name ?? "Unknown target";

  return (
    <ImagePreview src={src} alt={alt} label="Live-stack image viewer" onLoad={(event) => { setNativeSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight }); }}>
      <div className="pointer-events-none absolute inset-x-0 top-0 bg-bg/70 px-3 py-3 sm:px-16 text-center text-sm font-semibold text-fg backdrop-blur-sm sm:text-base">
        {targetHeader}
        {nativeSize !== null && (
          <span className="ml-2 font-normal text-fg-muted">
            · {String(nativeSize.width)}×{String(nativeSize.height)} px · Double-tap to zoom / fit
          </span>
        )}
      </div>
      <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-bg/70 px-3 py-2 font-mono text-[10px] leading-relaxed text-fg sm:text-xs">
        <p>LIVE&nbsp; {status.object_name ?? "Unknown target"} | {status.filter_name ?? "No filter"} | integration {formatIntegration(status.total_integration_s)}</p>
        <p>accepted {String(status.frame_count)} / received {String(received)} / rejected {String(rejected)} | exposure {status.exposure_s == null ? "—" : `${String(status.exposure_s)}s`} | gain {status.gain ?? "—"} | sensor {status.ccd_temp_c == null ? "—" : `${status.ccd_temp_c.toFixed(1)}°C`}</p>
        <p>HFR {status.hfr_px == null ? "—" : `${status.hfr_px.toFixed(2)}px`} | FWHM {status.fwhm_px == null ? "—" : `${status.fwhm_px.toFixed(2)}px`} | measured stars {status.star_count ?? "—"} | detected sources {status.detected_source_count ?? "—"} | frame contrast {status.frame_contrast_snr == null ? "—" : status.frame_contrast_snr.toFixed(1)} | stack SNR est. {status.snr_estimate == null ? "—" : status.snr_estimate.toFixed(1)}</p>
        <p>queue {String(status.pending_files ?? 0)} | {status.last_rejection_reason ?? status.error ?? "OK"} | {calibration}</p>
      </div>
    </ImagePreview>
  );
}

function MasterSelect({
  label,
  kind,
  value,
  masters,
  setValue,
  upload,
  automatic,
}: {
  label: string;
  kind: MasterKind;
  value: string | null | undefined;
  masters: LiveStackMaster[];
  setValue: Dispatch<SetStateAction<string | null | undefined>>;
  upload: (kind: MasterKind) => void;
  automatic: boolean;
}) {
  return (
    <label className="text-xs text-fg-muted">
      {label}
      <select
        className="mt-1 min-h-10 w-full rounded-md border border-border bg-bg px-2 text-sm text-fg"
        value={automatic ? "" : value ?? ""}
        disabled={automatic}
        onChange={(event) => {
          if (event.target.value === "__upload__") upload(kind);
          else setValue(event.target.value === "" ? null : event.target.value);
        }}
      >
        <option value="">{automatic ? "Automatic" : "None"}</option>
        {masters
          .filter((master) => master.frame_type === kind)
          .map((master) => (
            <option key={master.path} value={master.path}>{master.name}</option>
          ))}
        <option value="__upload__">Custom file…</option>
      </select>
    </label>
  );
}

export function LiveStackingTab() {
  const previewCardHeight = usePreviewCardHeight();
  const mastersQuery = useLiveStackMastersQuery();
  const statusQuery = useLiveStackQuery(true);
  const systemQuery = useSystemMetricsQuery();
  const resetStack = useResetLiveStackMutation();
  const saveStack = useSaveLiveStackMutation();
  const saveOperation = useLatestOperation("live_stack_save");
  const saveActive = saveOperation?.status === "queued" || saveOperation?.status === "running";
  const savedOutputPath = typeof saveOperation?.result?.output_path === "string"
    ? saveOperation.result.output_path
    : "the capture folder";
  const stopStack = useStopLiveStackMutation();
  const uploadMaster = useUploadLiveStackMasterMutation();
  const [response, setResponse] = useState<LiveStackResponse | null>(null);
  const [darkMaster, setDarkMaster] = useState<string | null>();
  const [biasMaster, setBiasMaster] = useState<string | null>();
  const [flatMaster, setFlatMaster] = useState<string | null>();
  const [uploadKind, setUploadKind] = useState<MasterKind | null>(null);
  const [autoSelectMasters, setAutoSelectMasters] = useState<boolean>();
  const [watchFolder, setWatchFolder] = useState<string>();
  const [watchRecursive, setWatchRecursive] = useState<boolean>();
  const [opticId, setOpticId] = useState("");
  const [binning, setBinning] = useState<1 | 2>();
  const [autoCrop, setAutoCrop] = useState(true);
  const [folderBrowser, setFolderBrowser] = useState<LiveStackDirectoryResponse | null>(null);
  const [folderBrowserBusy, setFolderBrowserBusy] = useState(false);
  const [folderBrowserError, setFolderBrowserError] = useState<string | null>(null);
  const uploadInput = useRef<HTMLInputElement>(null);
  const status = statusQuery.data ?? response;
  const selectedWatchFolder = watchFolder ?? status?.watch_folder ?? "";
  const selectedWatchRecursive = watchRecursive ?? status?.watch_recursive ?? false;
  const masters = mastersQuery.data?.masters ?? [];
  const selectedDark = darkMaster === undefined ? status?.dark_master ?? null : darkMaster;
  const selectedBias = biasMaster === undefined ? status?.bias_master ?? null : biasMaster;
  const selectedFlat = flatMaster === undefined ? status?.flat_master ?? null : flatMaster;
  const selectedAutoSelectMasters = autoSelectMasters ?? status?.auto_select_masters ?? true;
  const stackIdentity = `${status?.output_path ?? ""}:${String(status?.sequence ?? 0)}`;
  const liveImage = `/api/capture/live-stack/image?stack=${encodeURIComponent(stackIdentity)}&v=${String(status?.frame_count ?? 0)}&auto_crop=${String(autoCrop)}`;
  const totalMemory = systemQuery.data?.metrics.memory.total_bytes;
  const automaticBinning = totalMemory === undefined ? undefined : totalMemory < 3 * 1024 ** 3 ? 2 : 1;
  const selectedBinning = binning ?? status?.binning ?? automaticBinning;


  function chooseUpload(kind: MasterKind): void {
    setUploadKind(kind);
    uploadInput.current?.click();
  }

  async function browseFolder(path?: string): Promise<void> {
    setFolderBrowserBusy(true);
    setFolderBrowserError(null);
    try {
      setFolderBrowser(await browseLiveStackDirectories(path));
    } catch (error) {
      setFolderBrowserError(error instanceof Error ? error.message : "Could not browse that folder.");
    } finally {
      setFolderBrowserBusy(false);
    }
  }

  return (
    <PaneLayout
      primary={
        <div className="space-y-3">
          <ObservingModeSwitch />
          <CaptureStatusPanel />
        </div>
      }
      secondary={
      <Card
        title={
          <span aria-label="Live stack" className="inline-flex items-center gap-2">
            <span>Live stack</span>
            <span
              role="status"
              aria-label={status?.active === true ? "Live stacking active" : "Live stacking inactive"}
              className={`h-2.5 w-2.5 shrink-0 rounded-full ${status?.active === true ? "animate-pulse bg-dot-bad" : "bg-dot-unknown"}`}
              style={status?.active === true ? { animationDuration: "650ms" } : undefined}
            />
          </span>
        }
        collapseKey="liveStack.preview"
        className="mobile-preview-card live-stack-preview-card lg:sticky lg:top-4"
        style={{ "--preview-card-height": previewCardHeight === undefined ? undefined : `${String(previewCardHeight)}px` } as CSSProperties}
      >
        {status !== null && (
          <div className="mb-3 text-right text-xs text-fg-muted">
            {String(status.frame_count)} frame(s) · live_{String(status.sequence).padStart(3, "0")} · {String(status.binning ?? 1)}×{String(status.binning ?? 1)}
          </div>
        )}
        {status?.frame_count !== undefined && status.frame_count > 0 ? (
          <LiveStackViewer
            key={stackIdentity}
            src={liveImage}
            alt={`Live stack ${String(status.sequence)}`}
            status={status}
          />
        ) : (
          <div className="flex min-h-64 items-center justify-center rounded-md border border-border bg-surface-raised p-4 text-center text-sm text-fg-muted">
            Start live stacking, then new light frames in the current capture session will appear here automatically.
          </div>
        )}
      </Card>
      }
      detail={
      <div className="space-y-4">
        <Card>
          <CardTitle>Stacking controls</CardTitle>
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3">
            <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] items-end gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
              <Field
                label="Watch folder (optional)"
                value={selectedWatchFolder}
                placeholder="Use current Photon Commander capture session"
                onChange={(event) => { setWatchFolder(event.target.value); }}
              />
              <Button disabled={folderBrowserBusy} onClick={() => { void browseFolder(); }}>
                Browse
              </Button>
            </div>
            <p className="text-xs text-fg-muted">
              Server-side folder containing FITS files from Photon Commander or another capture application.
            </p>
            {folderBrowser !== null && (
              <div role="dialog" aria-label="Choose live-stack folder" className="rounded-md border border-border bg-bg p-3">
                <p className="break-all text-xs text-fg-muted">{folderBrowser.path}</p>
                <div className="mt-2 max-h-52 space-y-1 overflow-y-auto">
                  {folderBrowser.parent !== null && (
                    <button type="button" className="block w-full rounded px-2 py-1 text-left text-sm text-fg hover:bg-surface-raised" onClick={() => { void browseFolder(folderBrowser.parent ?? undefined); }}>
                      ↑ Parent folder
                    </button>
                  )}
                  {folderBrowser.directories.map((directory) => (
                    <button key={directory} type="button" className="block w-full rounded px-2 py-1 text-left text-sm text-fg hover:bg-surface-raised" onClick={() => { void browseFolder(directory); }}>
                      {directory.split("/").at(-1) ?? directory}
                    </button>
                  ))}
                  {folderBrowser.directories.length === 0 && <p className="px-2 py-1 text-sm text-fg-muted">No subfolders</p>}
                </div>
                <div className="mt-3 flex gap-2">
                  <Button variant="primary" onClick={() => { setWatchFolder(folderBrowser.path); setFolderBrowser(null); }}>
                    Select this folder
                  </Button>
                  <Button onClick={() => { setFolderBrowser(null); }}>Cancel</Button>
                </div>
              </div>
            )}
            {folderBrowserError !== null && <p role="alert" className="text-sm text-danger">{folderBrowserError}</p>}
            <label className="flex items-center gap-2 text-sm text-fg">
              <input
                type="checkbox"
                checked={selectedWatchRecursive}
                disabled={selectedWatchFolder.trim() === ""}
                onChange={(event) => { setWatchRecursive(event.target.checked); }}
              />
              Include subfolders
            </label>
            <label className="flex items-center gap-2 text-sm text-fg">
              <input
                type="checkbox"
                checked={selectedAutoSelectMasters}
                onChange={(event) => {
                  const automatic = event.target.checked;
                  setAutoSelectMasters(automatic);
                  if (!automatic) {
                    setDarkMaster(null);
                    setBiasMaster(null);
                    setFlatMaster(null);
                  }
                }}
              />
              Automatically select compatible masters
            </label>
            <label className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1 text-sm text-fg">
              Stack resolution
              <select
                aria-label="Stack resolution"
                className="min-w-0 w-full max-w-full rounded-md border border-border bg-bg px-3 py-2 text-fg"
                value={selectedBinning ?? 1}
                onChange={(event) => { setBinning(event.target.value === "2" ? 2 : 1); }}
              >
                <option value={1}>Full source resolution</option>
                <option value={2}>2×2 color binning (half width and height, faster)</option>
              </select>
              <span className="text-xs text-fg-muted">
                2×2 preserves Bayer color while reducing live-stack registration and memory use.
              </span>
            </label>
            <label className="flex items-center gap-2 text-sm text-fg">
              <input
                type="checkbox"
                checked={autoCrop}
                onChange={(event) => { setAutoCrop(event.target.checked); }}
              />
              Auto-crop dithering edges in preview and stream
            </label>
            <Field
              label="Optic ID for flat matching (optional)"
              value={opticId}
              placeholder="Read from light FITS when present"
              onChange={(event) => { setOpticId(event.target.value); }}
            />
            <MasterSelect label="Dark master" kind="dark" value={selectedDark} masters={masters} setValue={setDarkMaster} upload={chooseUpload} automatic={selectedAutoSelectMasters} />
            <MasterSelect label="Bias master" kind="bias" value={selectedBias} masters={masters} setValue={setBiasMaster} upload={chooseUpload} automatic={selectedAutoSelectMasters} />
            <MasterSelect label="Flat master" kind="flat" value={selectedFlat} masters={masters} setValue={setFlatMaster} upload={chooseUpload} automatic={selectedAutoSelectMasters} />
            <input
              ref={uploadInput}
              className="hidden"
              type="file"
              accept=".fit,.fits"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file !== undefined && uploadKind !== null) {
                  uploadMaster.mutate({ frameType: uploadKind, file }, {
                    onSuccess: (master) => {
                      if (master.frame_type === "dark") setDarkMaster(master.path);
                      if (master.frame_type === "bias") setBiasMaster(master.path);
                      if (master.frame_type === "flat") setFlatMaster(master.path);
                    },
                  });
                }
                event.target.value = "";
              }}
            />
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button
              variant="primary"
              disabled={resetStack.isPending}
              onClick={() => {
                resetStack.mutate(
                  {
                    dark_master_path: selectedAutoSelectMasters ? null : selectedDark,
                    bias_master_path: selectedAutoSelectMasters ? null : selectedBias,
                    flat_master_path: selectedAutoSelectMasters ? null : selectedFlat,
                    auto_select_masters: selectedAutoSelectMasters,
                    watch_folder: selectedWatchFolder.trim() === "" ? null : selectedWatchFolder.trim(),
                    watch_recursive: selectedWatchRecursive,
                    optic_id: opticId.trim() === "" ? null : opticId.trim(),
                    binning: selectedBinning,
                  },
                  {
                    onSuccess: setResponse,
                  },
                );
              }}
            >
              {status?.active === true ? "Reset stack" : "Start new stack"}
            </Button>
            {status?.active === true && (
              <Button disabled={saveStack.isPending || saveActive || status.frame_count === 0} onClick={() => { saveStack.mutate(undefined, { onSuccess: setResponse }); }}>
                {saveStack.isPending || saveActive ? "Saving…" : "Save stack"}
              </Button>
            )}
            {status?.active === true && (
              <Button disabled={stopStack.isPending} onClick={() => { stopStack.mutate(undefined, { onSuccess: setResponse }); }}>
                <StopIcon />
                Stop stacking
              </Button>
            )}
          </div>
          <p className="mt-3 text-sm text-fg-muted">
            Status: {status?.active === true ? "running" : "stopped"}. Capture continues independently.
          </p>
          <p className="mt-1 text-sm text-fg">
            {status?.processing_file != null
              ? `Calibrating, registering, and integrating ${status.processing_file.split("/").at(-1) ?? status.processing_file}…`
              : status?.active === true && (status.pending_files ?? 0) > 0
                ? `Waiting for ${String(status.pending_files)} file(s) to finish writing…`
                : status?.active === true
                  ? "Watching for new FITS files…"
                  : "No live-stack watcher is active."}
          </p>
          {status?.active === true && (status.pending_files ?? 0) > 0 && (
            <p className="mt-1 text-xs text-fg-muted">Queue: {String(status.pending_files)} file(s)</p>
          )}
          {status?.watch_folder != null && (
            <p className="mt-1 break-all text-xs text-fg-muted">
              Watching: {status.watch_folder}{status.watch_recursive === true ? " (including subfolders)" : ""} · FITS only
            </p>
          )}
          {status?.master_selection_message != null && (
            <p
              className={`mt-2 rounded-md border px-3 py-2 text-sm ${
                status.master_selection_message.includes("WARNING: dark temperature mismatch")
                  ? "border-warning bg-warning/10 font-medium text-warning"
                  : "border-border bg-surface-raised text-fg-muted"
              }`}
              role={status.master_selection_message.includes("WARNING: dark temperature mismatch") ? "alert" : undefined}
            >
              {status.master_selection_message}
            </p>
          )}
          {status?.last_rejection_reason != null && (
            <p className="mt-2 rounded-md border border-warning bg-warning/10 px-3 py-2 text-sm text-warning" role="status">
              Last rejected frame: {status.last_rejection_reason}
              {status.rejected_folder != null && <> Saved in {status.rejected_folder}</>}
            </p>
          )}
          {(resetStack.isError || saveStack.isError || stopStack.isError || uploadMaster.isError || status?.error != null) && (
            <div className="mt-3"><ActionableError
              message={(resetStack.error ?? saveStack.error ?? stopStack.error ?? uploadMaster.error)?.message ?? status?.error ?? "Live stack could not be updated."}
              impact="Capture continues, but new frames may not be added to the stack."
              action="Check the watch folder and calibration masters, then start a new stack."
            /></div>
          )}
          {saveOperation?.status === "failed" && (
            <p role="alert" className="mt-3 text-sm text-danger">{saveOperation.error}</p>
          )}
          {saveOperation?.status === "succeeded" && (
            <p role="status" className="mt-2 break-all text-xs text-success">
              Last stack saved to {savedOutputPath}.
            </p>
          )}
        </Card>
      </div>
      }
    />
  );
}
