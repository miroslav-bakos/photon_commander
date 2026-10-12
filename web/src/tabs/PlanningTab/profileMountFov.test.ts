// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { ProfileSummary } from "../../api/astro";
import { profileMountFov } from "./profileMountFov";

const profile: ProfileSummary = {
  id: "zwo-s30pro",
  name: "ZWO Seestar S30 Pro",
  vendor: "ZWO",
  mount: "altaz",
  eq_mode_available: true,
  aliases: ["S30 Pro"],
  source_urls: [],
  capabilities: {
    tele_camera: true,
    wide_camera: true,
    wide_camera_autofocus: true,
    focuser: true,
    filter_wheel: true,
    dew_heater: true,
  },
  defaults: {
    gain: 80,
    exposure_stack_ms: 10000,
    exposure_preview_ms: 500,
    dither_enabled: true,
    dither_pixels: 50,
    dither_every_n_frames: 10,
    lp_filter: false,
    dew_heater_percent: 0,
  },
  limits: {
    min_altitude_deg: 20,
    battery_low_percent: 15,
    max_exposure_altaz_s: null,
    max_exposure_eq_s: null,
  },
  calibration: {
    measured: false,
    pixel_scale_arcsec: null,
    pa_offset_deg: null,
    boresight_offset_arcmin: null,
    solved_at: null,
    solve_rms_arcsec: null,
  },
  optics: [{
    id: "tele",
    label: "Telephoto",
    focal_length_mm: 160,
    aperture_mm: 30,
    focal_ratio: 5.3,
    design: "quadruplet APO (ED)",
    distortion: "rectilinear",
    sensor: { name: "IMX585", w_px: 2160, h_px: 3840, pixel_um: 2.9 },
  }],
};

describe("profileMountFov", () => {
  it("returns the S30 Pro tele field in its real portrait orientation", () => {
    const field = profileMountFov(profile, "tele");
    expect(field?.widthDeg).toBeCloseTo(2.2431, 3);
    expect(field?.heightDeg).toBeCloseTo(3.9878, 3);
    expect(field?.heightDeg).toBeGreaterThan(field?.widthDeg ?? Number.POSITIVE_INFINITY);
    expect(field?.rotationDeg).toBe(0);
  });

  it("uses a measured profile pixel scale when available", () => {
    const measured = {
      ...profile,
      calibration: { ...profile.calibration, measured: true, pixel_scale_arcsec: 3.672 },
    };
    const field = profileMountFov(measured, "tele");
    expect(field?.widthDeg).toBeCloseTo(2.2032, 6);
    expect(field?.heightDeg).toBeCloseTo(3.9168, 6);
  });

  it("returns null instead of inventing geometry for an unknown optic", () => {
    expect(profileMountFov(profile, "missing")).toBeNull();
  });
});
