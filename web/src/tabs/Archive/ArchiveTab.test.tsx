// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ArchiveEntry } from "../../api/capture";
import { renderWithQueryClient } from "../../test/queryClient";
import { ArchiveTab } from "./ArchiveTab";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function entry(overrides: Partial<ArchiveEntry> = {}): ArchiveEntry {
  return {
    key: "M31/lights/light-001.fit",
    name: "light-001.fit",
    directory: "M31/lights",
    kind: "fits",
    size_bytes: 2_000_000,
    modified_at: "2026-09-12T21:00:00Z",
    date_obs: "2026-09-12T20:00:00Z",
    date_source: "fits_header",
    catalogue_id: null,
    object_name: "M31",
    filter_name: "LP",
    frame_type: "Light",
    exposure_s: 10,
    gain: 200,
    ccd_temp_c: 12,
    width: 3840,
    height: 2160,
    bayer_pattern: "GRBG",
    preview_available: true,
    ...overrides,
  };
}

function stubArchive(entries: ArchiveEntry[], directories = ["M31", "M31/lights", "empty"]) {
  const requests: { path: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: string | Request, init?: RequestInit) => {
    if (input instanceof Request) init = { method: input.method, headers: input.headers, body: await input.clone().text() };
    const url = new URL(input instanceof Request ? input.url : input, "http://localhost");
    if (url.pathname === "/api/capture/archive") return Promise.resolve(Response.json({ root_name: "Photon", storage_total_bytes: 100_000_000, storage_free_bytes: 75_000_000, entries, directories, truncated: false }));
    if (url.pathname === "/api/capture/calibration/masters") return Promise.resolve(Response.json({ masters: [] }));
    if (url.pathname === "/api/capture/archive/delete-files" || url.pathname === "/api/capture/archive/delete-folder") {
      requests.push({ path: url.pathname, body: JSON.parse(typeof init?.body === "string" ? init.body : "null") as unknown });
      return Promise.resolve(Response.json({ files_deleted: 1, catalogue_rows_deleted: 1, bytes_deleted: 2_000_000, folder_deleted: url.pathname.endsWith("folder") ? "M31/lights" : null }));
    }
    if (url.pathname === "/api/capture/archive/transfer") {
      const body = JSON.parse(typeof init?.body === "string" ? init.body : "null") as { keys: string[]; directories: string[]; destination: string; mode: "move" | "copy" };
      requests.push({ path: url.pathname, body });
      return Promise.resolve(Response.json({ files_transferred: body.keys.length, directories_transferred: body.directories.length, bytes_transferred: 2_000_000, catalogue_rows_updated: body.mode === "move" ? 1 : 0, destination: body.destination, mode: body.mode }));
    }
    if (url.pathname === "/api/capture/archive/selection-zip") {
      requests.push({ path: url.pathname, body: JSON.parse(typeof init?.body === "string" ? init.body : "null") as unknown });
      return Promise.resolve(new Response(new Uint8Array([0x7a, 0x69, 0x70]), { headers: { "Content-Type": "application/zip", "Content-Disposition": "attachment; filename=Photon-selection.zip" } }));
    }
    if (url.pathname === "/api/capture/archive/header") return Promise.resolve(Response.json({ SIMPLE: true, OBJECT: "M31" }));
    if (url.pathname === "/api/capture/archive/quality") return Promise.resolve(Response.json({ star_count: 42, hfr_px: 1.54, fwhm_px: 3.51, hfr_arcsec: null, fwhm_arcsec: null, background: 100, noise: 4 }));
    return Promise.resolve(Response.json({ detail: "unexpected request" }, { status: 500 }));
  }));
  return requests;
}

async function loadedExplorer(): Promise<HTMLElement> {
  const table = screen.getByRole("table", { name: "Archive files" });
  await waitFor(() => { expect(within(table).getByRole("button", { name: "Open M31 folder contents" })).toBeInTheDocument(); });
  return table;
}

async function openLights(): Promise<void> {
  const table = await loadedExplorer();
  fireEvent.click(within(table).getByRole("button", { name: "Open M31 folder contents" }));
  fireEvent.click(screen.getByRole("button", { name: "Open lights folder contents" }));
}

