// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "./client";
import { timedApiFetch } from "../performance/diagnostics";
import type { components } from "./schema";
import { acknowledgeCommand, pendingCommandKey, waitForOperation, type OperationState } from "./operations";

/** `fetch` directly, for the one section below with no route in
 * `schema.d.ts` yet — shaped like `openapi-fetch`'s own return so `unwrap`
 * needs no special-casing (same helper as `api/mount.ts` and
 * `api/capture.ts`, not shared, deliberately — see this section's header). */
async function rawCall(
  path: string,
  init?: RequestInit,
): Promise<{ data?: unknown; error?: unknown; response: Response }> {
  const response = await timedApiFetch(new Request(new URL(path, window.location.origin), { credentials: "same-origin", ...init }));
  const text = await response.text();
  let body: unknown;
  if (text.length > 0) {
    try {
      body = JSON.parse(text);
    } catch {
      body = undefined;
    }
  }
  return response.ok ? { data: body, response } : { error: body, response };
}

export interface ServerConfigurationBundle {
  schema_version: 1;
  exported_at: string;
  settings: Record<string, unknown>;
}

export interface ConfigurationImportResponse {
  schema_version: 1;
  applied_sections: string[];
  warnings: string[];
}

export async function exportServerConfiguration(): Promise<ServerConfigurationBundle> {
  return unwrap(rawCall("/api/settings/configuration/export") as Promise<{ data?: ServerConfigurationBundle; error?: unknown; response: Response }>);
}

export async function importServerConfiguration(bundle: ServerConfigurationBundle): Promise<ConfigurationImportResponse> {
  const operation = await unwrap(rawCall("/api/settings/configuration/import", {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": pendingCommandKey("configuration_import"),
    },
    body: JSON.stringify(bundle),
  }) as Promise<{ data?: OperationState; error?: unknown; response: Response }>);
  const result = await waitForOperation<ConfigurationImportResponse>(operation);
  acknowledgeCommand("configuration_import");
  return result;
}

export interface ClientConfiguration {
  entries: Record<string, string>;
}

export async function readClientConfiguration(): Promise<ClientConfiguration> {
  return unwrap(rawCall("/api/settings/client-configuration") as Promise<{ data?: ClientConfiguration; error?: unknown; response: Response }>);
}

export async function writeClientConfiguration(entries: Record<string, string>): Promise<ClientConfiguration> {
  return unwrap(rawCall("/api/settings/client-configuration", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entries }),
  }) as Promise<{ data?: ClientConfiguration; error?: unknown; response: Response }>);
}

const settingsKey = ["settings"] as const;
const externalToolsKey = ["settings", "external-tools"] as const;
const remoteAccessKey = ["settings", "remote-access"] as const;

export interface ExternalTool {
  id: string;
  name: string;
  purpose: string;
  status: "installed" | "incomplete" | "not_installed";
  detail: string;
  version: string | null;
  detected_path: string | null;
  license: string;
  distribution: "system_package" | "separate_process";
  source_url: string;
  guide_article_id: string;
  supported_platforms: string[];
}

export interface ExternalToolsResponse {
  platform: string;
  architecture: string;
  tools: ExternalTool[];
}

type ExternalToolsResult = Promise<{ data?: ExternalToolsResponse; error?: unknown; response: Response }>;

export function useExternalToolsQuery() {
  return useQuery({
    queryKey: externalToolsKey,
    queryFn: async () => unwrap(rawCall("/api/settings/external-tools") as ExternalToolsResult),
  });
}

export type RemoteAccess = components["schemas"]["RemoteAccessResponse"];

export function useRemoteAccessQuery() {
  return useQuery({
    queryKey: remoteAccessKey,
    queryFn: async () => unwrap(api.GET("/api/settings/remote-access")),
    refetchInterval: 15_000,
  });
}

type LocationSource = components["schemas"]["LocationSource"];

export function useSettingsQuery() {
  return useQuery({
    queryKey: settingsKey,
    queryFn: async () => unwrap(api.GET("/api/settings")),
  });
}

