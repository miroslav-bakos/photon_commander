// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Button } from "./Button";
import { Card, CardTitle } from "./Card";
import { Field } from "./Field";

describe("shared controls", () => {
  it("keeps action buttons non-submitting and preserves disabled safety", () => {
    const click = vi.fn();
    render(<Button variant="danger" disabled onClick={click}>Stop</Button>);
    const button = screen.getByRole("button", { name: "Stop" });
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveClass("ssc-button-danger", "rounded-full");
    fireEvent.click(button);
    expect(click).not.toHaveBeenCalled();
  });

  it("associates hints with their field without losing caller descriptions", () => {
    render(<><p id="validation">Check the value</p><Field label="Exposure" hint="Seconds per frame" aria-describedby="validation" /></>);
    const input = screen.getByLabelText("Exposure");
    expect(input).toHaveAccessibleDescription("Check the value Seconds per frame");
    expect(input.getAttribute("aria-describedby")?.split(" ")).toHaveLength(2);
  });

  it("preserves caller IDs and descriptions when there is no hint", () => {
    render(<><p id="external">External guidance</p><Field id="gain-input" label="Gain" aria-describedby="external" /></>);
    expect(screen.getByLabelText("Gain")).toHaveAttribute("id", "gain-input");
    expect(screen.getByLabelText("Gain")).toHaveAttribute("aria-describedby", "external");
  });

  it("preserves mixed case and astronomical acronyms in section titles", () => {
    render(<Card><CardTitle>NGC 1499 live view</CardTitle></Card>);
    expect(screen.getByRole("heading", { name: "NGC 1499 live view" })).toHaveClass("ssc-card-title");
  });

  it("keeps foldable section labels readable and exposes disclosure state", () => {
    render(<Card title="Night summary" collapseKey="hig-test-summary" defaultCollapsed><span>Summary content</span></Card>);
    const disclosure = screen.getByRole("button", { name: "Night summary" });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(disclosure);
    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Summary content")).toBeVisible();
  });
});
