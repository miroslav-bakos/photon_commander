// SPDX-License-Identifier: GPL-3.0-only
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { readClientConfiguration } from "../api/settings";
import { SetupOffer } from "./SetupOffer";
vi.mock("../api/settings", () => ({ readClientConfiguration: vi.fn() }));
vi.mock("../tabs/Setup/SetupTab", () => ({ SETUP_STORAGE_KEY: "ssc.setup.v1", SetupLink: () => <a href="#/setup">Open setup</a> }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
function show(path = "/dashboard") { render(<MemoryRouter initialEntries={[path]}><SetupOffer /></MemoryRouter>); }
describe("first-run setup offer", () => {
 it("ignores a late new-preferences response after entering setup", async () => {
  let resolve!: (value: { entries: Record<string, string> }) => void;
  vi.mocked(readClientConfiguration).mockReturnValue(new Promise((done) => { resolve = done; }));
  function Navigation() {
   const navigate = useNavigate();
   return <><button onClick={() => { void navigate("/setup"); }}>Enter</button><button onClick={() => { void navigate("/dashboard"); }}>Return</button><SetupOffer /></>;
  }
  render(<MemoryRouter><Navigation /></MemoryRouter>);
  fireEvent.click(screen.getByText("Enter"));
  await act(async () => { resolve({ entries: {} }); await Promise.resolve(); });
  fireEvent.click(screen.getByText("Return"));
  expect(screen.queryByText("Open setup")).not.toBeInTheDocument();
 });
 it("offers setup for known new preferences and can be dismissed without redirect", async () => {
  vi.mocked(readClientConfiguration).mockResolvedValue({ entries: {} }); show();
  expect(await screen.findByText("Open setup")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Continue without the guide" }));
  expect(screen.queryByText("Open setup")).not.toBeInTheDocument();
 });
 it("does not interrupt restored setup progress", async () => {
  vi.mocked(readClientConfiguration).mockResolvedValue({ entries: { "ssc.setup.v1": '{"step":4,"reviewed":[0]}' } }); show();
  await waitFor(() => { expect(readClientConfiguration).toHaveBeenCalled(); });
  expect(screen.queryByText("Open setup")).not.toBeInTheDocument();
 });
 it("does not claim a first run when server preferences are unavailable", async () => {
  vi.mocked(readClientConfiguration).mockRejectedValue(new Error("offline")); show();
  await waitFor(() => { expect(readClientConfiguration).toHaveBeenCalled(); });
  expect(screen.queryByText("Open setup")).not.toBeInTheDocument();
 });
});
