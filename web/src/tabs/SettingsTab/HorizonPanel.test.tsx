// SPDX-License-Identifier: GPL-3.0-only
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { renderWithQueryClient } from "../../test/queryClient";
import { HorizonPanel } from "./HorizonPanel";

it("preserves horizon edits made while an earlier save is pending", async () => {
  const horizon = { active: true, points: [{ azimuth_deg: 0, altitude_deg: 0 }, { azimuth_deg: 180, altitude_deg: 0 }] };
  const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn((request: Request) => request.method === "PUT"
    ? new Promise<Response>((resolve) => { finish = resolve; }) : Promise.resolve(json({ horizon }))));
  renderWithQueryClient(<HorizonPanel />);
  const altitude = await screen.findByLabelText("Altitude, degrees, point 1");
  fireEvent.change(altitude, { target: { value: "10" } });
  fireEvent.click(screen.getByRole("button", { name: "Save horizon" }));
  await waitFor(() => { expect(finish).toBeDefined(); });
  fireEvent.change(altitude, { target: { value: "20" } });
  await act(async () => { finish(json({ ...horizon, points: [{ azimuth_deg: 0, altitude_deg: 10 }, { azimuth_deg: 180, altitude_deg: 0 }] })); await Promise.resolve(); });
  await waitFor(() => { expect(screen.getByRole("button", { name: "Save horizon" })).toBeEnabled(); });
  expect(altitude).toHaveValue(20);
  expect(screen.getByRole("button", { name: "Discard changes" })).toBeInTheDocument();
});