describe("ArchiveTab", () => {
  it("uses one full-width explorer card with folders and files", async () => {
    stubArchive([entry()]);
    renderWithQueryClient(<ArchiveTab />);

    const table = await loadedExplorer();
    expect(within(table).getByRole("button", { name: "Open empty folder contents" })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "Open M31 folder contents" }).querySelector("[data-folder-icon]")).toHaveClass("text-accent");
    expect(within(table).queryByRole("button", { name: "Open parent folder" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Browse" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Details" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Calibration masters" })).not.toBeInTheDocument();
    expect(document.querySelector("[data-panes]")).not.toBeInTheDocument();
    expect(within(table).getByRole("checkbox", { name: "Select folder M31" })).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Capture storage used" })).toHaveAttribute("aria-valuenow", "25");
    expect(screen.getByText("23.8 MiB used")).toBeInTheDocument();
    expect(document.querySelector("[data-archive-storage]")).toHaveClass("order-last", "mx-auto", "w-full", "sm:w-[30vw]");
    expect(document.querySelector("[data-archive-storage]")).not.toHaveClass("mt-auto");
    expect(document.querySelector("[data-archive-storage]")).not.toHaveClass("border", "bg-surface");
  });

  it("downloads mixed selected files and recursive folders from the Actions menu", async () => {
    const requests = stubArchive([entry(), entry({ key: "preview.png", name: "preview.png", directory: ".", kind: "image" })]);
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const createObjectUrl = vi.spyOn(globalThis.URL, "createObjectURL").mockReturnValue("blob:archive-selection");
    const revokeObjectUrl = vi.spyOn(globalThis.URL, "revokeObjectURL").mockImplementation(() => undefined);
    renderWithQueryClient(<ArchiveTab />);
    await loadedExplorer();

    fireEvent.click(screen.getByRole("button", { name: "Select all" }));
    expect(screen.getByText("Actions (3)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select folder M31" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select preview.png" }));
    fireEvent.click(screen.getByText("Actions (2)"));
    fireEvent.click(screen.getByRole("button", { name: "Download a ZIP" }));

    await waitFor(() => { expect(requests).toContainEqual({ path: "/api/capture/archive/selection-zip", body: { keys: ["preview.png"], directories: ["M31"] } }); });
    await waitFor(() => { expect(click).toHaveBeenCalledOnce(); });
    expect(createObjectUrl).toHaveBeenCalledOnce();
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:archive-selection");
    expect(screen.queryByText("Download folder ZIP")).not.toBeInTheDocument();
  });

  it("navigates to parent and capture home from the explorer toolbar", async () => {
    stubArchive([entry()]);
    renderWithQueryClient(<ArchiveTab />);

    await openLights();
    expect(screen.queryByRole("button", { name: "Delete this folder" })).not.toBeInTheDocument();
    expect(screen.getByText("light-001.fit")).toBeInTheDocument();
    expect(screen.getByText("Photon/M31/lights")).toBeInTheDocument();
    const parent = screen.getByRole("button", { name: "Open parent folder" });
    expect(parent).toHaveTextContent("..");
    expect(parent.closest("tr")).toBe(screen.getByRole("table", { name: "Archive files" }).querySelector("tbody tr:first-child"));
    expect(screen.queryByRole("button", { name: "Go to parent folder" })).not.toBeInTheDocument();
    fireEvent.click(parent);
    expect(screen.getByText("Photon/M31")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open capture home folder" }));
    expect(screen.getAllByText("Photon").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Open capture home folder" })).toBeDisabled();
  });

  it("opens and closes a fullscreen file preview", async () => {
    stubArchive([entry()]);
    renderWithQueryClient(<ArchiveTab />);
    await openLights();
    fireEvent.doubleClick(screen.getByRole("button", { name: "Open light-001.fit" }));

    expect(screen.getByRole("img", { name: "light-001.fit" })).toHaveAttribute("src", expect.stringContaining("key=M31%2Flights%2Flight-001.fit"));
    expect(screen.getByRole("dialog", { name: "Preview light-001.fit" })).toBeInTheDocument();
    const details = screen.getByRole("complementary", { name: "FITS details" });
    await waitFor(() => { expect(within(details).getByText("1.54 px")).toBeInTheDocument(); });
    expect(within(details).getByText("3.51 px")).toBeInTheDocument();
    expect(within(details).getByText("42")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Hide FITS details" }));
    expect(screen.queryByRole("complementary", { name: "FITS details" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show FITS details" }));
    expect(screen.getByRole("complementary", { name: "FITS details" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close preview" }));
    expect(screen.queryByRole("dialog", { name: "Preview light-001.fit" })).not.toBeInTheDocument();
  });

  it("requires explicit confirmation before deleting selected files", async () => {
    const requests = stubArchive([entry()]);
    renderWithQueryClient(<ArchiveTab />);
    await openLights();

    fireEvent.click(screen.getByRole("checkbox", { name: "Select light-001.fit" }));
    fireEvent.click(screen.getByText("Actions (1)"));
    const actionsMenu = screen.getByText("Actions (1)").closest("details");
    expect(actionsMenu).toHaveAttribute("open");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(actionsMenu).not.toHaveAttribute("open");
    expect(requests).toHaveLength(0);
    expect(screen.getByText(/This cannot be undone/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));

    await waitFor(() => { expect(requests).toEqual([{ path: "/api/capture/archive/delete-files", body: { keys: ["M31/lights/light-001.fit"] } }]); });
  });

  it("uses a fresh command after a busy delete fails", async () => {
    const commandKeys: string[] = [];
    let deleteAttempts = 0;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: string | Request, init?: RequestInit) => {
    if (input instanceof Request) init = { method: input.method, headers: input.headers, body: await input.clone().text() };
      const url = new URL(input instanceof Request ? input.url : input, "http://localhost");
      if (url.pathname === "/api/capture/archive") return Promise.resolve(Response.json({ root_name: "Photon", storage_total_bytes: 100_000_000, storage_free_bytes: 75_000_000, entries: [entry()], directories: ["M31", "M31/lights"], truncated: false }));
      if (url.pathname === "/api/capture/archive/delete-files") {
        commandKeys.push(new Headers(init?.headers).get("Idempotency-Key") ?? "");
        deleteAttempts += 1;
        if (deleteAttempts === 1) return Promise.resolve(Response.json({ id: "busy", kind: "archive_delete", status: "failed", message: "Archive deletion failed", error: "ArchiveDeleteBusyError: archive files cannot be deleted while live stacking is running", result: null }));
        return Promise.resolve(Response.json({ files_deleted: 1, catalogue_rows_deleted: 0, bytes_deleted: 2_000_000, folder_deleted: null }));
      }
      return Promise.resolve(Response.json({ detail: "unexpected request" }, { status: 500 }));
    }));
    renderWithQueryClient(<ArchiveTab />);
    await openLights();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select light-001.fit" }));
    fireEvent.click(screen.getByText("Actions (1)"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));
    expect(await screen.findByText(/ArchiveDeleteBusyError/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));

    await waitFor(() => { expect(commandKeys).toHaveLength(2); });
    expect(commandKeys[0]).not.toBe("");
    expect(commandKeys[1]).not.toBe(commandKeys[0]);
  });

  it("deletes a selected folder only through the Actions menu", async () => {
    const requests = stubArchive([entry()]);
    renderWithQueryClient(<ArchiveTab />);
    await loadedExplorer();

    expect(screen.queryByRole("button", { name: "Delete this folder" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select folder M31" }));
    fireEvent.click(screen.getByText("Actions (1)"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByText(/Permanently delete 1 selected item/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));

    await waitFor(() => { expect(requests).toEqual([{ path: "/api/capture/archive/delete-folder", body: { directory: "M31" } }]); });
  });

  it("sorts explorer columns and navigates images in the fullscreen viewer", async () => {
    stubArchive([
      entry(),
      entry({ key: "M31/lights/light-002.png", name: "light-002.png", kind: "image", date_obs: "2026-09-13T20:00:00Z", modified_at: "2026-09-14T20:00:00Z" }),
    ]);
    renderWithQueryClient(<ArchiveTab />);
    await openLights();

    const table = screen.getByRole("table", { name: "Archive files" });
    expect(within(table).getByRole("columnheader", { name: /Name/ })).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(within(table).getByRole("button", { name: /Date taken/ }));
    expect(within(table).getByRole("columnheader", { name: /Date taken/ })).toHaveAttribute("aria-sort", "ascending");

    fireEvent.doubleClick(screen.getByRole("button", { name: "Open light-001.fit" }));
    fireEvent.click(screen.getByRole("button", { name: "Next image" }));
    expect(screen.getByRole("dialog", { name: "Preview light-002.png" })).toBeInTheDocument();
  });

  it("moves selected rows through the actions menu", async () => {
    const requests = stubArchive([entry()]);
    renderWithQueryClient(<ArchiveTab />);
    await openLights();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select light-001.fit" }));
    fireEvent.click(screen.getByText("Actions (1)"));
    fireEvent.click(screen.getByRole("button", { name: /Move/ }));
    fireEvent.change(screen.getByRole("combobox", { name: "Destination folder" }), { target: { value: "empty" } });
    fireEvent.click(screen.getByRole("button", { name: "Move here" }));

    await waitFor(() => { expect(requests).toContainEqual({ path: "/api/capture/archive/transfer", body: { keys: ["M31/lights/light-001.fit"], directories: [], destination: "empty", mode: "move" } }); });
  });
});


it("keeps checkbox taps and scrolling from opening a preview", async () => {
  stubArchive([entry()]);
  renderWithQueryClient(<ArchiveTab />);
  await openLights();
  const checkbox = screen.getByRole("checkbox", { name: "Select light-001.fit" });
  for (let i = 0; i < 2; i++) {
    fireEvent.pointerDown(checkbox, { pointerType: "touch", clientX: 10, clientY: 10 });
    fireEvent.pointerUp(checkbox, { pointerType: "touch", clientX: 10, clientY: 10 });
    fireEvent.click(checkbox);
  }
  fireEvent.doubleClick(checkbox);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  const file = screen.getByRole("button", { name: "Open light-001.fit" });
  fireEvent.pointerDown(file, { pointerType: "touch", clientX: 100, clientY: 100 });
  fireEvent.pointerMove(file, { pointerType: "touch", clientX: 100, clientY: 150 });
  fireEvent.pointerUp(file, { pointerType: "touch", clientX: 100, clientY: 150 });
  fireEvent.pointerDown(file, { pointerType: "touch", clientX: 100, clientY: 100 });
  fireEvent.pointerUp(file, { pointerType: "touch", clientX: 100, clientY: 100 });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.pointerDown(file, { pointerType: "touch", clientX: 100, clientY: 100 });
  fireEvent.pointerUp(file, { pointerType: "touch", clientX: 100, clientY: 100 });
  expect(screen.getByRole("dialog", { name: "Preview light-001.fit" })).toBeInTheDocument();
  expect(document.body.style.overflow).toBe("hidden");
  fireEvent.click(screen.getByRole("button", { name: "Close preview" }));
  expect(document.body.style.overflow).toBe("");
});

it("keeps fullscreen preview focus inside and restores the file trigger after Escape", async () => {
  stubArchive([entry()]);
  renderWithQueryClient(<ArchiveTab />);
  await openLights();
  const trigger = screen.getByRole("button", { name: "Open light-001.fit" });
  trigger.focus();
  fireEvent.doubleClick(trigger);
  const dialog = screen.getByRole("dialog", { name: "Preview light-001.fit" });
  const close = within(dialog).getByRole("button", { name: "Close preview" });
  expect(close).toHaveFocus();
  fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
  expect(dialog.contains(document.activeElement)).toBe(true);
  expect(close).not.toHaveFocus();
  const last = document.activeElement;
  if (!(last instanceof HTMLElement)) throw new Error("No focused preview control");
  fireEvent.keyDown(last, { key: "Tab" });
  expect(close).toHaveFocus();
  trigger.focus();
  expect(close).toHaveFocus();
  fireEvent.keyDown(close, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "Preview light-001.fit" })).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});
