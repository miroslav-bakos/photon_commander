// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useQuery } from "@tanstack/react-query";
import { timedApiFetch } from "../performance/diagnostics";
import { translate, useLanguage } from "../i18n/locale";
import { useEffect, useState } from "react";
import { Battery, PictureInPicture2 } from "lucide-react";
import { useConnectionStore } from "../store/connectionStore";
import type { DevicesState } from "../api/types";
import type { WsConnectionStatus } from "../ws/client";
import { useMountCenterProgressQuery, useMountState, type MountState } from "../api/mount";
import { useCaptureState, useLiveStackQuery, useLiveStackRtspQuery, type CaptureStage, type CaptureState } from "../api/capture";
import { usePolarAlignmentState, type PolarAlignmentState } from "../api/polarAlignment";
import {
  useFilterWheelQuery,
  type FilterWheelState,
} from "../api/filterwheel";
import { useAutofocusStateQuery, useFocuserPositionQuery, type FocuserPositionResponse } from "../api/focuser";
import { useHeaterQuery, type HeaterState } from "../api/heater";
import { deviceNotesSayDisconnected } from "../api/deviceStatus";
import { useNow } from "../tabs/MountTab/useNow";
import { useLayoutStore } from "../layout/layoutStore";
import { useLayoutRoute } from "../layout/LayoutContext";
import { activityProgress, formatActivityCount, selectFooterActivities } from "./footerActivity";
import { useStreamPipStore } from "../store/streamPipStore";
import { FullscreenToggle } from "./FullscreenToggle";

/**
 * Always-visible, icon-first observatory status strip. It reports operational
 * state, not a second inventory of every Alpaca endpoint: the mount/link
 * indicator already answers whether the appliance is connected. Each
 * remaining glyph has a stable meaning, a full accessible name and a desktop
 * tooltip. Visible prose is deliberately reserved for transient notices.
 *
 * **Lightness carries state, not hue** — the same fix `TabNav.tsx`
 * (`77be897`) made for the Mount tab, for the same reason: the red-light
 * theme is red-only, and there `--ssc-success` (#b23d3d) is *darker* than
 * `--ssc-fg-muted` (#c85050), so a green-for-connected scheme would render
 * "connected" as *less* prominent than "disconnected" in exactly the theme
 * this matters most. Every dot here is one of three brightness tiers
 * (`text-fg` / `text-fg-muted` / `text-fg-disabled`) that mean the same
 * thing in both themes; nothing is ever the sole carrier — each entry also
 * has an accessible name and a `title` saying the state in words. This is
 * also a fix, not just a move: the old `ConnectionBadge` picked
 * `bg-success`/`bg-warning` by hue, which had exactly the bug above in
 * red-light mode (warning #d94f4f reads *brighter* than success #b23d3d) —
 * never actually visible because nothing before this compared the two
 * tokens' lightness in that theme.
 *
 * On phones the strip is the last non-shrinking flex row in the shell's
 * bottom dock. Navigation below it owns the home-indicator inset.
 * Only main scrolls, so status remains visible without Safari's fixed-bottom
 * painting heuristic. The desktop footer uses the same content in normal flow.
 *
 * Activity glyphs are driven only by authoritative state: mount telemetry,
 * capture pipeline stages and polar-alignment stages. In particular,
 * settling is shown only when PA reports it; it is never guessed from a
 * client-side timer.
 */