export function useRecordLocationFixMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["LocationFixRequest"]) =>
      unwrap(api.PUT("/api/settings/location/fix", { body })),
    onSuccess: (saved) => {
      queryClient.setQueryData<components["schemas"]["SettingsResponse"]>(settingsKey, (current) =>
        current === undefined ? current : { ...current, location: saved });
      void queryClient.invalidateQueries({ queryKey: settingsKey });
    },
  });
}

export function useClearLocationFixMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (source: LocationSource) =>
      unwrap(api.DELETE("/api/settings/location/fix/{source}", { params: { path: { source } } })),
    onSuccess: (saved) => {
      queryClient.setQueryData<components["schemas"]["SettingsResponse"]>(settingsKey, (current) =>
        current === undefined ? current : { ...current, location: saved });
      void queryClient.invalidateQueries({ queryKey: settingsKey });
    },
  });
}

export function useSetPreferredSourceMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (source: LocationSource | null) =>
      unwrap(api.PUT("/api/settings/location/preferred-source", { body: { source } })),
    onSuccess: (saved) => {
      queryClient.setQueryData<components["schemas"]["SettingsResponse"]>(settingsKey, (current) =>
        current === undefined ? current : { ...current, location: saved });
      void queryClient.invalidateQueries({ queryKey: settingsKey });
    },
  });
}

/** Replace the site's skyline mask. Rejections are surfaced verbatim
 * (`ApiError` already pulls the `detail` field out — see `client.ts`) rather
 * than replaced with a generic message, because the server names the fix. */
export function useSetHorizonMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["HorizonRequest"]) =>
      unwrap(api.PUT("/api/settings/horizon", { body })),
    onSuccess: (saved) => {
      queryClient.setQueryData<components["schemas"]["SettingsResponse"]>(settingsKey, (current) =>
        current === undefined ? current : { ...current, horizon: saved });
      void queryClient.invalidateQueries({ queryKey: settingsKey });
    },
  });
}

/** Clears the profile back to *unknown* — distinct from saving a flat one
 * (architecture policy invariant 7). */
export function useClearHorizonMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrap(api.DELETE("/api/settings/horizon")),
    onSuccess: (saved) => {
      queryClient.setQueryData<components["schemas"]["SettingsResponse"]>(settingsKey, (current) =>
        current === undefined ? current : { ...current, horizon: saved });
      void queryClient.invalidateQueries({ queryKey: settingsKey });
    },
  });
}

export function useSetAppearanceMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["AppearanceRequest"]) =>
      unwrap(api.PUT("/api/settings/appearance", { body })),
    onSuccess: (saved) => {
      queryClient.setQueryData<components["schemas"]["SettingsResponse"]>(settingsKey, (current) =>
        current === undefined ? current : { ...current, appearance: saved });
      void queryClient.invalidateQueries({ queryKey: settingsKey });
    },
  });
}

export interface StreamingSettings {
  broadcast_when_unparked: boolean;
  wide_camera_picture_in_picture: boolean;
  rtsp_host: string;
  rtsp_port: number;
  rtsp_path: string;
  rtsp_width: number;
  rtsp_height: number;
  rtsp_fps: 1 | 2;
  rtsp_bitrate_kbps: number;
  rtsp_update_interval_s: 5 | 10 | 30;
  rtsp_auto_crop: boolean;
  youtube_resolution: "720p" | "1080p" | "2160p";
  youtube_stream_key_set: boolean;
  active: boolean;
  url: string;
  source: "wide_camera" | "live_stack" | null;
  last_error: string | null;
}

const streamingSettingsKey = ["settings", "streaming"] as const;

export function useStreamingSettingsQuery() {
  return useQuery({
    queryKey: streamingSettingsKey,
    queryFn: async () => unwrap(rawCall("/api/settings/streaming") as Promise<{ data?: StreamingSettings; error?: unknown; response: Response }>),
    refetchInterval: 2_000,
  });
}

export function useSetStreamingSettingsMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (settings: Omit<StreamingSettings, "youtube_stream_key_set" | "active" | "url" | "source" | "last_error">) =>
      unwrap(rawCall("/api/settings/streaming", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      }) as Promise<{ data?: StreamingSettings; error?: unknown; response: Response }>),
    onSuccess: (saved) => {
      queryClient.setQueryData(streamingSettingsKey, saved);
      void queryClient.invalidateQueries({ queryKey: streamingSettingsKey });
      void queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "rtsp"] });
    },
  });
}

