// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ProfileSummary } from "../../api/astro";
import { SessionControls } from "./SessionControls";

const BASE_PROFILE: ProfileSummary = {
  id: "zwo-s30",
  name: "Seestar S30",
  vendor: "ZWO",
  mount: "altaz",
  eq_mode_available: true,
  aliases: [],
  source_urls: [],
  capabilities: { tele_camera: true, wide_camera: false, wide_camera_autofocus: false, focuser: true, filter_wheel: true, dew_heater: true },
  defaults: { gain: 80, exposure_stack_ms: 10000, exposure_preview_ms: 500, dither_enabled: true, dither_pixels: 50, dither_every_n_frames: 10, lp_filter: false, dew_heater_percent: 0 },
  limits: { min_altitude_deg: 20, battery_low_percent: 15, max_exposure_altaz_s: null, max_exposure_eq_s: null },
  calibration: { measured: false, pixel_scale_arcsec: null, pa_offset_deg: null, boresight_offset_arcmin: null, solved_at: null, solve_rms_arcsec: null },
  optics: [{ id: "tele", label: "Telephoto", focal_length_mm: 150, aperture_mm: 30, focal_ratio: 5, design: "APO", distortion: "rectilinear", sensor: { name: "sensor", w_px: 1920, h_px: 1080, pixel_um: 2.9 } }],
};

describe("SessionControls", () => {
  it("opens a map-only FOV profile list beside mount mode", () => {
    const onProfileChange = vi.fn();
    render(
      <SessionControls
        profiles={[BASE_PROFILE, { ...BASE_PROFILE, id: "zwo-s50", name: "Seestar S50" }]}
        profileId="zwo-s30"
        mount="eq"
        mosaicPanel={<p>Mosaic controls</p>}
        onProfileChange={onProfileChange}
        onMountChange={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: /planning equipment settings/i })).not.toBeInTheDocument();
    const chooser = screen.getByRole("button", { name: /choose skymap fov profile/i });
    expect(chooser).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(chooser);

    const list = screen.getByRole("listbox", { name: /skymap fov profiles/i });
    expect(chooser).toHaveAttribute("aria-expanded", "true");
    expect(within(list).getByRole("option", { name: /seestar s30/i })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(within(list).getByRole("option", { name: /seestar s50/i }));
    expect(onProfileChange).toHaveBeenCalledWith("zwo-s50");
    expect(screen.queryByRole("listbox", { name: /skymap fov profiles/i })).not.toBeInTheDocument();
  });
});


it("selects the map lens independently of the hardware profile", () => {
  const onOpticChange = vi.fn();
  const onProfileChange = vi.fn();
  const tele = BASE_PROFILE.optics[0];
  if (tele === undefined) throw new Error("Missing tele fixture");
  const profile = { ...BASE_PROFILE, optics: [...BASE_PROFILE.optics, { ...tele, id: "wide", label: "Wide" }] };
  render(<SessionControls profiles={[profile]} profileId={profile.id} mount="eq" opticId="tele"
    mosaicPanel={null} onMountChange={vi.fn()} onProfileChange={onProfileChange} onOpticChange={onOpticChange} />);
  expect(screen.getByRole("radio", { name: "Tele" })).toHaveAttribute("aria-checked", "true");
  fireEvent.click(screen.getByRole("radio", { name: "Wide" }));
  expect(onOpticChange).toHaveBeenCalledWith("wide");
  expect(onProfileChange).not.toHaveBeenCalled();
});

it("moves focus into the nonmodal mosaic popover and restores it on Escape", () => {
  render(<SessionControls profiles={[BASE_PROFILE]} profileId={BASE_PROFILE.id} mount="eq"
    mosaicPanel={<input aria-label="Panel overlap" />} onMountChange={vi.fn()} onProfileChange={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: "Configure mosaic" });
  fireEvent.click(trigger);
  expect(screen.getByRole("button", { name: "Close mosaic configuration" })).toHaveFocus();
  const dialog = screen.getByRole("dialog", { name: "Mosaic configuration" });
  expect(dialog).not.toHaveAttribute("aria-modal", "true");
  const input = screen.getByRole("textbox", { name: "Panel overlap" });
  input.focus();
  fireEvent.keyDown(input, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it("supports focused profile navigation and restores the chooser after selection or Escape", () => {
  const onProfileChange = vi.fn();
  render(<SessionControls profiles={[BASE_PROFILE, { ...BASE_PROFILE, id: "second", name: "Second" }]}
    profileId={BASE_PROFILE.id} mount="eq" mosaicPanel={null}
    onMountChange={vi.fn()} onProfileChange={onProfileChange} />);
  const trigger = screen.getByRole("button", { name: "Choose skymap FOV profile" });
  fireEvent.click(trigger);
  const first = screen.getByRole("option", { name: "Seestar S30" });
  const second = screen.getByRole("option", { name: "Second" });
  expect(first).toHaveFocus();
  fireEvent.keyDown(first, { key: "ArrowDown" });
  expect(second).toHaveFocus();
  fireEvent.keyDown(second, { key: "ArrowDown" });
  expect(first).toHaveFocus();
  fireEvent.keyDown(first, { key: "End" });
  expect(second).toHaveFocus();
  fireEvent.click(second);
  expect(onProfileChange).toHaveBeenCalledWith("second");
  expect(trigger).toHaveFocus();
  fireEvent.click(trigger);
  fireEvent.keyDown(screen.getByRole("option", { name: "Seestar S30" }), { key: "Escape" });
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it("renders named icon choices and preserves mount selection", () => {
  const onMountChange = vi.fn();
  render(<SessionControls profiles={[BASE_PROFILE]} profileId={BASE_PROFILE.id} mount="eq"
    opticId="tele" onOpticChange={vi.fn()} mosaicPanel={null}
    onMountChange={onMountChange} onProfileChange={vi.fn()} />);
  for (const name of ["ALT/AZ", "EQ", "Tele"]) {
    const choice = screen.getByRole("radio", { name });
    expect(choice).toHaveTextContent("");
    expect(choice.querySelector('svg[aria-hidden="true"]')).not.toBeNull();
  }
  fireEvent.click(screen.getByRole("radio", { name: "ALT/AZ" }));
  expect(onMountChange).toHaveBeenCalledWith("altaz");
});