export function ConnectionsFooter({ docked = false }: { docked?: boolean }) {
  const language = useLanguage();
  const build = useQuery({ queryKey: ["running-build-version"], staleTime: 60_000,
    queryFn: async (): Promise<{ version?: string | null }> => {
      const response = await timedApiFetch(new Request(new URL("/api/health", window.location.origin)));
      if (!response.ok) throw new Error("Version unavailable");
      return response.json() as Promise<{ version?: string | null }>;
    },
  });
  const now = useNow(1000);
  const status = useConnectionStore((s) => s.status);
  const lastResync = useConnectionStore((s) => s.lastResync);
  const devices = useConnectionStore((s): DevicesState | null => s.snapshot?.devices ?? null);
  const snapshot = useConnectionStore((s) => s.snapshot);
  const mount = useMountState();
  const capture = useCaptureState();
  const polarAlignment = usePolarAlignmentState();
  const filterWheel = useFilterWheelQuery();
  const focuser = useFocuserPositionQuery();
  const autofocus = useAutofocusStateQuery();
  const centering = useMountCenterProgressQuery(true);
  const liveStack = useLiveStackQuery(true);
  const stream = useLiveStackRtspQuery();
  const heater = useHeaterQuery();
  const layoutRoute = useLayoutRoute();
  const layoutUnlocked = useLayoutStore((state) => state.unlocked);
  const setLayoutUnlocked = useLayoutStore((state) => state.setUnlocked);
  const resetTabLayout = useLayoutStore((state) => state.resetTab);
  const pipEnabled = useStreamPipStore((state) => state.enabled);
  const setPipEnabled = useStreamPipStore((state) => state.setEnabled);
  const pipAvailable = stream.data?.active === true && stream.data.encoded_version != null;

  // Ported unchanged from `ConnectionBadge`: auto-hide the resync notice 8s
  // after it lands. `dismissedAt` only ever changes inside the timeout
  // below, never synchronously in the effect body, so a new `lastResync` (a
  // different `.at`) makes `showResync` true again on its own.
  const [dismissedAt, setDismissedAt] = useState<number | null>(null);
  useEffect(() => {
    if (lastResync === null) return;
    const timer = setTimeout(() => {
      setDismissedAt(lastResync.at);
    }, 8000);
    return () => {
      clearTimeout(timer);
    };
  }, [lastResync]);
  const resyncNotice = lastResync !== null && dismissedAt !== lastResync.at ? lastResync : null;

  return (
    <div
      className={`ssc-connections-footer ${docked ? "relative shrink-0" : "fixed inset-x-0 bottom-0"} z-10 flex min-w-0 items-center justify-between gap-1 bg-surface/75 px-2 text-[10px] leading-4 text-fg-muted backdrop-blur-md md:static`}
      aria-label="Connections"
    >
      <div className="flex min-w-0 shrink items-center gap-1.5">
        <MountStatusEntry mount={mount} />
        <CameraActivityEntry capture={capture} polarAlignment={polarAlignment} />
        <HeaterEntry heater={heater.data} />
        <SensorTemperatureEntry devices={devices} />
        {snapshot?.device?.battery && (() => {
          const battery = snapshot.device.battery;
          const measuredAt = Date.parse(battery.measured_at ?? "");
          const stale = !Number.isFinite(measuredAt) || now - measuredAt > 120_000;
          const charging = /^(charging|charge)$/i.test(battery.charging_status ?? "");
          const full = /^full$/i.test(battery.charging_status ?? "");
          const powerKnown = !stale && (battery.external_power != null || charging);
          const powered = battery.external_power === true || charging;
          const powerLabel = !powerKnown ? "Power state unknown" : powered
            ? full ? "Fully charged — external power connected" : charging ? "Charging" : "External power connected"
            : "Discharging";
          const iconColour = !powerKnown ? "text-fg-muted" : powered ? "text-success" : "text-danger";
          return <span className={`inline-flex shrink-0 items-center gap-1 ${stale ? "text-fg-muted" : "text-fg"}`}
            title={`Battery: ${String(battery.percent ?? translate("unknown", language))}% · ${powerLabel} · measured ${battery.measured_at ?? translate("never", language)}${stale ? " · stale" : ""}${battery.note ? ` · ${battery.note}` : ""}`}
            aria-label={`Battery ${String(battery.percent ?? translate("unknown", language))} percent, ${powerLabel}${stale ? ", stale" : ""}`}>
            <Battery className={`size-4 ${iconColour}`} aria-hidden="true" />{battery.percent == null ? "—" : `${String(battery.percent)}%`}{stale && "?"}
          </span>;
        })()}
        <FooterActivityEntry
          snapshot={snapshot}
          capture={capture}
          polarAlignment={polarAlignment}
          autofocus={autofocus.data}
          centering={centering.data}
          liveStack={liveStack.data}
          mount={mount}
          filterWheel={filterWheel.data}
          focuser={focuser.data}
        />
      </div>
      <div className="flex min-w-0 shrink items-center justify-end gap-1.5">
        {layoutUnlocked && (
          <button
            type="button"
            className="inline-flex size-6 shrink-0 items-center justify-center rounded text-fg-muted hover:text-fg"
            title="Reset this tab's layout"
            aria-label="Reset this tab's layout"
            onClick={() => { resetTabLayout(layoutRoute.tab); }}
          >
            ↺
          </button>
        )}
        <button
          type="button"
          className={`inline-flex size-6 shrink-0 items-center justify-center rounded ${layoutUnlocked ? "text-accent" : "text-fg-muted"}`}
          title={layoutUnlocked ? "Lock card layout" : "Unlock card layout"}
          aria-label={layoutUnlocked ? "Lock card layout" : "Unlock card layout"}
          aria-pressed={layoutUnlocked}
          onClick={() => { setLayoutUnlocked(!layoutUnlocked); }}
        >
          <LayoutLockIcon unlocked={layoutUnlocked} />
        </button>
        <BackendEntry status={status} />
        {resyncNotice !== null && (
          // Only the transient resync notice is a `status` region — worth an
          // announcement on its own (ported from `ConnectionBadge`, which put
          // `role="status"` on this badge alone, not on itself). The strip as
          // a whole is not: it can flicker on a flaky reconnect loop, and
          // making the entire footer a live region would announce every dot
          // colour change.
          <span
            className="inline-flex shrink-0 text-warning"
            role="status"
            title={`Resynced: ${resyncLabel(resyncNotice.reason)}`}
            aria-label={`Resynced: ${resyncLabel(resyncNotice.reason)}`}
          >
            <StatusIcon kind="resync" />
          </span>
        )}
        <button
          type="button"
          className={`inline-flex size-6 shrink-0 items-center justify-center rounded ${pipEnabled ? "text-accent" : "text-fg-muted"} disabled:cursor-not-allowed disabled:opacity-40`}
          title={pipEnabled ? "Hide stream picture in picture" : pipAvailable ? "Show stream picture in picture" : "Stream picture in picture unavailable"}
          aria-label={pipEnabled ? "Hide stream picture in picture" : pipAvailable ? "Show stream picture in picture" : "Stream picture in picture unavailable"}
          aria-pressed={pipEnabled}
          disabled={!pipEnabled && !pipAvailable}
          onClick={() => { setPipEnabled(!pipEnabled); }}
        >
          <PictureInPicture2 aria-hidden="true" className="size-4" strokeWidth={1.8} />
        </button>
        <span className="shrink-0 text-[10px] text-fg-muted" title="Running version">{build.data?.version ?? "—"}</span>
        <FullscreenToggle />
      </div>
    </div>
  );
}