export function useRestartApplicationMutation() {
  return useMutation({
    mutationFn: async () =>
      unwrap(rawCall("/api/settings/restart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      }) as Promise<{ data?: { accepted: boolean }; error?: unknown; response: Response }>),
  });
}

export interface HostRebootStatus {
  available: boolean;
  reason: string;
}

export function useHostRebootStatusQuery() {
  return useQuery({
    queryKey: ["settings", "host-reboot"],
    queryFn: async () =>
      unwrap(rawCall("/api/settings/host-reboot") as Promise<{
        data?: HostRebootStatus; error?: unknown; response: Response;
      }>),
    refetchInterval: 5_000,
  });
}

export function useHostRebootMutation() {
  return useMutation({
    mutationFn: async () =>
      unwrap(rawCall("/api/settings/host-reboot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      }) as Promise<{
        data?: { accepted: boolean; previous_epoch: string }; error?: unknown; response: Response;
      }>),
  });
}

export function useHostShutdownStatusQuery() {
  return useQuery({
    queryKey: ["settings", "host-shutdown"],
    queryFn: async () =>
      unwrap(rawCall("/api/settings/host-shutdown") as Promise<{
        data?: HostRebootStatus; error?: unknown; response: Response;
      }>),
    refetchInterval: 5_000,
  });
}

export function useHostShutdownMutation() {
  return useMutation({
    mutationFn: async () =>
      unwrap(rawCall("/api/settings/host-shutdown", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      }) as Promise<{
        data?: { accepted: boolean; previous_epoch: string }; error?: unknown; response: Response;
      }>),
  });
}

// -- weather providers (DESIGN.md §6) --------------------------------------- //
//
// The two shipped providers are configured differently on purpose and these
// hooks keep that difference visible. Met.no takes a *contact*, which is sent
// in the User-Agent of every request and is therefore not a secret.
// OpenWeatherMap takes a *key*, which is, and so it goes through the ordinary
// secrets endpoint and is only ever reported back as a boolean
// (architecture policy invariant 8).

export type WeatherSettings = components["schemas"]["WeatherSettingsResponse"];
export type WeatherProviderStatus = components["schemas"]["WeatherProviderStatus"];

/** The keyring name the server reads OpenWeatherMap's key from
 * (`ssc.weather.providers.openweathermap.DEFAULT_SECRET_NAME`). */
export const OPENWEATHERMAP_SECRET_NAME = "openweathermap_api_key";
export const WEATHERAPI_SECRET_NAME = "weatherapi_api_key";
export const VISUALCROSSING_SECRET_NAME = "visualcrossing_api_key";
export const YOUTUBE_STREAM_KEY_NAME = "youtube_stream_key";

const weatherKey = ["settings", "weather"] as const;

export function useWeatherSettingsQuery() {
  return useQuery({
    queryKey: weatherKey,
    queryFn: async () => unwrap(api.GET("/api/settings/weather")),
  });
}

/** Saving also re-applies the contact to the live service, so the provider
 * status in the response is the state *after* the change — not a stale read
 * that needs a restart to catch up. */
export function useSetMetnoContactMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (contact: string | null) =>
      unwrap(api.PUT("/api/settings/weather/metno-contact", { body: { contact } })),
    onSuccess: (saved) => {
      queryClient.setQueryData(weatherKey, saved);
      void queryClient.invalidateQueries({ queryKey: weatherKey });
      void queryClient.invalidateQueries({ queryKey: settingsKey });
    },
  });
}

export function useSetWeatherProviderMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (provider: components["schemas"]["WeatherProviderRequest"]["provider"]) =>
      unwrap(api.PUT("/api/settings/weather/provider", { body: { provider } })),
    onSuccess: (saved) => {
      queryClient.setQueryData(weatherKey, saved);
      void queryClient.invalidateQueries({ queryKey: weatherKey });
      void queryClient.invalidateQueries({ queryKey: settingsKey });
    },
  });
}

