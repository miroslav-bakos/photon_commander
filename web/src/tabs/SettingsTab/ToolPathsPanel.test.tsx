import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ToolPathsPanel } from "./ToolPathsPanel";

const save = vi.hoisted(() => vi.fn());
vi.mock("../../api/externalTools", () => ({
  useToolPathsQuery: () => ({ data: { astap_cli: "/server/astap", astap_data_dir: "/server/stars" }, isPending: false, isError: false }),
  useSaveToolPaths: () => ({ mutate: save, isPending: false, isError: false, isSuccess: false }),
  useHostPathsQuery: () => ({ isPending: false, isError: false }),
}));

it("keeps path edits local until explicit Save and permits restoring auto-detection", () => {
  render(<ToolPathsPanel />);
  fireEvent.change(screen.getByLabelText("ASTAP executable"), { target: { value: "" } });
  expect(save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Save ASTAP paths" }));
  expect(save).toHaveBeenCalledWith({ astap_cli: null, astap_data_dir: "/server/stars" });
});
