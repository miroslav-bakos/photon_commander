// SPDX-License-Identifier: GPL-3.0-only
import { useEffect, useState } from "react";
import { useConnectionStore } from "../../store/connectionStore";
import type { MountState } from "../../api/mount";

/** A display clock only; server readings remain the sole state authority. */
export function useTelemetryAvailability(mount: MountState | null | undefined) {
  const status = useConnectionStore((state) => state.status);
  const snapshot = useConnectionStore((state) => state.snapshot);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => { setNow(Date.now()); }, 1000);
    return () => { window.clearInterval(timer); };
  }, []);
  const unavailable = status !== "open" ? "Disconnected"
    : snapshot == null ? "Unknown"
    : snapshot.link?.control_connected !== true ? "Disconnected"
    : snapshot.link.status === "degraded" || snapshot.devices?.stale === true ? "Unknown"
    : null;
  const polled = mount?.polled_at == null ? NaN : Date.parse(mount.polled_at);
  const mountUnavailable = unavailable ?? (mount == null ? "Unknown"
    : mount.stale || !Number.isFinite(polled) || now - polled > 5000 ? "Stale" : null);
  return { unavailable, mountUnavailable, now, connected: status === "open" && snapshot != null };
}