export function useSetSecretMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ name, value }: { name: string; value: string }) =>
      unwrap(api.PUT("/api/settings/secrets", { body: { name, value } })),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: weatherKey });
      void queryClient.invalidateQueries({ queryKey: settingsKey });
      void queryClient.invalidateQueries({ queryKey: streamingSettingsKey });
      void queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "youtube"] });
    },
  });
}

export function useDeleteSecretMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) =>
      unwrap(api.DELETE("/api/settings/secrets/{name}", { params: { path: { name } } })),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: weatherKey });
      void queryClient.invalidateQueries({ queryKey: settingsKey });
      void queryClient.invalidateQueries({ queryKey: streamingSettingsKey });
      void queryClient.invalidateQueries({ queryKey: ["capture", "live-stack", "youtube"] });
    },
  });
}

// -- click-through object info (ssc/objectinfo, M11) ------------------------- //
//
// One on/off switch, off by default (THIRD_PARTY_NOTICES.md's shipped-disabled
// recommendation) — no BYO endpoint field, by design: the backend only ever
// has this one toggle, so there is nothing else here to configure.

const objectInfoKey = ["settings", "object-info"] as const;

export function useObjectInfoSettingsQuery() {
  return useQuery({
    queryKey: objectInfoKey,
    queryFn: async () => unwrap(api.GET("/api/settings/object-info")),
  });
}

export function useSetObjectInfoEnabledMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (enabled: boolean) => unwrap(api.PUT("/api/settings/object-info", { body: { enabled } })),
    onSuccess: (saved) => {
      queryClient.setQueryData(objectInfoKey, saved);
      void queryClient.invalidateQueries({ queryKey: objectInfoKey });
    },
  });
}

// -- cutout autofetch on target resolve (ssc/skymap) ------------------------- //
//
// This setting is deliberately source-specific:
// `ssc/settings/models.py`'s `SkymapSettings`, `ssc/api/routes/settings.py`'s
// `/skymap-autofetch`, and `ssc/api/routes/astro.py`'s `resolve_target` use
// `SkymapSettings.cutout_autofetch_enabled`, and turning it on makes a
// resolved target call `CutoutService.schedule_autofetch`
// (`ssc/skymap/cutouts.py`) — which fetches **unWISE only**
// (`AUTOFETCH_SOURCE_ID = CUTOUT_REGISTRY[0].id.value`, i.e. `"unwise"`).
// The method has no `source_id` parameter, so a future caller cannot
// accidentally widen the automatic network request to another source.
//
// `schema.d.ts` has no `/api/settings/skymap-autofetch` route yet (checked
// 2026-09-10, codegen not rerun against the uncommitted backend change) —
// path and field names below are read directly from
// `SkymapAutofetchSettingsResponse`/`SkymapAutofetchEnabledRequest`
// (`ssc/api/schemas.py`) rather than guessed, but this is still the same
// deliberate, temporary hand-written exception `api/mount.ts` and
// `api/capture.ts` document. TODO once codegen is rerun: replace with a
// `components["schemas"][...]` alias.
//
// **Off by default is not this file's decision to relax** — every read
// below trusts the server's own value rather than assuming it, per the
// brief's verification requirement 3.
export interface SkymapAutofetchSettings {
  cutout_autofetch_enabled: boolean;
}

const skymapAutofetchKey = ["settings", "skymap-autofetch"] as const;

type SkymapAutofetchResult = Promise<{ data?: SkymapAutofetchSettings; error?: unknown; response: Response }>;

export function useSkymapAutofetchSettingsQuery() {
  return useQuery({
    queryKey: skymapAutofetchKey,
    // `rawCall`'s `data` is `unknown` — this section's own header already
    // flags this shape as unconfirmed against a generated type, so the cast
    // happens once here, at this one call site.
    queryFn: async () => unwrap(rawCall("/api/settings/skymap-autofetch") as SkymapAutofetchResult),
  });
}

export function useSetSkymapAutofetchEnabledMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (cutout_autofetch_enabled: boolean) =>
      unwrap(
        rawCall("/api/settings/skymap-autofetch", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cutout_autofetch_enabled }),
        }) as SkymapAutofetchResult,
      ),
    onSuccess: (saved) => {
      queryClient.setQueryData(skymapAutofetchKey, saved);
      void queryClient.invalidateQueries({ queryKey: skymapAutofetchKey });
    },
  });
}

