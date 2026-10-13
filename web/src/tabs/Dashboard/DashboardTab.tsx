// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { timedApiFetch } from "../../performance/diagnostics";
import { useEffect, type CSSProperties, type ReactNode } from "react";
import { useQueries } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  CalendarClock,
  Camera,
  Crosshair,
  Filter,
  Focus,
  Layers3,
  Move3d,
  Telescope,
  ThermometerSun,
} from "lucide-react";

import { Card } from "../../components/Card";
import { LayoutPanelProvider } from "../../layout/LayoutContext";
import { useCaptureDitherSettingsQuery } from "../../api/settings";
import {
  useCaptureFrameQualityQuery,
  useCaptureFramesQuery,
  useCaptureState,
  useLiveStackQuery,
  type CaptureFrameQuality,
} from "../../api/capture";
import { useAutofocusStateQuery, useFocuserPositionQuery } from "../../api/focuser";
import { useFilterWheelQuery } from "../../api/filterwheel";
import { useHeaterQuery } from "../../api/heater";
import { isAlpacaDeviceConnected } from "../../api/deviceAvailability";
import { useMountState } from "../../api/mount";
import { usePolarAlignmentState } from "../../api/polarAlignment";
import { nearestForecastPoint, useForecastQuery } from "../../api/weather";
import { useConnectionStore } from "../../store/connectionStore";
import { formatDecDeg, formatDeg1, formatRaHours } from "../MountTab/format";
import { ObservingSessionPanel } from "../Observe/ObservingSessionPanel";
import { WorkflowGuide } from "../Observe/WorkflowGuide";
import { useTelemetryAvailability } from "../Observe/useTelemetryAvailability";
import { useLayoutStore } from "../../layout/layoutStore";

const activeClass = "border-accent";
const activeStyle: CSSProperties = {
  backgroundColor: "color-mix(in srgb, var(--ssc-accent) 32%, var(--ssc-surface))",
};

function ConnectionDot({ connected, unknown = false, label }: { connected: boolean; unknown?: boolean; label: string }) {
  const status = unknown ? "connection unknown" : connected ? "connected" : "disconnected";
  return (
    <span
      aria-label={`${label} ${status}`}
      title={`${label} ${status}`}
      className={`absolute right-3 top-3 size-2.5 rounded-full ring-2 ring-surface ${unknown ? "bg-warning" : connected ? "bg-dot-ok" : "bg-dot-bad"}`}
    />
  );
}

function DashboardGrid({ children }: { children: ReactNode }) {
  return (
    <LayoutPanelProvider panel="secondary" panels={{}}>
      <div role="region" aria-label="Observatory status" data-layout-panel-target="secondary" className="grid grid-cols-2 items-stretch gap-3 lg:grid-cols-4 lg:gap-4">
        {children}
      </div>
    </LayoutPanelProvider>
  );
}

function StatusCard({
  layoutId,
  title,
  icon,
  connected,
  readingStatus,
  active = false,
  className = "",
  style,
  to,
  children,
}: {
  layoutId: string;
  title: string;
  icon: ReactNode;
  connected?: boolean;
  readingStatus?: string | null;
  active?: boolean;
  className?: string;
  style?: CSSProperties;
  to: string;
  children: ReactNode;
}) {
  const navigate = useNavigate();
  const layoutUnlocked = useLayoutStore((state) => state.unlocked);

  function openControls(): void {
    if (!layoutUnlocked) void navigate(to);
  }

  return (
    <Card
      layoutId={layoutId}
      layoutLabel={title}
      className={`min-h-48 ${layoutUnlocked ? "" : "cursor-pointer transition-colors hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"} ${active ? activeClass : ""} ${className}`}
      style={{ ...(active ? activeStyle : {}), ...style }}
      role={layoutUnlocked ? undefined : "link"}
      tabIndex={layoutUnlocked ? undefined : 0}
      aria-label={layoutUnlocked ? undefined : `Open ${title} controls`}
      onClick={(event) => {
        if ((event.target as HTMLElement).closest("button, a, input, select, textarea") === null) openControls();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        openControls();
      }}
    >
      {connected !== undefined && <ConnectionDot connected={connected && !readingStatus?.startsWith("Disconnected")} unknown={!connected && readingStatus?.startsWith("Stale")} label={title} />}
      <div className="mb-4 flex items-center gap-2 pr-5 text-fg-muted">
        {icon}
        <h3 className="text-sm font-semibold tracking-wide uppercase">{title}</h3>
      </div>
      {children}
      <div className="mt-3 min-h-8">
        {readingStatus != null && <p role="status" aria-label={`${title} reading status`} className="text-xs text-warning">{readingStatus}</p>}
      </div>
    </Card>
  );
}

