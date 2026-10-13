// SPDX-License-Identifier: GPL-3.0-only
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { WorkflowGuide } from "./WorkflowGuide";
const mocks = vi.hoisted((): { data: { appearance: { workflow_guide_visible: boolean; theme: string } } | undefined; mutate: ReturnType<typeof vi.fn>; error: boolean } => ({ data: { appearance: { workflow_guide_visible: true, theme: "dark" } }, mutate: vi.fn(), error: false }));
vi.mock("../../api/settings", () => ({ useSettingsQuery: () => ({ data: mocks.data }), useSetAppearanceMutation: () => ({ mutate: mocks.mutate, isPending: false, isError: mocks.error }) }));
afterEach(() => { cleanup(); mocks.data = { appearance: { workflow_guide_visible: true, theme: "dark" } }; mocks.error = false; mocks.mutate.mockClear(); });
const renderGuide = () => renderWithQueryClient(<MemoryRouter><WorkflowGuide /></MemoryRouter>);
describe("WorkflowGuide persistence", () => {
  it("requests a server preference change while retaining the guide until confirmed", () => {
    renderGuide();
    fireEvent.click(screen.getByRole("checkbox", { name: "Do not show again" }));
    expect(mocks.mutate).toHaveBeenCalledWith({ workflow_guide_visible: false, theme: "dark" });
    expect(screen.getByRole("navigation", { name: "Your observing night" })).toBeInTheDocument();
  });
  it("hides only when server settings say hidden", () => {
    mocks.data = { appearance: { workflow_guide_visible: false, theme: "dark" } };
    renderGuide();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
  it("keeps loading visible and dismissal disabled", () => {
    mocks.data = undefined; renderGuide();
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(screen.getAllByRole("link")).toHaveLength(4);
  });
  it("surfaces save failures and retains the guide", () => {
    mocks.error = true; renderGuide();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not save appearance settings.");
    expect(screen.getByRole("navigation")).toBeInTheDocument();
  });
});