// -- capture folder + path template (ssc/settings/capture_folder.py, --------- //
// -- ssc/capture/pathtemplate.py, DESIGN.md §9.5.2) --------------------------- //
//
// `GET`/`PUT /api/settings/capture-folder` landed 2026-09-10 (`912fbac`).
// The path template is the same `backend-core` agent's *parallel,
// uncommitted* work — but by the time this section was finished, enough of
// it had landed in this same working copy to cross-check against, rather
// than build blind from the brief's own paraphrase (`api/capture.ts`'s own
// method for `frame_type`). Confirmed against `ssc/api/schemas.py` on disk:
//
// - `CaptureFolderResponse` gained `default_root: str | null` — "a
//   *suggestion* only … never what `folder` silently defaults to" (its own
//   docstring, and `ssc.settings.capture_folder.default_capture_root`'s:
//   architecture policy invariant 11's storage sibling rule). This panel shows it as a
//   suggestion the user can apply, never as a prefilled value.
// - the path template is **not** a field on `CaptureFolderRequest`/
//   `CaptureFolderResponse` at all — the brief's "alongside it" reads
//   naturally as one endpoint, but the actual schema is a wholly separate
//   pair, `CaptureFolderTemplateRequest`/`CaptureFolderTemplateResponse`
//   (`template: str`), so this section saves folder and template
//   independently rather than in one combined PUT.
// - `CapturePathTagModel` (`name`/`description`/`example`, all `str`) matches
//   this section's `CapturePathTag` exactly. `name` mirrors
//   `ssc.capture.pathtemplate.PathTag.name` verbatim, which is the **bare**
//   token (`"TARGET"`), not the `%TARGET%` form used inside a template —
//   confirmed by reading that dataclass on disk. Substitution and display
//   below wrap it in `%…%` rather than assuming either convention.
//
// **Still not verifiable end to end**, and noted as such rather than
// guessed: `ssc/api/routes/settings.py` only wires `/capture-folder` so far
// (checked 2026-09-10) — no route yet for the template or for
// `GET /api/settings/capture/path-tags` (the exact path the brief itself
// pins, kept verbatim below). The template endpoint's path is not in the
// routes file at all yet; `/api/settings/capture-folder-template` below
// follows this file's own kebab-case-of-the-schema-name convention
// (`capture-folder` ↔ `CaptureFolderRequest`, `skymap-autofetch` ↔
// `SkymapAutofetchEnabledRequest`) but is an inference, not a read value —
// flagged here rather than left implicit, and worth re-checking once
// `backend-core`'s change lands.
//
// `schema.d.ts` has none of these routes yet — same hand-written, temporary
// exception as the skymap-autofetch section above, via the same `rawCall`.

/** `CaptureFolderRequest`/`CaptureFolderResponse`'s shape. `folder: null`
 * means unset, matching `CaptureFolderRequest`'s own "clears the configured
 * folder back to unset" doc comment. `default_root` is read-only — there is
 * no field for it on the request, only the response. */
export interface CaptureFolderSettings {
  folder: string | null;
  default_root: string | null;
}

/** `CaptureFolderTemplateRequest`/`CaptureFolderTemplateResponse`'s shape —
 * a separate endpoint from the folder itself, see this section's header. */
export interface CaptureFolderTemplateSettings {
  template: string;
}

export interface CaptureFilenameTemplateSettings {
  template: string;
}

export interface CalibrationPathTemplates {
  bias_folder_template: string;
  dark_folder_template: string;
  flat_folder_template: string;
  filename_template: string;
}

/** One entry from `GET /api/settings/capture/path-tags`
 * (`CapturePathTagModel`) — `name` is the **bare** token
 * (`ssc.capture.pathtemplate.PathTag.name`, e.g. `"TARGET"`), not the
 * `%TARGET%` form a template actually uses; callers wrap it themselves. */
export interface CapturePathTag {
  name: string;
  description: string;
  example: string;
}

export interface CapturePathTagsResponse {
  tags: CapturePathTag[];
}

