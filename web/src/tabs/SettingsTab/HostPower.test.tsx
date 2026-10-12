import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { AboutPanel } from "./AboutPanel";

const controls = vi.hoisted(() => ({ reboot: vi.fn(), shutdown: vi.fn() }));
vi.mock("../../api/settings", () => ({
  useRestartApplicationMutation: () => ({ mutate: vi.fn(), isPending: false }),
  useHostRebootStatusQuery: () => ({ data: { available: true, reason: "Ready" } }),
  useHostShutdownStatusQuery: () => ({ data: { available: true, reason: "Ready" } }),
  useHostRebootMutation: () => ({ mutate: controls.reboot, isPending: false }),
  useHostShutdownMutation: () => ({ mutate: controls.shutdown, isPending: false }),
}));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); });
describe("Raspberry Pi power controls", () => {
  it("requires explicit confirmation before sending reboot or shutdown", () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderWithQueryClient(<AboutPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Reboot Raspberry Pi" }));
    fireEvent.click(screen.getByRole("button", { name: "Shut down Raspberry Pi" }));
    expect(controls.reboot).not.toHaveBeenCalled();
    expect(controls.shutdown).not.toHaveBeenCalled();
  });
  it("sends a confirmed shutdown and locks controls after acceptance", () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    controls.shutdown.mockImplementation((_input, options: { onSuccess: () => void }) => { options.onSuccess(); });
    renderWithQueryClient(<AboutPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Shut down Raspberry Pi" }));
    expect(controls.shutdown).toHaveBeenCalledOnce();
    expect(screen.getByRole("status")).toHaveTextContent("Shutdown requested");
    expect(screen.getByRole("button", { name: "Reboot Raspberry Pi" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Shut down Raspberry Pi" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Restart backend + frontend" })).toBeDisabled();
  });
});