function Value({ children }: { children: ReactNode }) {
  return <p className="text-2xl font-semibold leading-tight">{children}</p>;
}

function DetailList({ children }: { children: ReactNode }) {
  return <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">{children}</dl>;
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return <><dt className="text-fg-muted">{label}</dt><dd className="min-w-0 truncate text-right">{children}</dd></>;
}

function formatTimestamp(value: string | null | undefined): string {
  if (value == null) return "Never";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}

function formatNumber(value: number | null | undefined, suffix = ""): string {
  return value == null ? "Unknown" : `${value.toFixed(2)}${suffix}`;
}

function formatSignedPolarError(valueDeg: number): string {
  return `${valueDeg >= 0 ? "+" : ""}${valueDeg.toFixed(2)}°`;
}

function mean(values: (number | null | undefined)[]): number | null {
  const finite = values.filter((value): value is number => value != null && Number.isFinite(value));
  if (finite.length === 0) return null;
  return finite.reduce((sum, value) => sum + value, 0) / finite.length;
}

function filterStyle(name: string | null): { className: string; style?: React.CSSProperties } {
  switch (name?.toUpperCase()) {
    case "LP": return { className: "border-danger", style: { backgroundColor: "color-mix(in srgb, var(--ssc-danger) 34%, var(--ssc-surface))" } };
    case "DARK": return { className: "border-fg-muted", style: { backgroundColor: "color-mix(in srgb, var(--ssc-fg-muted) 28%, var(--ssc-surface))" } };
    case "IR": return { className: "", style: { borderColor: "var(--ssc-filter-ir)", backgroundColor: "color-mix(in srgb, var(--ssc-filter-ir) 34%, var(--ssc-surface))" } };
    default: return { className: "" };
  }
}

function currentFilter(names: string[] | null | undefined, position: number | null | undefined): string | null {
  if (names == null || position == null || position < 0) return null;
  return names[position] ?? null;
}

function mountHeadline(mount: ReturnType<typeof useMountState>): string {
  if (mount == null) return "No mount data";
  if (mount.slewing === true || mount.jog_primary_active === true || mount.jog_secondary_active === true) return "Moving";
  if (mount.at_park === true) return "Parked";
  if (mount.at_home === true) return "At home";
  return mount.tracking === true ? "Tracking" : mount.tracking === false ? "Not tracking" : "Status unknown";
}

async function fetchQuality(frameId: number): Promise<CaptureFrameQuality> {
  const response = await timedApiFetch(new Request(new URL(`/api/capture/frames/${String(frameId)}/quality`, window.location.origin), { credentials: "same-origin" }));
  if (!response.ok) throw new Error(`Quality request failed (${String(response.status)})`);
  return response.json() as Promise<CaptureFrameQuality>;
}

