// SPDX-License-Identifier: GPL-3.0-only

import { ImagePreview } from "../../components/ImagePreview";
import {
  useLiveStackRtspQuery,
  useRestartLiveStackRtspMutation,
  useStartYoutubeStreamMutation,
  useStopYoutubeStreamMutation,
  useYoutubeStreamQuery,
} from "../../api/capture";
import { Button } from "../../components/Button";
import { RestartStreamingIcon, StartYoutubeIcon, StopYoutubeIcon } from "../../components/ActionIcons";
import { Card, CardTitle } from "../../components/Card";
import { useStreamingSettingsQuery } from "../../api/settings";
import { PaneLayout } from "../../components/PaneLayout";

export function StreamingTab() {
  const status = useLiveStackRtspQuery();
  const youtube = useYoutubeStreamQuery();
  const startYoutube = useStartYoutubeStreamMutation();
  const stopYoutube = useStopYoutubeStreamMutation();
  const settings = useStreamingSettingsQuery();
  const restart = useRestartLiveStackRtspMutation();
  const stream = status.data;
  const configuredUrl = settings.data?.url ?? null;
  const url = (stream?.url ?? configuredUrl)?.replace("HOST", window.location.hostname) ?? null;
  const source = stream?.source === "live_stack"
    ? "Live stack"
    : stream?.source === "wide_camera"
      ? "Seestar S30 Wide Camera"
      : "Waiting";

  return (
    <PaneLayout
      fill
      secondary={
      <Card className="mobile-preview-card flex min-h-0 flex-col lg:h-full">
        <div className="mb-3 flex items-center justify-between gap-3">
          <CardTitle>Stream preview</CardTitle>
          <span className="text-xs text-fg-muted">{source}</span>
        </div>
        {stream?.active === true && stream.encoded_version != null ? (
          <ImagePreview label="Stream preview viewport" src={`/api/capture/live-stack/rtsp/preview?v=${encodeURIComponent(stream.encoded_version)}`} alt={`RTSP stream preview: ${source}`} />
        ) : (
          <div className="flex min-h-64 items-center justify-center rounded-md border border-border bg-surface-raised p-4 text-center text-sm text-fg-muted">
            {stream?.active === true
              ? "The first stream frame is being encoded…"
              : "Broadcasting from the wide camera will start automatically once the Seestar S30 Pro arm is open."}
          </div>
        )}
      </Card>
      }
      detail={
      <Card>
        <CardTitle>Broadcast</CardTitle>
        {url !== null && <a href={url} className="break-all font-mono text-sm text-accent underline">{url}</a>}
        <p className="mt-3 text-sm text-fg-muted">
          {stream?.active === true
            ? `${source} · ${String(stream.clients)} RTSP client connection(s), including the YouTube relay`
            : "Broadcast is off. Enable automatic broadcasting in Settings and unpark the telescope."}
        </p>
        <div className="mt-3">
          <Button
            variant="secondary"
            disabled={stream?.active !== true || restart.isPending || youtube.data?.active === true}
            onClick={() => { restart.mutate(); }}
          >
            <RestartStreamingIcon />
            {restart.isPending ? "Restarting…" : "Restart streaming"}
          </Button>
          {youtube.data?.active === true && (
            <p className="mt-1 text-xs text-fg-muted">Stop YouTube before restarting its source.</p>
          )}
        </div>
        {(status.isError || stream?.last_error != null) && (
          <p role="alert" className="mt-3 text-sm text-danger">
            {stream?.last_error ?? "Stream status is unavailable."}
          </p>
        )}
        {restart.isError && (
          <p role="alert" className="mt-3 text-sm text-danger">
            {restart.error.message}
          </p>
        )}
        <div className="mt-6 border-t border-border pt-4">
          <p className="text-sm font-medium text-fg">YouTube Live</p>
          <p className="mt-1 text-xs text-fg-muted">
            Publishes this Photon Commander picture as {settings.data?.youtube_resolution === "2160p" ? "4K at 30 Mbps" : settings.data?.youtube_resolution === "1080p" ? "1080p at 10 Mbps" : "720p at 4 Mbps"} H.264 at 30 fps, with silent
            AAC audio, to the reusable broadcast configured in YouTube Studio. No microphone or
            telescope sound is captured.
          </p>
          <p className="mt-2 text-xs text-warning">
            Confirm that the broadcast is Unlisted in YouTube Studio before starting. Photon Commander cannot set privacy through a stream key.
          </p>
          <div className="mt-3">
            {youtube.data?.active === true ? (
              <Button
                disabled={stopYoutube.isPending}
                onClick={() => { stopYoutube.mutate(); }}
              >
                <StopYoutubeIcon />
                {stopYoutube.isPending ? "Stopping…" : "Stop YouTube stream"}
              </Button>
            ) : (
              <Button
                variant="primary"
                disabled={
                  startYoutube.isPending ||
                  stream?.active !== true ||
                  youtube.data?.configured !== true
                }
                onClick={() => { startYoutube.mutate(); }}
              >
                <StartYoutubeIcon />
                {startYoutube.isPending ? "Starting…" : "Start YouTube stream"}
              </Button>
            )}
          </div>
          {youtube.data?.active === true && <p role="status" className="mt-2 text-sm text-fg-muted">
            {youtube.data.publishing
              ? "Video frames are being sent. Confirm the preview in YouTube Studio."
              : "Waiting for outgoing video frames. Check the source and YouTube connection."}
          </p>}
          {youtube.data?.configured !== true && (
            <p className="mt-2 text-xs text-fg-muted">Save the stream key in Settings → Broadcasting first.</p>
          )}
          {stream?.active !== true && youtube.data?.active !== true && (
            <p className="mt-2 text-xs text-fg-muted">Start the Photon Commander broadcast first.</p>
          )}
          {(youtube.isError || startYoutube.isError || stopYoutube.isError || youtube.data?.last_error != null) && (
            <p role="alert" className="mt-3 text-sm text-danger">
              {startYoutube.error?.message ?? stopYoutube.error?.message ?? youtube.data?.last_error ?? "YouTube status is unavailable."}
            </p>
          )}
        </div>
      </Card>
      }
    />
  );
}
