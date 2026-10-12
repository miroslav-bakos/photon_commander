// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DraftNumberField } from "./DraftNumberField";

describe("DraftNumberField", () => {
  it("keeps the empty replacement draft instead of restoring an edge digit", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn<(value: number | null) => void>();
    render(<DraftNumberField label="Exposure" value={30} onValueChange={onValueChange} />);

    const field = screen.getByLabelText("Exposure");
    await user.clear(field);
    expect(field).toHaveValue(null);
    expect(onValueChange).not.toHaveBeenCalled();

    await user.type(field, "60");
    expect(field).toHaveValue(60);
    expect(onValueChange).toHaveBeenLastCalledWith(60);
  });

  it("publishes null for an optional field while retaining its empty draft", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn<(value: number | null) => void>();
    render(<DraftNumberField label="Timeout" value={30} nullable onValueChange={onValueChange} />);

    const field = screen.getByLabelText("Timeout");
    await user.clear(field);
    expect(field).toHaveValue(null);
    expect(onValueChange).toHaveBeenLastCalledWith(null);
  });

  it("restores a required model value when an empty draft loses focus", async () => {
    const user = userEvent.setup();
    render(<DraftNumberField label="Gain" value={80} onValueChange={vi.fn()} />);

    const field = screen.getByLabelText("Gain");
    await user.clear(field);
    await user.tab();
    expect(field).toHaveValue(80);
  });
});