function FooterActivityEntry({
  snapshot,
  capture,
  polarAlignment,
  autofocus,
  centering,
  liveStack,
  mount,
  filterWheel,
  focuser,
}: {
  snapshot: ReturnType<typeof useConnectionStore.getState>["snapshot"];
  capture: CaptureState | null | undefined;
  polarAlignment: PolarAlignmentState | null | undefined;
  autofocus: ReturnType<typeof useAutofocusStateQuery>["data"];
  centering: ReturnType<typeof useMountCenterProgressQuery>["data"];
  liveStack: ReturnType<typeof useLiveStackQuery>["data"];
  mount: MountState | null | undefined;
  filterWheel: FilterWheelState | undefined;
  focuser: FocuserPositionResponse | undefined;
}) {
  const now = useNow(200);
  const activities = selectFooterActivities({
    snapshot,
    capture,
    polarAlignment,
    autofocus,
    centering,
    liveStack: liveStack ?? undefined,
    mount,
    filterWheel,
    focuser,
    now,
  });
  const activity = activities[0];
  if (activity === undefined) return null;

  const progress = activityProgress(activity);
  const count = formatActivityCount(activity);
  const concurrent = activities.slice(1);
  const visible = `${activity.label}${count === null ? "" : ` · ${count}`}`;
  const title = [
    visible,
    activity.detail !== null && count !== activity.detail ? activity.detail : null,
    ...concurrent.map((item) => {
      const itemCount = formatActivityCount(item);
      return `${item.label}${itemCount === null ? "" : ` · ${itemCount}`}`;
    }),
  ].filter((part): part is string => part !== null).join("\n");

  return (
    <span
      className="inline-flex min-w-0 max-w-[52vw] items-center gap-1 text-accent lg:max-w-[34vw]"
      title={title}
      aria-label={title.replaceAll("\n", ". ")}
      role="status"
    >
      <span
        className="relative h-1.5 w-12 shrink-0 overflow-hidden rounded-full bg-border"
        role="progressbar"
        aria-label={activity.label}
        aria-valuemin={0}
        aria-valuemax={activity.total ?? undefined}
        aria-valuenow={activity.current ?? undefined}
        aria-valuetext={count ?? "In progress"}
      >
        <span
          className={`block h-full rounded-full bg-accent ${progress === null ? "w-1/2 animate-pulse" : ""}`}
          style={progress === null ? undefined : { width: `${String(progress)}%` }}
        />
      </span>
      <span className="truncate text-fg">{visible}</span>
      {concurrent.length > 0 && (
        <span className="shrink-0 text-fg-muted" aria-label={`${String(concurrent.length)} more active operations`}>
          +{String(concurrent.length)}
        </span>
      )}
    </span>
  );
}

