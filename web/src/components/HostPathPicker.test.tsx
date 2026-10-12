import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HostPathPicker } from "./HostPathPicker";

const listing = { path: "/server", parent: "/", roots: ["/"], truncated: false, entries: [
  { name: "stars", path: "/server/stars", directory: true },
  { name: "astap", path: "/server/astap", directory: false },
] };
vi.mock("../api/externalTools", () => ({ useHostPathsQuery: () => ({ data: listing, isPending: false, isError: false }) }));

describe("HostPathPicker", () => {
  it("selects a server executable without uploading a browser-local file", () => {
    const select = vi.fn();
    render(<HostPathPicker label="ASTAP executable" initialPath="" directory={false} onSelect={select} />);
    fireEvent.click(screen.getByRole("button", { name: "Browse ASTAP executable" }));
    expect(screen.getByText(/computer running Photon Commander/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "File: astap" }));
    expect(select).toHaveBeenCalledWith("/server/astap");
    expect(screen.queryByRole("region", { name: "Browse ASTAP executable" })).toBeNull();
  });

  it("only offers directory selection for the capture root", () => {
    const select = vi.fn();
    render(<HostPathPicker label="capture folder" initialPath="/server" directory onSelect={select} />);
    fireEvent.click(screen.getByRole("button", { name: "Browse capture folder" }));
    expect(screen.queryByRole("button", { name: "File: astap" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Choose this folder" }));
    expect(select).toHaveBeenCalledWith("/server");
  });
});
