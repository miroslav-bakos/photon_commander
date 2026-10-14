// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { usePlanningUiStore } from "../PlanningTab/planningUiStore";
import { PlanningEquipmentPanel } from "./PlanningEquipmentPanel";

vi.mock("../../api/astro", () => ({
  useAstroProfilesQuery: () => ({
    data: {
      profiles: [{
        id: "fov-s30",
        name: "S30 FOV",
        optics: [
          { id: "tele", label: "Telephoto", focal_length_mm: 150, distortion: "rectilinear" },
          { id: "wide", label: "Wide", focal_length_mm: 6, distortion: "approximate" },
        ],
      }],
    },
  }),
}));

const lpState = vi.hoisted(() => ({ data: { enabled: false } as { enabled: boolean } | undefined, isError: false, refetch: vi.fn() }));
vi.mock("../../api/settings", () => ({
  useWideCompanionSettingsQuery: () => ({ data: { enabled: false }, isError: false }),
  useSetWideCompanionMutation: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useAutoLpFilterSettingsQuery: () => lpState,
  useSetAutoLpFilterMutation: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}));

vi.mock("../../store/connectionStore", () => ({
  useConnectionStore: (selector: (state: { snapshot: { settings: { hardware_profile_id: string } } }) => unknown) =>
    selector({ snapshot: { settings: { hardware_profile_id: "fov-s30" } } }),
}));

afterEach(() => {
  usePlanningUiStore.setState(usePlanningUiStore.getInitialState());
  lpState.data = { enabled: false }; lpState.isError = false; lpState.refetch.mockClear();
});

describe("PlanningEquipmentPanel", () => {
  it("does not let an unknown LP preference look editable after a failed read", () => {
    lpState.data = undefined; lpState.isError = true;
    render(<PlanningEquipmentPanel />);
    expect(screen.getByRole("checkbox", { name: /Automatically use LP/ })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Could not load");
    fireEvent.click(screen.getByRole("button", { name: "Retry now" }));
    expect(lpState.refetch).toHaveBeenCalledOnce();
  });
  it("explains a pending read and enables the control when the saved value arrives", () => {
    lpState.data = undefined;
    const view = render(<PlanningEquipmentPanel />);
    expect(screen.getByRole("checkbox", { name: /Automatically use LP/ })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Loading the saved LP");
    lpState.data = { enabled: true }; view.rerender(<PlanningEquipmentPanel />);
    expect(screen.getByRole("checkbox", { name: /Automatically use LP/ })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: /Automatically use LP/ })).toBeChecked();
  });
  it("stores lens, centering tolerance, and altitude defaults for Planning", () => {
    render(<PlanningEquipmentPanel />);

    fireEvent.change(screen.getByRole("combobox", { name: "Lens" }), { target: { value: "wide" } });
    fireEvent.change(screen.getByLabelText(/desired deviation/i), { target: { value: "45" } });
    fireEvent.change(screen.getByLabelText(/altitude limit/i), { target: { value: "35" } });

    expect(usePlanningUiStore.getState()).toMatchObject({
      chosenOpticId: "wide",
      centeringToleranceText: "45",
      altitudeLimitDeg: 35,
    });
    expect(screen.getByText(/overlay is indicative only/i)).toBeInTheDocument();
  });
});