export function DashboardTab() {
  const snapshot = useConnectionStore((state) => state.snapshot);
  const mountReading = useMountState();
  const { unavailable, mountUnavailable, now } = useTelemetryAvailability(mountReading);
  // Freshness affects display labels and activity styling, never retained readings.
  const mount = mountReading;
  const polarAlignment = usePolarAlignmentState();
  const captureReading = useCaptureState();
  const capture = captureReading;
  const focuser = useFocuserPositionQuery();
  const autofocus = useAutofocusStateQuery();
  const filterWheel = useFilterWheelQuery();
  const heater = useHeaterQuery();
  const weather = useForecastQuery(true);
  const dither = useCaptureDitherSettingsQuery();
  const liveStack = useLiveStackQuery(true);
  const frames = useCaptureFramesQuery(10);
  const lastQuality = useCaptureFrameQualityQuery(capture?.last_frame_id);

  useEffect(() => {
    if (capture?.last_frame_id != null) void frames.refetch();
  }, [capture?.last_frame_id]); // eslint-disable-line react-hooks/exhaustive-deps -- refetch identity is query-managed

  function readingStatus(reason: string | null, hasReading: boolean): string | null {
    if (reason == null) return null;
    const status = reason === "Disconnected" ? "Disconnected" : reason === "Read failed" ? "Read failed" : "Stale";
    return hasReading ? `${status} · Last reading` : reason === "Unknown" ? "Telemetry unavailable" : status;
  }
  function queryStatus(query: { data?: unknown; isError?: boolean; dataUpdatedAt?: number }): string | null {
    const reason = unavailable ?? (query.isError ? "Read failed" : query.dataUpdatedAt != null && now - query.dataUpdatedAt > 15000 ? "Stale" : null);
    return readingStatus(reason, query.data != null);
  }
  const mountStatus = readingStatus(mountUnavailable, mount != null);
  const captureStatus = readingStatus(unavailable, capture != null);
  const focuserStatus = queryStatus(focuser);
  const filterWheelStatus = queryStatus(filterWheel);
  const heaterStatus = queryStatus(heater);
  const liveStackStatus = queryStatus(liveStack);
  const ditherStatus = queryStatus(dither);

  const recentFrames = frames.data?.frames.slice(0, 10) ?? [];
  const recentQualities = useQueries({
    queries: recentFrames.map((frame) => ({
      queryKey: ["capture", "frame-quality", frame.id] as const,
      queryFn: () => fetchQuality(frame.id),
      staleTime: Number.POSITIVE_INFINITY,
      retry: false,
    })),
  });
  const qualityValues = recentQualities.flatMap((query) => query.data === undefined ? [] : [query.data]);
  const averageHfr = mean(qualityValues.map((quality) => quality.hfr_px));
  const averageFwhm = mean(qualityValues.map((quality) => quality.fwhm_px));

  const mountMoving = mount?.slewing === true || mount?.jog_primary_active === true || mount?.jog_secondary_active === true;
  const mountActive = mountStatus === null && (mountMoving || mount?.tracking === true);
  const focusMoving = focuser.data?.is_moving === true || autofocus.data?.status === "running";
  const filter = currentFilter(filterWheel.data?.names, filterWheel.data?.position);
  const filterTone = filterStyle(filter);
  const forecastPoint = nearestForecastPoint(weather.data?.points ?? []);
  const run = snapshot?.run;
  const runActive = run?.status === "running" || run?.status === "paused";
  const unplannedCapture = capture?.in_progress === true && !runActive;
  const historyResult = polarAlignment?.history?.at(-1);
  const polarResult = polarAlignment?.refinement?.result ?? polarAlignment?.result ?? historyResult;
  const desiredPolarErrorArcmin = polarAlignment?.assessment?.max_error_arcmin ?? 2;
  const polarErrorArcmin = polarResult == null ? null : Math.abs(polarResult.total_error_deg) * 60;
  const polarWithinTarget = polarErrorArcmin !== null && polarErrorArcmin <= desiredPolarErrorArcmin;
  const polarMeasuredAt = polarAlignment?.refinement?.result?.epoch
    ?? polarAlignment?.result?.epoch
    ?? historyResult?.completed_at
    ?? null;
  const currentItem = run?.items?.find((item) => item.item_id === run.current_item_id);
  const target = capture?.object_name ?? liveStack.data?.object_name ?? null;
  const calibration = capture?.calibration;
  const cameraCount = capture == null ? "Unknown" : calibration?.status === "running"
    ? `${String(calibration.completed_frames)} / ${String(calibration.total_frames)}`
    : capture.loop_active === true
      ? `${String(capture.loop_completed_frames ?? 0)} / continuous`
      : runActive && currentItem?.end_utc != null
        ? `${String(liveStack.data?.frame_count ?? 0)} / until ${new Date(currentItem.end_utc).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
        : capture.in_progress ? "1 / 1" : "Idle";
  const hasReadings = mount != null || capture != null || focuser.data != null || filterWheel.data != null || heater.data != null || liveStack.data != null;
  const unavailableMessage = unavailable === "Disconnected"
    ? hasReadings ? "Disconnected. Showing last readings." : "Disconnected. Waiting for readings."
    : hasReadings ? "Telemetry is stale. Showing last readings." : "Telemetry unavailable. Waiting for readings.";
  const cameraStage = unavailable === "Unknown" && capture != null ? "Stale" : unavailable ?? (capture == null ? "Unknown" : capture.in_progress ? (capture.stage ?? "working") : "idle");

  return (
    <section aria-labelledby="dashboard-title" className="space-y-4">
      <div>
        <h2 id="dashboard-title" className="text-xl font-semibold">Observe</h2>
        <p className="text-sm text-fg-muted">The current night, from target to last frame</p>
      </div>
      <WorkflowGuide />
      {unavailable !== null && <p role="status" className="rounded border border-warning bg-surface-raised p-3 text-sm text-warning">{unavailableMessage}</p>}
      <section aria-labelledby="equipment-imaging-title" className="rounded-lg border border-border bg-surface/60 p-3">
      <h3 id="equipment-imaging-title" className="min-h-11 py-3 text-sm font-medium">Equipment and imaging details</h3>
      <DashboardGrid>

        <StatusCard layoutId="mount" title="Mount" to="/mount?panel=pointing" icon={<Telescope className="size-5" />} connected={isAlpacaDeviceConnected(snapshot?.devices, "telescope")} active={mountActive} readingStatus={mountStatus}>
          <Value>{mountUnavailable === "Unknown" && mount != null ? "Stale" : mountUnavailable ?? mountHeadline(mount)}</Value>
          <p className="mt-1 text-xs text-fg-muted">{target ?? "No current target name"}</p>
          <DetailList>
            <Detail label="RA">{formatRaHours(mount?.ra_hours ?? null)}</Detail>
            <Detail label="Dec">{formatDecDeg(mount?.dec_deg ?? null)}</Detail>
            <Detail label="Alt / Az">{formatDeg1(mount?.alt_deg ?? null)} / {formatDeg1(mount?.az_deg ?? null)}</Detail>
            <Detail label="State">{mountMoving ? "Moving" : mount?.at_park === true ? "Parked" : mount?.at_park === false ? "Unparked" : "Unknown"}</Detail>
          </DetailList>
        </StatusCard>

        <StatusCard layoutId="focuser" title="Focuser" to="/mount?panel=focuser" icon={<Focus className="size-5" />} connected={isAlpacaDeviceConnected(snapshot?.devices, "focuser")} active={focuserStatus === null && focusMoving} readingStatus={focuserStatus}>
          <Value>{focuser.data?.position == null ? "Unknown" : String(focuser.data.position)}</Value>
          <p className="mt-1 text-xs text-fg-muted">{focuserStatus === null && focusMoving ? "Focusing…" : "Focus position"}</p>
          <DetailList>
            <Detail label="Last result">{autofocus.data?.status ?? "Unknown"}</Detail>
            <Detail label="Last focus">{formatTimestamp(autofocus.data?.completed_at)}</Detail>
            <Detail label="Best position">{autofocus.data?.final_position ?? "Unknown"}</Detail>
          </DetailList>
        </StatusCard>

        <StatusCard layoutId="filter-wheel" title="Filter wheel" to="/mount?panel=filter-wheel" icon={<Filter className="size-5" />} connected={isAlpacaDeviceConnected(snapshot?.devices, "filterwheel")} className={filterTone.className} style={filterTone.style} readingStatus={filterWheelStatus}>
          <Value>{filter ?? "Unknown"}</Value>
          <p className="mt-1 text-xs text-fg-muted">Current filter</p>
          <DetailList><Detail label="Position">{filterWheel.data?.position == null ? "Unknown" : String(filterWheel.data.position + 1)}</Detail></DetailList>
        </StatusCard>

        <StatusCard layoutId="dew-heater" title="Dew heater" to="/mount?panel=dew-heater" icon={<ThermometerSun className="size-5" />} connected={isAlpacaDeviceConnected(snapshot?.devices, "switch")} active={heaterStatus === null && heater.data?.enabled === true} readingStatus={heaterStatus}>
          <Value>{heater.data?.enabled === true ? "On" : heater.data?.enabled === false ? "Off" : "Unknown"}</Value>
          <p className="mt-1 text-xs text-fg-muted">Outside {forecastPoint?.temp_c == null ? "unknown" : `${forecastPoint.temp_c.toFixed(1)} °C`}</p>
          <DetailList>
            <Detail label="Dew point">{forecastPoint?.dew_point_c == null ? "Unknown" : `${forecastPoint.dew_point_c.toFixed(1)} °C`}</Detail>
            <Detail label="Weather">{forecastPoint?.source ?? "Unavailable"}</Detail>
          </DetailList>
        </StatusCard>

        <StatusCard layoutId="dithering" title="Dithering" to="/mount?panel=dithering" icon={<Move3d className="size-5" />} connected={isAlpacaDeviceConnected(snapshot?.devices, "telescope")} active={ditherStatus === null && dither.data?.enabled === true} readingStatus={ditherStatus}>
          <Value>{dither.data?.enabled === true ? "Enabled" : dither.data?.enabled === false ? "Disabled" : "Unknown"}</Value>
          <p className="mt-1 text-xs text-fg-muted">{ditherStatus === null && capture?.stage === "dithering" ? "Dithering now…" : "Mount offset pattern"}</p>
          <DetailList>
            <Detail label="Distance">{dither.data === undefined ? "Unknown" : `${String(dither.data.min_pixels)}–${String(dither.data.max_pixels)} px`}</Detail>
            <Detail label="Frequency">{dither.data === undefined ? "Unknown" : `Every ${String(dither.data.every_n_frames)} frames`}</Detail>
          </DetailList>
        </StatusCard>

        <StatusCard layoutId="camera" title="Camera" to="/live" icon={<Camera className="size-5" />} connected={isAlpacaDeviceConnected(snapshot?.devices, "camera")} active={captureStatus === null && capture?.in_progress === true} readingStatus={captureStatus}>
          <Value>{cameraStage.replaceAll("_", " ")}</Value>
          <p className="mt-1 text-xs text-fg-muted">{cameraCount}</p>
          <DetailList>
            <Detail label="Exposure">{formatNumber(capture?.duration_s ?? liveStack.data?.exposure_s, " s")}</Detail>
            <Detail label="Gain">{capture?.gain ?? liveStack.data?.gain ?? "Unknown"}</Detail>
            <Detail label="Last HFR / FWHM">{formatNumber(lastQuality.data?.hfr_px)} / {formatNumber(lastQuality.data?.fwhm_px)}</Detail>
            <Detail label="Average HFR / FWHM">{formatNumber(averageHfr)} / {formatNumber(averageFwhm)}</Detail>
          </DetailList>
        </StatusCard>

        <StatusCard layoutId="plan" title="Plan" to="/scheduling" icon={<CalendarClock className="size-5" />} active={unavailable === null && (runActive || unplannedCapture)} readingStatus={readingStatus(unavailable, snapshot != null)}>
          <Value>{snapshot == null ? "Unknown" : runActive ? run.plan_name : unplannedCapture ? "Unplanned capture" : "No active plan"}</Value>
          <p className="mt-1 text-xs text-fg-muted">{target ?? "No current target"}</p>
          <DetailList>
            <Detail label="Status">{snapshot == null ? "Unknown" : runActive ? run.status : unplannedCapture ? "capturing" : "Idle"}</Detail>
            <Detail label="Operation">{currentItem?.reason ?? capture?.stage ?? "None"}</Detail>
          </DetailList>
        </StatusCard>

        <StatusCard layoutId="live-stacking" title="Live stacking" to="/live-stacking" icon={<Layers3 className="size-5" />} active={liveStackStatus === null && liveStack.data?.active === true} readingStatus={liveStackStatus}>
          <Value>{liveStackStatus?.split(" · ")[0] ?? (liveStack.data === null ? "Idle" : liveStack.data === undefined ? "Unknown" : liveStack.data.active ? "Active" : "Stopped")}</Value>
          <p className="mt-1 text-xs text-fg-muted">{liveStack.data?.object_name ?? "No stack target"}</p>
          <DetailList>
            <Detail label="Accepted">{liveStack.data === null ? 0 : liveStack.data?.frame_count ?? "Unknown"}</Detail>
            <Detail label="Rejected">{liveStack.data === null ? 0 : liveStack.data?.rejected_count ?? "Unknown"}</Detail>
            <Detail label="Integration">{liveStack.data === null ? `${(0).toFixed(1)} min` : liveStack.data?.total_integration_s == null ? "Unknown" : `${(liveStack.data.total_integration_s / 60).toFixed(1)} min`}</Detail>
          </DetailList>
        </StatusCard>

        <StatusCard layoutId="polar-alignment" title="Polar alignment" to="/polar-align" icon={<Crosshair className="size-5" />} active={polarWithinTarget}>
          <Value>{polarErrorArcmin === null ? "Not measured" : `${(polarErrorArcmin / 60).toFixed(2)}° total`}</Value>
          <p className="mt-1 text-xs text-fg-muted">{polarWithinTarget ? "Within desired error" : polarErrorArcmin === null ? "Run polar alignment to measure" : "Adjustment recommended"}</p>
          <DetailList>
            <Detail label="Altitude error">{polarResult == null ? "Unknown" : formatSignedPolarError(polarResult.altitude_error_deg)}</Detail>
            <Detail label="Azimuth error">{polarResult == null ? "Unknown" : formatSignedPolarError(polarResult.azimuth_error_deg)}</Detail>
            <Detail label="Desired">≤ {(desiredPolarErrorArcmin / 60).toFixed(2)}°</Detail>
            <Detail label="Measured">{formatTimestamp(polarMeasuredAt)}</Detail>
          </DetailList>
        </StatusCard>
        <ObservingSessionPanel />
      </DashboardGrid>
      </section>
    </section>
  );
}