const captureFolderKey = ["settings", "capture-folder"] as const;
const captureFolderTemplateKey = ["settings", "capture-folder-template"] as const;
const captureFilenameTemplateKey = ["settings", "capture-filename-template"] as const;
const calibrationPathTemplatesKey = ["settings", "calibration-path-templates"] as const;
const captureRejectedFramesKey = ["settings", "capture-rejected-frames"] as const;
const capturePathTagsKey = ["settings", "capture-path-tags"] as const;
export interface CaptureDitherSettings {
  enabled: boolean;
  every_n_frames: number;
  min_pixels: number;
  max_pixels: number;
}

export interface CaptureRejectedFramesSettings {
  keep_rejected_frames: boolean;
}

export function useCaptureRejectedFramesSettingsQuery() {
  return useQuery({
    queryKey: captureRejectedFramesKey,
    queryFn: async () => unwrap(rawCall("/api/settings/capture-rejected-frames") as Promise<{ data?: CaptureRejectedFramesSettings; error?: unknown; response: Response }>),
  });
}

export function useSetCaptureRejectedFramesMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: CaptureRejectedFramesSettings) =>
      unwrap(rawCall("/api/settings/capture-rejected-frames", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) as Promise<{ data?: CaptureRejectedFramesSettings; error?: unknown; response: Response }>),
    onSuccess: (saved) => {
      queryClient.setQueryData(captureRejectedFramesKey, saved); void queryClient.invalidateQueries({ queryKey: captureRejectedFramesKey }); },
  });
}
const captureDitherKey = ["settings", "capture-dither"] as const;

export function useCaptureDitherSettingsQuery() {
  return useQuery({
    queryKey: captureDitherKey,
    queryFn: async () => unwrap(rawCall("/api/settings/capture-dither") as Promise<{ data?: CaptureDitherSettings; error?: unknown; response: Response }>),
  });
}

export function useSetCaptureDitherMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: CaptureDitherSettings) =>
      unwrap(rawCall("/api/settings/capture-dither", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) as Promise<{ data?: CaptureDitherSettings; error?: unknown; response: Response }>),
    onSuccess: (saved) => {
      queryClient.setQueryData(captureDitherKey, saved); void queryClient.invalidateQueries({ queryKey: captureDitherKey }); },
  });
}

export interface CaptureCenterAfterDriftSettings {
  enabled: boolean;
  threshold_pixels: number;
}

const captureCenterAfterDriftKey = ["settings", "capture-center-after-drift"] as const;

export function useCaptureCenterAfterDriftSettingsQuery() {
  return useQuery({
    queryKey: captureCenterAfterDriftKey,
    queryFn: async () => unwrap(rawCall("/api/settings/capture-center-after-drift") as Promise<{ data?: CaptureCenterAfterDriftSettings; error?: unknown; response: Response }>),
  });
}

export function useSetCaptureCenterAfterDriftMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: CaptureCenterAfterDriftSettings) =>
      unwrap(rawCall("/api/settings/capture-center-after-drift", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) as Promise<{ data?: CaptureCenterAfterDriftSettings; error?: unknown; response: Response }>),
    onSuccess: (saved) => {
      queryClient.setQueryData(captureCenterAfterDriftKey, saved);
      void queryClient.invalidateQueries({ queryKey: captureCenterAfterDriftKey });
    },
  });
}

export interface AutoLpFilterSettings { enabled: boolean }
const autoLpFilterKey = ["settings", "auto-lp-filter"] as const;

export function useAutoLpFilterSettingsQuery() {
  return useQuery({
    queryKey: autoLpFilterKey,
    queryFn: async () => unwrap(rawCall("/api/settings/auto-lp-filter") as Promise<{ data?: AutoLpFilterSettings; error?: unknown; response: Response }>),
  });
}

export function useSetAutoLpFilterMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: AutoLpFilterSettings) =>
      unwrap(rawCall("/api/settings/auto-lp-filter", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) as Promise<{ data?: AutoLpFilterSettings; error?: unknown; response: Response }>),
    onSuccess: (saved) => {
      queryClient.setQueryData(autoLpFilterKey, saved); void queryClient.invalidateQueries({ queryKey: autoLpFilterKey }); },
  });
}

