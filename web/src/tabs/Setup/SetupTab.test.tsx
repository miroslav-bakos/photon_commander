// SPDX-License-Identifier: GPL-3.0-only
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SETUP_STORAGE_KEY, SetupTab } from "./SetupTab";
vi.mock("../../i18n/LanguagePicker", () => ({ LanguagePicker: () => <p>Language choices</p> }));
vi.mock("../Capture/AlpacaDevicesPanel", () => ({ AlpacaDevicesPanel: () => <p>Reported device status</p> }));
vi.mock("../SettingsTab/DeviceConnectionPanel", () => ({ DeviceConnectionPanel: () => <button type="button">Connect explicitly</button> }));
vi.mock("../SettingsTab/PlanningEquipmentPanel", () => ({ PlanningEquipmentPanel: () => <p>Equipment choices</p> }));
vi.mock("../SettingsTab/LocationPanel", () => ({ LocationPanel: () => <p>Site</p> }));
vi.mock("../SettingsTab/HorizonPanel", () => ({ HorizonPanel: () => <p>Horizon</p> }));
vi.mock("../SettingsTab/CaptureFolderPanel", () => ({ CaptureFolderPanel: () => <p>Capture folders</p> }));
vi.mock("../SettingsTab/ExternalToolsPanel", () => ({ ExternalToolsPanel: () => <p>Solver status</p> }));
beforeEach(() => {
 const values = new Map<string, string>();
 vi.stubGlobal("localStorage", { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } });
});
afterEach(() => { cleanup(); localStorage.removeItem(SETUP_STORAGE_KEY); });
function show() { return render(<MemoryRouter><SetupTab /></MemoryRouter>); }
describe("Guided setup review", () => {
 it("offers both Windows installers immediately after language selection", () => {
  show();
  fireEvent.change(screen.getByRole("combobox", { name: "Computer running Photon" }), { target: { value: "windows" } });
  expect(screen.getByRole("link", { name: "1. Download ASTAP" })).toHaveAttribute("href", "https://sourceforge.net/projects/astap-program/files/windows_installer/astap_setup.exe/download");
  expect(screen.getByRole("link", { name: "2. Download D20 star database" })).toHaveAttribute("href", "https://sourceforge.net/projects/astap-program/files/star_databases/d20_star_database.exe/download");
  expect(screen.getByRole("link", { name: "1. Download ASTAP" })).toHaveAttribute("target", "_blank");
  expect(screen.getByText(/not on the phone/)).toBeInTheDocument();
  expect(screen.getByText(/Compatibility may be lost permanently/)).toBeInTheDocument();
 });
 it("keeps skipped steps incomplete and resumes the current step", () => {
  const view = show();
  expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
  expect(screen.getByRole("heading", { level: 2 })).toHaveFocus();
  view.unmount(); show();
  expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("2/8");
  for (let i = 1; i < 8; i++) fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
  expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Setup review remains incomplete");
 });
 it("records acknowledgement separately from panel actions", () => {
  localStorage.setItem(SETUP_STORAGE_KEY, JSON.stringify({ step: 3, reviewed: [] }));
  show();
  expect(screen.getByRole("button", { name: "Connect explicitly" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(JSON.parse(localStorage.getItem(SETUP_STORAGE_KEY) ?? "null")).toEqual({ step: 4, reviewed: [3] });
 });
 it("recovers from malformed storage", () => {
  localStorage.setItem(SETUP_STORAGE_KEY, "invalid JSON"); show();
  expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("1/8");
 });
 it("does not treat duplicate stored acknowledgements as completion", () => {
  localStorage.setItem(SETUP_STORAGE_KEY, JSON.stringify({ step: 8, reviewed: Array(8).fill(0) }));
  show(); expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Setup review remains incomplete");
 });
});
