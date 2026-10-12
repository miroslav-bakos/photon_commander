// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The numbers `SkyMap`'s shapes cannot say on their own — skymap-viz.md:
 * "Surface the inscribed-circle number... as text, not just as a shape."
 * Every threshold shown here (`rotation_severity`, `high_altitude_transit`,
 * `zenith_singularity`) is read straight off `SessionEnvelopeModel`, never
 * recomputed against the amber-15deg/red-40deg/transit-70deg numbers from
 * DESIGN.md §3.2 — those live in `ssc/astro/framing.py` and duplicating them
 * here would be exactly the drift skymap-viz.md warns against.
 */
import { Badge } from "../../components/Badge";
import { Card, CardTitle } from "../../components/Card";
import type {
  DeviceMountModeCheck,
  FieldGeometryModel,
  MountMode,
  RotationSeverity,
  SessionEnvelopeModel,
} from "../../api/astro";

const SEVERITY_TONE: Record<RotationSeverity, "neutral" | "warning" | "danger"> = {
  none: "neutral",
  amber: "warning",
  red: "danger",
};

export function FramingStatsPanel({
  mount,
  geometry,
  envelope,
  calibrationMeasured,
  deviceMountCheck,
  moon,
}: {
  mount: MountMode;
  geometry: FieldGeometryModel;
  envelope: SessionEnvelopeModel;
  calibrationMeasured: boolean;
  deviceMountCheck: DeviceMountModeCheck | null;
  moon: { illuminatedFraction: number; separationDeg: number | null } | null;
}) {
  return (
    <Card className="space-y-4">
      <CardTitle>Framing</CardTitle>

      {!calibrationMeasured && (
        <p className="rounded-md border border-warning bg-surface-raised px-2 py-1.5 text-xs text-warning">
          Uncalibrated: this overlay is datasheet-derived, not from a plate solve.
        </p>
      )}
      {geometry.distortion === "approximate" && (
        <p className="rounded-md border border-warning bg-surface-raised px-2 py-1.5 text-xs text-warning">
          {geometry.label}: indicative only — barrel distortion is not modelled.
        </p>
      )}

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-fg-muted">Field of view</dt>
        <dd className="text-fg tabular-nums">
          {geometry.width_deg.toFixed(2)}° × {geometry.height_deg.toFixed(2)}°
        </dd>
        <dt className="text-fg-muted">Inscribed circle</dt>
        <dd className="text-fg tabular-nums">{geometry.inscribed_circle_deg.toFixed(2)}°</dd>
        <dt className="text-fg-muted">Pixel scale</dt>
        <dd className="text-fg tabular-nums">{geometry.pixel_scale_arcsec.toFixed(2)}″/px</dd>
      </dl>

      {deviceMountCheck?.checked === true && deviceMountCheck.conflict && (
        <p className="rounded-md border border-warning bg-surface-raised px-2 py-1.5 text-xs text-warning">
          {deviceMountCheck.message}
        </p>
      )}

      {mount === "altaz" && (
        <div className="space-y-2 border-t border-border pt-3">
          <div className="flex items-center justify-between text-sm">
            <span className="text-fg-muted">Total rotation</span>
            <Badge tone={SEVERITY_TONE[envelope.rotation_severity]}>{envelope.total_rotation_deg.toFixed(1)}°</Badge>
          </div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            {/* Area, not minimum diameter. The minimum diameter is the frame's
                short side both at zero rotation and at the inscribed-circle
                limit, so it is constant for a given optic however long the
                session runs — it cannot show that a longer session costs sky.
                Area does. The diameter is still shown, as the floor to quote. */}
            <dt className="text-fg-muted">Frame kept</dt>
            <dd className="text-fg tabular-nums">
              {(envelope.keep_area_fraction * 100).toFixed(0)}% of the frame
              {envelope.keep_area_fraction < 0.5 && (
                <span className="ml-1 text-danger">— over half the frame is lost to rotation</span>
              )}
            </dd>
            <dt className="text-fg-muted">Guaranteed-keep region</dt>
            <dd className="text-fg tabular-nums">
              {envelope.keep_region_min_diameter_deg.toFixed(2)}° min diameter
              {envelope.keep_region_min_diameter_deg < geometry.inscribed_circle_deg && (
                <span className="ml-1 text-danger">— below the {geometry.inscribed_circle_deg.toFixed(2)}° inscribed-circle bound</span>
              )}
            </dd>
            <dt className="text-fg-muted">Peak rotation rate</dt>
            <dd className="text-fg tabular-nums">{envelope.max_abs_rotation_rate_deg_per_hour.toFixed(1)}°/h</dd>
          </dl>

          {envelope.high_altitude_transit && (
            <p className="rounded-md border border-warning bg-surface-raised px-2 py-1.5 text-xs text-warning">
              Rotation-spike risk: this target transits above ~70° altitude, where parallactic angle changes fastest.
            </p>
          )}
          {envelope.zenith_singularity && (
            <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-xs text-danger">
              Near-zenith pass: position angle is singular at the zenith itself — expect a very fast swing.
            </p>
          )}
          {envelope.warnings.map((warning) => (
            <p key={warning} className="rounded-md border border-warning bg-surface-raised px-2 py-1.5 text-xs text-warning">
              {warning}
            </p>
          ))}
        </div>
      )}

      {moon !== null && (
        <div className="space-y-1 border-t border-border pt-3 text-sm">
          <div className="flex items-center justify-between">
            <span className="text-fg-muted">Moon illumination</span>
            <span className="text-fg tabular-nums">{(moon.illuminatedFraction * 100).toFixed(0)}%</span>
          </div>
          {moon.separationDeg !== null && (
            <div className="flex items-center justify-between">
              <span className="text-fg-muted">Moon separation</span>
              <span className={`tabular-nums ${moon.separationDeg < 30 ? "text-warning" : "text-fg"}`}>
                {moon.separationDeg.toFixed(1)}°
              </span>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
