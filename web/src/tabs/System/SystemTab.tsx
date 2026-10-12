// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useMemo, useState, type ReactNode } from "react";
import { Card } from "../../components/Card";
import { useSystemMetricsQuery, type SystemMetrics } from "../../api/system";
import { LayoutPanelProvider } from "../../layout/LayoutContext";
import { PerformanceDiagnostics } from "../../performance/PerformanceDiagnostics";

function SystemCardGrid({ children }: { children: ReactNode }) {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const panels = useMemo(() => element === null ? {} : { secondary: element }, [element]);
  return (
    <LayoutPanelProvider panel="secondary" panels={panels}>
      <div ref={setElement} data-layout-panel-target="secondary" className="grid items-start gap-4 md:grid-cols-2 xl:grid-cols-4">
        {children}
      </div>
    </LayoutPanelProvider>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} ${unit ?? "KiB"}`;
}

function formatDuration(seconds: number): string {
  const totalMinutes = Math.floor(seconds / 60);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `${String(days)}d ${String(hours)}h`;
  if (hours > 0) return `${String(hours)}h ${String(minutes)}m`;
  return `${String(minutes)}m`;
}

function tone(percent: number): string {
  if (percent >= 90) return "bg-danger";
  if (percent >= 75) return "bg-warning";
  return "bg-accent";
}

function Meter({ value, label }: { value: number; label: string }) {
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs text-fg-muted"><span>{label}</span><span>{value.toFixed(1)}%</span></div>
      <div className="h-2 overflow-hidden rounded-full bg-surface-raised" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value}>
        <div className={`h-full rounded-full ${tone(value)}`} style={{ width: `${String(Math.max(0, Math.min(100, value)))}%` }} />
      </div>
    </div>
  );
}

function Sparkline({ values, label, suffix = "" }: { values: (number | null)[]; label: string; suffix?: string }) {
  const finite = values.filter((value): value is number => value !== null && Number.isFinite(value));
  if (finite.length === 0) return <div className="flex h-24 items-center justify-center text-sm text-fg-muted">Unavailable</div>;
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const spread = Math.max(1, max - min);
  const denominator = Math.max(1, values.length - 1);
  const points = values.map((value, index) => {
    if (value === null || !Number.isFinite(value)) return null;
    const x = (index / denominator) * 300;
    const y = 76 - ((value - min) / spread) * 68;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).filter((point): point is string => point !== null).join(" ");
  const current = finite.at(-1) ?? 0;
  const peak = Math.max(...finite);
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs text-fg-muted">
        <span>Now {current.toFixed(1)}{suffix}</span><span>Peak {peak.toFixed(1)}{suffix}</span>
      </div>
      <svg viewBox="0 0 300 84" preserveAspectRatio="none" className="h-24 w-full" role="img" aria-label={`${label} rolling graph`}>
        <path d="M0 76H300M0 42H300M0 8H300" stroke="var(--color-border)" strokeWidth="1" fill="none" />
        <polyline points={points} fill="none" stroke="var(--color-accent)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      </svg>
    </div>
  );
}

function volumeTitle(metric: SystemMetrics["volumes"][number]): string {
  if (metric.kind === "os") return "OS / SD card";
  return metric.same_device_as_os ? "Photon data (OS volume)" : "Photon data / SSD";
}

function StorageCard({ metric }: { metric: SystemMetrics["volumes"][number] }) {
  return (
    <Card title={volumeTitle(metric)} collapseKey={`system.storage.${metric.kind}`} className="xl:col-span-2">
      <p className="mb-3 text-2xl font-semibold">{formatBytes(metric.free_bytes)} <span className="text-sm font-normal text-fg-muted">free</span></p>
      <Meter value={metric.percent_used} label="Space used" />
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-fg-muted">Capacity</dt><dd>{formatBytes(metric.total_bytes)}</dd>
        <dt className="text-fg-muted">Device</dt><dd className="break-all">{metric.device ?? "Unavailable"}</dd>
        <dt className="text-fg-muted">Filesystem</dt><dd>{metric.filesystem ?? "Unavailable"}</dd>
        <dt className="text-fg-muted">Path</dt><dd className="break-all">{metric.path}</dd>
      </dl>
    </Card>
  );
}

export function SystemTab() {
  const query = useSystemMetricsQuery();
  const metrics = query.data?.metrics;
  const history = query.data?.history ?? [];

  if (query.isPending) return <section className="space-y-4"><p className="text-sm text-fg-muted">Reading system telemetry…</p><PerformanceDiagnostics /></section>;
  if (query.isError) return <section className="space-y-4"><p role="alert" className="text-sm text-danger">{query.error.message}</p><PerformanceDiagnostics /></section>;
  if (metrics === undefined) return <section className="space-y-4"><p role="alert" className="text-sm text-danger">System telemetry is unavailable.</p><PerformanceDiagnostics /></section>;

  const flags = metrics.cpu.throttle_flags;
  const healthyPower = flags !== null && flags.length === 0;
  return (
    <section aria-labelledby="system-title" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div><h2 id="system-title" className="text-xl font-semibold">System</h2><p className="text-sm text-fg-muted">{metrics.model ?? `${metrics.platform} ${metrics.architecture}`} · {metrics.hostname}</p></div>
        <p className="text-xs text-fg-muted">Updated {new Date(metrics.collected_at).toLocaleTimeString()}</p>
      </div>

      <SystemCardGrid>
        <Card title="CPU" collapseKey="system.cpu">
          <p className="text-3xl font-semibold">{metrics.cpu.percent.toFixed(1)}%</p>
          <p className="text-xs text-fg-muted">{String(metrics.cpu.logical_cores)} logical cores · load {metrics.cpu.load_1m.toFixed(2)} / {metrics.cpu.load_5m.toFixed(2)} / {metrics.cpu.load_15m.toFixed(2)}</p>
          <Sparkline values={history.map((point) => point.cpu)} label="CPU usage" suffix="%" />
        </Card>
        <Card title="Temperature & power" collapseKey="system.temperature">
          <p className="text-3xl font-semibold">{metrics.cpu.temperature_c === null ? "Unavailable" : `${metrics.cpu.temperature_c.toFixed(1)} °C`}</p>
          <p className={`mt-1 text-xs ${healthyPower ? "text-success" : flags === null ? "text-fg-muted" : "text-warning"}`}>
            {flags === null ? "Pi throttling telemetry is unavailable on this host." : healthyPower ? `No throttling or undervoltage (${metrics.cpu.throttled_raw ?? "0x0"}).` : flags.join(", ").replaceAll("_", " ")}
          </p>
          <Sparkline values={history.map((point) => point.temperature)} label="CPU temperature" suffix=" °C" />
        </Card>
        <Card title="Memory" collapseKey="system.memory">
          <p className="text-3xl font-semibold">{formatBytes(metrics.memory.available_bytes)} <span className="text-sm font-normal text-fg-muted">available</span></p>
          <div className="mt-3 space-y-3"><Meter value={metrics.memory.percent_used} label="RAM used" /><Meter value={metrics.swap.percent_used} label="Swap used" /></div>
          <Sparkline values={history.map((point) => point.memory)} label="Memory usage" suffix="%" />
        </Card>
        <Card title="Photon Commander process" collapseKey="system.process">
          <p className="text-3xl font-semibold">{formatDuration(metrics.process.uptime_s)}</p>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs"><dt className="text-fg-muted">CPU</dt><dd>{metrics.process.cpu_percent.toFixed(1)}%</dd><dt className="text-fg-muted">Memory</dt><dd>{formatBytes(metrics.process.memory_bytes)}</dd><dt className="text-fg-muted">Threads</dt><dd>{String(metrics.process.threads)}</dd><dt className="text-fg-muted">PID</dt><dd>{String(metrics.process.pid)}</dd></dl>
          <Sparkline values={history.map((point) => point.processCpu)} label="Photon Commander CPU usage" suffix="%" />
        </Card>
        {metrics.volumes.map((metric) => <StorageCard key={metric.kind} metric={metric} />)}
        <Card title="Network" collapseKey="system.network" className="xl:col-span-2">
          <div className="grid grid-cols-2 gap-4 text-sm"><div><p className="text-fg-muted">Receive now</p><p className="text-xl font-semibold">{(history.at(-1)?.receiveMiBs ?? 0).toFixed(2)} MiB/s</p></div><div><p className="text-fg-muted">Send now</p><p className="text-xl font-semibold">{(history.at(-1)?.sendMiBs ?? 0).toFixed(2)} MiB/s</p></div></div>
          <Sparkline values={history.map((point) => point.receiveMiBs)} label="Network receive rate" suffix=" MiB/s" />
          <div className="mt-2 flex flex-wrap gap-2">{metrics.network.interfaces.map((item) => <span key={`${item.name}-${item.ipv4_address}`} className="rounded border border-border px-2 py-1 text-xs"><span className="text-fg-muted">{item.name}</span> {item.ipv4_address}</span>)}</div>
        </Card>
        <Card title="Host" collapseKey="system.host" className="xl:col-span-2">
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm"><dt className="text-fg-muted">Hostname</dt><dd>{metrics.hostname}</dd><dt className="text-fg-muted">Operating system</dt><dd>{metrics.os_name}</dd><dt className="text-fg-muted">OS kernel</dt><dd className="break-all">{metrics.kernel}</dd><dt className="text-fg-muted">Architecture</dt><dd>{metrics.architecture}</dd><dt className="text-fg-muted">System uptime</dt><dd>{formatDuration(metrics.system_uptime_s)}</dd></dl>
        </Card>
        <PerformanceDiagnostics className="md:col-span-2 xl:col-span-4" />
      </SystemCardGrid>
    </section>
  );
}
