// SPDX-License-Identifier: GPL-3.0-only

import { useState } from "react";
import {
  YOUTUBE_STREAM_KEY_NAME,
  useDeleteSecretMutation,
  useSetSecretMutation,
  useSetStreamingSettingsMutation,
  useStreamingSettingsQuery,
  type StreamingSettings,
} from "../../api/settings";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { Field } from "../../components/Field";

type StoredBroadcastSettings = Omit<
  StreamingSettings,
  "youtube_stream_key_set" | "active" | "url" | "source" | "last_error"
>;
type BroadcastDraft = Omit<StoredBroadcastSettings, "rtsp_port" | "rtsp_bitrate_kbps"> & {
  rtsp_port: string;
  rtsp_bitrate_kbps: string;
};

function makeDraft(settings: StreamingSettings): BroadcastDraft {
  return {
    broadcast_when_unparked: settings.broadcast_when_unparked,
    wide_camera_picture_in_picture: settings.wide_camera_picture_in_picture,
    rtsp_host: settings.rtsp_host,
    rtsp_port: String(settings.rtsp_port),
    rtsp_path: settings.rtsp_path,
    rtsp_width: settings.rtsp_width,
    rtsp_height: settings.rtsp_height,
    rtsp_fps: settings.rtsp_fps,
    rtsp_bitrate_kbps: String(settings.rtsp_bitrate_kbps),
    rtsp_update_interval_s: settings.rtsp_update_interval_s,
    rtsp_auto_crop: settings.rtsp_auto_crop,
    youtube_resolution: settings.youtube_resolution,
  };
}

