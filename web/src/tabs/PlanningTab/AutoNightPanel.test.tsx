// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { newPlanDocument } from "../SchedulingTab/itemKinds";
import { AutoNightPanel } from "./AutoNightPanel";
import { objectTypesForCategories } from "./targetCategories";

const props = { startTime: "2026-10-07T10:00:00Z", endTime: "2026-10-08T10:00:00Z", nightDate: "2026-10-07", timeZone: "Europe/Bratislava", profileId: "zwo-s30pro", opticId: "tele", mountMode: "eq" as const, minAltitudeDeg: 30, categories: ["galaxies", "emission_nebulae"] as const, siteKey: "site-one", lpAvailable: true };
const document = newPlanDocument("Automatic night");
const response = { document, blocks: [{ designation: "M31", common_name: "Andromeda", object_type: "galaxy", start_time: "2026-10-07T19:00:00Z", end_time: "2026-10-07T20:30:00Z", filter_name: "IR", peak_when: "2026-10-07T20:00:00Z", peak_altitude_deg: 70, min_altitude_deg: 40, min_moon_separation_deg: 90, max_moon_altitude_deg: -10, max_moon_illuminated_fraction: .2, score: 1, reason_code: "moon_down_ir" }], gaps: [], moon_illuminated_fraction: .2, darkness_hours: 10, scheduled_hours: 1.5, candidates_considered: 10, note: "sampled_policy_weather_unchecked" };
function setup() {
  const calls: { path: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    if (request.method === "GET") return Promise.resolve(Response.json({ horizon: null }));
    return request.clone().json().then((body: unknown) => {
      calls.push({ path, body });
      return Response.json(path === "/api/astro/auto-night" ? response : { document, revision: 1, night_date: props.nightDate });
    });
  }));
  return calls;
}
afterEach(() => { vi.unstubAllGlobals(); });
describe("AutoNightPanel", () => {
  it("shares category exclusions and requires cluster consent for mixed nebulae", () => {
    expect(objectTypesForCategories(["emission_nebulae"])).not.toContain("cluster_with_nebula");
    expect(objectTypesForCategories(["emission_nebulae", "star_clusters"])).toContain("cluster_with_nebula");
    expect(objectTypesForCategories([])).toEqual([]);
  });
  it("sends selected constraints and saves a new dated plan without starting a run", async () => {
    const calls = setup();
    renderWithQueryClient(<MemoryRouter><AutoNightPanel {...props} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Preview night plan" }));
    await screen.findByText("M31 · Andromeda · IR");
    expect(calls[0]?.body).toMatchObject({ profile_id: props.profileId, optic_id: "tele", min_altitude_deg: 30, start_time: "2026-10-07T10:00:00.000Z", end_time: "2026-10-08T10:00:00.000Z", available_filters: ["IR", "LP"], allowed_object_types: objectTypesForCategories(props.categories) });
    fireEvent.click(screen.getByRole("button", { name: "Save as new plan" }));
    await screen.findByText("New plan saved.");
    expect(calls[1]).toEqual({ path: "/api/plans", body: { document, night_date: "2026-10-07" } });
    expect(calls.map((call) => call.path)).toEqual(["/api/astro/auto-night", "/api/plans"]);
    expect(screen.getByRole("button", { name: "Save as new plan" })).toBeDisabled();
  });
  it("removes wide LP and prevents saving after inputs or observing site change", async () => {
    const calls = setup();
    function SiteHarness() {
      const [site, setSite] = useState("site-one");
      return <MemoryRouter><button onClick={() => { setSite("site-two"); }}>Change site</button><AutoNightPanel {...props} opticId="wide" lpAvailable={false} siteKey={site} /></MemoryRouter>;
    }
    renderWithQueryClient(<SiteHarness />);
    expect(screen.getByRole("checkbox", { name: "Light pollution (LP)" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Preview night plan" }));
    await screen.findByText("M31 · Andromeda · IR");
    expect(calls[0]?.body).toMatchObject({ optic_id: "wide", available_filters: ["IR"] });
    expect(screen.getByRole("button", { name: "Save as new plan" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Gain"), { target: { value: "100" } });
    expect(screen.getByRole("button", { name: "Save as new plan" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Gain"), { target: { value: "200" } });
    fireEvent.click(screen.getByRole("button", { name: "Change site" }));
    await waitFor(() => { expect(screen.getByRole("button", { name: "Save as new plan" })).toBeDisabled(); });
    expect(calls).toHaveLength(1);
  });
  it("invalidates a preview when the configured horizon changes", async () => {
    setup();
    function HorizonHarness() {
      const client = useQueryClient();
      return <MemoryRouter><button onClick={() => { client.setQueryData(["settings"], { horizon: { points: [{ az_deg: 90, alt_deg: 25 }] } }); }}>Change horizon</button><AutoNightPanel {...props} /></MemoryRouter>;
    }
    renderWithQueryClient(<HorizonHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Preview night plan" }));
    await screen.findByText("M31 · Andromeda · IR");
    expect(screen.getByRole("button", { name: "Save as new plan" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Change horizon" }));
    await waitFor(() => { expect(screen.getByRole("button", { name: "Save as new plan" })).toBeDisabled(); });
  });

});
