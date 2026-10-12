// SPDX-License-Identifier: GPL-3.0-only
import { fireEvent, render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { AppDiagnostics } from "./AppDiagnostics";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
it("records only allowlisted categories, never URL, DOM or error values", () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true });
  vi.stubGlobal("fetch", fetch);
  const now = vi.spyOn(Date, "now").mockReturnValue(1000);
  const { getByRole } = render(<MemoryRouter initialEntries={["/settings?location=private-secret"]}>
    <AppDiagnostics /><button value="private-secret">private-secret</button>
  </MemoryRouter>);
  now.mockReturnValue(2000);
  fireEvent.click(getByRole("button"));
  now.mockReturnValue(3000);
  window.dispatchEvent(new ErrorEvent("error", { message: "private-secret", filename: "private-secret" }));
  const bodies = fetch.mock.calls.map((call: unknown[]) => JSON.parse((call[1] as RequestInit).body as string) as unknown);
  expect(bodies).toEqual([
    { kind: "navigation", screen: "settings", control: "none" },
    { kind: "action", screen: "settings", control: "button" },
    { kind: "error", screen: "settings", control: "none" },
  ]);
  expect(JSON.stringify(fetch.mock.calls)).not.toContain("private-secret");
});
