// SPDX-License-Identifier: GPL-3.0-only
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { setLanguage } from "../../i18n/locale";
import { SetupEphemeris } from "./SetupEphemeris";
import type { EphemerisSetup } from "../../api/ephemeris";

const missing: EphemerisSetup = {
  loaded: false, error: "Kernel missing", folder_path: "C:\\Photon\\data\\ephemeris",
  kernel_path: "C:\\Photon\\data\\ephemeris\\de440s.bsp", filename: "de440s.bsp",
  download_url: "https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de440s.bsp",
  expected_size_bytes: 32726016,
  expected_sha256: "c1c7feeab882263fc493a9d5a5b2ddd71b54826cdf65d8d17a76126b260a49f2",
};
const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
beforeEach(() => { setLanguage("en"); });
afterEach(() => {
  cleanup(); vi.unstubAllGlobals(); setLanguage("en");
  if (clipboardDescriptor) Object.defineProperty(navigator, "clipboard", clipboardDescriptor);
  else Reflect.deleteProperty(navigator, "clipboard");
});
function serve(data: EphemerisSetup = missing) {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(data), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("SetupEphemeris", () => {
  it("waits for server paths without inventing a client folder", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderWithQueryClient(<SetupEphemeris />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking the server");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("uses the host path, copies its exact folder and shows manual installation details", async () => {
    const fetch = serve(); const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    renderWithQueryClient(<SetupEphemeris />);
    const folder = await screen.findByRole("textbox", { name: "Destination folder on the server" });
    expect(folder).toHaveValue(missing.folder_path);
    expect(folder).toHaveAttribute("readonly");
    fireEvent.focus(folder);
    expect((folder as HTMLInputElement).selectionStart).toBe(0);
    expect((folder as HTMLInputElement).selectionEnd).toBe(missing.folder_path.length);
    const link = screen.getByRole("link", { name: "Download DE440s from NASA/JPL" });
    expect(link).toHaveAttribute("href", missing.download_url);
    expect(link).toHaveClass("min-h-11");
    expect(screen.getByText(/move the file to that PC or Pi, not the phone/)).toBeInTheDocument();
    expect(screen.getByText(/restart Photon Commander/)).toBeInTheDocument();
    expect(screen.getByText(/SHA-256/)).toHaveTextContent(missing.expected_sha256);
    fireEvent.click(screen.getByRole("button", { name: "Copy folder address" }));
    await waitFor(() => { expect(writeText).toHaveBeenCalledWith(missing.folder_path); });
    expect(await screen.findByText("Folder address copied.")).toBeInTheDocument();
    expect(fetch.mock.calls[0]?.[0].url).toContain("/api/settings/ephemeris");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("shows bundled readiness without an unnecessary download or restart instruction", async () => {
    serve({ ...missing, loaded: true, error: null });
    renderWithQueryClient(<SetupEphemeris />);
    expect(await screen.findByText(/No download is needed/)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByText(/restart Photon Commander/)).not.toBeInTheDocument();
  });

  it("keeps the path selectable when clipboard access fails", async () => {
    serve(); Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    renderWithQueryClient(<SetupEphemeris />);
    fireEvent.click(await screen.findByRole("button", { name: "Copy folder address" }));
    expect(await screen.findByText(/copy it manually/)).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue(missing.folder_path);
  });

  it("reaches the download, selectable path and copy action with the keyboard", async () => {
    const user = userEvent.setup(); serve();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    renderWithQueryClient(<SetupEphemeris />);
    await screen.findByRole("textbox");
    await user.tab(); expect(screen.getByRole("link")).toHaveFocus();
    await user.tab(); expect(screen.getByRole("textbox")).toHaveFocus();
    await user.tab(); expect(screen.getByRole("button", { name: "Copy folder address" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(writeText).toHaveBeenCalledWith(missing.folder_path);
  });

  it("shows a read error and recovers after an explicit check", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response("{}", { status: 503 })).mockResolvedValue(new Response(JSON.stringify(missing), { status: 200 }));
    vi.stubGlobal("fetch", fetch); renderWithQueryClient(<SetupEphemeris />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to check");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByRole("textbox")).toHaveValue(missing.folder_path);
  });

  it.each(["sk", "cs"] as const)("explains host installation and restart in %s", async (language) => {
    setLanguage(language); serve(); renderWithQueryClient(<SetupEphemeris />);
    expect(await screen.findByRole("textbox")).toHaveValue(missing.folder_path);
    expect(screen.getByRole("link")).toHaveTextContent("NASA/JPL");
    expect(screen.getByText(language === "sk" ? /nie do telefónu/ : /ne do telefonu/)).toBeInTheDocument();
    expect(screen.getByText(language === "sk" ? /reštartujte Photon/ : /restartujte Photon/)).toBeInTheDocument();
  });
});
