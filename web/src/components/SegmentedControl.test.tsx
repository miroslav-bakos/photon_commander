// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SegmentedControl } from "./SegmentedControl";

const options = [{ value: "tele", label: "Tele" }, { value: "wide", label: "Wide" }, { value: "missing", label: "Unavailable", disabled: true }];
describe("joined segmented selection", () => {
  it("uses one tab stop and skips unavailable choices when selecting with arrows", () => {
    const change = vi.fn();
    render(<SegmentedControl label="Lens" value="tele" options={options} onChange={change} />);
    const tele = screen.getByRole("radio", { name: "Tele" });
    const wide = screen.getByRole("radio", { name: "Wide" });
    expect(tele).toHaveAttribute("tabindex", "0");
    expect(wide).toHaveAttribute("tabindex", "-1");
    tele.focus(); fireEvent.keyDown(tele, { key: "ArrowRight" });
    expect(wide).toHaveFocus(); expect(change).toHaveBeenCalledWith("wide");
    fireEvent.keyDown(wide, { key: "ArrowRight" });
    expect(tele).toHaveFocus();
    expect(screen.getByRole("radio", { name: "Unavailable" })).toBeDisabled();
  });
  it("toolbar arrows move focus without applying a mount or lens change", () => {
    const change = vi.fn();
    render(<div role="toolbar"><SegmentedControl label="Lens" value="tele" options={options} onChange={change} /></div>);
    fireEvent.keyDown(screen.getByRole("radio", { name: "Tele" }), { key: "End" });
    expect(screen.getByRole("radio", { name: "Wide" })).toHaveFocus();
    expect(change).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("radio", { name: "Wide" }));
    expect(change).toHaveBeenCalledWith("wide");
  });
  it("action-backed segments never send a command merely on keyboard navigation", () => {
    const change = vi.fn();
    render(<SegmentedControl label="Filter" value="tele" options={options} semantics="pressed" onChange={change} />);
    fireEvent.keyDown(screen.getByRole("button", { name: "Tele" }), { key: "ArrowRight" });
    expect(screen.getByRole("button", { name: "Wide" })).toHaveFocus();
    expect(change).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Tele" }));
    expect(change).toHaveBeenCalledWith("tele");
  });
  it("unknown selection remains unknown and disabled groups cannot mutate", () => {
    const change = vi.fn();
    const { rerender } = render(<SegmentedControl label="Lens" value="" options={options} onChange={change} />);
    expect(screen.getAllByRole("radio").every((item) => item.getAttribute("aria-checked") === "false")).toBe(true);
    rerender(<SegmentedControl label="Lens" value="tele" options={options} onChange={change} disabled />);
    fireEvent.click(screen.getByRole("radio", { name: "Wide" }));
    expect(change).not.toHaveBeenCalled();
  });
});