type CaptureFolderResult = Promise<{ data?: CaptureFolderSettings; error?: unknown; response: Response }>;
type CaptureFolderTemplateResult = Promise<{
  data?: CaptureFolderTemplateSettings;
  error?: unknown;
  response: Response;
}>;
type CaptureFilenameTemplateResult = Promise<{
  data?: CaptureFilenameTemplateSettings;
  error?: unknown;
  response: Response;
}>;
type CapturePathTagsResult = Promise<{ data?: CapturePathTagsResponse; error?: unknown; response: Response }>;

export function useCaptureFolderSettingsQuery() {
  return useQuery({
    queryKey: captureFolderKey,
    queryFn: async () => unwrap(rawCall("/api/settings/capture-folder") as CaptureFolderResult),
  });
}

/** A bad folder (does not exist, not a directory, not writable, too little
 * free space) is refused (422) naming the problem — that rejection reaches
 * the caller as `ApiError.message` verbatim via `unwrap`, the same as every
 * other settings mutation in this file. */
export function useSetCaptureFolderMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (folder: string | null) =>
      unwrap(
        rawCall("/api/settings/capture-folder", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ folder }),
        }) as CaptureFolderResult,
      ),
    onSuccess: (saved) => {
      queryClient.setQueryData(captureFolderKey, saved);
      void queryClient.invalidateQueries({ queryKey: captureFolderKey });
    },
  });
}

export function useCaptureFolderTemplateQuery() {
  return useQuery({
    queryKey: captureFolderTemplateKey,
    queryFn: async () =>
      unwrap(rawCall("/api/settings/capture-folder-template") as CaptureFolderTemplateResult),
  });
}

/** The server refuses a bad template at save time — unknown `%PLACEHOLDER%`,
 * a `..` escape, or one that renders to an absolute path — naming the
 * problem (`ssc.capture.pathtemplate.validate_capture_folder_template`);
 * shown verbatim, same as every other settings mutation in this file. */
export function useSetCaptureFolderTemplateMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (template: string) =>
      unwrap(
        rawCall("/api/settings/capture-folder-template", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ template }),
        }) as CaptureFolderTemplateResult,
      ),
    onSuccess: (saved) => {
      queryClient.setQueryData(captureFolderTemplateKey, saved);
      void queryClient.invalidateQueries({ queryKey: captureFolderTemplateKey });
    },
  });
}

export function useCaptureFilenameTemplateQuery() {
  return useQuery({
    queryKey: captureFilenameTemplateKey,
    queryFn: async () =>
      unwrap(rawCall("/api/settings/capture-filename-template") as CaptureFilenameTemplateResult),
  });
}

export function useSetCaptureFilenameTemplateMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (template: string) =>
      unwrap(
        rawCall("/api/settings/capture-filename-template", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ template }),
        }) as CaptureFilenameTemplateResult,
      ),
    onSuccess: (saved) => {
      queryClient.setQueryData(captureFilenameTemplateKey, saved);
      void queryClient.invalidateQueries({ queryKey: captureFilenameTemplateKey });
    },
  });
}

export function useCalibrationPathTemplatesQuery() {
  return useQuery({
    queryKey: calibrationPathTemplatesKey,
    queryFn: async () =>
      unwrap(rawCall("/api/settings/calibration-path-templates") as Promise<{
        data?: CalibrationPathTemplates;
        error?: unknown;
        response: Response;
      }>),
  });
}

export function useSetCalibrationPathTemplatesMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: CalibrationPathTemplates) =>
      unwrap(rawCall("/api/settings/calibration-path-templates", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }) as Promise<{ data?: CalibrationPathTemplates; error?: unknown; response: Response }>),
    onSuccess: (saved) => {
      queryClient.setQueryData(calibrationPathTemplatesKey, saved);
      void queryClient.invalidateQueries({ queryKey: calibrationPathTemplatesKey });
    },
  });
}

/** A fixed vocabulary generated from `FrameMetadata`, not live capture
 * state — still an ordinary query (no `staleTime` override) so a header
 * field added on the backend shows up on this panel's next visit without
 * this file needing to know that happened. */
export function useCapturePathTagsQuery() {
  return useQuery({
    queryKey: capturePathTagsKey,
    queryFn: async () => unwrap(rawCall("/api/settings/capture/path-tags") as CapturePathTagsResult),
  });
}