function SensorTemperatureEntry({ devices }: { devices: DevicesState | null }) {
  const camera = (devices?.items ?? []).find((item) => item.kind === "camera" && item.number === 0);
  const temperature = camera?.temperature_c;
  const available = typeof temperature === "number";
  const stale = devices?.stale === true;
  const reading = available ? `${temperature.toFixed(1)}°C` : "—°C";
  const title = available
    ? `Seestar sensor temperature: ${reading}${stale ? " (stale)" : ""}`
    : `Seestar sensor temperature unavailable${camera?.temperature_note == null ? "" : ` — ${camera.temperature_note}`}`;
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-0.5 ${available ? (stale ? "text-dot-unknown" : "text-fg-muted") : "text-fg-disabled"}`}
      title={title}
      aria-label={title}
    >
      <StatusIcon kind="temperature" />
      <span>{reading}</span>
    </span>
  );
}

function LayoutLockIcon({ unlocked }: { unlocked: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="size-4" aria-hidden>
      <rect x="5" y="10" width="14" height="11" rx="2" />
      {unlocked ? <path d="M9 10V7a4 4 0 0 1 7.5-2" /> : <path d="M8 10V7a4 4 0 0 1 8 0v3" />}
      <circle cx="12" cy="15" r="1" />
    </svg>
  );
}

const BACKEND_STATUS_LABEL: Record<WsConnectionStatus, string> = {
  connecting: "Connecting…",
  open: "Live",
  reconnecting: "Reconnecting…",
  closed: "Disconnected",
};

/** Bright / muted / disabled — the same three tiers `TabNav` uses for the
 * mount signal, applied here to the WS transport status. `open` is the only
 * state the app is actually live in, so it gets the brightest tier;
 * `connecting`/`reconnecting` are transitional and get the middle tier;
 * `closed` gets the dimmest, matching "there is nothing behind this right
 * now" from `TabNav`'s own `down` tier. */
const BACKEND_DOT_CLASS: Record<WsConnectionStatus, string> = {
  open: "text-dot-ok",
  connecting: "text-dot-unknown",
  reconnecting: "text-dot-unknown",
  closed: "text-dot-bad",
};

/**
 * One word for what the mount is doing, derived client-side from
 * `MountState` in a fixed priority order — an in-motion or homing state is
 * more worth surfacing than a static one, so `slewing` wins over `tracking`
 * wins over the two static/positional facts, which win over the plain
 * tracking on/off word. There is no `settling` here (see this file's own
 * module doc) and no attempt to guess one from a timer after a slew ends.
 */
type MountStatusWord = "slewing" | "tracking" | "not_tracking" | "parked" | "home" | "unknown";

function mountStatusWord(mount: MountState): MountStatusWord {
  if (mount.slewing === true) return "slewing";
  if (mount.at_park === true) return "parked";
  if (mount.at_home === true) return "home";
  if (mount.tracking === true) return "tracking";
  if (mount.tracking === false) return "not_tracking";
  return "unknown";
}

const MOUNT_WORD_LABEL: Record<MountStatusWord, string> = {
  slewing: "Slewing",
  tracking: "Tracking",
  not_tracking: "Not tracking",
  parked: "Parked",
  home: "Home",
  unknown: "Unknown",
};

function MountStatusEntry({ mount }: { mount: MountState | null | undefined }) {
  if (mount === undefined) return null;
  if (mount === null) {
    return (
      <span
        className="inline-flex shrink-0 items-center text-fg-disabled"
        title="No mount reading yet"
        aria-label="No mount reading yet"
      >
        <StatusIcon kind="unknown" />
      </span>
    );
  }
  const word = mountStatusWord(mount);
  const label = MOUNT_WORD_LABEL[word];
  const title = `Mount: ${label}${mount.stale ? " (stale)" : ""}`;
  return (
    <span
      className={`inline-flex shrink-0 items-center ${mount.stale ? "text-dot-unknown" : "text-dot-ok"}`}
      title={title}
      aria-label={title}
    >
      <StatusIcon kind={word} active={word === "slewing"} />
    </span>
  );
}

/** Mirrors `ssc.state.models.CaptureStage` — see `api/capture.ts`'s own
 * `CaptureStage` type doc for why there is no `platesolving` entry. */
const CAPTURE_STAGE_LABEL: Record<CaptureStage, string> = {
  dithering: "Dithering mount",
  exposing: "Exposing",
  downloading: "Downloading",
  transferring: "Transferring picture",
  saving: "Saving",
};

const CAPTURE_STAGE_ICON: Record<CaptureStage, StatusIconKind> = {
  dithering: "dithering",
  exposing: "exposing",
  downloading: "downloading",
  transferring: "transferring",
  saving: "saving",
};

const CAPTURE_STAGE_CLASS: Record<CaptureStage, string> = {
  dithering: "text-warning",
  exposing: "text-accent",
  downloading: "text-accent",
  transferring: "text-accent",
  saving: "text-success",
};

/** Camera "capturing" + elapsed/total progress bar, and — right next to it —
 * the pipeline-stage word (owner: "transferring picture, platesolving, and
 * so on"). Both read the same `CaptureState` `CaptureStatusPanel.tsx` on the
 * Capture tab reads; this is the same data, just always visible rather than
 * only on one screen. Renders nothing while idle — the footer stays minimal
 * (module doc: "minimum height... nothing else") when there is nothing
 * running to report.
 */
function CameraActivityEntry({
  capture,
  polarAlignment,
}: {
  capture: CaptureState | null | undefined;
  polarAlignment: PolarAlignmentState | null | undefined;
}) {
  // 200ms, matching `CaptureStatusPanel.tsx`'s own tick rate for the same bar.
  const now = useNow(200);

  let label = "Camera idle";
  let startedAt: string | null | undefined = null;
  let durationS: number | null | undefined = null;
  let active = false;
  let stageLabel: string | null = null;
  let stage: CaptureStage | null = null;
  let activityKind: StatusIconKind = "camera";
  let activityClass = "text-fg-disabled";

  if (capture?.in_progress === true) {
    label = "Camera capturing";
    startedAt = capture.started_at;
    durationS = capture.duration_s;
    active = true;
    stage = capture.stage ?? null;
    stageLabel = stage !== null ? CAPTURE_STAGE_LABEL[stage] : null;
    activityClass = "text-accent";
  } else if (polarAlignment?.refinement?.status === "running") {
    const refinement = polarAlignment.refinement;
    if (refinement.stage === "capturing") {
      label = "Camera capturing PA";
      startedAt = refinement.exposure_started_at;
      durationS = refinement.exposure_duration_s;
    } else if (refinement.stage === "solving") {
      label = "Camera platesolving PA";
      activityKind = "solving";
      activityClass = "text-accent";
    } else {
      label = "Camera settling PA";
      activityKind = "settling";
      activityClass = "text-warning";
    }
    active = true;
  } else if (polarAlignment?.status === "running" && polarAlignment.current_point_index !== null) {
    const point = (polarAlignment.points ?? []).find(
      (candidate) => candidate.index === polarAlignment.current_point_index,
    );
    if (point?.status === "settling") {
      label = "Camera settling";
      activityKind = "settling";
      activityClass = "text-warning";
      active = true;
    } else if (point?.status === "capturing") {
      label = "Camera capturing PA";
      startedAt = polarAlignment.exposure_started_at;
      durationS = polarAlignment.exposure_duration_s;
      active = true;
    } else if (point?.status === "solving") {
      label = "Camera platesolving PA";
      activityKind = "solving";
      activityClass = "text-accent";
      active = true;
    }
  }
  if (active && activityClass === "text-fg-disabled") activityClass = "text-accent";

  let progress: { elapsedS: number; totalS: number } | null = null;
  if (startedAt != null && durationS != null && durationS > 0) {
    const startedMs = Date.parse(startedAt);
    if (!Number.isNaN(startedMs)) {
      const elapsedS = Math.max(0, Math.min(durationS, (now - startedMs) / 1000));
      progress = { elapsedS, totalS: durationS };
    }
  }
  const progressTitle =
    progress !== null ? `${label} — ${progress.elapsedS.toFixed(1)}s of ${progress.totalS.toFixed(0)}s` : label;
  const progressPct = progress === null ? undefined : Math.min(100, (progress.elapsedS / progress.totalS) * 100);

  // The mount icon is the one connection indicator for the whole Seestar.
  // Camera/filter/focuser glyphs appear only while they are doing work (the
  // latter two through `hardwareActivities`), never as a duplicate idle
  // device roster in the footer.
  if (!active) return null;

  return (
    <>
      <span
        className={`inline-flex shrink-0 items-center ${activityClass}`}
        title={progressTitle}
        aria-label={progressTitle}
      >
        <StatusIcon kind={activityKind} active={activityKind !== "camera"} progressPct={progressPct} />
        {progress !== null && <span className="sr-only" role="progressbar" aria-label="Exposure progress" aria-valuemin={0} aria-valuemax={progress.totalS} aria-valuenow={progress.elapsedS} />}
      </span>
      {stageLabel !== null && stage !== null && (
        <span
          className={`inline-flex shrink-0 items-center ${CAPTURE_STAGE_CLASS[stage]}`}
          title={`Pipeline stage: ${stageLabel}`}
          aria-label={`Pipeline stage: ${stageLabel}`}
        >
          <StatusIcon kind={CAPTURE_STAGE_ICON[stage]} active={stage === "dithering" || stage === "exposing"} />
        </span>
      )}
    </>
  );
}

function HeaterEntry({ heater }: { heater: HeaterState | undefined }) {
  if (heater !== undefined && deviceNotesSayDisconnected(heater.note)) {
    return (
      <span
        className="inline-flex shrink-0 items-center text-dot-bad"
        title="Dew heater disconnected"
        aria-label="Dew heater disconnected"
      >
        <StatusIcon kind="heater" />
      </span>
    );
  }
  const state = heater?.enabled === true ? "on" : heater?.enabled === false ? "off" : "unknown";
  const title = state === "unknown" ? (heater?.note ?? "Dew heater state has not been read yet") : `Dew heater ${state}`;
  return (
    <span
      className={`inline-flex shrink-0 items-center ${state === "on" ? "text-warning" : state === "off" ? "text-fg-muted" : "text-fg-disabled"}`}
      title={title}
      aria-label={title}
    >
      <StatusIcon kind="heater" active={state === "on"} />
    </span>
  );
}

function BackendEntry({ status }: { status: WsConnectionStatus }) {
  const title = `Connection to ssc-core: ${BACKEND_STATUS_LABEL[status]}`;
  return (
    <span className="inline-flex shrink-0 items-center" title={title}>
      <ConnectionIcon kind="backend" className={BACKEND_DOT_CLASS[status]} />
      <span className="sr-only">{title}</span>
    </span>
  );
}

function ConnectionIcon({
  kind,
  className,
}: {
  kind: "backend" | "filterwheel" | "focuser";
  className: string;
}) {
  const common = {
    className: `size-4 shrink-0 ${className}`,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  if (kind === "backend") {
    return <svg {...common}><rect x="4" y="3" width="16" height="7" rx="1.5" /><rect x="4" y="14" width="16" height="7" rx="1.5" /><path d="M8 6.5h.01M8 17.5h.01M12 6.5h5M12 17.5h5" /></svg>;
  }
  if (kind === "focuser") {
    return <svg {...common}><circle cx="12" cy="12" r="7" /><circle cx="12" cy="12" r="2" /><path d="M12 2v3m0 14v3M2 12h3m14 0h3" /></svg>;
  }
  return <svg {...common}><circle cx="12" cy="12" r="9" /><circle cx="12" cy="7" r="2" /><circle cx="8" cy="14" r="2" /><circle cx="16" cy="14" r="2" /></svg>;
}

type StatusIconKind =
  | MountStatusWord
  | "camera"
  | "settling"
  | "solving"
  | "dithering"
  | "exposing"
  | "downloading"
  | "transferring"
  | "saving"
  | "heater"
  | "temperature"
  | "resync";

/** Shape is the primary state signal; colour and optional motion reinforce it. */
function StatusIcon({
  kind,
  active = false,
  progressPct,
}: {
  kind: StatusIconKind;
  active?: boolean;
  progressPct?: number;
}) {
  const common = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  const motion = active ? "motion-safe:animate-pulse" : "";
  let glyph;
  switch (kind) {
    case "slewing":
      glyph = <><path d="m5 7 10 4 2-4L7 3 5 7Zm6 3-3 10m3-6 5 6M7 20h10" /><path d="M18 10h4m-2-2 2 2-2 2" /></>;
      break;
    case "tracking":
      glyph = <><circle cx="12" cy="12" r="7" /><path d="M12 2v4m0 12v4M2 12h4m12 0h4" /><circle cx="12" cy="12" r="1.5" /></>;
      break;
    case "not_tracking":
      glyph = <><circle cx="12" cy="12" r="7" /><path d="M7 7 17 17" /></>;
      break;
    case "parked":
      glyph = <><path d="M5 21V4h7a5 5 0 0 1 0 10H5M8 7h4a2 2 0 0 1 0 4H8" /></>;
      break;
    case "home":
      glyph = <><path d="m3 11 9-8 9 8v10h-6v-6H9v6H3V11Z" /></>;
      break;
    case "settling":
      glyph = <><path d="M6 3h12M6 21h12M8 3c0 5 2 6 4 9-2 3-4 4-4 9m8-18c0 5-2 6-4 9 2 3 4 4 4 9" /><path d="M9 18h6" /></>;
      break;
    case "solving":
      glyph = <><circle cx="11" cy="13" r="6" /><path d="M11 4v3m0 12v3M2 13h3m12 0h3m1-10v4m-2-2h4" /></>;
      break;
    case "dithering":
      glyph = <><path d="M4 12h16M7 9l-3 3 3 3m10-6 3 3-3 3M12 4v16M9 7l3-3 3 3m-6 10 3 3 3-3" /></>;
      break;
    case "downloading":
      glyph = <><path d="M12 3v12m-4-4 4 4 4-4M4 19h16" /></>;
      break;
    case "transferring":
      glyph = <><path d="M4 8h14m-4-4 4 4-4 4m6 4H6m4-4-4 4 4 4" /></>;
      break;
    case "saving":
      glyph = <><path d="M4 3h13l3 3v15H4V3Z" /><path d="M8 3v6h8V3M8 21v-7h8v7" /></>;
      break;
    case "heater":
      glyph = <><path d="M12 22c4 0 7-2.8 7-6.5 0-3-1.8-5.5-4-7.5.2 2-1 3.5-2.2 4.2.5-4-1.4-7.1-4.3-9.2.2 3-3.5 6.4-3.5 11.3C5 18.7 8 22 12 22Z" /></>;
      break;
    case "temperature":
      glyph = <><path d="M9 14.8V5a3 3 0 0 1 6 0v9.8a5 5 0 1 1-6 0Z" /><path d="M12 8v9" /><circle cx="12" cy="18" r="1.5" /></>;
      break;
    case "resync":
      glyph = <><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 8A7 7 0 0 1 18 6l2 2M18 16a7 7 0 0 1-11.9 2L4 16" /></>;
      break;
    case "exposing":
    case "camera":
      glyph = <><path d="M4 7h4l1.5-2h5L16 7h4v12H4V7Z" /><circle cx="12" cy="13" r="3.5" /></>;
      break;
    case "unknown":
      glyph = <><circle cx="12" cy="12" r="9" /><path d="M9.5 9a2.7 2.7 0 1 1 4.2 2.3c-1.2.8-1.7 1.3-1.7 2.7m0 3h.01" /></>;
      break;
  }
  const circumference = 62.83;
  const progressOffset = progressPct === undefined ? circumference : circumference * (1 - progressPct / 100);
  return (
    <svg {...common} className={`size-4 shrink-0 ${motion}`} data-status-icon={kind}>
      {glyph}
      {progressPct !== undefined && (
        <circle
          cx="12"
          cy="12"
          r="10"
          strokeWidth="1.5"
          strokeDasharray={circumference}
          strokeDashoffset={progressOffset}
          transform="rotate(-90 12 12)"
        />
      )}
    </svg>
  );
}

function resyncLabel(reason: string): string {
  switch (reason) {
    case "gap":
      return "missed updates";
    case "overflow":
      return "fell behind";
    case "server_restarted":
      return "server restarted";
    default:
      return reason;
  }
}
