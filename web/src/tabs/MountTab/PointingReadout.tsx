// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Badge } from "../../components/Badge";
import type { MountState } from "../../api/mount";
import { ageSeconds, formatAge, formatDecDeg, formatDeg1, formatRaHours } from "./format";
import { useNow } from "./useNow";

/** Matches `ssc.state.models.MOUNT_STALE_AFTER_S` — kept in sync by hand
 * since it is not (yet) served anywhere a client can read it. The server's
 * own `mount.stale` is a `@computed_field`, correct only at the instant it
 * was serialised; nothing recomputes it in the browser as time passes with
 * no new frame, which is exactly the "stale data rendered as live" failure
 * the tab's brief warns against. This threshold is the client-side backstop
 * that keeps ticking after the last message, not a replacement for the
 * server's own flag — `stale` below is the OR of both. */
const STALE_AFTER_SECONDS = 5;

export function PointingReadout({ mount }: { mount: MountState }) {
  const now = useNow(1000);
  const age = ageSeconds(mount.polled_at, now);
  const stale = mount.stale || age === null || age > STALE_AFTER_SECONDS;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={stale ? "warning" : "success"} role="status">
          {stale ? "Stale" : "Live"} · polled {formatAge(age)}
        </Badge>
        {mount.slewing === true && <Badge tone="accent">Slewing</Badge>}
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-fg-muted">RA (topocentric)</dt>
        <dd className={`tabular-nums ${stale ? "text-fg-muted" : "text-fg"}`}>
          {formatRaHours(mount.ra_hours)}
        </dd>
        <dt className="text-fg-muted">Dec (topocentric)</dt>
        <dd className={`tabular-nums ${stale ? "text-fg-muted" : "text-fg"}`}>
          {formatDecDeg(mount.dec_deg)}
        </dd>
        <dt className="text-fg-muted">Altitude</dt>
        <dd className={`tabular-nums ${stale ? "text-fg-muted" : "text-fg"}`}>{formatDeg1(mount.alt_deg)}</dd>
        <dt className="text-fg-muted">Azimuth</dt>
        <dd className={`tabular-nums ${stale ? "text-fg-muted" : "text-fg"}`}>{formatDeg1(mount.az_deg)}</dd>
        <dt className="text-fg-muted">Sidereal time</dt>
        <dd className={`tabular-nums ${stale ? "text-fg-muted" : "text-fg"}`}>
          {formatRaHours(mount.sidereal_time_hours)}
        </dd>
      </dl>
      <p className="text-xs text-fg-muted">
        RA/Dec are coordinates <em>of date</em> from this mount (
        <code>EquatorialSystem</code> = {mount.equatorial_system ?? UNKNOWN_LABEL}), not J2000 — expect
        roughly 20′ of difference against a J2000 catalogue or a plate-solved position.
      </p>
    </div>
  );
}

const UNKNOWN_LABEL = "unknown";
