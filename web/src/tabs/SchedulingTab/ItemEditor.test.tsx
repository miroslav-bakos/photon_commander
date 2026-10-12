// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { PlanItem } from "../../api/plans";
import { renderWithQueryClient } from "../../test/queryClient";
import { ItemEditor } from "./ItemEditor";

const item: PlanItem = {
  id: "e1", kind: "take_exposures", start: { kind: "asap" }, end: { kind: "duration", minutes: 30 }, enabled: true,
  count: { kind: "fixed", frames: 50 }, exposure_sec: 30, gain: 80, filter_name: null, frame_type: "light",
  dither: null, target: { kind: "catalog", name: "M31", ra_j2000_h: 0.7123, dec_j2000_deg: 41.269 },
  framing: { mode: "eq", pa_offset_deg: 0, lens: "tele" },
  constraints: { min_alt_deg: null, min_moon_sep_deg: null }, live_stack: true,
};

function stubEditorApi(): void {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const path = new URL(input instanceof Request ? input.url : String(input), "http://localhost").pathname;
    const body = path === "/api/filterwheel"
      ? { names: ["Dark", "UVIRCut", "LP"], position: 1 }
      : path.endsWith("/target-suggestions")
        ? { suggestions: [{
            name: "M42", designations: ["M42", "NGC 1976"], common_name: "Orion Nebula", object_type: "nebula",
            ra_j2000_h: 5.588, dec_j2000_deg: -5.391, magnitude: 4, size_major_arcmin: 85,
            size_minor_arcmin: 60, position_angle_deg: null, constellation: "Ori", source: "Messier", sources: ["Messier"],
          }] }
        : path.endsWith("/bodies") ? { bodies: [] } : { detail: `unexpected ${path}` };
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));
  }));
}

describe("ItemEditor", () => {
  it("places Save beside Delete in the header and keeps Cancel below", async () => {
    stubEditorApi();
    const user = userEvent.setup();
    const onDelete = vi.fn();
    renderWithQueryClient(
      <ItemEditor
        item={item} isDirty={true} saving={false} conflict={false} saveError={null} diagnostics={[]}
        resolved={{ status: "pending" }} lensOptions={[]} siteTimeZone="Europe/Bratislava"
        onChange={vi.fn()} onSave={vi.fn()} onCancel={vi.fn()} onDelete={onDelete}
      />,
    );
    const header = screen.getByTestId("item-details-header");
    expect(within(header).getByRole("button", { name: "Delete item" })).toBeInTheDocument();
    expect(within(header).getByRole("button", { name: "Save" })).toBeEnabled();
    expect(within(header).queryByRole("button", { name: "Cancel" })).toBeNull();
    await user.click(within(header).getByRole("button", { name: "Delete item" }));
    expect(onDelete).toHaveBeenCalledOnce();
  });

  it("uses live filter-wheel choices and Planning-style target suggestions", async () => {
    stubEditorApi();
    const user = userEvent.setup();
    const onChange = vi.fn<(next: PlanItem) => void>();
    renderWithQueryClient(
      <ItemEditor
        item={item} isDirty={false} saving={false} conflict={false} saveError={null} diagnostics={[]}
        resolved={{ status: "pending" }} lensOptions={[{ value: "tele", label: "Tele" }]}
        siteTimeZone="Europe/Bratislava" onChange={onChange} onSave={vi.fn()} onCancel={vi.fn()}
      />,
    );

    const filter = await screen.findByLabelText("Filter");
    await waitFor(() => { expect(filter).toHaveTextContent("UVIRCut"); });
    expect(filter).toHaveTextContent("Dark");
    expect(filter).toHaveTextContent("LP");

    const search = screen.getByRole("combobox", { name: "Search target" });
    await user.clear(search);
    await user.type(search, "Orion");
    await user.click(await screen.findByRole("option", { name: /Orion Nebula/i }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({
      target: { kind: "catalog", name: "M42", ra_j2000_h: 5.588, dec_j2000_deg: -5.391 },
    }));
  });

  it.each(["disconnected", "query failed"] as const)("allows offline filter selection when %s", async (condition) => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const path = new URL(input instanceof Request ? input.url : String(input), "http://localhost").pathname;
      if (path === "/api/filterwheel") {
        return condition === "query failed"
          ? Promise.reject(new Error("offline"))
          : Promise.resolve(new Response(JSON.stringify({ names: [], position: -1, names_note: "Not connected" }), {
              status: 200, headers: { "content-type": "application/json" },
            }));
      }
      return Promise.resolve(new Response(JSON.stringify({ suggestions: [], bodies: [] }), {
        status: 200, headers: { "content-type": "application/json" },
      }));
    }));
    const user = userEvent.setup();
    const onChange = vi.fn<(next: PlanItem) => void>();
    renderWithQueryClient(
      <ItemEditor
        item={item} isDirty={false} saving={false} conflict={false} saveError={null} diagnostics={[]}
        resolved={{ status: "pending" }} lensOptions={[{ value: "tele", label: "Tele" }]}
        siteTimeZone="Europe/Bratislava" onChange={onChange} onSave={vi.fn()} onCancel={vi.fn()}
      />,
    );

    const filter = screen.getByRole("combobox", { name: "Filter" });
    expect(filter).toBeEnabled();
    expect(filter).toHaveTextContent("Dark");
    expect(filter).toHaveTextContent("IR");
    expect(filter).toHaveTextContent("LP");
    await user.selectOptions(filter, "LP");
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ filter_name: "LP" }));
  });
});