export function StreamingSettingsPanel() {
  const settings = useStreamingSettingsQuery();
  const save = useSetStreamingSettingsMutation();
  const setSecret = useSetSecretMutation();
  const deleteSecret = useDeleteSecretMutation();
  const [youtubeKey, setYoutubeKey] = useState("");
  const [draftOverride, setDraftOverride] = useState<BroadcastDraft | null>(null);

  if (settings.isPending) {
    return <p className="text-sm text-fg-muted">Loading broadcast settings…</p>;
  }
  if (settings.data === undefined) {
    return <p role="alert" className="text-sm text-danger">Broadcast settings are unavailable.</p>;
  }
  const draft = draftOverride ?? makeDraft(settings.data);

  const url = settings.data.url.replace("HOST", window.location.hostname);
  const source = settings.data.source === "live_stack"
    ? "live stack"
    : settings.data.source === "wide_camera"
      ? "Seestar S30 Wide Camera"
      : "none";
  const resolution = `${String(draft.rtsp_width)}x${String(draft.rtsp_height)}`;
  const update = (changes: Partial<BroadcastDraft>) => { setDraftOverride({ ...draft, ...changes }); };

  return (
    <Card>
      {settings.isError && <p role="alert" className="mb-3 text-sm text-danger">Could not refresh settings. Showing the last saved values; your edits are retained.</p>}
      <CardTitle>RTSP broadcast</CardTitle>
      <p className="mb-3 text-sm text-fg-muted">
        Configure the server-owned stream used by VLC, OBS, the Streaming screen, and YouTube Live.
      </p>
      <a href={url} className="break-all font-mono text-sm text-accent underline">{url}</a>
      <p className="mt-2 text-xs text-fg-muted">
        Status: {settings.data.active ? `streaming ${source}` : "off"}
      </p>

      <div className="mt-4 grid gap-3">
        <div className="grid grid-cols-2 gap-2">
          <Field label="Bind address" value={draft.rtsp_host} onChange={(event) => { update({ rtsp_host: event.target.value }); }} />
          <Field label="RTSP port" value={draft.rtsp_port} onChange={(event) => { update({ rtsp_port: event.target.value }); }} />
        </div>
        <Field label="Stream path" value={draft.rtsp_path} onChange={(event) => { update({ rtsp_path: event.target.value }); }} />
        <label className="grid gap-1 text-sm text-fg">
          Resolution
          <select
            className="rounded-md border border-border bg-bg px-3 py-2"
            value={resolution}
            onChange={(event) => {
              const [width, height] = event.target.value.split("x").map(Number);
              const bitrate = width === 3840 ? "12000" : width === 1920 ? "6000" : "2500";
              update({
                rtsp_width: width ?? 1280,
                rtsp_height: height ?? 720,
                rtsp_bitrate_kbps: bitrate,
              });
            }}
          >
            <option value="1280x720">1280×720 (recommended)</option>
            <option value="1920x1080">1920×1080</option>
            <option value="3840x2160">3840×2160</option>
          </select>
        </label>
        <div className="grid grid-cols-2 gap-2">
          <label className="grid gap-1 text-sm text-fg">
            Output rate
            <select className="rounded-md border border-border bg-bg px-3 py-2" value={draft.rtsp_fps} onChange={(event) => { update({ rtsp_fps: event.target.value === "2" ? 2 : 1 }); }}>
              <option value={1}>1 fps</option>
              <option value={2}>2 fps</option>
            </select>
          </label>
          <Field label="Bitrate (kbps)" value={draft.rtsp_bitrate_kbps} onChange={(event) => { update({ rtsp_bitrate_kbps: event.target.value }); }} />
        </div>
        <label className="grid gap-1 text-sm text-fg">
          Overlay refresh
          <select className="rounded-md border border-border bg-bg px-3 py-2" value={draft.rtsp_update_interval_s} onChange={(event) => {
            const value = Number(event.target.value);
            update({ rtsp_update_interval_s: value === 30 ? 30 : value === 10 ? 10 : 5 });
          }}>
            <option value={5}>Every 5 seconds</option>
            <option value={10}>Every 10 seconds</option>
            <option value={30}>Every 30 seconds</option>
          </select>
        </label>
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm text-fg">
          <input type="checkbox" checked={draft.rtsp_auto_crop} onChange={(event) => { update({ rtsp_auto_crop: event.target.checked }); }} />
          Auto-crop dithering edges in the broadcast
        </label>
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm text-fg">
          <input type="checkbox" checked={draft.broadcast_when_unparked} onChange={(event) => { update({ broadcast_when_unparked: event.target.checked }); }} />
          Automatically broadcast while the telescope is unparked
        </label>
        <p className="-mt-2 text-xs text-fg-muted">
          Starts with the Seestar S30 Wide Camera, switches to the first accepted live-stack image,
          and returns to the wide camera when stacking stops.
        </p>
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm text-fg">
          <input type="checkbox" checked={draft.wide_camera_picture_in_picture} onChange={(event) => { update({ wide_camera_picture_in_picture: event.target.checked }); }} />
          Show Seestar S30 Wide Camera as picture-in-picture on the live stack
        </label>
        <Button
          variant="primary"
          disabled={save.isPending || draft.rtsp_host.trim() === "" || draft.rtsp_path.trim() === ""}
          onClick={() => {
            save.mutate(
              {
                ...draft,
                rtsp_host: draft.rtsp_host.trim(),
                rtsp_path: draft.rtsp_path.trim(),
                rtsp_port: Number(draft.rtsp_port),
                rtsp_bitrate_kbps: Number(draft.rtsp_bitrate_kbps),
              },
              { onSuccess: () => { setDraftOverride((current) => current === draft ? null : current); } },
            );
          }}
        >
          {save.isPending ? "Saving…" : "Save broadcast settings"}
        </Button>
        {settings.data.active && (
          <p className="text-xs text-fg-muted">
            Source and picture-in-picture changes apply immediately. Address, resolution,
            frame-rate, bitrate, and refresh changes apply the next time the broadcast starts.
            If YouTube is active, Photon Commander keeps its RTSP source running until you stop YouTube.
          </p>
        )}
      </div>

      <div className="mt-5 border-t border-border pt-4">
        <p className="mb-3 text-sm font-medium text-fg">YouTube Live</p>
        <label className="mb-3 grid gap-1 text-sm text-fg">
          YouTube quality
          <select
            className="rounded-md border border-border bg-bg px-3 py-2"
            value={draft.youtube_resolution}
            onChange={(event) => {
              update({ youtube_resolution: event.target.value as BroadcastDraft["youtube_resolution"] });
            }}
          >
            <option value="720p">720p · 4 Mbps (recommended)</option>
            <option value="1080p">1080p · 10 Mbps</option>
            <option value="2160p">4K · 30 Mbps</option>
          </select>
          <span className="text-xs text-fg-muted">
            Applied the next time YouTube starts. 4K needs substantially more CPU and upload capacity.
          </span>
        </label>
        <div className="flex flex-wrap items-end gap-2">
          <Field
            label="YouTube stream key"
            wrapperClassName="min-w-[16rem] flex-1"
            type="password"
            autoComplete="off"
            placeholder={settings.data.youtube_stream_key_set ? "Stored — enter a new key to replace" : "Paste from YouTube Studio"}
            value={youtubeKey}
            onChange={(event) => { setYoutubeKey(event.target.value); }}
            hint="Stored in this machine's keyring and never returned to the browser."
          />
          <Button
            variant="primary"
            disabled={setSecret.isPending || youtubeKey.trim() === ""}
            onClick={() => {
              setSecret.mutate(
                { name: YOUTUBE_STREAM_KEY_NAME, value: youtubeKey.trim() },
                { onSuccess: () => { setYoutubeKey(""); } },
              );
            }}
          >
            {setSecret.isPending ? "Saving…" : "Save key"}
          </Button>
          {settings.data.youtube_stream_key_set && (
            <Button variant="secondary" disabled={deleteSecret.isPending} onClick={() => { deleteSecret.mutate(YOUTUBE_STREAM_KEY_NAME); }}>
              Remove key
            </Button>
          )}
        </div>
        <p className="mt-2 text-xs text-warning">
          Before starting from Photon Commander, configure this reusable broadcast as Unlisted in YouTube Studio. A stream key does not let Photon Commander set or verify its privacy.
        </p>
      </div>
      {(save.isError || setSecret.isError || deleteSecret.isError || settings.data.last_error !== null) && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {save.error instanceof Error
            ? save.error.message
            : setSecret.error?.message ?? deleteSecret.error?.message ?? settings.data.last_error}
        </p>
      )}
    </Card>
  );
}
