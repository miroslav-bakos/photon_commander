// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SettingsTab } from "./SettingsTab";

vi.mock("./DeviceConnectionPanel", () => ({ DeviceConnectionPanel: () => <p>Connection contents</p> }));
vi.mock("./PlanningEquipmentPanel", () => ({ PlanningEquipmentPanel: () => <p>Planning equipment contents</p> }));
vi.mock("./LocationPanel", () => ({ LocationPanel: () => <p>Location contents</p> }));
vi.mock("./HorizonPanel", () => ({ HorizonPanel: () => <p>Horizon contents</p> }));
vi.mock("./CaptureFolderPanel", () => ({ CaptureFolderPanel: () => <p>Capture contents</p> }));
vi.mock("./WeatherPanel", () => ({ WeatherPanel: () => <p>Weather contents</p> }));
vi.mock("./OfflineMapsPanel", () => ({ OfflineMapsPanel: () => <p>Cache contents</p> }));
vi.mock("./SkymapAutofetchPanel", () => ({ SkymapAutofetchPanel: () => <p>Imagery contents</p> }));
vi.mock("./ObjectInfoSettingsPanel", () => ({ ObjectInfoSettingsPanel: () => <p>Object contents</p> }));
vi.mock("./AppearancePanel", () => ({ AppearancePanel: () => <p>Appearance contents</p> }));
vi.mock("./AboutPanel", () => ({ AboutPanel: () => <p>About contents</p> }));
vi.mock("./ExternalToolsPanel", () => ({ ExternalToolsPanel: () => <p>External tools contents</p> }));
vi.mock("./RemoteAccessPanel", () => ({ RemoteAccessPanel: () => <p>Remote access contents</p> }));
vi.mock("./ExportImportPanel", () => ({ ExportImportPanel: () => <p>Export contents</p> }));

afterEach(() => {
  cleanup();
});

describe("SettingsTab", () => {
  it("uses responsive masonry columns so expanded cards do not leave row gaps", () => {
    render(<SettingsTab />);
    expect(screen.getByTestId("settings-columns")).toHaveClass(
      "columns-1",
      "lg:columns-2",
    );
    expect(screen.getByRole("button", { name: "Equipment" }).closest(".rounded-lg")).toHaveClass(
      "inline-block",
      "w-full",
      "break-inside-avoid",
    );
  });

  it("starts as a concise index of intuitively named collapsed blocks", () => {
    render(<SettingsTab />);
    const collapsedNames = [
      "Equipment",
      "Observing site",
      "Capture files",
      "Broadcasting",
      "Weather",
      "Sky map & cache",
      "Appearance",
      "Remote access",
      "Integrations & tools",
      "Export & import",
    ];
    for (const name of collapsedNames) {
      expect(screen.getByRole("button", { name })).toHaveAttribute("aria-expanded", "false");
    }
    expect(screen.getByRole("button", { name: "About Photon Commander" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("About contents")).toBeInTheDocument();
    expect(screen.queryByText("Capture contents")).not.toBeInTheDocument();
  });

  it("mounts a block's settings when it is expanded", () => {
    render(<SettingsTab />);
    fireEvent.click(screen.getByRole("button", { name: "Capture files" }));
    expect(screen.getByRole("button", { name: "Capture files" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("Capture contents")).toBeInTheDocument();
  });

  it("groups connection and Planning defaults under Equipment", () => {
    render(<SettingsTab />);
    fireEvent.click(screen.getByRole("button", { name: "Equipment" }));
    expect(screen.getByText("Connection contents")).toBeInTheDocument();
    expect(screen.getByText("Planning equipment contents")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Telescope connection" })).not.toBeInTheDocument();
  });

  it("does not expose pairing controls while they are on hold", () => {
    render(<SettingsTab />);
    fireEvent.click(screen.getByRole("button", { name: "Appearance" }));
    expect(screen.getByText("Appearance contents")).toBeInTheDocument();
    expect(screen.queryByText("Access contents")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /pair/i })).not.toBeInTheDocument();
  });
});
