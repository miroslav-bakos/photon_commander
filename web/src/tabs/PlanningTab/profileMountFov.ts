// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { ProfileSummary } from "../../api/astro";
import type { MountFovDeg } from "../../skymap/mountPointing";

const ARCSEC_PER_RADIAN_PER_MM = 206.265;
const ARCSEC_PER_DEG = 3600;

/** The selected Planning optic's field when Alpaca cannot describe it. */
export function profileMountFov(profile: ProfileSummary | null, opticId: string | null): MountFovDeg | null {
  if (profile === null || opticId === null) return null;
  const optic = profile.optics.find((candidate) => candidate.id === opticId);
  if (optic === undefined) return null;

  // Mirrors `ssc.astro.framing`: calibrated pixel scale wins over the
  // sensor-pitch/focal-length baseline. Rotation remains neutral until a
  // live plate solve measures it.
  const configuredScale = ARCSEC_PER_RADIAN_PER_MM * optic.sensor.pixel_um / optic.focal_length_mm;
  const pixelScale =
    profile.calibration.measured && profile.calibration.pixel_scale_arcsec !== null
      ? profile.calibration.pixel_scale_arcsec
      : configuredScale;
  if (!Number.isFinite(pixelScale) || pixelScale <= 0) return null;

  return {
    widthDeg: optic.sensor.w_px * pixelScale / ARCSEC_PER_DEG,
    heightDeg: optic.sensor.h_px * pixelScale / ARCSEC_PER_DEG,
    rotationDeg: 0,
  };
}
